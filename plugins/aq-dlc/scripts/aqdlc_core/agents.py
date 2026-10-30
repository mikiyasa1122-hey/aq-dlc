"""サブエージェントの役割・引き継ぎ・成果物の検証・権限。

  起動前（PreToolUse: Agent）  … 前の工程が揃っているか確認。並列起動を拒否し、前景で実行させる
  起動時（SubagentStart）      … 引き継ぎ情報（仕様・設計の場所と指紋、指摘、未設定の検証など）を注入
  終了時（SubagentStop）       … 成果物を検証。不足があれば終了させずに続けさせる。揃えば登録する
  実行中（PreToolUse: Bash / Write / Edit） … 役割ごとに書き込み先とエンジンのコマンドを制限
"""
import os
import re
import shutil
import time

from . import gate as G
from .project import config, engine_cmd
from .state import (ROLES, ROLE_LABEL, Ctx, artifacts_dir, bump, doc_hash, doc_path, graph_issues,
                    id_index, iter_verifications, last_impl, latest_review, problems, run_dir,
                    running_agents, save_state)
from .util import now_iso, read_text, short, write_text, is_within, norm

PREFIX = "aq-dlc:"


def role_of(agent_type):
    if not agent_type:
        return "orchestrator"
    name = agent_type.split(":")[-1]
    return name if name in ROLES and (agent_type.startswith(PREFIX) or ":" not in agent_type) else "other"


# ------------------------------------------------------------------ markdown helpers
def headings(md):
    return [ln.lstrip("#").strip() for ln in (md or "").splitlines() if re.match(r"^#{1,4}\s", ln)]


def missing_headings(md, required):
    hs = headings(md)
    return [r for r in required if not any(r in h for h in hs)]


def section(md, keyword):
    lines = (md or "").splitlines()
    out, on, level = [], False, 0
    for ln in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            if on and len(m.group(1)) <= level:
                break
            if keyword in m.group(2) and not on:
                on, level = True, len(m.group(1))
                continue
        if on:
            out.append(ln)
    return "\n".join(out).strip() if on else None


def hash_line_ok(md, label, full):
    if not full:
        return False
    m = re.search(rf"{label}\s*[:：]\s*`?([0-9a-f]{{8,40}})", md or "")
    return bool(m) and full.startswith(m.group(1))


def bullet_items(text):
    items = []
    for ln in (text or "").splitlines():
        m = re.match(r"^\s*[-*・]\s*(.+)", ln)
        if m:
            t = m.group(1).strip()
            if t and not re.match(r"^(なし|特になし|無し|none)\b", t, re.I):
                items.append(t)
    return items


REQUIRED = {
    "requirements": ["目的", "機能要件", "非機能要件", "受け入れ条件", "リスク", "不明点", "対象外"],
    "design": ["技術スタック", "アーキテクチャ", "ディレクトリ構成", "データ", "エラー", "セキュリティ",
               "テスト方針", "要求との対応", "設計判断"],
    "implementer": ["実施内容", "検証結果", "差し戻し"],
}


# ------------------------------------------------------------------ what each role must fix now
def pending_fix(ctx):
    """実装担当に今回求める修正（レビュー指摘・gate 不合格・CI 不合格）。"""
    s = ctx.state
    rv = latest_review(s)
    li = last_impl(s)
    items, kind = [], None
    if rv and rv.get("blocking", 0) > 0 and (not li or li["seq"] < rv["seq"]):
        kind = ("review", rv["n"])
        items = [f for f in rv.get("findings", []) if f.get("severity") == "重大"]
    g = s.get("gate")
    if not kind and g and (not li or li["seq"] < g["seq"]):
        failed = [(n, r) for n, r in g["results"]["checks"].items() if not r["pass"]]
        vfail = [(n, r) for n, r in g["results"]["verifications"].items()
                 if not r.get("pass") and not r.get("pending_after_merge") and not r.get("manual")]
        if failed or vfail:
            kind = ("gate", g["stamp"])
            items = [{"label": n, "text": r.get("skipped") or r.get("error") or f"不合格（ログ: {r.get('log')}）"}
                     for n, r in failed + vfail]
    return kind, items


# ------------------------------------------------------------------ briefs
def brief(role, ctx):
    s, project, rd = ctx.state, ctx.project, ctx.rd
    aq = f'{engine_cmd()} <コマンド> --project "{project}"'
    head = [
        f"【AQ-DLC 引き継ぎ情報】あなたは AQ-DLC の {ROLE_LABEL[role]} です。",
        f"- プロジェクト: {project}",
        f"- run: {s['run_id']}（{s.get('title')}）",
        f"- エンジン: {aq}  （以下 $AQ と書く。必ず --project を付ける）",
        f"- 成果物の置き場所: {artifacts_dir(rd)}",
        "- 言語: 報告・成果物・コード内のコメントはすべて日本語で書く。",
        "- 人間には質問できない。人間の判断が必要なことは成果物に書いて統括に返す。",
        "- state.json と config.json は直接編集しない（エンジンのコマンドで更新する）。",
        "- 終了時にエンジンが成果物を検証する。不足があると終了できず、理由が返ってくるので直して終える。",
        "",
    ]
    body = globals()[f"_brief_{role}"](ctx)
    return "\n".join(head + body)


