#!/usr/bin/env python3
"""AQ-DLC engine (Claude Code plugin).

  PL = python3 "<plugin>/scripts/aqdlc.py"

  $PL setup                                  ~/.aq-dlc を初期化（何度実行してもよい）
  $PL init ["<app名>"] [--project DIR]       開いているフォルダをプロジェクトとして初期化
  $PL create "<app名>" [--parent DIR]         サブフォルダを新規作成して初期化（指示があるときだけ）
  $PL publish [--visibility private|public]  git init → GitHubリポジトリ作成 → push
  $PL new "<run名>"        --project DIR      runを開始
  $PL status               --project DIR      状態と未充足条件
  $PL gate                 --project DIR      Quality Gate を実行して記録
  $PL check                --project DIR      完了判定（Stop hook と同じ）
  $PL memory [--tags a,b] [--stack x] [--all] --project DIR
  $PL stop-hook / guard-hook                  Claude Code の hook から呼ばれる

--project を省略すると、カレントディレクトリから上に .aq-dlc/project.json を探す。
Python 3.8+ 標準ライブラリのみ。
"""
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(PLUGIN_ROOT, "templates")
HOME = os.path.abspath(os.path.expanduser(os.environ.get("AQDLC_HOME", "~/.aq-dlc")))
ACTIVE_PATH = os.path.join(HOME, "active.json")

DEFAULTS = {
    "workspace_dir": "",
    "github": {"default_visibility": "private", "default_branch": "main"},
    "checks": [],
    "gate": {
        "default_timeout_sec": 600,
        "ignore_dirs": [".git", ".aq-dlc", ".claude", "node_modules", ".venv", "venv",
                        "__pycache__", "dist", "build", "target", ".next", ".turbo",
                        ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".gradle"],
    },
    "automation": {
        "auto_start": True,
        "max_auto_continues": 20,
        "max_fix_iterations": 8,
        "allow_github_ops": True,
        "block_force_push": True,
        "block_push_to_default_branch": True,
        "block_pr_merge": True,
        "block_destructive_gh": True,
    },
    "memory": {
        "max_loaded_per_run": 10,
        "auto_save_min_score": 3,
        "promote_to_active_min_runs": 2,
        "max_active_per_type": 30,
    },
}


