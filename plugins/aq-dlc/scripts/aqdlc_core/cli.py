"""コマンドと hook の入口。"""
import datetime as _dt
import json
import locale
import os
import re
import shutil
import sys
import time

from . import agents as A
from . import gate as G
from . import gitops, memory
from .project import (cmd_create, cmd_init, cmd_setup, config, engine_cmd, find_project, project_config_path,
                      resolve_project, unregister_active, active_project_for, register_active)
from .state import (ROLE_LABEL, Ctx, bump, describe_state, ensure_new_id, find, human_event, id_index,
                    iter_verifications, last_human_seq, load_state, log, new_run, next_action, problems,
                    req_by_id, require_state, run_dir, running_agents, save_state, spec_sheet, trace_rows)
from .util import (die, info, load_json, now_iso, pop_flag, pop_opt, read_text, save_json, setup_stdio,
                   sh_run, short, split_list, write_text)

USAGE = """AQ-DLC エンジン

準備:   setup / doctor / init ["<名前>"] / create "<名前>" / publish [--visibility V] [--dry-run] / new "<run名>"
要求:   req <ID> "<要求>" [--critical] [--kind nonfunctional] / accept <要求ID> <ID> "<受け入れ条件>"
        risk <要求ID> <ID> "<リスク>" / verify <受入ID|リスクID> <ID> "<確かめ方>" [--level L] [--manual "<手順>"] [--after-merge]
        question <ID> "<質問>" [--options "A（推奨）|B"] / assume "<内容>" [--out] / drop <ID>
設計:   design-map <要求ID> "<設計要素>" / decision "<判断>" / add-check <名前> "<コマンド>" --kind K [--needs a,b] / remove-check
実装:   bind <検証ID> [--command "..."] [--checks a,b] [--test "..."] [--level L] / impl-map <要求ID> --files a,b
統括:   next / status / spec [--present | --approve "<人間の返答の要約>"] / answer <質問ID> "<回答>"
        ask "<質問>" / resume / wait "<理由>" / confirm <検証ID> "<人間の確認内容>" / escalation [<ID> --resolve "<対応>"]
        gate [--after-merge] / ci [--wait 分] / complete / abort
Memory: memory [--tags a,b] [--record] / memory-use <ID> --result helped|not_applicable|harmful
        memory-done [--saved ID,..] [--merged ID,..] [--skipped "<理由>"]
参照:   trace / history [--since N] / show <ID> / checks / check / metrics [--json]
すべてのコマンドに --project <パス> を付けられる（省略時はカレントから探す）。"""


# ------------------------------------------------------------------ helpers
def open_state(args):
    project = resolve_project(args)
    state, spath = require_state(project)
    return project, state, spath


def mutate(args, fn):
    project, state, spath = open_state(args)
    if state.get("status") not in ("in_progress", "pr_ready"):
        die(f"この run は {state.get('status')} です。新しい開発は new で始めてください。")
    fn(project, state, args)
    save_state(spath, state)


def unique_id(state, prefix):
    idx, _ = id_index(state)
    n = 1
    while f"{prefix}{n}" in idx:
        n += 1
    return f"{prefix}{n}"


KIND_ALIASES = {"機能": "functional", "非機能": "nonfunctional", "functional": "functional",
                "nonfunctional": "nonfunctional", "non-functional": "nonfunctional"}


# ------------------------------------------------------------------ run
def cmd_new(args):
    force = pop_flag(args, "--force")
    project = resolve_project(args)
    cur, _ = load_state(project)
    if cur and cur.get("status") in ("in_progress", "pr_ready") and not force:
        die(f"進行中の run があります: {cur['run_id']}（{cur.get('status')}）。complete か abort をしてから始めるか、"
            "--force を付けてください（進行中の run はそのまま残ります）。")
    title = " ".join(args).strip() or "run"
    rid, d = new_run(project, title)
    info(f"run を開始しました: {rid}\n  状態: {os.path.join(d, 'state.json')}\n  記録: {os.path.join(d, 'run.md')}")
    print("次: Agent ツールで aq-dlc:requirements を呼び、ユーザーの依頼文を渡す。")


# ------------------------------------------------------------------ requirements role
def cmd_req(args):
    critical = pop_flag(args, "--critical")
    kind = pop_opt(args, "--kind")

    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: req <ID> "<要求>" [--critical] [--kind nonfunctional]')
        rid, text = a[0], " ".join(a[1:])
        k = KIND_ALIASES.get(kind or "functional")
        if not k:
            die("--kind は functional / nonfunctional（機能 / 非機能）")
        node = ensure_new_id(s, rid, ("requirement", None))
        if node:
            before = dict(text=node["text"], critical=node.get("critical"), kind=node.get("kind"))
            node.update(text=text, critical=critical or node.get("critical", False), kind=kind and k or node.get("kind"))
            log(s, project, "req.update", {"id": rid, "before": before, "after": text})
        else:
            s["requirements"].append({"id": rid, "text": text, "kind": k, "critical": critical,
                                      "acceptance": [], "risks": []})
            log(s, project, "req.add", {"id": rid, "text": text})
        info(f"要求 {rid}{'（重要）' if critical else ''}{'（非機能）' if k == 'nonfunctional' else ''}: {text}")
    mutate(args, fn)


def _child(args, list_key, kind, usage):
    def fn(project, s, a):
        if len(a) < 3:
            die(usage)
        r = req_by_id(s, a[0])
        nid, text = a[1], " ".join(a[2:])
        node = ensure_new_id(s, nid, (kind, r["id"]))
        if node:
            log(s, project, f"{kind}.update", {"id": nid, "before": node["text"], "after": text})
            node["text"] = text
        else:
            r.setdefault(list_key, []).append({"id": nid, "text": text, "verifications": []})
            log(s, project, f"{kind}.add", {"id": nid, "parent": r["id"], "text": text})
        info(f"{'受け入れ条件' if kind == 'acceptance' else 'リスク'} {r['id']}/{nid}: {text}")
    mutate(args, fn)