def _req_lines(s, with_ac=True):
    out = []
    for r in s["requirements"]:
        out.append(f"- {r['id']}{'【重要】' if r.get('critical') else ''}"
                   f"{'（非機能）' if r.get('kind') == 'nonfunctional' else ''} {r.get('text')}")
        if with_ac:
            for a in r.get("acceptance", []):
                out.append(f"    - {a['id']}: {a.get('text')}")
    return out or ["（まだ登録されていない）"]


def _brief_requirements(ctx):
    s = ctx.state
    sp = doc_path(ctx.rd, "spec")
    lines = [
        "## あなたの仕事",
        "統括から渡されたユーザーの依頼を分析し、人間が承認できる仕様を作る。",
        "**曖昧な点を勝手に確定しない。** 答えによって作るものが変わる点は question にする。",
        "",
        "1. 要求: `$AQ req R1 \"<要求>\" [--critical] [--kind nonfunctional]`（性能・セキュリティ・対応環境などは非機能）",
        "2. 受け入れ条件: `$AQ accept R1 AC1 \"<観察できる条件>\"`（全要求に1つ以上）",
        "3. Failure First: `$AQ risk R1 K1 \"<起こりうる失敗>\"`（重要な要求には必須。不正入力・権限・境界値・"
        "データ不整合・エラー時・秘密情報・同時操作の観点で洗い出す）",
        "4. 検証方法: `$AQ verify AC1 V1 \"<確かめ方>\" --level unit|integration|e2e|manual [--after-merge]`"
        "（受け入れ条件・リスクごとに1つ以上。実行コマンドは実装担当が後で設定する。"
        "人にしか確かめられないものは `--manual \"<手順>\"`、デプロイ後にしか確かめられないものは `--after-merge`）",
        "5. 不明点・矛盾点: `$AQ question Q1 \"<質問>\" --options \"<推奨案>（推奨）|<案2>|<案3>\"`（最大4問程度。重要な順）",
        "6. 利用者に見える小さな決めごと（ほぼ確実なもの）: `$AQ assume \"<内容>\"`、作らないもの: `$AQ assume \"<内容>\" --out`",
        "7. 不要になった項目: `$AQ drop <ID>`（理由は仕様書に書く）",
        f"8. 仕様書を `{sp}` に書く。必須の見出し: 目的 / 機能要件 / 非機能要件 / 受け入れ条件 / リスク / "
        "不明点・矛盾点 / 対象外。登録した要求・受け入れ条件のIDを本文に必ず含める。",
        "",
        "ID は run 全体で一意（重複は拒否される）。現在の内容は `$AQ spec` と `$AQ trace` で確認できる。",
        "",
        "## 現在の要求",
    ] + _req_lines(s)
    answered = [q for q in s["questions"] if q.get("status") == "answered"]
    if answered:
        lines += ["", "## 人間の回答（仕様に反映すること）"]
        lines += [f"- {q['id']} {q['text']} → 回答: {q.get('answer')}" for q in answered]
    rv = latest_review(s)
    if rv and rv.get("blocking", 0) > 0:
        spec_f = [f for f in rv.get("findings", []) if f.get("category") == "仕様"]
        if spec_f or rv.get("missing_risks"):
            lines += ["", f"## レビュー#{rv['n']} の指摘（仕様・リスクに関するもの）"]
            lines += [f"- [{f['label']}] {f['text']}" for f in spec_f]
            lines += [f"- 不足しているリスク: {m}" for m in rv.get("missing_risks", [])]
    lines += ["", "## 終了時の報告",
              "作った要求・受け入れ条件・リスク・検証・質問の数と、統括への申し送り（人間に確認すべきこと）を短く書く。"]
    return lines


