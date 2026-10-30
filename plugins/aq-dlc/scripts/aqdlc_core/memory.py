"""Success / Failure Memory の検索（Markdown の外部記憶）。"""
import os
import re

from .project import config, ensure_home, home, resolve_project
from .util import die, info, now_iso, pop_flag, pop_opt, read_text, split_list

RESULTS = {"helped": "役立った", "not_applicable": "当てはまらなかった", "harmful": "適用して失敗した"}


def memory_roots(project):
    roots = [os.path.join(home(), "memory")]
    if project:
        roots.append(os.path.join(project, ".aq-dlc", "memory"))
    return roots


def all_memories(project):
    out = []
    for root in memory_roots(project):
        for dirpath, _d, files in os.walk(root):
            for name in files:
                if not name.endswith(".md") or name.lower() == "readme.md":
                    continue
                meta = parse_frontmatter(os.path.join(dirpath, name))
                if meta:
                    out.append(meta)
    return out


def parse_frontmatter(path):
    text = read_text(path)
    if text is None:
        return None
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.S)
    if not m:
        return None
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        meta[k.strip()] = v
    meta["_title"] = next((l.lstrip("# ").strip() for l in m.group(2).splitlines()
                           if l.startswith("# ")), "")
    meta["_path"] = path
    return meta


def cmd_memory(args):
    ensure_home()
    project = resolve_project(args, required=False)
    tags = {t.strip().lower() for t in (pop_opt(args, "--tags") or "").split(",") if t.strip()}
    stacks = {t.strip().lower() for t in (pop_opt(args, "--stack") or "").split(",") if t.strip()}
    show_all = pop_flag(args, "--all")
    record = pop_flag(args, "--record")
    limit = int(config(project)["memory"]["max_loaded_per_run"])
    roots = [os.path.join(home(), "memory")]
    if project:
        roots.append(os.path.join(project, ".aq-dlc", "memory"))
    rows, refs = [], []
    for root in roots:
        for dirpath, _d, files in os.walk(root):
            for name in files:
                if not name.endswith(".md") or name.lower() == "readme.md":
                    continue
                meta = parse_frontmatter(os.path.join(dirpath, name))
                if not meta:
                    continue
                status = meta.get("status", "")
                if status == "deprecated" and not show_all:
                    continue
                mtype = meta.get("type", "")
                mtags = {t.lower() for t in meta.get("tags", [])}
                mstack = {t.lower() for t in meta.get("stack", [])} or {"any"}
                if stacks and not (mstack & stacks or "any" in mstack):
                    continue
                score = len(mtags & tags) if tags else 0
                general = mtype in ("workflow-success", "rule", "project") or "workflow" in mtags
                imp = {"high": 2, "medium": 1}.get(meta.get("importance", ""), 0)
                if status == "candidate" and not show_all:
                    if score or general:
                        refs.append(((score, imp), meta))
                    continue
                if tags and score == 0 and not general and not show_all:
                    continue
                rows.append(((mtype == "project", general, status == "active", score, imp), meta))
    # 優先度の高い順。同じ優先度は ID 順（ファイルシステムの順序に依存しない）
    rows.sort(key=lambda r: r[1].get("id", ""))
    rows.sort(key=lambda r: r[0], reverse=True)
    refs.sort(key=lambda r: r[1].get("id", ""))
    refs.sort(key=lambda r: r[0], reverse=True)
    if not show_all:
        rows = rows[:limit]
        refs = refs[:max(0, min(3, limit - len(rows)))]
    print(f"[aq-dlc] 共通Memory: {os.path.join(home(), 'memory')}")
    if not rows and not refs:
        print("該当するMemoryはありません。")
        return

    def show(m):
        print(f"{m.get('id','?'):8} {m.get('status',''):10} {m.get('type',''):17} "
              f"{m.get('_title','')}  [{', '.join(m.get('tags', []))}]\n         {m['_path']}")
    if record and project:
        from .state import bump, load_state, log, save_state
        s, spath = load_state(project)
        if s:
            mem = s.setdefault("memory", {})
            loaded = mem.setdefault("loaded", {})
            for kind, items in (("loaded", rows), ("reference", refs)):
                for _r, m in items:
                    mid = m.get("id", "?")
                    if mid not in loaded:
                        loaded[mid] = {"kind": kind, "status": m.get("status"), "seq": bump(s), "at": now_iso()}
            log(s, project, "memory.load", {"ids": sorted(loaded)})
            save_state(spath, s)
            print("（読み込んだ Memory を記録しました。使った結果は memory-use で記録する）")
    if rows:
        print("■ 読み込むMemory（タグが合うもの＋手順・ルール・プロジェクト情報は常に含む）")
        for _r, m in rows:
            show(m)
    if refs:
        print("■ 未検証（参考）: 使って役立ったら memory Skill の手順で昇格させる")
        for _r, m in refs:
            show(m)