def cmd_accept(args):
    _child(args, "acceptance", "acceptance", '使い方: accept <要求ID> <受け入れ条件ID> "<条件>"')


def cmd_risk(args):
    _child(args, "risks", "risk", '使い方: risk <要求ID> <リスクID> "<起こりうる失敗>"')


def _apply_vopts(v, o, project, s):
    changes = {}
    for key, val in o.items():
        if val is None:
            continue
        if key == "level" and val not in G.LEVELS:
            die(f"--level は {' / '.join(sorted(G.LEVELS))} のどれか")
        if key == "checks":
            val = split_list(val)
            cfg_names = {c["name"] for c in config(project).get("checks", [])}
            unknown = [n for n in val if n not in cfg_names]
            if unknown:
                die(f"登録されていないチェック: {', '.join(unknown)}（add-check で登録）")
        if key == "timeout_sec":
            val = int(val)
        if v.get(key) != val:
            changes[key] = {"before": v.get(key), "after": val}
            v[key] = val
    return changes


def cmd_verify(args):
    o = {"level": pop_opt(args, "--level"), "manual": pop_opt(args, "--manual"),
         "command": pop_opt(args, "--command"), "checks": pop_opt(args, "--checks"),
         "timeout_sec": pop_opt(args, "--timeout")}
    after = pop_flag(args, "--after-merge")

    def fn(project, s, a):
        if len(a) < 3:
            die('使い方: verify <受け入れ条件ID|リスクID> <検証ID> "<確かめ方>" [--level L] [--manual "..."] [--after-merge]')
        kind, parent, _ = find(s, a[0], ("acceptance", "risk"))
        vid, method = a[1], " ".join(a[2:])
        v = ensure_new_id(s, vid, ("verification", parent["id"]))
        new = v is None
        if new:
            v = {"id": vid, "method": method}
            parent.setdefault("verifications", []).append(v)
        elif v.get("method") != method:
            v["method"] = method
        if after:
            v["after_merge"] = True
        changes = _apply_vopts(v, o, project, s)
        log(s, project, "verify.add" if new else "verify.update", {"id": vid, "parent": parent["id"],
                                                                     "method": method, "changes": changes})
        info(f"検証 {parent['id']}/{vid}: {method}{'（マージ後）' if v.get('after_merge') else ''}")
    mutate(args, fn)


def cmd_question(args):
    options = pop_opt(args, "--options")

    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: question <ID> "<質問>" [--options "A（推奨）|B"]')
        qid, text = a[0], " ".join(a[1:])
        q = ensure_new_id(s, qid, ("question", None))
        opts = [x.strip() for x in (options or "").split("|") if x.strip()]
        if q:
            q.update(text=text, options=opts or q.get("options", []))
        else:
            s["questions"].append({"id": qid, "text": text, "options": opts, "status": "open", "seq": bump(s)})
        log(s, project, "question", {"id": qid, "text": text, "options": opts})
        info(f"質問 {qid}: {text}" + (f"（選択肢: {' / '.join(opts)}）" if opts else ""))
    mutate(args, fn)


def cmd_assume(args):
    out = pop_flag(args, "--out")

    def fn(project, s, a):
        text = " ".join(a).strip()
        if not text:
            die('使い方: assume "<内容>" [--out]')
        key, prefix = ("out_of_scope", "O") if out else ("assumptions", "A")
        nid = unique_id(s, prefix)
        s[key].append({"id": nid, "text": text})
        log(s, project, key, {"id": nid, "text": text})
        info(f"{'対象外' if out else '仮定'} {nid}: {text}")
    mutate(args, fn)


def cmd_drop(args):
    def fn(project, s, a):
        if not a:
            die("使い方: drop <ID>")
        kind, node, parent = find(s, a[0])
        if kind == "requirement":
            s["requirements"].remove(node)
        elif kind in ("acceptance", "risk"):
            r = req_by_id(s, parent)
            r["acceptance" if kind == "acceptance" else "risks"].remove(node)
        elif kind == "verification":
            for r in s["requirements"]:
                for p in r.get("acceptance", []) + r.get("risks", []):
                    if node in p.get("verifications", []):
                        p["verifications"].remove(node)
        else:
            key = {"question": "questions", "assumption": "assumptions", "out_of_scope": "out_of_scope",
                   "decision": "decisions"}.get(kind)
            if not key:
                die(f"{kind} は drop できません。")
            s[key].remove(node)
        log(s, project, "drop", {"id": a[0], "kind": kind, "node": node})
        info(f"削除しました: {a[0]}（{kind}）。削除は履歴に残り、独立レビューで確認されます。")
    mutate(args, fn)


# ------------------------------------------------------------------ design role
def cmd_design_map(args):
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: design-map <要求ID> "<設計要素>"')
        req_by_id(s, a[0])
        s["design"]["map"][a[0]] = " ".join(a[1:])
        log(s, project, "design-map", {"id": a[0], "to": s["design"]["map"][a[0]]})
        info(f"設計の対応 {a[0]} → {s['design']['map'][a[0]]}")
    mutate(args, fn)


def cmd_decision(args):
    def fn(project, s, a):
        text = " ".join(a).strip()
        if not text:
            die('使い方: decision "<判断と理由>"')
        nid = unique_id(s, "D")
        s["decisions"].append({"id": nid, "text": text})
        log(s, project, "decision", {"id": nid, "text": text})
        info(f"設計判断 {nid}: {text}")
    mutate(args, fn)