def _brief_design(ctx):
    s = ctx.state
    lines = [
        "## あなたの仕事",
        f"承認済みの仕様 `{doc_path(ctx.rd, 'spec')}`（指紋 {short(ctx.spec_hash)}）に基づいて設計する。最初に仕様書を読む。",
        "**仕様は変更しない。** 仕様の変更が必要なら、設計書の「## 差し戻し」に理由を書いて終える（統括が人間と調整する）。",
        "",
        f"1. 設計書を `{doc_path(ctx.rd, 'design')}` に書く。先頭付近に「仕様: {short(ctx.spec_hash)}」の行を書く。",
        "   必須の見出し: 技術スタック / アーキテクチャ / ディレクトリ構成 / データモデル / エラーハンドリング / "
        "セキュリティ / テスト方針（単体・結合・E2E） / 要求との対応 / 設計判断とトレードオフ / 差し戻し（なければ「なし」）",
        "2. 要求と設計要素の対応: `$AQ design-map R1 \"<モジュール・ファイル・画面など>\"`（全要求）",
        "3. 内部の設計判断: `$AQ decision \"<判断と理由>\"`",
        "4. Quality Gate のチェック: `$AQ add-check <名前> \"<コマンド>\" --kind typecheck|lint|build|unit|integration|e2e"
        " [--needs <前提のチェック>]`",
        "   - 技術スタックに応じて、型チェック・Lint・単体・結合・ビルド・E2E（必要な場合）を登録する。",
        "   - E2E はビルドの後に実行され、ビルドが不合格なら実行されない（古い成果物での合格を防ぐ）。",
        "   - Webアプリの E2E は Playwright を候補にする（必須ではない）。",
        "   - テストファイルが0件だと失敗するランナーがある。そのチェックが最初から通るかを実装担当に申し送る。",
        "5. 検証のテストレベル: `$AQ bind <検証ID> --level unit|integration|e2e`",
        "",
        "方針: 技術スタックは固定しない。対象に対して妥当な構成を選ぶ。過剰な抽象化・不要な依存を避け、"
        "保守性・可読性・テスト容易性を優先する。",
        "",
        "## 要求",
    ] + _req_lines(s)
    lines += ["", "## 検証（レベル未設定のものは決める）"]
    for _r, p, v in iter_verifications(s):
        lines.append(f"- {v['id']}（{p['id']}）{v.get('method')} レベル: {v.get('level') or '未設定'}")
    if s.get("decisions"):
        lines += ["", "## 既存の設計判断"] + [f"- {d['id']} {d['text']}" for d in s["decisions"]]
    rv = latest_review(s)
    if rv and rv.get("blocking", 0) > 0:
        df = [f for f in rv.get("findings", []) if f.get("category") == "設計"]
        if df:
            lines += ["", f"## レビュー#{rv['n']} の設計に関する指摘"] + [f"- [{f['label']}] {f['text']}" for f in df]
    lines += ["", "## 終了時の報告", "選んだ構成と理由、登録したチェック、実装担当への申し送りを短く書く。"]
    return lines


def _brief_implementer(ctx):
    s = ctx.state
    sp, dp, ip = doc_path(ctx.rd, "spec"), doc_path(ctx.rd, "design"), doc_path(ctx.rd, "impl")
    kind, items = pending_fix(ctx)
    lines = [
        "## あなたの仕事",
        f"承認済みの仕様 `{sp}`（{short(ctx.spec_hash)}）と設計書 `{dp}`（{short(ctx.design_hash)}）に基づいて"
        "実装とテストを作る。**最初に両方を読む。仕様・設計を独断で変えない。**",
        "",
        "1. 実装と、単体テスト・必要な結合テスト・必要なら E2E テストを書く。型安全性・エラー処理・セキュリティに配慮する。",
        "2. 要求と実装ファイルの対応: `$AQ impl-map R1 --files \"src/a.ts,src/b.ts\"`（全要求）",
        "3. 各検証の実行方法: `$AQ bind V1 --command \"<そのリスクだけを確かめるコマンド（テスト名で絞る）>\" "
        "--test \"<テストの場所>\"`",
        "   例: `npx vitest run -t \"保存失敗\"`、`npx playwright test -g \"保存失敗\"`、`pytest -k isolation`",
        "4. 自分でテストを実行して確認する（`$AQ gate` も実行できる）。",
        f"5. 実装報告を `{ip}` に書く:",
        f"   - 「設計書: {short(ctx.design_hash)}」「仕様: {short(ctx.spec_hash)}」の行（読んだ版の確認に使う）",
        "   - ## 実施内容 / ## 検証結果（自分で実行したコマンドと結果）/ ## 差し戻し（仕様・設計の変更が必要なもの。なければ「なし」）"
        + ("\n   - ## 指摘への対応（下の各項目のラベルごとに、何をどう直したか）" if kind else ""),
        "",
        "禁止: テストを通すために検証条件を弱める・重要な検証を消す・テストを skip する。"
        "bind やチェックの変更は履歴に残り、独立レビューで確認される。",
        "",
    ]
    if kind:
        what = f"レビュー#{kind[1]} の重大な指摘" if kind[0] == "review" else f"gate {kind[1]} の不合格"
        lines += [f"## 今回の修正依頼（{what}）"]
        for f in items:
            lines.append(f"- [{f['label']}] {f['text']}")
        lines.append("")
    unbound = [v for _r, _p, v in iter_verifications(s) if not G.is_automated(v) and not v.get("manual")]
    if unbound:
        lines += ["## 実行方法が未設定の検証"] + [f"- {v['id']}: {v.get('method')}（レベル {v.get('level') or '未設定'}）"
                                              for v in unbound] + [""]
    lines += ["## 要求"] + _req_lines(s)
    lines += ["", "## Quality Gate のチェック"] + ([f"- {c['name']}（{c.get('kind', 'other')}）: {c['command']}"
                                                  for c in ctx.checks] or ["（未登録）"])
    return lines