# ------------------------------------------------------------------ 利用結果と整理の記録
def cmd_memory_use(args):
    """memory-use <ID> --result helped|not_applicable|harmful [--note "<内容>"]"""
    from .state import load_state, log, require_state, save_state
    result = pop_opt(args, "--result")
    note = pop_opt(args, "--note") or ""
    project = resolve_project(args)
    s, spath = require_state(project)
    if not args or result not in RESULTS:
        die("使い方: memory-use <ID> --result helped|not_applicable|harmful [--note \"<内容>\"]")
    mid = args[0]
    if mid not in {m.get("id") for m in all_memories(project)}:
        die(f"Memory が見つかりません: {mid}")
    s.setdefault("memory", {}).setdefault("used", {})[mid] = {"result": result, "note": note, "at": now_iso()}
    log(s, project, "memory.use", {"id": mid, "result": result, "note": note})
    save_state(spath, s)
    info(f"Memory {mid} の利用結果を記録しました: {RESULTS[result]}")


def check_memory_report(project, state, saved, merged, skipped, cfg):
    """memory-done の前提を確かめる。戻り値: 問題の一覧"""
    errs = []
    run_id = state["run_id"]
    mems = all_memories(project)
    by_id = {}
    for m in mems:
        by_id.setdefault(m.get("id"), []).append(m)
    dup_ids = sorted(i for i, ms in by_id.items() if i and len(ms) > 1)
    if dup_ids:
        errs.append("Memory の ID が重複している: " + "、".join(dup_ids))
    titles = {}
    for m in mems:
        t = (m.get("_title") or "").strip().lower()
        if t:
            titles.setdefault(t, []).append(m.get("id"))
    for t, ids in titles.items():
        if len(ids) > 1 and any(i in saved for i in ids):
            errs.append(f"同じ題名の Memory がある（統合を検討）: {'、'.join(ids)}")
    if not saved and not merged and not skipped:
        errs.append("保存・統合した Memory の ID（--saved / --merged）か、保存しなかった理由（--skipped）を指定する")
    promote = int(cfg["memory"].get("promote_to_active_min_runs", 2))
    for mid in saved + merged:
        ms = by_id.get(mid)
        if not ms:
            errs.append(f"Memory が見つからない: {mid}")
            continue
        m = ms[0]
        text = read_text(m["_path"]) or ""
        if run_id not in text:
            errs.append(f"{mid} に、この run（{run_id}）の証拠・適用の記録がない（evidence か applied_in に書く）")
        status = m.get("status")
        applied = m.get("applied_in") if isinstance(m.get("applied_in"), list) else []
        if status == "active" and len(applied) < promote:
            errs.append(f"{mid} は active だが、適用の記録が {len(applied)} 回（{promote} 回以上で active）。"
                        "未検証の学びを確定的に扱わない")
        if m.get("type") == "rule" and status == "active" and m.get("approved_by") != "human":
            errs.append(f"{mid} は active なルールだが、人間の承認（approved_by: human）がない")
    mem = state.get("memory", {})
    unused = [i for i, v in mem.get("loaded", {}).items()
              if v.get("kind") == "loaded" and i not in mem.get("used", {})]
    if unused:
        errs.append("読み込んだ Memory の利用結果が未記録（memory-use）: " + "、".join(sorted(unused)))
    return errs


def cmd_memory_done(args):
    """memory-done [--saved ID,..] [--merged ID,..] [--skipped "<保存しなかった理由>"]"""
    from .state import load_state, log, require_state, save_state
    saved = split_list(pop_opt(args, "--saved"))
    merged = split_list(pop_opt(args, "--merged"))
    skipped = pop_opt(args, "--skipped") or ""
    project = resolve_project(args)
    s, spath = require_state(project)
    errs = check_memory_report(project, s, saved, merged, skipped, config(project))
    if errs:
        die("Memory 整理を完了にできません:\n  - " + "\n  - ".join(errs)
            + "\n使い方: memory-done --saved F-0003 --merged W-0001 --skipped \"<保存しなかった候補と理由>\"")
    s.setdefault("memory", {})["report"] = {"saved": saved, "merged": merged, "skipped": skipped, "at": now_iso()}
    s["memory_done"] = True
    log(s, project, "memory-done", s["memory"]["report"])
    save_state(spath, s)
    info(f"Memory 整理を記録しました（保存 {len(saved)} 件・統合 {len(merged)} 件"
         + (f"・見送り: {skipped}" if skipped else "") + "）。")