def cmd_add_check(args):
    project = resolve_project(args)
    timeout = pop_opt(args, "--timeout")
    kind = pop_opt(args, "--kind") or "other"
    needs = split_list(pop_opt(args, "--needs"))
    if kind not in G.KINDS:
        die(f"--kind は {' / '.join(G.KIND_ORDER)} のどれか")
    if len(args) < 2:
        die('使い方: add-check <名前> "<コマンド>" --kind K [--needs a,b] [--timeout 秒]')
    name, command = args[0], " ".join(args[1:])
    if not re.match(r"^[A-Za-z0-9_.-]{1,40}$", name):
        die("チェック名は英数字と ._- の40文字以内")
    path = project_config_path(project)
    cfg = load_json(path, {}) or {}
    old = next((c for c in cfg.get("checks", []) if c.get("name") == name), None)
    entry = {"name": name, "command": command, "kind": kind}
    if needs:
        entry["needs"] = needs
    if timeout:
        entry["timeout_sec"] = int(timeout)
    cfg["checks"] = [c for c in cfg.get("checks", []) if c.get("name") != name] + [entry]
    save_json(path, cfg)
    state, spath = load_state(project)
    if state:
        log(state, project, "check.update" if old else "check.add", {"name": name, "before": old, "after": entry})
        save_state(spath, state)
    info(f"チェックを登録しました: {name}（{kind}）→ {command}")


def cmd_remove_check(args):
    project = resolve_project(args)
    if not args:
        die("使い方: remove-check <名前>")
    path = project_config_path(project)
    cfg = load_json(path, {}) or {}
    old = next((c for c in cfg.get("checks", []) if c.get("name") == args[0]), None)
    cfg["checks"] = [c for c in cfg.get("checks", []) if c.get("name") != args[0]]
    save_json(path, cfg)
    state, spath = load_state(project)
    if state and old:
        log(state, project, "check.remove", {"name": args[0], "before": old})
        save_state(spath, state)
    info(f"{'削除しました' if old else '見つかりませんでした'}: {args[0]}")


def cmd_checks(args):
    project = resolve_project(args)
    checks = config(project).get("checks", [])
    if not checks:
        print("チェックは登録されていません。")
    for c in G.ordered_checks(checks):
        need = G.implicit_needs(c, checks)
        print(f"{c['name']}（{c.get('kind', 'other')}）: {c['command']}" + (f"  前提: {', '.join(need)}" if need else ""))


# ------------------------------------------------------------------ implementer role
def cmd_bind(args):
    o = {"command": pop_opt(args, "--command"), "checks": pop_opt(args, "--checks"),
         "test": pop_opt(args, "--test"), "level": pop_opt(args, "--level"),
         "timeout_sec": pop_opt(args, "--timeout"), "manual": pop_opt(args, "--manual")}

    def fn(project, s, a):
        if not a or all(v is None for v in o.values()):
            die('使い方: bind <検証ID> [--command "..."] [--checks a,b] [--test "..."] [--level L]')
        _k, v, parent = find(s, a[0], ("verification",))
        changes = _apply_vopts(v, o, project, s)
        if changes:
            log(s, project, "bind", {"id": a[0], "parent": parent, "changes": changes})
        info(f"検証 {a[0]} を更新: " + ("、".join(f"{k}={c['after']}" for k, c in changes.items()) or "変更なし"))
    mutate(args, fn)


def cmd_impl_map(args):
    files = split_list(pop_opt(args, "--files"))

    def fn(project, s, a):
        if not a or not files:
            die('使い方: impl-map <要求ID> --files "src/a.ts,src/b.ts"')
        req_by_id(s, a[0])
        rels = []
        for f in files:
            p = f if os.path.isabs(f) else os.path.join(project, f)
            if not os.path.exists(p):
                die(f"ファイルがありません: {f}")
            rels.append(os.path.relpath(p, project).replace(os.sep, "/"))
        s["impl"]["map"][a[0]] = rels
        log(s, project, "impl-map", {"id": a[0], "files": rels})
        info(f"実装の対応 {a[0]} → {', '.join(rels)}")
    mutate(args, fn)


# ------------------------------------------------------------------ orchestrator
def cmd_spec(args):
    present = pop_flag(args, "--present")
    approve = pop_opt(args, "--approve")
    project, s, spath = open_state(args)
    ctx = Ctx(project, s)
    sp = s["spec"]
    if approve is None and not present:
        print(spec_sheet(s), end="")
        st = "未登録" if not sp.get("registered_hash") else (
            "承認済み" if sp.get("approved_hash") == ctx.spec_hash else
            "提示済み・承認待ち" if sp.get("presented_hash") == ctx.spec_hash else "未提示")
        print(f"\n（仕様 {short(ctx.spec_hash)}: {st}）")
        return
    probs = [p for p in problems(ctx) if p["code"] in ("dup_ids", "spec_missing", "graph_incomplete",
                                                         "spec_unregistered", "questions_open", "answers_unapplied")]
    if probs:
        die("仕様を提示・承認できる状態ではありません: " + probs[0]["msg"])
    if present:
        print(spec_sheet(s), end="")
        sp["presented_hash"] = ctx.spec_hash
        sp["presented_seq"] = bump(s)
        save_state(spath, s)
        print(f"\n（仕様 {short(ctx.spec_hash)} を提示として記録。人間の承認を得たら spec --approve \"<返答の要約>\"）")
        return
    if sp.get("presented_hash") != ctx.spec_hash:
        die("この版の仕様はまだ人間に提示されていません。spec --present で提示してから確認してください。")
    if last_human_seq(s) <= sp.get("presented_seq", 0):
        die("提示の後に人間の返答が記録されていません。AskUserQuestion などで人間に確認し、返答を得てから承認してください。")
    if not approve.strip():
        die("--approve には人間の返答の要約を書いてください。")
    sp.update({"approved_hash": ctx.spec_hash, "approved_seq": bump(s), "approved_at": now_iso(), "note": approve})
    s["awaiting_human"] = False
    log(s, project, "spec.approve", {"hash": ctx.spec_hash, "note": approve})
    save_state(spath, s)
    info(f"仕様 {short(ctx.spec_hash)} の承認を記録しました。次は設計（aq-dlc:design）です。")


def cmd_answer(args):
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: answer <質問ID> "<人間の回答>"')
        _k, q, _p = find(s, a[0], ("question",))
        if last_human_seq(s) <= q.get("seq", 0):
            die("この質問の後に人間の返答が記録されていません。AskUserQuestion で人間に聞いてから記録してください。")
        q.update(status="answered", answer=" ".join(a[1:]), answered_seq=bump(s), answered_at=now_iso())
        log(s, project, "answer", {"id": a[0], "answer": q["answer"]})
        info(f"回答を記録しました: {a[0]} → {q['answer']}")
    mutate(args, fn)