def _brief_reviewer(ctx):
    s = ctx.state
    g = s.get("gate") or {}
    prev = [r for r in s.get("reviews", []) if r.get("valid")]
    since = prev[-1]["seq"] if prev else 0
    lines = [
        "## あなたの仕事",
        "独立した品質レビュー。実装担当の自己評価や説明に頼らず、仕様・設計・ソースコード・実際の検証結果で判断する。",
        "**コードを変更しない。** Write / Edit は使えない。Bash でもファイルを書き換えない"
        "（レビュー中にコードが変わると、このレビューは無効になる）。",
        "",
        "## 読むもの",
        f"- 仕様: `{doc_path(ctx.rd, 'spec')}`（{short(ctx.spec_hash)}）",
        f"- 設計: `{doc_path(ctx.rd, 'design')}`（{short(ctx.design_hash)}）",
        f"- 実装報告: `{doc_path(ctx.rd, 'impl')}`（参考。鵜呑みにしない）",
        "- 要求→受け入れ条件→設計→実装→テスト→結果の対応: `$AQ trace`",
        f"- 検証条件・チェックの変更履歴: `$AQ history --since {since}`（弱められた検証がないか）",
        f"- gate の結果: {g.get('stamp')}（ログ: {os.path.join(ctx.rd, 'gate')}）",
        "",
        "## 見ること（重要度順）",
        "1. 仕様との整合（受け入れ条件を満たしているか）  2. 設計との整合",
        "3. バグ・セキュリティ・認可・データ整合性・異常系",
        "4. テストの品質: 検証がリスクを本当に確かめているか（モックだけ、弱いアサーション、常に通る、skip）。"
        "E2E の不足。ビルド前の古い成果物に対するテスト",
        "5. テストや検証条件の改ざん・弱体化（git diff と history で確認）",
        "",
        "テストを実行するときは、出力を一時フォルダに出す（例: Playwright は `--output` に OS の一時フォルダ）。"
        "`$AQ gate` は実行しない。依存関係のインストールもしない。",
        "",
    ]
    if prev and prev[-1].get("findings"):
        lines += [f"## 前回（レビュー#{prev[-1]['n']}）の指摘（直っているか確認する）"]
        lines += [f"- [{f['label']}] {f['text']}" for f in prev[-1]["findings"]] + [""]
    li = last_impl(s)
    if li and li.get("code_fp") and li["code_fp"] != ctx.code_fp:
        lines += ["## 注意: 実装担当の終了後に、他の誰かがコードを変更している",
                  "- 変更内容を確認し、問題があれば指摘する。", ""]
    lines += [
        "## 最後の報告（この形式で書く。エンジンが読み取って記録する）",
        "```",
        "## レビュー結果",
        "判定: 合格 または 要修正",
        "重大な指摘: <数>",
        "軽微な指摘: <数>",
        f"レビュー対象: 仕様 {short(ctx.spec_hash)} / 設計 {short(ctx.design_hash)} / コード {short(ctx.code_fp)}",
        "### 指摘",
        "- [重大1] 区分: 実装|テスト|仕様|設計 / 対象: R1, V3 / 内容: … / 根拠: ファイル:行 や 再現手順 / 提案: …",
        "- [軽微1] 区分: … / 対象: … / 内容: …",
        "### 不足しているリスク",
        "- R1: …（なければ「なし」）",
        "### 確認したこと",
        "- 実行したコマンドと結果",
        "```",
        "「重大」の基準: 受け入れ条件の未達、重要な要求の検証不足・空洞化、設計との重大な不整合、重大なセキュリティの穴、"
        "テストの改ざん・弱体化。スタイルや好みは「軽微」。重大が1件でもあれば判定は「要修正」。",
    ]
    return lines


# ------------------------------------------------------------------ spawn gate
def spawn_problem(role, ctx):
    s = ctx.state
    if s.get("status") not in ("in_progress", "pr_ready"):
        return "実行中の run がありません。new で run を開始してください。"
    run = running_agents(s)
    if run:
        names = "、".join(ROLE_LABEL.get(v["role"], v["role"]) for v in run.values())
        return f"{names} が実行中です。サブエージェントは順番に実行します。完了を待ってください。"
    if role == "requirements":
        return None
    probs = problems(ctx)
    codes = [p["code"] for p in probs]
    spec_block = {"spec_missing", "graph_incomplete", "spec_unregistered", "questions_open",
                  "answers_unapplied", "spec_unpresented", "spec_unapproved", "dup_ids"}
    blocking = [p for p in probs if p["code"] in spec_block]
    if role == "design":
        if blocking:
            return "仕様が承認されていないため、設計に進めません: " + blocking[0]["msg"]
        return None
    if blocking:
        return "仕様が承認されていないため、進めません: " + blocking[0]["msg"]
    design_block = [p for p in probs if p["code"] in ("design_missing", "design_changed", "design_stale",
                                                      "design_unmapped", "no_checks", "escalation")]
    if role == "implementer":
        if design_block:
            return "設計が揃っていないため、実装に進めません: " + design_block[0]["msg"]
        return None
    if role == "reviewer":
        if design_block:
            return "設計が揃っていないため、レビューできません: " + design_block[0]["msg"]
        pre = [p for p in probs if p["code"] in ("impl_missing", "impl_stale", "impl_untraced", "verif_unbound",
                                                 "review_fix", "gate_missing", "gate_stale", "gate_fail")]
        if pre:
            return "Quality Gate を通過した最新の実装がないため、レビューできません: " + pre[0]["msg"]
        return None
    return None


