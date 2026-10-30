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
from . import approval as AP
from . import gitops, memory
from .project import (cmd_create, cmd_init, cmd_setup, config, engine_cmd, find_project, project_config_path,
                      resolve_project, unregister_active, active_project_for, register_active)
from .state import (ROLE_LABEL, Ctx, StateError, bump, describe_state, ensure_new_id, find, human_event, id_index,
                    iter_verifications, last_human_seq, load_state, log, new_run, next_action, problems,
                    req_by_id, require_state, run_dir, running_agents, save_state, spec_sheet, trace_rows)
from .util import (die, info, load_json, now_iso, pop_flag, pop_opt, read_text, save_json, setup_stdio,
                   sh_run, short, split_list, write_text)

USAGE = """E-AQDL エンジン（Evidence-driven Adaptive Quality Development Lifecycle / 旧 AQ-DLC）

準備:   setup / doctor / init ["<名前>"] / create "<名前>" / publish [--visibility V] [--dry-run] / new "<run名>"
要求:   req <ID> "<要求>" [--critical] [--kind nonfunctional] / accept <要求ID> <ID> "<受け入れ条件>"
        risk <要求ID> <ID> "<リスク>" / verify <受入ID|リスクID> <ID> "<確かめ方>" [--level L] [--manual "<手順>"] [--after-merge]
        question <ID> "<質問>" [--options "A（推奨）|B"] / assume "<内容>" [--out] / drop <ID>
設計:   design-map <要求ID> "<設計要素>" / decision "<判断>" / add-check <名前> "<コマンド>" --kind K [--needs a,b] / remove-check
実装:   bind <検証ID> [--command "..."] [--checks a,b] [--test "..."] [--level L] / impl-map <要求ID> --files a,b
統括:   next / status / spec [--present | --approve "<人間の返答の要約>"] / answer <質問ID> "<回答>"
        ask "<質問>" / resume / wait "<理由>" / confirm <検証ID> "<人間の確認内容>" / escalation [<ID> --resolve "<対応>"]
        gate [--after-merge] / cf [--plan] [--only M1] / cf-add <検証ID> --file F --line N --op OP
        cf-mark <M番号> --equivalent "<理由>" / ci [--wait 分] / complete / abort
Memory: memory-scope none|project|all / memory [--tags a,b] [--record]
        memory-use <ID> --result helped|not_applicable|harmful|unknown / memory-approve <ID>
        memory-share propose <ID> | decide | list
        memory-done --end project|share|none [--saved ID,..] [--merged ID,..] [--skipped "<理由>"]
参照:   evidence [--json] / roles [--role R] [--check] / trace / history [--since N] / show <ID> / checks / check / metrics [--json] / audit [--verify]
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
            s["design"]["map"].pop(node["id"], None)
            s["impl"]["map"].pop(node["id"], None)
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
    state, spath = load_state(project)
    if state and state.get("config_fp_registered") and state["config_fp_registered"] != G.config_fp(config(project), project):
        die("設定がエンジンのコマンド以外で変更されています。先に人間の確認（config-accept）を済ませてください。")
    save_json(path, cfg)
    if state:
        state["config_fp_registered"] = G.config_fp(config(project), project)
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
    state, spath = load_state(project)
    if state and state.get("config_fp_registered") and state["config_fp_registered"] != G.config_fp(config(project), project):
        die("設定がエンジンのコマンド以外で変更されています。先に人間の確認（config-accept）を済ませてください。")
    save_json(path, cfg)
    if state and old:
        state["config_fp_registered"] = G.config_fp(config(project), project)
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
        t = AP.target_id("SPEC", ctx.spec_hash)
        print(f"\n（仕様 {t} を提示として記録。人間に「この仕様で進める（承認 {t}）」/「修正したい」を選んでもらい、"
              f"承認の返答を得たら spec --approve \"<返答の要約>\"）")
        return
    if sp.get("presented_hash") != ctx.spec_hash:
        die("この版の仕様はまだ人間に提示されていません。spec --present で提示してから確認してください。")
    if last_human_seq(s) <= sp.get("presented_seq", 0):
        die("提示の後に人間の返答が記録されていません。AskUserQuestion などで人間に確認し、返答を得てから承認してください。")
    if not approve.strip():
        die("--approve には人間の返答の要約を書いてください。")
    target = AP.target_id("SPEC", ctx.spec_hash)
    decision, ev, _f = AP.find_decision(s, target, sp.get("presented_seq", 0))
    if decision is None:
        die(f"提示の後の人間の返答に、この仕様（{target}）への明示的な承認がありません。"
            f"「承認 {target}」を選択肢に含めて確認し直してください（「OK」などの対象のない返答は承認にしません）。")
    if decision == "reject":
        AP.record(s, target, "reject", ev, ctx.rd, approve, "spec")
        save_state(spath, s)
        die(f"人間は仕様（{target}）を却下しました。修正の要望を aq-dlc:requirements に依頼してください。")
    AP.record(s, target, "approve", ev, ctx.rd, approve, "spec")
    sp.update({"approved_hash": ctx.spec_hash, "approved_seq": bump(s), "approved_at": now_iso(), "note": approve,
               "approval_target": target})
    s["awaiting_human"] = False
    log(s, project, "spec.approve", {"hash": ctx.spec_hash, "target": target, "note": approve})
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
        if not " ".join(a).strip():
            die('使い方: ask "<人間に確認すること>"（質問の内容を必ず書く）')
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
        decision, ev, _f = AP.find_decision(s, a[0], s["asked_seq"])
        if decision != "approve":
            die(f"ask の後の人間の返答に、{a[0]} への明示的な確認（「承認 {a[0]}」）がありません"
                + ("（却下の返答があります）" if decision == "reject" else "") + "。")
        AP.record(s, a[0], "approve", ev, run_dir(project, s.get("run_id")), " ".join(a[1:]), "confirm")
        v.update(confirmed_by="human", confirm_note=" ".join(a[1:]), confirmed_seq=bump(s))
        log(s, project, "confirm", {"id": a[0], "note": v["confirm_note"]})
        info(f"人間の確認として記録しました: {a[0]}")
    mutate(args, fn)


def cmd_config_accept(args):
    """config-accept "<人間の返答の要約>" — エンジン以外で変わった設定を、人間が確認したうえで受け入れる"""
    def fn(project, s, a):
        note = " ".join(a).strip()
        if not note:
            die('使い方: config-accept "<人間の返答の要約>"（ask で変更内容を人間に確認した後に実行する）')
        if not s.get("asked_seq") or last_human_seq(s) <= s["asked_seq"]:
            die("ask の後に人間の返答が記録されていません。設定の変更内容を ask で人間に確認してから実行してください。")
        new = G.config_fp(config(project), project)
        target = AP.target_id("CFG", new)
        decision, ev, _f = AP.find_decision(s, target, s["asked_seq"])
        if decision != "approve":
            die(f"ask の後の人間の返答に、この設定（{target}）への明示的な承認がありません"
                + ("（却下の返答があります）" if decision == "reject" else "")
                + f"。「承認 {target}」を選択肢に含めて確認してください。")
        AP.record(s, target, "approve", ev, run_dir(project, s.get("run_id")), note, "config")
        log(s, project, "config.accept", {"before": s.get("config_fp_registered"), "after": new, "note": note})
        s["config_fp_registered"] = new
        info("設定の変更を、人間の確認済みとして受け入れました（gate と独立レビューはやり直しになります）。")
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
                                                              "questions_open", "spec_unregistered", "memory_scope",
                                                              "state_invalid")]
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
        before = G.snapshot(project, cfg, fresh=True)
        checks_fp_before = G.checks_fp(cfg)
        config_fp_before = G.config_fp(cfg, project)
        vfp_before = {v["id"]: G.verification_fp(v, cfg) for _r, _p, v in iter_verifications(s)}
        t0 = time.time()
        results, ok = G.run_gate(project, s, cfg, iter_verifications, os.path.join(ctx.rd, "gate"), stamp,
                                 after_merge)
        dur = round(time.time() - t0, 1)
        cfg_after = config(project)
        after = G.snapshot(project, cfg, fresh=True)
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
    if G.config_fp(cfg_after, project) != config_fp_before:
        changed_defs.append("設定")
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
                 "checks_fp": checks_fp_before, "config_fp": config_fp_before, "graph_fp": ctx.graph_fp, "checks_pass": ok, "results": results,
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


# ------------------------------------------------------------------ Counterfactual Gate
def cmd_cf(args):
    """cf [--plan] [--only M1,M2] — 反証テスト（隔離したコピーに欠陥を入れ、テストが失敗するかを確かめる）"""
    from . import counterfactual as CF
    show_plan = pop_flag(args, "--plan")
    only = split_list(pop_opt(args, "--only"))
    project, s, spath = open_state(args)
    cfg = config(project)
    ctx = Ctx(project, s, cfg, fresh=True)
    cc = CF.cf_config(cfg)
    muts, notes = CF.plan(project, s, cfg, source=list(ctx.snap))
    if show_plan:
        print(f"反証テストの計画（mode: {cc['mode']}、上限 {cc['max_mutations']} 件、検証ごと {cc['per_verification']} 件）")
        for m in muts:
            print(f"  {m['id']} {m['verification']}（{m.get('risk') or m['parent']}）{m['file']}:{m['line']} "
                  f"{m['category']}\n      - {m['original'].strip()}\n      + {m['mutated'].strip()}")
        for n in notes:
            print(f"  注意: {n}")
        if not muts:
            print("  対象なし（自動の検証・impl-map の実装ファイル（Python / JS / TS）・適用できる箇所が必要）")
        return
    if cc["mode"] == "off":
        die("counterfactual.mode が off です（反証テストは無効）。使う場合は人間の確認のうえで .aq-dlc/config.json の "
            "counterfactual.mode を advisory か enforce にしてください（計画の確認だけなら cf --plan）。")
    if s.get("status") not in ("in_progress", "pr_ready"):
        die(f"この run は {s.get('status')} です。")
    from .state import actor_hint, gate_status
    actor = actor_hint(project)
    run = running_agents(s)
    if run and not (actor == "implementer" and all(v["role"] == "implementer" for v in run.values())):
        die("サブエージェントの実行中は反証テストを実行しません。完了を待ってください。")
    fresh, passing = gate_status(ctx)
    if not (fresh and passing):
        die("最新のコードで通常の Quality Gate に合格していません。反証テストは gate の合格後に実行します。")
    lock = os.path.join(ctx.rd, "cf.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3600:
        die("別の反証テストが実行中です（前回が異常終了した場合は数分後に再実行するか、cf.lock を人が削除する）。")
    write_text(lock, str(os.getpid()))
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        info(f"反証テスト {stamp}（{len(muts)} 件、mode: {cc['mode']}）")
        rec = CF.execute(project, s, cfg, muts, os.path.join(ctx.rd, "cf"), stamp, set(only) if only else None)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    s, spath = load_state(project)
    ctx2 = Ctx(project, s, cfg)
    rec["notes"] = notes + rec.get("notes", [])
    rec.update({"seq": bump(s), "checks_fp": ctx2.checks_fp, "graph_fp": ctx2.graph_fp,
                "def_fp": CF.definition_fp(s, cfg, muts), "env_fp": CF.env_fp(cfg), "partial": bool(only)})
    if only:
        rec["status"] = "partial" if rec["status"] == "done" else rec["status"]
        rec["notes"].append("一部だけ実行した（--only）。完了判定には全件の実行が必要")
    eqs = (s.get("counterfactual") or {}).get("equivalent", {})
    for m in rec["mutations"]:
        if CF.mutation_key(m) in eqs:
            m["equivalent"] = eqs[CF.mutation_key(m)]
    rec["summary"] = CF.summarize(rec)
    rec["fp"] = CF.fp_now(ctx2, muts)
    cfst = s.setdefault("counterfactual", {})
    cfst["latest"] = rec
    cfst.setdefault("runs", []).append({k: rec[k] for k in ("stamp", "at", "status", "summary", "mode")})
    cfst["runs"] = cfst["runs"][-20:]
    log(s, project, "counterfactual", {"stamp": stamp, "status": rec["status"], "summary": rec["summary"]})
    save_state(spath, s)
    c = rec["summary"]["counts"]
    print(f"結果: killed {c['killed']} / survived {c['survived']} / invalid {c['invalid']} / timeout {c['timeout']} / "
          f"対象外 {c['not_applicable']}（検出率 = killed ÷（killed＋survived）= "
          f"{rec['summary']['score'] if rec['summary']['score'] is not None else '算出不可'}、"
          f"母集団 {rec['summary']['valid']} 件、除外 {rec['summary']['excluded']} 件）")
    for n in rec["notes"]:
        print(f"  注意: {n}")
    if rec["status"] != "done":
        print(f"  ! この結果は使えません（{rec['status']}）")
    print("  通常の gate の合格とは別の結果です。survived は「テストが欠陥を見逃した」か「等価な変異」のどちらかです。")


def cmd_cf_add(args):
    """cf-add <検証ID> --file <パス> --line <行> --op <operator> — 反証テストの対象を追加する"""
    from . import mutation as MU
    rel, line, op = pop_opt(args, "--file"), pop_opt(args, "--line"), pop_opt(args, "--op")

    def fn(project, s, a):
        if not a or not rel or not line or not op:
            die(f"使い方: cf-add <検証ID> --file <パス> --line <行> --op <{'|'.join(MU.OPERATORS)}>")
        _k, v, _p = find(s, a[0], ("verification",))
        if op not in MU.OPERATORS:
            die(f"--op は {' / '.join(MU.OPERATORS)} のどれか")
        r = rel.replace("\\", "/")
        if os.path.isabs(r) or ".." in r.split("/"):
            die("--file はプロジェクト内の相対パスで指定してください")
        path = os.path.join(project, r)
        if os.path.islink(path) or not os.path.isfile(path):
            die(f"ファイルがない、またはシンボリックリンクです: {r}")
        if not MU.language(r):
            die("対応している言語は Python と JavaScript / TypeScript です")
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.read().splitlines(keepends=True)
        n = int(line)
        if MU.apply(op, lines, n - 1, MU.language(r)) is None:
            die(f"{r}:{n} に {op} を適用できません（形式的な適用の検査で不可）")
        cfst = s.setdefault("counterfactual", {})
        tid = f"T{len(cfst.get('targets', [])) + 1}"
        cfst.setdefault("targets", []).append({"id": tid, "verification": a[0], "file": r, "line": n, "operator": op})
        log(s, project, "cf.add", {"id": tid, "verification": a[0], "file": r, "line": n, "operator": op})
        info(f"反証テストの対象 {tid} を追加しました: {a[0]} {r}:{n} {op}")
    mutate(args, fn)


def cmd_cf_mark(args):
    """cf-mark <M番号> --equivalent "<理由>" — survived の変異を等価として除外する（人間の承認が必要）"""
    from . import counterfactual as CF
    reason = pop_opt(args, "--equivalent")

    def fn(project, s, a):
        if not a or not reason:
            die('使い方: cf-mark <M番号> --equivalent "<等価と判断した理由>"（ask で人間の承認を得てから）')
        rec = CF.current(s)
        m = next((x for x in (rec or {}).get("mutations", []) if x["id"] == a[0]), None)
        if not m:
            die(f"最新の反証テストに {a[0]} がありません。")
        if m.get("result") != "survived":
            die(f"{a[0]} は survived ではありません（{m.get('result')}）。")
        target = f"EQ-{rec['stamp']}-{a[0]}"
        if not s.get("asked_seq"):
            die(f"ask で人間に確認してください（選択肢「等価として除外する（承認 {target}）」）。")
        decision, ev, _f = AP.find_decision(s, target, s["asked_seq"])
        if decision != "approve":
            die(f"ask の後の人間の返答に「承認 {target}」がありません。等価かどうかは人間が判断します。")
        AP.record(s, target, "approve", ev, run_dir(project, s.get("run_id")), reason, "equivalent")
        info_ = {"reason": reason, "approval": target, "at": now_iso()}
        s["counterfactual"].setdefault("equivalent", {})[CF.mutation_key(m)] = info_
        m["equivalent"] = info_
        rec["summary"] = CF.summarize(rec)
        log(s, project, "cf.equivalent", {"id": a[0], "reason": reason})
        info(f"{a[0]} を等価な変異として除外しました（検出率の母集団から外し、除外件数に数えます）。")
    mutate(args, fn)


# ------------------------------------------------------------------ Role Profile / Evidence
def cmd_roles(args):
    """roles [--role R] [--check] [--json] — 適用される Role Profile と理由"""
    from . import roles as RL
    check = pop_flag(args, "--check")
    as_json = pop_flag(args, "--json")
    role = pop_opt(args, "--role")
    project = resolve_project(args, required=False)
    cfg = config(project)
    profiles, rejected = RL.load_profiles(cfg)
    if check:
        print(f"Role Profile: {len(profiles)} 件（{RL.roles_dir()}）")
        for p in sorted(profiles.values(), key=lambda x: (x["kind"], x["id"])):
            print(f"  OK {p['kind']:6} {p['id']} v{p['version']} {p['hash'][:12]}  {p['title']}")
        for r in rejected:
            print(f"  NG {os.path.basename(r['path'])}: " + " / ".join(r["errors"]))
        if rejected:
            sys.exit(1)
        return
    s, _ = load_state(project) if project else (None, None)
    if not s:
        die("実行中の run がありません（Profile の検査だけなら roles --check）。")
    ctx = Ctx(project, s, cfg)
    out = {}
    for r in ([role] if role else ["requirements", "design", "implementer", "reviewer"]):
        kind, _items = A.pending_fix(ctx) if r == "implementer" else (None, [])
        out[r] = RL.public(A.role_selection(r, ctx, kind, (s["agents"].get("prompts") or {}).get(r, "")
                                            if r == "requirements" else ""))
    if as_json:
        print(json.dumps({"current": out, "history": (s.get("roles") or {}).get("history", [])},
                         ensure_ascii=False, indent=2))
        return
    for r, sel in out.items():
        print(f"■ {ROLE_LABEL.get(r, r)}")
        for x in sel["stage"]:
            print(f"  工程: {x['id']} v{x['version']}（{x['title']}）")
        for x in sel["domain"]:
            print(f"  追加: {x['id']} v{x['version']} 点数 {x['score']} — {' / '.join(x['reasons'])}"
                  + (f"（関係: {', '.join(x['related'][:6])}）" if x["related"] else ""))
        for x in sel["dropped"]:
            print(f"  見送り: {x['id']} — {x['why']}")
        if sel.get("rejected"):
            print("  ! 不正な Profile（使わない）: " + "、".join(x["path"] for x in sel["rejected"]))
    hist = (s.get("roles") or {}).get("history", [])
    if hist:
        print("■ 適用の履歴")
        for h in hist[-10:]:
            print(f"  #{h.get('seq')} {h.get('agent')}（{h.get('trigger')}）: "
                  + ", ".join([x["id"] for x in h.get("stage", [])] + [x["id"] for x in h.get("domain", [])]))


def cmd_evidence(args):
    """evidence [--json] — 検証ごとの証拠の状態（Negative Evidence Graph）"""
    from . import evidence as EV
    as_json = pop_flag(args, "--json")
    project, s, _ = open_state(args)
    ctx = Ctx(project, s)
    rows, nodes, counts = EV.summary(ctx)
    if as_json:
        print(json.dumps({"verifications": rows, "nodes": nodes, "counts": counts}, ensure_ascii=False, indent=2))
        return
    print("受け入れ条件・リスクごとの状態: " + "、".join(f"{EV.LABEL[k]} {v}" for k, v in counts.items()))
    print("| 要求 | 受入/リスク | 検証 | 種類 | 通常Gate | 反証テスト | レビュー | 状態 | 根拠 | 失効の原因 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for x in rows:
        print("| " + " | ".join(str(c).replace("|", "/") for c in [
            x["requirement"] + ("【重要】" if x["critical"] else ""), x["parent"], x["verification"], x["kind"],
            x["gate"], x["counterfactual"], x["review"], f"{x['state']}（{EV.LABEL[x['state']]}）",
            "；".join(x["basis"]), "；".join(x["causes"])]) + " |")
    missing = [nid for nid, n in nodes.items() if not n["verifications"]]
    if missing:
        print("検証のない受け入れ条件・リスク: " + "、".join(missing))


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
    ctx = Ctx(project, s, fresh=True)
    probs = problems(ctx, "pr")
    if probs:
        print("[E-AQDL] まだ PR を出せる状態ではありません:\n  - " + "\n  - ".join(p["msg"] for p in probs))
        sys.exit(1)
    final = problems(ctx, "final")
    if final:
        s["status"] = "pr_ready"
        save_state(spath, s)
        print("[E-AQDL] PR を出せる状態です（status: pr_ready）。マージ後の確認が残っています:\n  - "
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
    probs = problems(Ctx(project, s, fresh=True), stage)
    if probs:
        print("[E-AQDL] 完了条件を満たしていません:\n  - " + "\n  - ".join(p["msg"] for p in probs))
        sys.exit(1)
    info("完了条件 OK")


def cmd_trace(args):
    project, s, _ = open_state(args)
    from . import evidence as EV
    ctx = Ctx(project, s)
    rows = trace_rows(ctx)
    ev = {x["verification"]: x for x in EV.compute(ctx)}
    head = ["要求", "受入/リスク", "設計", "実装ファイル", "検証", "テスト", "結果", "gate", "反証テスト", "証拠の状態",
            "失効の原因"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        vid = str(r[4]).split("（")[0]
        x = ev.get(vid)
        extra = [x["counterfactual"], f"{x['state']}（{EV.LABEL[x['state']]}）", "；".join(x["causes"])] if x else \
            ["", "insufficient（証拠不足）", ""]
        print("| " + " | ".join(str(c).replace("|", "/") for c in list(r) + extra) + " |")


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
        "role_profiles": _role_metrics(s),
        "counterfactual": _cf_metrics(s),
        "memory": _memory_metrics(s),
        "approvals": {"approve": sum(1 for a in s.get("approvals", []) if a.get("decision") == "approve"),
                      "reject": sum(1 for a in s.get("approvals", []) if a.get("decision") == "reject")},
        "conditions": {"memory_scope": (s.get("memory_scope") or {}).get("choice"),
                       "counterfactual_mode": (((s.get("counterfactual") or {}).get("latest") or {}).get("mode")),
                       "roles_enabled": bool((s.get("roles") or {}).get("history")),
                       "engine": "E-AQDL" if s.get("eaqdl") else "AQ-DLC v0.5"},
    }


def _role_metrics(s):
    hist = (s.get("roles") or {}).get("history", [])
    domains = {}
    for h in hist:
        for d in h.get("domain", []):
            domains[d["id"]] = domains.get(d["id"], 0) + 1
    return {"applications": len(hist), "by_trigger": {t: sum(1 for h in hist if h.get("trigger") == t)
                                                      for t in ("start", "transition", "sendback")},
            "domains": domains}


def _cf_metrics(s):
    from . import counterfactual as CF
    cf = s.get("counterfactual") or {}
    rec = cf.get("latest")
    out = {"runs": len(cf.get("runs", [])), "latest": None,
           "score_definition": "killed ÷（killed＋survived）。invalid・timeout・対象外・等価は母集団から除外し、件数を別に示す"}
    if rec:
        sm = CF.summarize(rec)
        out["latest"] = dict(sm, stamp=rec.get("stamp"), status=rec.get("status"))
    return out


def _memory_metrics(s):
    mem = s.get("memory") or {}
    used = mem.get("used", {})
    res = {}
    for v in used.values():
        res[v.get("result")] = res.get(v.get("result"), 0) + 1
    share = mem.get("share", {})
    return {"scope": (s.get("memory_scope") or {}).get("choice"), "loaded": len(mem.get("loaded", {})),
            "results": res, "shared": sum(1 for v in share.values() if v.get("status") == "shared"),
            "share_rejected": sum(1 for v in share.values() if v.get("status") == "rejected"),
            "end": (mem.get("report") or {}).get("end")}


def cmd_metrics(args):
    as_json = pop_flag(args, "--json")
    all_runs = pop_flag(args, "--runs")
    if all_runs:
        return _metrics_runs(args, as_json)
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
    rp = m["role_profiles"]
    print(f"Role Profile の適用: {rp['applications']} 回（開始 {rp['by_trigger']['start']}・遷移 {rp['by_trigger']['transition']}"
          f"・差し戻し {rp['by_trigger']['sendback']}）追加の専門性: "
          + ("、".join(f"{k} {v}" for k, v in rp["domains"].items()) or "なし"))
    cf = m["counterfactual"]
    if cf["latest"]:
        c = cf["latest"]["counts"]
        print(f"反証テスト: {cf['runs']} 回。最新 killed {c['killed']}・survived {c['survived']}・invalid {c['invalid']}・"
              f"timeout {c['timeout']}・対象外 {c['not_applicable']}・等価 {cf['latest']['equivalent']}、"
              f"検出率 {cf['latest']['score']}（{cf['score_definition']}）")
    else:
        print("反証テスト: 未実行")
    mm = m["memory"]
    print(f"Memory: 利用範囲 {mm['scope']}・読み込み {mm['loaded']} 件・結果 {mm['results'] or 'なし'}・"
          f"共有 {mm['shared']} 件（却下 {mm['share_rejected']}）・終了時 {mm['end']}")


def cmd_audit(args):
    """audit [--verify] [--limit N] — 承認・同意・共有の決定、hook の異常、状態の保存拒否の監査ログ"""
    verify = pop_flag(args, "--verify")
    limit = int(pop_opt(args, "--limit") or 30)
    from .project import home
    project = resolve_project(args, required=False)
    paths = []
    if project:
        s, _ = load_state(project)
        rd = run_dir(project, s.get("run_id")) if s else None
        if rd:
            paths.append(("この run", os.path.join(rd, "audit.jsonl")))
    paths.append(("全体（hook の異常・共通 Memory の共有）", os.path.join(home(), "audit.jsonl")))
    paths.append(("共通 Memory", os.path.join(home(), "memory", "audit.jsonl")))
    bad = False
    for label, path in paths:
        n, errs = AP.audit_verify(path)
        print(f"■ {label}: {path}（{n} 行）")
        if verify:
            if errs:
                bad = True
                print("  ! 連鎖の不一致: " + " / ".join(errs[:5]))
            else:
                print("  連鎖の確認: 不一致なし（ファイルごと作り直す改ざんは検出できない）")
        for rec in AP.audit_read(path, limit):
            detail = {k: v for k, v in rec.items() if k not in ("at", "event")}
            print(f"  {rec.get('at')} {rec.get('event')} {json.dumps(detail, ensure_ascii=False)[:300]}")
    if bad:
        sys.exit(1)


def _metrics_runs(args, as_json):
    """metrics --runs — このプロジェクトの run を条件ごとに並べる（比較実験の材料。統計的な結論は出さない）"""
    from .state import runs_dir, migrate
    project = resolve_project(args)
    rows = []
    d = runs_dir(project)
    for rid in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not os.path.isfile(os.path.join(d, rid, "state.json")):
            continue
        st = load_json(os.path.join(d, rid, "state.json"), None)
        if not isinstance(st, dict):
            continue
        st = migrate(st)
        m = st.get("metrics") or collect_metrics(st)
        rows.append({"run": rid, "status": st.get("status"), **m["conditions"], "gate_failed": m["gate"]["failed"],
                     "fix_iterations": m["fix_iterations"], "reviews_needs_fix": m["reviews"]["needs_fix"],
                     "cf_score": ((m["counterfactual"].get("latest") or {}).get("score")),
                     "human_asks": m["human"]["asks"], "run_sec": m["run_sec"]})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print("| run | 状態 | エンジン | Memory | 反証 | gate 不合格 | 修正ループ | レビュー要修正 | 検出率 | 確認依頼 | 秒 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['run']} | {r['status']} | {r['engine']} | {r['memory_scope']} | {r['counterfactual_mode']} | "
              f"{r['gate_failed']} | {r['fix_iterations']} | {r['reviews_needs_fix']} | {r['cf_score']} | "
              f"{r['human_asks']} | {r['run_sec']} |")
    print(f"run 数 {len(rows)}。課題・受け入れ条件・評価基準・モデルが揃っていない run どうしの差は、改善の根拠にならない。"
          "少ない試行の差を統計的な改善と断定しない。")


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
    """hook の入力。読めなければ None（安全側の判断に使う）。"""
    from .util import decode
    try:
        data = json.loads(decode(sys.stdin.buffer.read()) or "{}")
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


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
    # 実装担当以外の Bash は、実行前のコードと設定の状態をプロジェクトの外に記録し、実行後に比べる。
    # run がない間も「記録した」こと自体は残す（実行後に記録が消えていたら改ざんとして扱うため）。
    if role != "implementer":
        sdir = A.bash_snapshot_dir(project)
        if os.path.isdir(sdir):  # 対応する PostToolUse が来なかった古い記録を掃除する
            for f in os.listdir(sdir):
                fp = os.path.join(sdir, f)
                try:
                    if time.time() - os.path.getmtime(fp) > 3600:
                        os.remove(fp)
                except OSError:
                    pass
        active = bool(state and state.get("status") in ("in_progress", "pr_ready"))
        prev = (state.get("gate") or {}).get("snapshot") if state else None
        save_json(A.bash_snapshot_path(project, payload.get("tool_use_id")),
                  {"role": role, "cmd": cmd[:2000], "active": active,
                   "snapshot": G.snapshot(project, cfg, prev) if active else None,
                   "ignore_dirs": list(cfg["gate"]["ignore_dirs"]), "config_fp": G.config_fp(cfg, project)})
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
    """実装担当以外の Bash がコード・テスト・設定を変えていないか。

    リダイレクト・スクリプト・変数展開などの書き方に関係なく、実行前後のファイル内容で判定する。
    """
    role = A.role_of(payload.get("agent_type"))
    if role == "implementer":
        return None
    cwd, project, state, spath = hook_context(payload)
    if not project:
        return None
    snap_path = A.bash_snapshot_path(project, payload.get("tool_use_id"))
    pre = load_json(snap_path, None)
    active = bool(state and state.get("status") in ("in_progress", "pr_ready"))
    cfg = config(project)
    if pre is None:
        if not active:
            return None
        v = {"role": role, "at": now_iso(), "cmd": str((payload.get("tool_input") or {}).get("command", ""))[:500],
             "files": [], "count": 0, "kind": "tamper",
             "note": "実行前の記録が見つからない（記録の削除・改ざんの可能性）"}
    else:
        notes = []
        now_fp = G.config_fp(cfg, project)
        if pre.get("config_fp") and pre["config_fp"] != now_fp and not (state and state.get("config_fp_registered") == now_fp):
            # エンジンのコマンド（add-check など）で登録された変更は除く
            notes.append("設定（config.json または .claude の設定・定義）が変更された")
        files = []
        if pre.get("snapshot") is not None:
            # 実行前と同じ対象で比べる（設定で対象を外す改ざんをしても、ファイルの変更は見える）
            same = dict(cfg, gate=dict(cfg["gate"], ignore_dirs=pre.get("ignore_dirs", cfg["gate"]["ignore_dirs"])))
            # 変更の比較は ctime・inode 付きのキャッシュで十分（utime で ctime は戻せない）。
            # 全ファイルのハッシュのし直しは、判定と登録の時点で行う（hook の時間制限を超えないため）
            # Windows の ctime は作成時刻で書き換えを表さないため、Windows では全ファイルをハッシュし直す
            after = G.snapshot(project, same, pre["snapshot"], fresh=(os.name == "nt"))
            c, a, r = G.snapshot_diff(pre["snapshot"], after)
            files = c + a + r
        if (not files and not notes) or (not active and not notes):
            try:
                os.remove(snap_path)
            except OSError:
                pass
            return None
        v = A.detect_bash_changes(role, pre.get("cmd", ""), {}, {}, project) or \
            {"role": role, "at": now_iso(), "cmd": pre.get("cmd", "")[:500], "kind": "write"}
        v.update(files=files[:50], count=len(files))
        if A.GIT_SYNC.search(pre.get("cmd", "")):
            v["kind"] = "git"
        if notes and not files:
            v["kind"] = "config"
        if notes:
            v["note"] = "、".join(notes)
    try:
        os.remove(snap_path)
    except OSError:
        pass
    state, spath = load_state(project)
    if not state:
        return None
    v["seq"] = bump(state)
    if v["kind"] == "config":
        v["resolved"] = {"by": "config_changed で人間が確認する"}  # 設定の変更は config_changed（人間の確認）で扱う
    state.setdefault("violations", []).append(v)
    save_state(spath, state)
    who = ROLE_LABEL.get(role, "統括")
    detail = f"{v['count']} 件: {', '.join(v['files'][:5])}" if v.get("files") else v.get("note", "")
    reason = (f"E-AQDL: 担当外の変更を検出しました（{who} の Bash: {detail}）。"
              "run の実行中にコードやテストを変えられるのは実装担当だけで、設定はエンジンのコマンドでだけ変えます。"
              "この変更は記録され、実装担当の確認・取り込み（設定なら人間の確認）と gate・独立レビューのやり直しが"
              "済むまで、レビュー・完了に進めません。")
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
            return A.deny("E-AQDL プロジェクトではありません。init で初期化し、new で run を開始してください。")
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
        text, parsed = AP.parse_ask_response(payload.get("tool_response"))
        human_event(state, "answer", text, parsed)
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
    ctx = Ctx(project, state, cfg, fresh=state.get("status") == "complete")
    total_cap = int(cfg["automation"].get("max_total_continues", 200))
    if int(state.get("stop_blocks_total", 0)) >= total_cap and state.get("status") != "complete":
        print(f"[E-AQDL] run 全体での自動継続の上限（{total_cap}回）に達したため停止を許可しました。"
              "状況を確認して人間が判断してください。", file=sys.stderr)
        return None
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
        print(f"[E-AQDL] 自動継続の上限（{max_blocks}回）に達したため停止を許可しました。", file=sys.stderr)
        return None
    phase, nxt, _probs = next_action(ctx)
    state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    state["stop_blocks_total"] = int(state.get("stop_blocks_total", 0)) + 1
    save_state(spath, state)
    reason = (f"E-AQDL: run が進行中です（{project} / フェーズ: {phase}）。\n次: {nxt}\n"
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
        lines = [f"このフォルダは E-AQDL プロジェクトです（{project}）。",
                 "あなたは E-AQDL の統括（Main Orchestrator）です。開発・修正・機能追加の依頼は、コマンドがなくても "
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
                 "このフォルダを E-AQDL プロジェクトとして初期化して開発してください。",
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
# 許可してよいと確定できないときは拒否する hook（PreToolUse）
FAIL_CLOSED = {"pre-bash", "pre-write", "pre-agent"}


def _record_hook_error(payload, err):
    """検出の処理で例外が起きたら、見逃さないように「確認が必要な変更」として記録する（安全側）。"""
    try:
        cwd = payload.get("cwd") or os.getcwd()
        project = active_project_for(cwd)
        state, spath = load_state(project) if project else (None, None)
        if state and state.get("status") in ("in_progress", "pr_ready"):
            state.setdefault("violations", []).append({
                "role": A.role_of(payload.get("agent_type")), "at": now_iso(), "seq": bump(state), "kind": "error",
                "cmd": str((payload.get("tool_input") or {}).get("command", ""))[:500], "files": [], "count": 0,
                "note": f"変更の検出処理でエラー: {err}"})
            save_state(spath, state)
    except Exception:  # noqa: BLE001
        pass
    print(f"[E-AQDL] 変更の検出でエラーが発生したため、確認が必要な変更として記録しました: {err}", file=sys.stderr)


def _hook_project(payload):
    cwd = (payload or {}).get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return active_project_for(cwd) or find_project(cwd)
    except Exception:  # noqa: BLE001
        return None


def _audit_hook(name, payload, why):
    from .project import home
    AP.audit_append(os.path.join(home(), "audit.jsonl"), "hook.error", hook=name, reason=str(why)[:300],
                    cwd=str((payload or {}).get("cwd", ""))[:300], tool=str((payload or {}).get("tool_name", "")))


def run_hook(name):
    payload = read_payload()
    fn = HOOKS.get(name)
    if not fn:
        return
    if payload is None:
        # 入力を読めない。重要な PreToolUse は、E-AQDL のプロジェクトの中なら許可しない
        _audit_hook(name, None, "hook の入力（JSON）を読めない")
        if name in FAIL_CLOSED and _hook_project({}):
            emit(A.deny("hook の入力を読めなかったため、安全のため止めました。もう一度実行してください。"))
        return
    if name != "pre-bash":
        cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        if A.forged_hook(active_project_for(cwd), name):
            print("[E-AQDL] Bash から hook を直接呼ぶことはできません。", file=sys.stderr)
            return
    try:
        emit(fn(payload))
    except SystemExit as e:
        # 状態・設定を読めない（die）など。重要な PreToolUse は許可せずに止める
        if name in FAIL_CLOSED and e.code not in (0, None) and _hook_project(payload):
            _audit_hook(name, payload, f"処理が中断された（終了コード {e.code}）")
            emit(A.deny("状態・設定を読めないため、許可してよいか確定できず止めました。"
                        "state.json・config.json が壊れていないか人間に確認してください。"))
            return
        raise
    except Exception as e:  # noqa: BLE001
        _audit_hook(name, payload, e)
        if name == "post-bash":
            _record_hook_error(payload, e)
            return
        if name in FAIL_CLOSED:
            cmd = (payload.get("tool_input") or {}).get("command", "") or ""
            if _hook_project(payload) or gitops.dangerous_words(cmd):
                emit(A.deny(f"判定の処理でエラーが発生し、許可してよいか確定できないため止めました（{e}）。"
                            "状況を人間に報告してください（監査ログ: ~/.aq-dlc/audit.jsonl）。"))
                return
        print(f"[E-AQDL] hook（{name}）でエラーが発生しました（作業は継続します）: {e}", file=sys.stderr)


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
    "memory": memory.cmd_memory, "memory-done": memory.cmd_memory_done, "memory-use": memory.cmd_memory_use,
    "memory-scope": memory.cmd_memory_scope, "memory-approve": memory.cmd_memory_approve,
    "memory-share": memory.cmd_memory_share, "complete": cmd_complete, "abort": cmd_abort,
    "next": cmd_next, "status": cmd_status, "check": cmd_check, "trace": cmd_trace, "history": cmd_history,
    "show": cmd_show, "metrics": cmd_metrics, "config-accept": cmd_config_accept, "audit": cmd_audit,
    "cf": cmd_cf, "cf-add": cmd_cf_add, "cf-mark": cmd_cf_mark, "roles": cmd_roles, "evidence": cmd_evidence,
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
    try:
        COMMANDS[name](rest)
    except StateError as e:
        die(str(e))