def cmd_ask(args):
    def fn(project, s, a):
        s.update(awaiting_human=True, question=" ".join(a), asked_seq=bump(s))
        s["asks_total"] = int(s.get("asks_total", 0)) + 1
        info("人間への確認待ちとして記録しました（返答を得たら resume）。")
    mutate(args, fn)


def cmd_resume(args):
    def fn(project, s, a):
        s.update(awaiting_human=False, waiting=None)
        info("待機を解除しました。")
    mutate(args, fn)


def cmd_wait(args):
    minutes = int(pop_opt(args, "--minutes") or 30)

    def fn(project, s, a):
        s["waiting"] = {"for": " ".join(a) or "バックグラウンド処理", "since": now_iso(),
                        "until": time.time() + minutes * 60}
        info(f"待機中として記録しました（最大{minutes}分）: {s['waiting']['for']}")
    mutate(args, fn)


def cmd_confirm(args):
    def fn(project, s, a):
        if len(a) < 2:
            die('使い方: confirm <検証ID> "<人間の確認内容>"')
        _k, v, _p = find(s, a[0], ("verification",))
        if G.is_automated(v) and not v.get("after_merge"):
            die(f"{a[0]} は自動の検証です。gate で確かめてください。")
        if not s.get("asked_seq") or last_human_seq(s) <= s["asked_seq"]:
            die("ask の後に人間の返答が記録されていません。ask を実行して人間に確認してから記録してください。")
        v.update(confirmed_by="human", confirm_note=" ".join(a[1:]), confirmed_seq=bump(s))
        log(s, project, "confirm", {"id": a[0], "note": v["confirm_note"]})
        info(f"人間の確認として記録しました: {a[0]}")
    mutate(args, fn)


def cmd_escalation(args):
    resolve = pop_opt(args, "--resolve")
    project, s, spath = open_state(args)
    if not args:
        if not s["escalations"]:
            print("差し戻しはありません。")
        for e in s["escalations"]:
            print(f"{e['id']} [{'対応済み' if e.get('status') == 'resolved' else '未対応'}]（{e['from']}）{e['text']}"
                  + (f" → {e.get('resolution')}" if e.get("resolution") else ""))
        return
    if not resolve:
        die('使い方: escalation <ID> --resolve "<対応内容>"')
    _k, e, _p = find(s, args[0], ("escalation",))
    e.update(status="resolved", resolution=resolve, resolved_seq=bump(s))
    log(s, project, "escalation.resolve", {"id": args[0], "resolution": resolve})
    save_state(spath, s)
    info(f"差し戻し {args[0]} を対応済みにしました。")