# ------------------------------------------------------------------ report parsing
def parse_review(text):
    t = text or ""
    m = re.search(r"判定\s*[:：]\s*\**\s*(合格|要修正)", t)
    verdict = m.group(1) if m else None
    m = re.search(r"重大な指摘\s*[:：]\s*\**\s*(\d+)", t)
    head_block = int(m.group(1)) if m else None
    m = re.search(r"軽微な指摘\s*[:：]\s*\**\s*(\d+)", t)
    head_minor = int(m.group(1)) if m else 0
    findings = []
    cur = None
    for ln in t.splitlines():
        mm = re.match(r"^\s*[-*]?\s*\[(重大|軽微)\s*(\d+)\]\s*(.*)", ln)
        if mm:
            cur = {"severity": mm.group(1), "label": f"{mm.group(1)}{mm.group(2)}", "text": mm.group(3).strip()}
            c = re.search(r"区分\s*[:：]\s*(実装|テスト|仕様|設計)", ln)
            cur["category"] = c.group(1) if c else "実装"
            tg = re.search(r"対象\s*[:：]\s*([^/／]+)", ln)
            cur["targets"] = [x.strip() for x in re.split(r"[,、\s]+", tg.group(1)) if x.strip()] if tg else []
            findings.append(cur)
        elif cur and ln.startswith((" ", "\t")) and ln.strip():
            cur["text"] += " " + ln.strip()
        elif ln.strip().startswith("#"):
            cur = None
    missing = bullet_items(section(t, "不足しているリスク"))
    blocking_entries = sum(1 for f in findings if f["severity"] == "重大")
    errors = []
    if verdict is None:
        errors.append("「判定: 合格 / 要修正」の行がない")
    if head_block is None:
        errors.append("「重大な指摘: <数>」の行がない")
    blocking = max(head_block or 0, blocking_entries)
    if head_block is not None and head_block != blocking_entries:
        errors.append(f"重大な指摘の数（{head_block}）と [重大n] の項目数（{blocking_entries}）が一致しない")
    if verdict == "合格" and blocking > 0:
        verdict = "要修正"
    return {"verdict": verdict or "要修正", "blocking": blocking, "minor": max(head_minor,
            sum(1 for f in findings if f["severity"] == "軽微")), "findings": findings,
            "missing_risks": missing}, errors


# ------------------------------------------------------------------ validation at SubagentStop
def validate(role, ctx, entry, message):
    s, rd = ctx.state, ctx.rd
    errs = []
    if role == "requirements":
        md = read_text(doc_path(rd, "spec"))
        if md is None:
            errs.append(f"仕様書 {doc_path(rd, 'spec')} がない")
        else:
            mh = missing_headings(md, REQUIRED["requirements"])
            if mh:
                errs.append("仕様書に必須の見出しがない: " + "、".join(mh))
            ids = [r["id"] for r in s["requirements"]] + [a["id"] for r in s["requirements"] for a in r["acceptance"]]
            absent = [i for i in ids if i not in md]
            if absent:
                errs.append("仕様書の本文に要求・受け入れ条件のIDがない: " + "、".join(absent[:8]))
        if not s["requirements"]:
            errs.append("要求が登録されていない（req）")
        _i, dups = id_index(s)
        if dups:
            errs.append("IDが重複している: " + "、".join(dups))
        errs += graph_issues(s)
    elif role == "design":
        md = read_text(doc_path(rd, "design"))
        if md is None:
            errs.append(f"設計書 {doc_path(rd, 'design')} がない")
        else:
            mh = missing_headings(md, REQUIRED["design"])
            if mh:
                errs.append("設計書に必須の見出しがない: " + "、".join(mh))
            if not hash_line_ok(md, "仕様", ctx.spec_hash):
                errs.append(f"設計書に、基にした仕様の指紋の行（仕様: {short(ctx.spec_hash)}）がない、または古い")
        unmapped = [r["id"] for r in s["requirements"] if r["id"] not in s["design"].get("map", {})]
        if unmapped:
            errs.append("設計に対応付いていない要求（design-map）: " + "、".join(unmapped))
        if not ctx.checks:
            errs.append("Quality Gate のチェックが登録されていない（add-check）")
    elif role == "implementer":
        md = read_text(doc_path(rd, "impl"))
        if md is None or (entry.get("impl_hash_start") and doc_hash(rd, "impl") == entry.get("impl_hash_start")):
            errs.append(f"今回の実装報告 {doc_path(rd, 'impl')} が書かれていない")
        else:
            mh = missing_headings(md, REQUIRED["implementer"])
            if mh:
                errs.append("実装報告に必須の見出しがない: " + "、".join(mh))
            if not hash_line_ok(md, "設計書", ctx.design_hash):
                errs.append(f"実装報告の「設計書: {short(ctx.design_hash)}」の行がない、または古い版を参照している")
            if not hash_line_ok(md, "仕様", ctx.spec_hash):
                errs.append(f"実装報告の「仕様: {short(ctx.spec_hash)}」の行がない、または古い版を参照している")
            fix = entry.get("fix") or {}
            if fix.get("labels"):
                sec = section(md, "指摘への対応") or ""
                miss = [lb for lb in fix["labels"] if lb not in sec]
                if miss:
                    errs.append("「## 指摘への対応」に次の項目がない: " + "、".join(miss))
        for r in s["requirements"]:
            files = s["impl"]["map"].get(r["id"]) or []
            if not files:
                errs.append(f"{r['id']} の実装ファイルが未登録（impl-map）")
            else:
                miss = [f for f in files if not os.path.exists(os.path.join(ctx.project, f))]
                if miss:
                    errs.append(f"{r['id']} の実装ファイルが存在しない: {', '.join(miss)}")
        unbound = [v["id"] for _r, _p, v in iter_verifications(s) if not G.is_automated(v) and not v.get("manual")]
        if unbound:
            errs.append("実行方法が未設定の検証（bind）: " + "、".join(unbound))
    elif role == "reviewer":
        _rec, perrs = parse_review(message)
        errs += perrs
    return errs