# ------------------------------------------------------------------ utils
def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def die(msg, code=1):
    print(f"[aq-dlc] {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        die(f"JSONの形式が不正です: {path}: {e}")


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def slugify(s, n=40):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-").lower()[:n]
    return s


def pop_opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            val = args[i + 1]
            del args[i:i + 2]
            return val
        del args[i]
    return default


def pop_flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


# ------------------------------------------------------------------ home / config
def ensure_home(verbose=False):
    created = []
    os.makedirs(HOME, exist_ok=True)
    cfg_path = os.path.join(HOME, "config.json")
    if not os.path.exists(cfg_path):
        save_json(cfg_path, {
            "_note": "AQ-DLC の全体設定。プロジェクトごとの設定は <project>/.aq-dlc/config.json で上書きできる。",
            "workspace_dir": "",
            "github": DEFAULTS["github"],
            "automation": DEFAULTS["automation"],
            "memory": DEFAULTS["memory"],
        })
        created.append(cfg_path)
    src = os.path.join(TEMPLATES, "home", "memory")
    dst = os.path.join(HOME, "memory")
    for dirpath, _d, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for name in files:
            t = os.path.join(dst, rel, name)
            if not os.path.exists(t):
                shutil.copy2(os.path.join(dirpath, name), t)
                created.append(t)
    if verbose:
        print(f"[aq-dlc] home: {HOME}")
        for c in created:
            print(f"  + {c}")
        if not created:
            print("  （既に初期化済み）")


def config(project=None):
    cfg = deep_merge(DEFAULTS, load_json(os.path.join(HOME, "config.json"), {}) or {})
    if project:
        cfg = deep_merge(cfg, load_json(os.path.join(project, ".aq-dlc", "config.json"), {}) or {})
    return cfg


# ------------------------------------------------------------------ project / run
def find_project(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, ".aq-dlc", "project.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve_project(args, required=True):
    p = pop_opt(args, "--project") or os.environ.get("AQDLC_PROJECT")
    if p:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(os.path.join(p, ".aq-dlc", "project.json")):
            die(f"AQ-DLC プロジェクトではありません: {p}（init で初期化してください）")
        return p
    p = find_project(os.getcwd())
    if p or not required:
        return p
    die("プロジェクトが見つかりません。--project <パス> を付けるか、プロジェクト内で実行してください。")


def run_dir(project, run_id=None):
    if not run_id:
        try:
            run_id = open(os.path.join(project, ".aq-dlc", "runs", "CURRENT"),
                          encoding="utf-8").read().strip()
        except FileNotFoundError:
            return None
    d = os.path.join(project, ".aq-dlc", "runs", run_id)
    return d if run_id and os.path.isdir(d) else None


def load_state(project):
    d = run_dir(project)
    if not d:
        return None, None
    p = os.path.join(d, "state.json")
    return load_json(p, None), p


def register_active(project, run_id):
    a = load_json(ACTIVE_PATH, {}) or {}
    a.setdefault("projects", {})[project] = {"run_id": run_id, "updated": time.time()}
    save_json(ACTIVE_PATH, a)


def unregister_active(project):
    a = load_json(ACTIVE_PATH, {}) or {}
    if project in a.get("projects", {}):
        del a["projects"][project]
        save_json(ACTIVE_PATH, a)


def related(a, b):
    a, b = os.path.abspath(a), os.path.abspath(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def active_project_for(cwd):
    """Most recently updated active project related to the session cwd."""
    p = find_project(cwd)
    if p:
        return p
    a = load_json(ACTIVE_PATH, {}) or {}
    best, best_t = None, -1
    for proj, info in a.get("projects", {}).items():
        if os.path.isdir(proj) and related(proj, cwd) and info.get("updated", 0) > best_t:
            best, best_t = proj, info.get("updated", 0)
    return best


def iter_verifications(state):
    for req in state.get("requirements", []):
        for risk in req.get("risks", []):
            for v in risk.get("verifications", []):
                yield req, risk, v


def run_cmd(cmd, cwd, timeout):
    start = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = 124, f"TIMEOUT after {timeout}s"
    return code, out, round(time.time() - start, 2)


def latest_source_mtime(project, cfg):
    ignore = set(cfg["gate"]["ignore_dirs"])
    newest, newest_path = 0.0, None
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > newest:
                newest, newest_path = m, p
    return newest, newest_path


# ------------------------------------------------------------------ commands
def cmd_setup(args):
    ensure_home(verbose=True)


def scaffold(target, name):
    """Copy the project template into target without overwriting existing files."""
    src = os.path.join(TEMPLATES, "project")
    added, kept = [], []
    for dirpath, _d, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(target, rel), exist_ok=True)
        for f in files:
            dst = os.path.normpath(os.path.join(target, rel, f))
            if os.path.exists(dst):
                kept.append(os.path.relpath(dst, target))
                continue
            text = open(os.path.join(dirpath, f), encoding="utf-8").read()
            open(dst, "w", encoding="utf-8").write(text.replace("{app_name}", name))
            added.append(os.path.relpath(dst, target))
    save_json(os.path.join(target, ".aq-dlc", "project.json"), {
        "name": name, "slug": slugify(name) or "app", "created": now_iso(),
        "aqdlc_version": load_json(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), {}).get("version"),
    })
    return added, kept


def cmd_init(args):
    """Initialize the folder opened in Claude Code (or --project DIR) as a AQ-DLC project."""
    ensure_home()
    target = pop_opt(args, "--project") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(target):
        die(f"フォルダがありません: {target}")
    if os.path.realpath(target) in (os.path.realpath(os.path.expanduser("~")), "/"):
        die("ホームフォルダやルートは初期化できません。アプリ用のフォルダを開いてください。")
    existing = find_project(target)
    if existing == target:
        print(f"[aq-dlc] 初期化済み: {target}")
        print(target)
        return
    if existing:
        die(f"既存のAQ-DLCプロジェクトの内側です: {existing}")
    name = " ".join(args).strip() or os.path.basename(target)
    others = [e for e in os.listdir(target) if not e.startswith(".")]
    added, kept = scaffold(target, name)
    print(f"[aq-dlc] 初期化しました: {target}")
    for a in added:
        print(f"  + {a}")
    for k in kept:
        print(f"  = {k}（既存のため変更なし）")
    if others:
        print(f"  ! 既存のファイルが {len(others)} 件あります。新規開発の前提と合うか確認してください。")
    print(target)


def cmd_create(args):
    """Create a new subfolder and initialize it (used only when asked)."""
    ensure_home()
    parent = pop_opt(args, "--parent")
    name = " ".join(args).strip()
    if not name:
        die('アプリ名を指定してください: create "<app名>"')
    cfg = config()
    parent = os.path.abspath(os.path.expanduser(parent or cfg.get("workspace_dir") or os.getcwd()))
    target = os.path.join(parent, slugify(name) or "app")
    if os.path.exists(target) and os.listdir(target):
        die(f"既に存在し、空ではありません: {target}（別の名前にするか --parent を指定）")
    if find_project(parent):
        die(f"既存のAQ-DLCプロジェクトの内側には作れません: {find_project(parent)}")
    os.makedirs(target, exist_ok=True)
    scaffold(target, name)
    print(target)


def cmd_publish(args):
    vis = pop_opt(args, "--visibility")
    project = resolve_project(args)
    cfg = config(project)
    vis = vis or cfg["github"]["default_visibility"]
    if vis not in ("private", "public"):
        die("--visibility は private か public")
    branch = cfg["github"]["default_branch"]
    meta = load_json(os.path.join(project, ".aq-dlc", "project.json"), {})

    def sh(cmd, check=True):
        p = subprocess.run(cmd, shell=True, cwd=project, capture_output=True, text=True)
        if check and p.returncode != 0:
            die(f"失敗: {cmd}\n{p.stdout}{p.stderr}")
        return p

    if shutil.which("git") is None:
        die("git が見つかりません")
    if not os.path.isdir(os.path.join(project, ".git")):
        sh(f"git init -b {branch}")
    sh("git add -A")
    if sh("git diff --cached --quiet", check=False).returncode != 0:
        sh('git commit -m "chore: initialize project with AQ-DLC"')
    if sh("git remote get-url origin", check=False).returncode == 0:
        print("[aq-dlc] origin は設定済み。push のみ行います。")
        sh(f"git push -u origin {branch}")
        return
    if shutil.which("gh") is None or sh("gh auth status", check=False).returncode != 0:
        die("gh が未インストールか未ログインです。`gh auth login` の後に再実行してください。"
            "（ローカルのgitリポジトリは作成済み）")
    sh(f'gh repo create "{meta.get("slug") or os.path.basename(project)}" --{vis} '
       f'--source . --remote origin --push')
    url = sh("gh repo view --json url -q .url", check=False).stdout.strip()
    print(f"[aq-dlc] GitHub に {vis} リポジトリを作成しました: {url}")


def cmd_new(args):
    project = resolve_project(args)
    title = " ".join(args).strip() or "run"
    runs = os.path.join(project, ".aq-dlc", "runs")
    rid = _dt.datetime.now().strftime("%Y%m%d-%H%M") + "-" + (slugify(title) or "run")
    d = os.path.join(runs, rid)
    os.makedirs(os.path.join(d, "gate"), exist_ok=True)
    save_json(os.path.join(d, "state.json"), {
        "run_id": rid, "title": title, "status": "in_progress", "phase": "00_start",
        "created": now_iso(), "awaiting_human": False, "question": "",
        "memory_loaded": [], "requirements": [],
        "review": {"done": False, "blocking_findings": 0, "summary": ""},
        "memory_done": False, "fix_iterations": 0, "gate": None, "stop_blocks": 0,
    })
    tmpl = os.path.join(project, ".aq-dlc", "run-template.md")
    body = open(tmpl, encoding="utf-8").read() if os.path.exists(tmpl) else "# {title}\n"
    open(os.path.join(d, "run.md"), "w", encoding="utf-8").write(
        body.replace("{title}", title).replace("{run_id}", rid).replace("{date}", now_iso()))
    open(os.path.join(runs, "CURRENT"), "w", encoding="utf-8").write(rid + "\n")
    register_active(project, rid)
    print(f"[aq-dlc] run開始: {rid}\n  state: {os.path.join(d, 'state.json')}\n  記録:  {os.path.join(d, 'run.md')}")


def cmd_gate(args):
    project = resolve_project(args)
    state, spath = load_state(project)
    if not state:
        die("実行中のrunがありません。先に new を実行してください。")
    cfg = config(project)
    checks = cfg.get("checks", [])
    default_to = int(cfg["gate"]["default_timeout_sec"])
    gdir = os.path.join(os.path.dirname(spath), "gate")
    os.makedirs(gdir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    results = {"checks": {}, "verifications": {}}
    print(f"[aq-dlc] gate {stamp}  ({project})")

    def record(kind, key, cmd, timeout):
        code, out, dur = run_cmd(cmd, project, timeout)
        log = os.path.join(gdir, f"{stamp}-{kind}-{key}.log")
        open(log, "w", encoding="utf-8").write(f"$ {cmd}\nexit={code}\n\n{out}")
        return {"pass": code == 0, "exit": code, "sec": dur, "log": os.path.relpath(log, project)}

    for c in checks:
        r = record("check", c["name"], c["command"], int(c.get("timeout_sec", default_to)))
        results["checks"][c["name"]] = r
        print(f"  {'PASS' if r['pass'] else 'FAIL'} check:{c['name']} ({r['sec']}s)")
    for _q, _k, v in iter_verifications(state):
        vid = v.get("id", "?")
        if v.get("command"):
            r = record("verify", vid, v["command"], int(v.get("timeout_sec", default_to)))
        elif v.get("checks"):
            missing = [n for n in v["checks"] if n not in results["checks"]]
            r = {"pass": not missing and all(results["checks"][n]["pass"] for n in v["checks"]),
                 "via_checks": v["checks"], "missing_checks": missing}
        else:
            r = {"pass": False, "error": "command も checks もありません"}
        results["verifications"][vid] = r
        print(f"  {'PASS' if r['pass'] else 'FAIL'} verify:{vid}")

    ok = bool(checks) and all(r["pass"] for r in results["checks"].values())
    state["gate"] = {"ran_at": now_iso(), "finished_epoch": time.time(), "stamp": stamp,
                     "checks_pass": ok, "results": results}
    state["stop_blocks"] = 0
    save_json(spath, state)
    register_active(project, state["run_id"])
    if not checks:
        print("  ! .aq-dlc/config.json に checks がありません（完了条件を満たせません）")
    print(f"[aq-dlc] gate結果: {'PASS' if ok else 'FAIL'}")


def completion_problems(project, state):
    cfg = config(project)
    p = []
    if not cfg.get("checks"):
        p.append(".aq-dlc/config.json に checks が1つも登録されていない")
    g = state.get("gate")
    if not g:
        p.append("Quality Gate が未実行（gate）")
    else:
        if not g.get("checks_pass"):
            failed = [n for n, r in g["results"]["checks"].items() if not r["pass"]]
            p.append("gateのcheckが失敗: " + ", ".join(failed or ["(checksなし)"]))
        newest, newest_path = latest_source_mtime(project, cfg)
        if newest > g.get("finished_epoch", 0) + 1:
            p.append("gate実行後にファイルが変更された（gateを再実行）: "
                     + os.path.relpath(newest_path, project))
    reqs = state.get("requirements", [])
    if not reqs:
        p.append("requirements が空")
    vres = (g or {}).get("results", {}).get("verifications", {})
    for req in reqs:
        risks = req.get("risks", [])
        if req.get("critical") and not any(r.get("verifications") for r in risks):
            p.append(f"{req.get('id')}: critical なのに risk/verification がない")
        for risk in risks:
            for v in risk.get("verifications", []):
                vid = v.get("id", "?")
                if v.get("waived_by") == "human":
                    continue
                if v.get("manual") and not v.get("command") and not v.get("checks"):
                    p.append(f"{vid}: 手動確認待ち（人間の確認後 waived_by: human）")
                    continue
                r = vres.get(vid)
                if not r:
                    p.append(f"{vid}: gate結果なし（gateを再実行）")
                elif not r.get("pass"):
                    p.append(f"{vid}: 検証が失敗")
    rv = state.get("review", {})
    if not rv.get("done"):
        p.append("独立レビュー（aq-dlc:reviewer）が未実施")
    elif rv.get("blocking_findings", 0) > 0:
        p.append(f"レビューの blocking 指摘が {rv['blocking_findings']} 件残っている")
    if not state.get("memory_done"):
        p.append("Memory整理（aq-dlc:memory）が未実施")
    return p


def cmd_status(args):
    project = resolve_project(args)
    state, _ = load_state(project)
    print(f"project: {project}")
    if not state:
        print("実行中のrunはありません。")
        return
    print(f"run: {state['run_id']}  status: {state['status']}  phase: {state.get('phase')}")
    if state.get("awaiting_human"):
        print(f"awaiting_human: {state.get('question')}")
    print(f"requirements: {len(state.get('requirements', []))}  "
          f"verifications: {sum(1 for _ in iter_verifications(state))}")
    probs = completion_problems(project, state)
    print("完了条件: OK" if not probs else "未充足:\n  - " + "\n  - ".join(probs))


def cmd_check(args):
    project = resolve_project(args)
    state, _ = load_state(project)
    if not state:
        die("実行中のrunがありません。")
    probs = completion_problems(project, state)
    if probs:
        print("[aq-dlc] 完了条件を満たしていません:\n  - " + "\n  - ".join(probs))
        sys.exit(1)
    print("[aq-dlc] 完了条件OK")


# ------------------------------------------------------------------ memory
def parse_frontmatter(path):
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
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
    roots = [os.path.join(HOME, "memory")]
    if project:
        roots.append(os.path.join(project, ".aq-dlc", "memory"))
    rows = []
    for root in roots:
        for dirpath, _d, files in os.walk(root):
            for name in files:
                if not name.endswith(".md") or name.lower() == "readme.md":
                    continue
                meta = parse_frontmatter(os.path.join(dirpath, name))
                if not meta:
                    continue
                status = meta.get("status", "")
                if not show_all and status not in ("active", "validated"):
                    continue
                mtags = {t.lower() for t in meta.get("tags", [])}
                mstack = {t.lower() for t in meta.get("stack", [])} or {"any"}
                if stacks and not (mstack & stacks or "any" in mstack):
                    continue
                score = len(mtags & tags) if tags else 0
                is_proj = meta.get("type") == "project"
                if tags and score == 0 and not show_all and not is_proj:
                    continue
                rank = (is_proj, status == "active", score,
                        {"high": 2, "medium": 1}.get(meta.get("importance", ""), 0))
                rows.append((rank, meta))
    rows.sort(key=lambda r: r[0], reverse=True)
    if not show_all:
        rows = rows[:limit]
    print(f"[aq-dlc] global memory: {os.path.join(HOME, 'memory')}")
    if not rows:
        print("該当するMemoryはありません。")
        return
    for _r, m in rows:
        print(f"{m.get('id','?'):8} {m.get('status',''):10} {m.get('type',''):17} "
              f"{m.get('_title','')}  [{', '.join(m.get('tags', []))}]\n         {m['_path']}")


# ------------------------------------------------------------------ hooks
def read_payload():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def cmd_stop_hook(args):
    payload = read_payload()
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = active_project_for(cwd)
    if not project:
        return
    state, spath = load_state(project)
    if not state or state.get("status") in ("aborted", "closed"):
        return
    if state.get("awaiting_human"):
        return
    cfg = config(project)
    max_blocks = int(cfg["automation"]["max_auto_continues"])
    if state.get("stop_blocks", 0) >= max_blocks:
        state["stop_blocks"] = 0
        save_json(spath, state)
        print(f"[aq-dlc] 自動継続の上限（{max_blocks}回）に達したため停止を許可しました。", file=sys.stderr)
        return
    probs = completion_problems(project, state)
    if state.get("status") == "complete":
        if not probs:
            state["status"] = "closed"
            state["closed_at"] = now_iso()
            save_json(spath, state)
            unregister_active(project)
            return
        state["status"] = "in_progress"
        reason = ("AQ-DLC: status=complete ですが完了条件を満たしていません。"
                  "証拠を揃えてから再度完了にしてください:\n- " + "\n- ".join(probs))
    else:
        reason = (f"AQ-DLC: run が進行中です（project: {project} / phase: {state.get('phase')}）。"
                  "次の未充足条件に取り組んでください。人間の判断が必要なら state.json に "
                  "awaiting_human: true と question を書いてから質問してください。"
                  "中断するなら status を aborted にしてください:\n- " + "\n- ".join(probs[:8]))
    state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    save_json(spath, state)
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def cmd_session_hook(args):
    """Inject context so AQ-DLC runs without the user typing a command."""
    payload = read_payload()
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if os.path.realpath(cwd) in (os.path.realpath(os.path.expanduser("~")), "/"):
        return
    project = find_project(cwd)
    if not config(project)["automation"].get("auto_start", True):
        return
    engine = f'python3 "{os.path.abspath(__file__)}"'
    if project:
        state, _ = load_state(project)
        lines = [f"このフォルダは AQ-DLC プロジェクトです（{project}）。",
                 "開発・修正・機能追加の依頼は、ユーザーがコマンドを打たなくても aq-dlc:start Skill の手順で進めてください。"]
        if state and state.get("status") == "in_progress":
            probs = completion_problems(project, state)
            lines.append(f"進行中の run があります: {state['run_id']}（phase: {state.get('phase')}）。"
                         "ユーザーの依頼がこのrunの続きなら、そのまま再開してください。")
            if state.get("awaiting_human"):
                lines.append(f"人間への確認待ち: {state.get('question')}")
            if probs:
                lines.append("未充足の完了条件:\n- " + "\n- ".join(probs[:8]))
        lines.append(f"エンジン: {engine} <command> --project \"{project}\"")
    else:
        visible = [e for e in os.listdir(cwd) if not e.startswith(".")]
        if visible:
            return  # existing non-AQ-DLC work: stay out of the way
        lines = [f"このフォルダ（{cwd}）は空です。",
                 "ユーザーが新しいアプリやツールの作成を依頼したら、コマンドを打たれなくても aq-dlc:start Skill を使い、"
                 "このフォルダを AQ-DLC プロジェクトとして初期化して開発してください。",
                 "開発以外の質問や雑談なら、通常どおり答えてください。"]
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": "\n".join(lines)}}, ensure_ascii=False))


GUARDS = [
    ("block_force_push", r"\bgit\b[^;&|]*\bpush\b[^;&|]*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)",
     "force push は人間の確認が必要です"),
    ("block_pr_merge", r"\bgh\s+pr\s+merge\b", "PR のマージは人間が行います"),
    ("block_destructive_gh", r"\bgh\s+(repo\s+delete|release\s+delete|repo\s+archive)\b",
     "リポジトリ・リリースの削除やアーカイブは人間の確認が必要です"),
    ("block_destructive_gh", r"\bgh\s+repo\s+edit\b[^;&|]*--visibility",
     "公開範囲の変更は人間の確認が必要です"),
    ("block_destructive_gh", r"\bgit\b[^;&|]*\bpush\b[^;&|]*\s(--delete|-d)\b|\bgit\b[^;&|]*\bpush\b[^;&|]*\s:\S",
     "リモートブランチの削除は人間の確認が必要です"),
    ("block_destructive_gh", r"\bgit\s+(filter-branch|filter-repo)\b", "履歴の書き換えは人間の確認が必要です"),
]


def current_branch(path):
    try:
        p = subprocess.run("git rev-parse --abbrev-ref HEAD", shell=True, cwd=path,
                           capture_output=True, text=True, timeout=5)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def cmd_guard_hook(args):
    payload = read_payload()
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not cmd or not re.search(r"\b(git|gh)\b", cmd):
        return
    cwd = payload.get("cwd") or os.getcwd()
    project = active_project_for(cwd)
    if not project:
        for m in re.finditer(r"(?:cd|-C|--project)\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd):
            cand = find_project(os.path.expanduser(m.group(1).strip("\"'")))
            if cand:
                project = cand
                break
    if not project:
        return  # not a AQ-DLC context: leave the user's other work alone
    auto = config(project)["automation"]
    reasons = [msg for key, pat, msg in GUARDS if auto.get(key, True) and re.search(pat, cmd)]
    if auto.get("block_push_to_default_branch", True) and re.search(r"\bgit\b[^;&|]*\bpush\b", cmd):
        default = config(project)["github"]["default_branch"]
        explicit = re.search(rf"\bpush\b[^;&|]*\s(\S+:)?(refs/heads/)?({re.escape(default)}|master)\b", cmd)
        bare = not re.search(r"\bpush\b\s+\S+\s+\S", cmd) and current_branch(project) in (default, "master")
        if explicit or bare:
            has_remote_branch = subprocess.run(
                f"git ls-remote --exit-code --heads origin {default}", shell=True, cwd=project,
                capture_output=True, text=True).returncode == 0
            if has_remote_branch:
                reasons.append(f"{default} への直接 push は人間の確認が必要です（作業ブランチを push して PR にしてください）")
    if reasons:
        print("AQ-DLC guard: " + " / ".join(dict.fromkeys(reasons)), file=sys.stderr)
        sys.exit(2)


# ------------------------------------------------------------------ main
COMMANDS = {
    "setup": cmd_setup, "init": cmd_init, "create": cmd_create, "publish": cmd_publish, "new": cmd_new,
    "status": cmd_status, "gate": cmd_gate, "check": cmd_check, "memory": cmd_memory,
    "stop-hook": cmd_stop_hook, "guard-hook": cmd_guard_hook, "session-hook": cmd_session_hook,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    name = sys.argv[1]
    if name.endswith("-hook"):
        # A bug in a hook must never break the user's session.
        try:
            COMMANDS[name](list(sys.argv[2:]))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[aq-dlc] hook error (ignored): {e}", file=sys.stderr)
        return
    COMMANDS[name](list(sys.argv[2:]))


if __name__ == "__main__":
    main()