def cmd_gate(args):
    after_merge = pop_flag(args, "--after-merge")
    force = pop_flag(args, "--force")
    project, s, spath = open_state(args)
    cfg = config(project)
    ctx = Ctx(project, s, cfg)
    _i, dups = id_index(s)
    if dups:
        die(f"IDが重複しているため gate を実行できません（結果が上書きされる）: {', '.join(dups)}")
    from .state import actor_hint
    actor = actor_hint(project)
    run = running_agents(s)
    if run and not force and not (actor == "implementer" and all(v["role"] == "implementer" for v in run.values())):
        die("サブエージェントの実行中は gate を実行しません（同じフォルダでテストが衝突するため）。完了を待ってください。")
    spec_block = [p for p in problems(ctx) if p["code"] in ("spec_missing", "spec_unpresented", "spec_unapproved",
                                                              "questions_open", "spec_unregistered")]
    if spec_block and not force:
        die(f"{spec_block[0]['msg']}。仕様の承認前は gate を実行しません（実装の前に仕様をすり合わせるため）。")
    lock = os.path.join(ctx.rd, "gate.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3600 and not force:
        die("別の gate が実行中です（終わってから再実行。前回が異常終了した場合は --force）。")
    write_text(lock, str(os.getpid()))
    try:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        info(f"gate {stamp}  ({project})")
        # 実行開始時のコード・チェック定義・検証条件（gate の結果はこれに紐付ける）
        before = G.snapshot(project, cfg, (s.get("gate") or {}).get("snapshot"))
        checks_fp_before = G.checks_fp(cfg)
        vfp_before = {v["id"]: G.verification_fp(v, cfg) for _r, _p, v in iter_verifications(s)}
        t0 = time.time()
        results, ok = G.run_gate(project, s, cfg, iter_verifications, os.path.join(ctx.rd, "gate"), stamp,
                                 after_merge)
        dur = round(time.time() - t0, 1)
        cfg_after = config(project)
        after = G.snapshot(project, cfg_after, before)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    s, spath = load_state(project)  # gate 実行中に他の記録が入っていてもよいように読み直す
    c, a, r = G.snapshot_diff(before, after)
    changed_defs = []
    if G.checks_fp(cfg_after) != checks_fp_before:
        changed_defs.append("チェックの定義")
    vfp_after = {v["id"]: G.verification_fp(v, cfg_after) for _r, _p, v in iter_verifications(s)}
    if vfp_after != vfp_before:
        changed_defs.append("検証条件")
    modified = None
    if c or a or r or changed_defs:
        modified = {"files": c + a + r, "changed": c, "added": a, "removed": r, "definitions": changed_defs,
                    "summary": G.describe_diff(before, after) or "、".join(changed_defs)}
        ok = False  # 検証したものと結果を紐付けるものが一致しないため不合格
    s["gate"] = {"seq": bump(s), "stamp": stamp, "ran_at": now_iso(), "after_merge": after_merge, "sec": dur,
                 "code_fp": G.code_fp(before), "code_fp_after": G.code_fp(after), "snapshot": before,
                 "checks_fp": checks_fp_before, "graph_fp": ctx.graph_fp, "checks_pass": ok, "results": results,
                 "modified_during_gate": modified}
    s.setdefault("gate_history", []).append({"stamp": stamp, "pass": ok, "sec": dur, "after_merge": after_merge,
                                             "modified": bool(modified)})
    if after_merge:
        s["after_merge_gate"] = True
    s["stop_blocks"] = 0
    save_state(spath, s)
    register_active(project, s["run_id"])
    if not cfg.get("checks"):
        print("  ! チェックが登録されていません（完了条件を満たせません）")
    if modified:
        print(f"  ! gate の実行中に変更がありました: {modified['summary']}")
        print("    検証したものと結果を紐付けるものが一致しないため、gate を不合格にしました。"
              "テストやビルドが追跡対象のファイルを書き換えていないか確認してください。")
        log_dir = os.path.join(ctx.rd, "gate")
        write_text(os.path.join(log_dir, f"{stamp}-modified.log"),
                   "\n".join(["gate の実行中に変わったもの"] + [f"変更: {x}" for x in c] + [f"追加: {x}" for x in a]
                             + [f"削除: {x}" for x in r] + [f"定義: {x}" for x in changed_defs]) + "\n")
    info(f"gate 結果: {'合格' if ok else '不合格'}")


def cmd_ci(args):
    wait_min = float(pop_opt(args, "--wait") or 8)
    project, s, spath = open_state(args)
    rec, msg = G.check_ci(project, wait_min)
    if rec is None:
        info(msg)
        return
    s, spath = load_state(project)
    s["ci"] = rec
    save_state(spath, s)
    G.print_ci(rec)
    if rec["result"] != "success":
        sys.exit(1)


def cmd_complete(args):
    project, s, spath = open_state(args)
    if running_agents(s):
        die("サブエージェントの実行中は完了にできません。")
    ctx = Ctx(project, s)
    probs = problems(ctx, "pr")
    if probs:
        print("[aq-dlc] まだ PR を出せる状態ではありません:\n  - " + "\n  - ".join(p["msg"] for p in probs))
        sys.exit(1)
    final = problems(ctx, "final")
    if final:
        s["status"] = "pr_ready"
        save_state(spath, s)
        print("[aq-dlc] PR を出せる状態です（status: pr_ready）。マージ後の確認が残っています:\n  - "
              + "\n  - ".join(p["msg"] for p in final)
              + "\n→ PR を作成して報告し、マージを待つ（この間 Stop hook は止めない）。"
                "\n→ マージ後: 既定ブランチを取り込み → gate --after-merge → 人の確認は ask でまとめて1回 → confirm → complete")
        return
    s["status"] = "complete"
    s["metrics"] = collect_metrics(s)
    save_state(spath, s)
    info("完了条件を満たしました。status を complete にしました。（運用の指標は metrics で確認）")


def cmd_abort(args):
    project, s, spath = open_state(args)
    s["status"] = "aborted"
    save_state(spath, s)
    unregister_active(project)
    info("run を中断しました。")


def cmd_next(args):
    project, s, _ = open_state(args)
    phase, nxt, probs = next_action(Ctx(project, s))
    print(f"フェーズ: {phase}\n次: {nxt}")
    if len(probs) > 1:
        print("ほかの未充足:\n  - " + "\n  - ".join(p["msg"] for p in probs[1:6]))


def cmd_status(args):
    project = resolve_project(args)
    s, _ = load_state(project)
    print(f"プロジェクト: {project}")
    if not s:
        print("実行中の run はありません。")
        return
    ctx = Ctx(project, s)
    phase, nxt, probs = next_action(ctx)
    print(f"run: {s['run_id']}  状態: {s['status']}  フェーズ: {phase}")
    for ln in describe_state(ctx):
        print("  " + ln)
    if s.get("legacy"):
        print("  ! 旧形式（v0.4 以前）の run です。新しい run で始め直すことをおすすめします。")
    print(f"次: {nxt}")
    if probs:
        print("未充足:\n  - " + "\n  - ".join(p["msg"] for p in probs[:8]))


def cmd_check(args):
    project, s, _ = open_state(args)
    stage = "final" if s.get("status") == "pr_ready" else "pr"
    probs = problems(Ctx(project, s), stage)
    if probs:
        print("[aq-dlc] 完了条件を満たしていません:\n  - " + "\n  - ".join(p["msg"] for p in probs))
        sys.exit(1)
    info("完了条件 OK")


def cmd_trace(args):
    project, s, _ = open_state(args)
    rows = trace_rows(Ctx(project, s))
    head = ["要求", "受入/リスク", "設計", "実装ファイル", "検証", "テスト", "結果", "gate"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(str(c).replace("|", "/") for c in r) + " |")


def cmd_history(args):
    since = int(pop_opt(args, "--since") or 0)
    project, s, _ = open_state(args)
    for h in s["history"]:
        if h["seq"] > since:
            print(f"#{h['seq']} {h['at']} [{h['actor']}] {h['action']} {json.dumps(h['detail'], ensure_ascii=False)}")


def cmd_show(args):
    project, s, _ = open_state(args)
    if not args:
        die("使い方: show <ID>")
    kind, node, parent = find(s, args[0])
    print(json.dumps({"kind": kind, "parent": parent, **node}, ensure_ascii=False, indent=2))


def _secs(a, b):
    try:
        return (_dt.datetime.fromisoformat(b) - _dt.datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return 0


def collect_metrics(s):
    hist = s["agents"].get("history", [])
    roles = {}
    for h in hist:
        r = roles.setdefault(h["role"], {"runs": 0, "sec": 0.0, "ok": 0, "stop_blocks": 0, "tokens": None})
        r["runs"] += 1
        r["ok"] += 1 if h.get("ok") else 0
        r["sec"] += h.get("sec") or _secs(h.get("start"), h.get("end"))
        r["stop_blocks"] += h.get("stop_blocks", 0)
        if h.get("tokens"):
            t = r["tokens"] or {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
            for k in t:
                t[k] += h["tokens"].get(k, 0)
            r["tokens"] = t
    gh = s.get("gate_history", [])
    reviews = s.get("reviews", [])
    end = s.get("closed_at") or now_iso()
    return {
        "run_sec": round(_secs(s.get("created"), end)),
        "roles": roles,
        "gate": {"runs": len(gh), "failed": sum(1 for g in gh if not g.get("pass")),
                 "modified_during": sum(1 for g in gh if g.get("modified")),
                 "sec": round(sum(g.get("sec") or 0 for g in gh), 1)},
        "reviews": {"runs": len(reviews), "passed": sum(1 for r in reviews if r.get("passed")),
                    "needs_fix": sum(1 for r in reviews if r.get("valid") and not r.get("passed")),
                    "invalid": sum(1 for r in reviews if not r.get("valid"))},
        "fix_iterations": s.get("fix_iterations", 0),
        "human": {"replies": len(s.get("human_events", [])), "asks": s.get("asks_total", 0)},
        "stop_blocks_total": s.get("stop_blocks_total", 0),
        "violations": len(s.get("violations", [])),
        "orchestrator_tokens": A.transcript_usage(s.get("session_transcript")),
    }


def cmd_metrics(args):
    as_json = pop_flag(args, "--json")
    project, s, _ = open_state(args)
    m = collect_metrics(s)
    if as_json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return

    def tok(t):
        return "取得できず" if not t else f"入力 {t['input']:,} / 出力 {t['output']:,} / キャッシュ読込 {t['cache_read']:,}"
    print(f"run 全体の経過時間: {m['run_sec'] // 60} 分 {m['run_sec'] % 60} 秒")
    print("| 担当 | 実行回数 | 再実行 | 所要時間 | 終了の差し戻し | トークン |\n|---|---|---|---|---|---|")
    for role, r in m["roles"].items():
        print(f"| {ROLE_LABEL.get(role, role)} | {r['runs']} | {max(0, r['runs'] - 1)} | {round(r['sec'])} 秒 | "
              f"{r['stop_blocks']} | {tok(r['tokens'])} |")
    g = m["gate"]
    print(f"Quality Gate: {g['runs']} 回（不合格 {g['failed']} 回、うち実行中の変更 {g['modified_during']} 回、計 {g['sec']} 秒）")
    rv = m["reviews"]
    print(f"独立レビュー: {rv['runs']} 回（合格 {rv['passed']}・要修正 {rv['needs_fix']}・無効 {rv['invalid']}）"
          f"  修正ループ: {m['fix_iterations']} 回")
    print(f"人間の介入: 返答 {m['human']['replies']} 回・確認依頼 {m['human']['asks']} 回  "
          f"Stop hook による継続: {m['stop_blocks_total']} 回  担当外の変更: {m['violations']} 件")
    print(f"統括のトークン: {tok(m['orchestrator_tokens'])}")


def cmd_doctor(args):
    project = resolve_project(args, required=False) or os.getcwd()
    ok = True

    def line(good, msg, fix=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK ' if good else 'NG '} {msg}" + (f"\n       → {fix}" if fix and not good else ""))
    info("環境の確認")
    line(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}", "Python 3.8 以上を入れてください")
    has_git = shutil.which("git") is not None
    line(has_git, "git", "git をインストールしてください")
    if has_git:
        for k in ("user.name", "user.email"):
            code, out = sh_run(f"git config --get {k}", project, 10)
            line(code == 0 and bool(out.strip()), f"git {k}: {out.strip() or '未設定'}",
                 f'git config --global {k} "<値>" を実行してください')
    has_gh = shutil.which("gh") is not None
    line(has_gh, "gh（GitHub CLI）", "GitHub 連携を使う場合はインストールして gh auth login を実行してください")
    if has_gh:
        line(sh_run("gh auth status", project, 20)[0] == 0, "gh のログイン", "gh auth login を実行してください")
    enc = locale.getpreferredencoding(False)
    print(f"  情報 OS の文字コード: {enc}（子プロセスの出力は UTF-8 → {enc} → cp932 の順で読む）")
    if sys.platform == "win32":
        print("  情報 Windows: Playwright はプロジェクト内にブラウザを入れる設定（PLAYWRIGHT_BROWSERS_PATH=0）を推奨。"
              "init 時に gate.env に設定済み。")
        if "WindowsApps" in sys.executable:
            print("  注意 Microsoft Store 版の Python です。AppData への書き込みが別の場所に振り替えられることがあります。")
    info("問題は見つかりませんでした。" if ok else "NG の項目を直すと、途中で止まりにくくなります。")


# ------------------------------------------------------------------ hooks
LANG_RULE = ("【言語】ユーザーへの返答、質問、進捗報告、run.md、Memory、コミットメッセージ、PR のタイトルと本文、"
             "Issue、コード内のコメント、サブエージェントへの依頼と報告は、すべて日本語で書いてください。"
             "英語のエラーやログを伝えるときは、日本語で要約してから必要な部分だけ引用してください。"
             "コードの識別子やコマンドは英語のままで構いません。")


def read_payload():
    from .util import decode
    try:
        return json.loads(decode(sys.stdin.buffer.read()) or "{}")
    except Exception:  # noqa: BLE001
        return {}


def emit(obj):
    if obj:
        print(json.dumps(obj, ensure_ascii=False))


def hook_context(payload):
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = active_project_for(cwd)
    state, spath = (load_state(project) if project else (None, None))
    tp = payload.get("transcript_path")
    if state and tp and not payload.get("agent_type") and state.get("session_transcript") != tp:
        state["session_transcript"] = tp  # 統括の会話記録（トークンの集計に使う）
        save_state(spath, state)
    return cwd, project, state, spath


def hook_pre_bash(payload):
    cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    cwd, project, state, spath = hook_context(payload)
    role = A.role_of(payload.get("agent_type"))
    if not project:
        for m in re.finditer(r"(?:cd|-C|--project)\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd):
            cand = find_project(os.path.expanduser(m.group(1).strip("\"'")))
            if cand:
                project = cand
                state, spath = load_state(project)
                break
    if not project:
        return None
    cfg = config(project)
    if state:
        rd = run_dir(project, state.get("run_id"))
        if rd:
            save_json(os.path.join(rd, "actor.json"), {"role": role, "at": time.time(), "cmd": cmd[:8000]})
    why = A.check_bash(role, cmd, project, state, cfg)
    if why:
        return A.deny(why)
    # 実装担当以外の Bash は、実行前のコードの状態を記録しておき、実行後に変更がないか確かめる
    if state and state.get("status") in ("in_progress", "pr_ready") and role != "implementer" \
            and cfg["automation"].get("enforce_roles", True):
        rd = run_dir(project, state.get("run_id"))
        if rd:
            tmpd = os.path.join(rd, "tmp")
            if os.path.isdir(tmpd):  # 対応する PostToolUse が来なかった古い記録を掃除する
                for f in os.listdir(tmpd):
                    fp = os.path.join(tmpd, f)
                    try:
                        if time.time() - os.path.getmtime(fp) > 3600:
                            os.remove(fp)
                    except OSError:
                        pass
            prev = (state.get("gate") or {}).get("snapshot")
            save_json(A.bash_snapshot_path(rd, payload.get("tool_use_id")),
                      {"role": role, "cmd": cmd[:2000], "snapshot": G.snapshot(project, cfg, prev)})
    if re.search(r"\b(git|gh)\b|api\.github\.com", cmd):
        try:
            reasons = gitops.guard_reasons(cmd, project, gitops.current_branch, gitops.remote_has_branch)
        except Exception as e:  # noqa: BLE001
            if gitops.dangerous_words(cmd):
                return A.deny(f"ガードの判定に失敗したため、安全のため止めました（{e}）。人間に確認してください。")
            reasons = []
        if reasons:
            return A.deny("ガード: " + " / ".join(dict.fromkeys(reasons)))
    return None


def hook_post_bash(payload):
    """実装担当以外の Bash がコードやテストを変えていないか（リダイレクト・スクリプト・変数展開などの手段を問わず検出）。"""
    cwd, project, state, spath = hook_context(payload)
    if not project or not state:
        return None
    rd = run_dir(project, state.get("run_id"))
    if not rd:
        return None
    snap_path = A.bash_snapshot_path(rd, payload.get("tool_use_id"))
    pre = load_json(snap_path, None)
    if not pre:
        return None
    try:
        os.remove(snap_path)
    except OSError:
        pass
    cfg = config(project)
    after = G.snapshot(project, cfg, pre["snapshot"])
    v = A.detect_bash_changes(pre["role"], pre["cmd"], pre["snapshot"], after, project)
    if not v:
        return None
    state, spath = load_state(project)
    v["seq"] = bump(state)
    state.setdefault("violations", []).append(v)
    save_state(spath, state)
    who = ROLE_LABEL.get(pre["role"], "統括")
    reason = (f"AQ-DLC: 担当外の変更を検出しました（{who} の Bash で {v['count']} 件: {', '.join(v['files'][:5])}）。"
              "run の実行中にコードやテストを変えられるのは実装担当だけです。この変更は記録され、"
              "実装担当が内容を確認・取り込み、gate と独立レビューをやり直すまで、レビュー・完了に進めません。")
    if v["kind"] == "git":
        reason += "（git の操作による作業ツリーの変化も、取り込みの確認が必要です）"
    return {"decision": "block", "reason": reason,
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reason}}


def hook_pre_write(payload):
    ti = payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    cwd, project, state, spath = hook_context(payload)
    if not project or not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    project = os.path.realpath(project)
    why = A.check_write(A.role_of(payload.get("agent_type")), path, project, state, config(project))
    return A.deny(why) if why else None


def hook_pre_agent(payload):
    cwd, project, state, spath = hook_context(payload)
    if not project:
        role = A.role_of((payload.get("tool_input") or {}).get("subagent_type"))
        if role in A.ROLES:
            return A.deny("AQ-DLC プロジェクトではありません。init で初期化し、new で run を開始してください。")
        return None
    return A.on_pre_agent(payload, project, state, spath)


def hook_subagent_start(payload):
    cwd, project, state, spath = hook_context(payload)
    return A.on_subagent_start(payload, project, state, spath) if project else None


def hook_subagent_stop(payload):
    cwd, project, state, spath = hook_context(payload)
    return A.on_subagent_stop(payload, project, state, spath) if project else None


def hook_user_prompt(payload):
    cwd, project, state, spath = hook_context(payload)
    if state and state.get("status") in ("in_progress", "pr_ready"):
        human_event(state, "prompt", payload.get("prompt") or "")
        state["awaiting_human"] = False  # 人間が返答したので、確認待ちを解除する
        save_state(spath, state)
    return None


def hook_post_ask(payload):
    if A.role_of(payload.get("agent_type")) != "orchestrator":
        return None
    cwd, project, state, spath = hook_context(payload)
    if state and state.get("status") in ("in_progress", "pr_ready"):
        resp = payload.get("tool_response")
        human_event(state, "answer", json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) else resp)
        save_state(spath, state)
    return None


def hook_stop(payload):
    cwd, project, state, spath = hook_context(payload)
    if not project or not state or state.get("status") in ("aborted", "closed", "pr_ready"):
        return None
    if state.get("awaiting_human") or running_agents(state):
        return None
    w = state.get("waiting")
    if w and time.time() < w.get("until", 0):
        return None
    cfg = config(project)
    ctx = Ctx(project, state, cfg)
    if state.get("status") == "complete":
        final = problems(ctx, "final")
        if not final:
            state["status"] = "closed"
            state["closed_at"] = now_iso()
            save_state(spath, state)
            unregister_active(project)
            return None
        state["status"] = "in_progress"
    max_blocks = int(cfg["automation"]["max_auto_continues"])
    if state.get("stop_blocks", 0) >= max_blocks:
        state["stop_blocks"] = 0
        save_state(spath, state)
        print(f"[aq-dlc] 自動継続の上限（{max_blocks}回）に達したため停止を許可しました。", file=sys.stderr)
        return None
    phase, nxt, _probs = next_action(ctx)
    state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    state["stop_blocks_total"] = int(state.get("stop_blocks_total", 0)) + 1
    save_state(spath, state)
    reason = (f"AQ-DLC: run が進行中です（{project} / フェーズ: {phase}）。\n次: {nxt}\n"
              "人間の判断が必要なら ask を実行してから質問してください。中断するなら abort。\n"
              "（報告や記録はすべて日本語で書いてください）")
    return {"decision": "block", "reason": reason}


def hook_session_start(payload):
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if os.path.realpath(cwd) in (os.path.realpath(os.path.expanduser("~")), os.path.realpath(os.sep)):
        return None
    project = find_project(cwd)
    if not config(project)["automation"].get("auto_start", True):
        return None
    eng = engine_cmd()
    if project:
        state, _ = load_state(project)
        lines = [f"このフォルダは AQ-DLC プロジェクトです（{project}）。",
                 "あなたは AQ-DLC の統括（Main Orchestrator）です。開発・修正・機能追加の依頼は、コマンドがなくても "
                 "aq-dlc:start Skill の手順で進めます。要求分析・設計・実装・レビューはサブエージェントに任せ、"
                 "あなたは進行管理・人間への確認・Quality Gate の実行に専念してください。"]
        if state and state.get("status") in ("in_progress", "pr_ready"):
            phase, nxt, _p = next_action(Ctx(project, state))
            lines.append(f"進行中の run: {state['run_id']}（フェーズ: {phase}）。依頼がこの run の続きなら再開してください。")
            lines.append(f"次: {nxt}")
        lines.append(f"エンジン: {eng} <コマンド> --project \"{project}\"")
    else:
        try:
            visible = [e for e in os.listdir(cwd) if not e.startswith(".")]
        except OSError:
            return None
        if visible:
            return None
        lines = [f"このフォルダ（{cwd}）は空です。",
                 "ユーザーが新しいアプリやツールの作成を依頼したら、コマンドを打たれなくても aq-dlc:start Skill を使い、"
                 "このフォルダを AQ-DLC プロジェクトとして初期化して開発してください。",
                 "開発以外の質問や雑談なら、通常どおり答えてください。"]
    if sys.platform == "win32":
        lines.append("【Windows】Playwright を使う場合は、ブラウザをプロジェクト内に入れてください"
                     "（PLAYWRIGHT_BROWSERS_PATH=0 を付けて npx playwright install）。")
    lines.append(LANG_RULE)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}


HOOKS = {
    "session-start": hook_session_start, "user-prompt": hook_user_prompt, "pre-bash": hook_pre_bash,
    "pre-write": hook_pre_write, "pre-agent": hook_pre_agent, "post-ask": hook_post_ask,
    "post-bash": hook_post_bash,
    "subagent-start": hook_subagent_start, "subagent-stop": hook_subagent_stop, "stop": hook_stop,
}
LEGACY_HOOKS = {"stop-hook": "stop", "guard-hook": "pre-bash", "session-hook": "session-start",
                "agent-start-hook": None, "agent-stop-hook": None}
FAIL_CLOSED = {"pre-bash"}


def run_hook(name):
    payload = read_payload()
    fn = HOOKS.get(name)
    if not fn:
        return
    if name != "pre-bash":
        cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        if A.forged_hook(active_project_for(cwd), name):
            print("[aq-dlc] Bash から hook を直接呼ぶことはできません。", file=sys.stderr)
            return
    try:
        emit(fn(payload))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        if name in FAIL_CLOSED and gitops.dangerous_words((payload.get("tool_input") or {}).get("command", "") or ""):
            emit(A.deny(f"ガードの判定中にエラーが発生したため、安全のため止めました（{e}）"))
            return
        print(f"[aq-dlc] hook（{name}）でエラーが発生しました（作業は継続します）: {e}", file=sys.stderr)


# ------------------------------------------------------------------ main
COMMANDS = {
    "setup": cmd_setup, "init": cmd_init, "create": cmd_create, "doctor": cmd_doctor,
    "publish": gitops.cmd_publish, "new": cmd_new,
    "req": cmd_req, "accept": cmd_accept, "risk": cmd_risk, "verify": cmd_verify, "question": cmd_question,
    "assume": cmd_assume, "drop": cmd_drop,
    "design-map": cmd_design_map, "decision": cmd_decision, "add-check": cmd_add_check,
    "remove-check": cmd_remove_check, "checks": cmd_checks,
    "bind": cmd_bind, "impl-map": cmd_impl_map,
    "spec": cmd_spec, "answer": cmd_answer, "ask": cmd_ask, "resume": cmd_resume, "wait": cmd_wait,
    "confirm": cmd_confirm, "escalation": cmd_escalation, "gate": cmd_gate, "ci": cmd_ci,
    "memory": memory.cmd_memory, "memory-done": memory.cmd_memory_done, "memory-use": memory.cmd_memory_use, "complete": cmd_complete, "abort": cmd_abort,
    "next": cmd_next, "status": cmd_status, "check": cmd_check, "trace": cmd_trace, "history": cmd_history,
    "show": cmd_show, "metrics": cmd_metrics,
}
REMOVED = {"review": "レビュー結果は aq-dlc:reviewer の終了時にエンジンが自動で記録します（手動では記録できません）。",
           "phase": "フェーズは状態から自動で決まります（next で確認）。",
           "risk-only": ""}


def main(argv=None):
    setup_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return
    name, rest = argv[0], argv[1:]
    if name == "hook":
        run_hook(rest[0] if rest else "")
        return
    if name in LEGACY_HOOKS:
        if LEGACY_HOOKS[name]:
            run_hook(LEGACY_HOOKS[name])
        else:
            read_payload()
        return
    if name in REMOVED:
        die(REMOVED[name])
    if name not in COMMANDS:
        print(USAGE)
        sys.exit(2)
    if name not in ("setup", "init", "create", "doctor", "help"):
        peek = list(rest)
        project = resolve_project(peek, required=False)
        if project:
            why = A.engine_policy(project, name, rest, config(project))
            if why:
                die(why)
    COMMANDS[name](rest)