def register(role, ctx, entry, message):
    """検証を通過した成果物を登録する（戻り値: 記録の要約）。"""
    s, rd = ctx.state, ctx.rd
    seq = bump(s)
    if role == "requirements":
        sp = s["spec"]
        sp["registered_hash"] = ctx.spec_hash
        sp["registered_seq"] = seq
        sp["version"] = int(sp.get("version", 0)) + 1
        return f"仕様 v{sp['version']}（{short(ctx.spec_hash)}）を登録"
    if role == "design":
        d = s["design"]
        d.update({"registered_hash": ctx.design_hash, "spec_hash": ctx.spec_hash, "registered_seq": seq,
                  "version": int(d.get("version", 0)) + 1})
        _escalate(s, "design", read_text(doc_path(rd, "design")), seq)
        return f"設計 v{d['version']}（{short(ctx.design_hash)}）を登録"
    if role == "implementer":
        rounds = s["impl"]["rounds"]
        n = len(rounds) + 1
        fix = entry.get("fix") or {}
        rec = {"n": n, "seq": seq, "at": now_iso(), "code_fp": ctx.code_fp, "design_hash": ctx.design_hash,
               "spec_hash": ctx.spec_hash, "report_hash": doc_hash(rd, "impl"), "fix": fix or None}
        rounds.append(rec)
        if fix:
            s["fix_iterations"] = int(s.get("fix_iterations", 0)) + 1
        try:
            shutil.copyfile(doc_path(rd, "impl"), os.path.join(artifacts_dir(rd), f"impl-{n}.md"))
        except OSError:
            pass
        _escalate(s, "implementer", read_text(doc_path(rd, "impl")), seq)
        return f"実装 第{n}回（コード {short(ctx.code_fp)}）を登録"
    if role == "reviewer":
        rec, _ = parse_review(message)
        n = len(s["reviews"]) + 1
        path = os.path.join(artifacts_dir(rd), f"review-{n}.md")
        write_text(path, message or "")
        code_now = ctx.code_fp
        invalid = None
        if entry.get("code_fp_start") and entry["code_fp_start"] != code_now:
            invalid = "レビュー中にコードが変更された"
        elif entry.get("format_errors"):
            invalid = "報告の形式が不正（" + "、".join(entry["format_errors"][:2]) + "）"
        elif not entry.get("gate_ok_at_start"):
            invalid = "最新のコードで Quality Gate を通過していない状態でレビューが始まった"
        rec.update({"n": n, "seq": seq, "at": now_iso(), "code_fp": code_now, "graph_fp": ctx.graph_fp,
                    "checks_fp": ctx.checks_fp, "spec_hash": ctx.spec_hash, "design_hash": ctx.design_hash,
                    "gate_stamp": (s.get("gate") or {}).get("stamp"), "report": os.path.relpath(path, ctx.project),
                    "valid": invalid is None, "invalid_reason": invalid})
        s["reviews"].append(rec)
        if invalid:
            return f"レビュー#{n} は無効（{invalid}）"
        return f"レビュー#{n}: {rec['verdict']}（重大 {rec['blocking']} 件・軽微 {rec['minor']} 件）"
    return ""


def _escalate(s, src, md, seq):
    known = {(e.get("from"), e.get("text")) for e in s["escalations"]}
    for item in bullet_items(section(md, "差し戻し")):
        if (src, item) in known:
            continue
        n = len(s["escalations"]) + 1
        s["escalations"].append({"id": f"E{n}", "from": src, "text": item, "seq": seq, "status": "open"})


