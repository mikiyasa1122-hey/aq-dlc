"""Success / Failure Memory の検索（Markdown の外部記憶）。"""
import os
import re

from .project import config, ensure_home, home, resolve_project
from .util import pop_flag, pop_opt, read_text


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
    rows.sort(key=lambda r: r[0], reverse=True)
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
    if rows:
        print("■ 読み込むMemory（タグが合うもの＋手順・ルール・プロジェクト情報は常に含む）")
        for _r, m in rows:
            show(m)
    if refs:
        print("■ 未検証（参考）: 使って役立ったら memory Skill の手順で昇格させる")
        for _r, m in refs:
            show(m)