# ------------------------------------------------------------------ hook handlers
def on_pre_agent(payload, project, state, spath):
    ti = payload.get("tool_input") or {}
    role = role_of(ti.get("subagent_type"))
    if role not in ROLES:
        return None
    cfg = config(project)
    if not state:
        return deny("実行中の run がありません。AQ-DLC の開発は new で run を開始してから行います。")
    ctx = Ctx(project, state, cfg)
    why = spawn_problem(role, ctx)
    if why:
        return deny(why)
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                  "permissionDecisionReason": f"AQ-DLC: {ROLE_LABEL[role]} を順番に実行します"}}
    if cfg["automation"].get("force_foreground_agents", True) and ti.get("run_in_background") is not False:
        upd = dict(ti)
        upd["run_in_background"] = False
        out["hookSpecificOutput"]["updatedInput"] = upd
    return out


def on_subagent_start(payload, project, state, spath):
    role = role_of(payload.get("agent_type"))
    if role not in ROLES or not state:
        return None
    ctx = Ctx(project, state)
    aid = payload.get("agent_id") or f"{role}-{time.time()}"
    g = state.get("gate") or {}
    gate_ok = bool(g) and g.get("code_fp") == ctx.code_fp and g.get("checks_fp") == ctx.checks_fp \
        and not [p for p in problems(ctx) if p["code"] in ("gate_missing", "gate_stale", "gate_fail")]
    kind, items = pending_fix(ctx) if role == "implementer" else (None, [])
    entry = {"role": role, "since": now_iso(), "epoch": time.time(), "start_seq": state.get("seq", 0),
             "code_fp_start": ctx.code_fp, "gate_ok_at_start": gate_ok, "blocks": 0,
             "impl_hash_start": doc_hash(ctx.rd, "impl") if role == "implementer" else None,
             "fix": {"kind": kind[0], "ref": kind[1], "labels": [f["label"] for f in items]} if kind else None}
    state["agents"]["running"][aid] = entry
    state["stop_blocks"] = 0
    save_state(spath, state)
    return {"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": brief(role, ctx)}}


def on_subagent_stop(payload, project, state, spath):
    role = role_of(payload.get("agent_type"))
    if role not in ROLES or not state:
        return None
    aid = payload.get("agent_id")
    running = state["agents"]["running"]
    entry = running.get(aid)
    if entry is None:
        cand = [k for k, v in running.items() if v.get("role") == role]
        aid = cand[-1] if cand else aid
        entry = running.get(aid, {"role": role, "since": now_iso(), "blocks": 0})
    message = payload.get("last_assistant_message") or _last_message(payload.get("agent_transcript_path"))
    ctx = Ctx(project, state)
    errs = validate(role, ctx, entry, message)
    max_blocks = int(config(project)["automation"].get("max_agent_stop_blocks", 3))
    if errs and entry.get("blocks", 0) < max_blocks and not (role == "reviewer" and _tainted(entry, ctx)):
        entry["blocks"] = entry.get("blocks", 0) + 1
        running[aid] = entry
        save_state(spath, state)
        reason = ("AQ-DLC: 成果物が完成していないため、まだ終了できません。次を直してから終えてください:\n- "
                  + "\n- ".join(errs[:10]))
        return {"decision": "block", "reason": reason}
    running.pop(aid, None)
    msg_dir = os.path.join(artifacts_dir(ctx.rd), "messages")
    try:
        write_text(os.path.join(msg_dir, f"{state.get('seq', 0) + 1:04d}-{role}.md"), message or "")
    except OSError:
        pass
    if errs and role == "reviewer":
        entry["format_errors"] = errs
    if errs and role != "reviewer":
        summary = "成果物が不完全なまま終了（登録しない）: " + "、".join(errs[:3])
        ok = False
    else:
        summary = register(role, ctx, entry, message)
        ok = True
    state["agents"]["history"].append({"role": role, "agent_id": aid, "start": entry.get("since"),
                                       "end": now_iso(), "ok": ok, "summary": summary})
    state["stop_blocks"] = 0
    save_state(spath, state)
    return None


def _tainted(entry, ctx):
    return bool(entry.get("code_fp_start")) and entry["code_fp_start"] != ctx.code_fp


def _last_message(path):
    if not path or not os.path.exists(path):
        return ""
    import json
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    obj = json.loads(ln)
                except ValueError:
                    continue
                msg = obj.get("message") or {}
                if obj.get("type") == "assistant" or msg.get("role") == "assistant":
                    content = msg.get("content")
                    if isinstance(content, str):
                        last = content
                    elif isinstance(content, list):
                        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                        if texts:
                            last = "\n".join(texts)
    except OSError:
        return ""
    return last


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "AQ-DLC: " + reason}}


# ------------------------------------------------------------------ permissions
ENGINE_RE = re.compile(r"aqdlc\.py[\"']?\s+[\"']?([a-z][a-z-]*)")
COMMON_READ = {"status", "next", "trace", "history", "show", "spec", "checks", "memory", "check", "escalation"}
ROLE_COMMANDS = {
    "requirements": COMMON_READ | {"req", "accept", "risk", "verify", "assume", "question", "drop"},
    "design": COMMON_READ | {"design-map", "decision", "add-check", "remove-check", "bind"},
    "implementer": COMMON_READ | {"bind", "impl-map", "add-check", "gate"},
    "reviewer": COMMON_READ,
}
WORK_COMMANDS = {"req", "accept", "risk", "verify", "assume", "question", "drop", "design-map", "decision",
                 "add-check", "remove-check", "bind", "impl-map"}
REVIEWER_BASH_DENY = re.compile(
    r"\bgit\s+(add|commit|push|checkout|switch|reset|stash|rebase|merge|restore|clean|rm|mv|apply|cherry-pick)\b"
    r"|(^|[\s;&|])(rm|mv|cp|touch|tee|truncate)\s|\bsed\s+-i|\bperl\s+-p?i"
    r"|\b(npm|pnpm|yarn|bun)\s+(i|install|add|remove|uninstall|update|upgrade)\b|\bpip3?\s+install\b"
    r"|--update-snapshots|\s-u\b.*\b(jest|vitest)\b|\b(jest|vitest)\b.*\s-u\b")


def check_bash(role, cmd, project, state, cfg):
    if not cfg["automation"].get("enforce_roles", True):
        return None
    if re.search(r"aqdlc\.py[\"']?\s+[\"']?(hook\b|[a-z-]+-hook\b)", cmd):
        return "hook 用のコマンドは手動で実行できません。"
    subs = ENGINE_RE.findall(cmd)
    for sub in subs:
        if role in ROLES:
            if sub not in ROLE_COMMANDS[role]:
                return (f"{ROLE_LABEL[role]} はエンジンの `{sub}` を使えません。"
                        "必要なら成果物（差し戻し・報告）に書いて統括に返してください。")
            if sub == "spec" and re.search(r"--(present|approve)", cmd):
                return "仕様の提示・承認は統括だけが行います。"
        elif role in ("orchestrator", "other") and sub in WORK_COMMANDS:
            owner = {"req": "requirements", "accept": "requirements", "risk": "requirements",
                     "verify": "requirements", "assume": "requirements", "question": "requirements",
                     "drop": "requirements", "design-map": "design", "decision": "design",
                     "add-check": "design", "remove-check": "design", "bind": "implementer",
                     "impl-map": "implementer"}[sub]
            return (f"`{sub}` は {ROLE_LABEL[owner]} の担当です。統括は直接編集せず、"
                    f"Agent ツールで aq-dlc:{owner} に依頼してください。")
    if role == "reviewer" and REVIEWER_BASH_DENY.search(cmd):
        return "独立レビュー担当は、ファイルやリポジトリを変更するコマンドを実行できません。"
    return None


def check_write(role, path, project, state, cfg):
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(project, path)
    auto = cfg["automation"]
    if role == "reviewer":
        return "独立レビュー担当はファイルを変更できません。"
    if not is_within(path, project):
        return None
    rel = os.path.relpath(norm(path), norm(project)).replace(os.sep, "/")
    rd = run_dir(project, state.get("run_id")) if state else None
    arts = os.path.relpath(norm(artifacts_dir(rd)), norm(project)).replace(os.sep, "/") if rd else None
    base = os.path.basename(rel)
    if rel.startswith(".aq-dlc/"):
        if base in ("state.json", "config.json", "project.json", "actor.json"):
            return f"{base} は直接編集できません。エンジンのコマンドで更新してください。"
        if arts and rel.startswith(arts + "/"):
            own = {"spec.md": "requirements", "design.md": "design", "impl.md": "implementer"}.get(rel[len(arts) + 1:])
            if own is None:
                return "この成果物はエンジンが書き込みます。"
            if role != own and auto.get("enforce_roles", True):
                return f"{rel[len(arts) + 1:]} は {ROLE_LABEL[own]} だけが書けます。"
            return None
        if role in ROLES and auto.get("enforce_roles", True):
            return "サブエージェントは自分の成果物以外の .aq-dlc 配下を変更できません。"
        return None
    if not state or state.get("status") not in ("in_progress", "pr_ready") or not auto.get("enforce_roles", True):
        return None
    if rel.startswith(".claude/"):
        return None if role == "orchestrator" else "サブエージェントは .claude 配下を変更できません。"
    if role == "implementer":
        return None
    if role in ("orchestrator", "other") and auto.get("orchestrator_edits_code", False):
        return None
    who = ROLE_LABEL.get(role, "統括")
    return (f"run の実行中は、コードやテストを書けるのは実装担当（aq-dlc:implementer）だけです（{who} は書けません）。"
            "修正が必要なら Agent ツールで aq-dlc:implementer に依頼してください。")
