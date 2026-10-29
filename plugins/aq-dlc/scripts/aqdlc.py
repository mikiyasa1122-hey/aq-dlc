#!/usr/bin/env python3
"""AQ-DLC engine (Claude Code plugin).

  PL = python3 "<plugin>/scripts/aqdlc.py"

  $PL setup                                  ~/.aq-dlc を初期化（何度実行してもよい）
  $PL init ["<app名>"] [--project DIR]       開いているフォルダをプロジェクトとして初期化
  $PL create "<app名>" [--parent DIR]         サブフォルダを新規作成して初期化（指示があるときだけ）
  $PL publish [--visibility V] [--dry-run]    git init → GitHubリポジトリ作成 → push（既存リポジトリでは追加コミットしない）
  $PL new "<run名>"        --project DIR      runを開始
  $PL add-check <名前> "<コマンド>" [--timeout 秒] / remove-check <名前> / checks
  $PL req <id> "<内容>" [--critical]
  $PL risk <req id> <risk id> "<内容>"
  $PL verify <risk id> <v id> "<方法>" --command "..." | --checks a,b | --manual "..."
  $PL phase <名前> / ask "<質問>" / resume / wait "<理由>" [--minutes N]
  $PL review --start | review --done --blocking N [--summary "..."]
  $PL memory-done / complete / abort
  $PL verify ... --after-merge / gate --after-merge / confirm <v id> "<人間の確認内容>"
  $PL ci [--wait 分]                          push 後に GitHub Actions の結果を確認
  $PL doctor                                   環境の確認
  $PL status               --project DIR      状態と未充足条件
  $PL gate                 --project DIR      Quality Gate を実行して記録
  $PL check                --project DIR      完了判定（Stop hook と同じ）
  $PL memory [--tags a,b] [--stack x] [--all] --project DIR
  $PL stop-hook / guard-hook                  Claude Code の hook から呼ばれる

--project を省略すると、カレントディレクトリから上に .aq-dlc/project.json を探す。
Python 3.8+ 標準ライブラリのみ。
"""
import datetime as _dt
import hashlib
import json
import locale
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
        "env": {},
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
def decode(b):
    """Decode subprocess output: UTF-8 first, then the OS locale (e.g. cp932 on Japanese Windows)."""
    if b is None:
        return ""
    if isinstance(b, str):
        return b
    for enc in ("utf-8", locale.getpreferredencoding(False), "cp932"):
        if not enc:
            continue
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", errors="replace")


def child_env(extra=None):
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (extra or {}).items()})
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def sh_run(cmd, cwd, timeout=None, env=None):
    """Run a shell command and return (returncode, decoded output)."""
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=timeout, env=child_env(env))
    return p.returncode, decode(p.stdout) + decode(p.stderr)


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


PHASES = ["00_start", "01_requirements", "02_failure_first", "03_implement", "04_gate",
          "04_review", "05_fix", "06_memory", "07_pr_ready", "08_after_merge", "09_done"]


def advance(state, phase, force=False):
    """Move the phase forward automatically (never backwards unless forced)."""
    cur = state.get("phase", "00_start")
    if force or cur not in PHASES or PHASES.index(phase) > PHASES.index(cur) \
            or (phase == "05_fix" and cur in ("04_gate", "04_review", "06_memory")):
        state["phase"] = phase


def iter_verifications(state):
    for req in state.get("requirements", []):
        for risk in req.get("risks", []):
            for v in risk.get("verifications", []):
                yield req, risk, v


def run_cmd(cmd, cwd, timeout, env=None):
    start = time.time()
    try:
        code, out = sh_run(cmd, cwd, timeout, env)
    except subprocess.TimeoutExpired:
        code, out = 124, f"タイムアウト（{timeout}秒）"
    return code, out, round(time.time() - start, 2)


def file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(project, cfg, previous=None):
    """{relpath: [size, mtime, sha1]} of project files. Reuses previous hashes when size/mtime match."""
    ignore = set(cfg["gate"]["ignore_dirs"])
    previous = previous or {}
    snap = {}
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, project).replace(os.sep, "/")
            try:
                st = os.stat(p)
            except OSError:
                continue
            old = previous.get(rel)
            if old and old[0] == st.st_size and abs(old[1] - st.st_mtime) < 1e-6:
                snap[rel] = old
                continue
            try:
                snap[rel] = [st.st_size, st.st_mtime, file_sha1(p)]
            except OSError:
                continue
    return snap


def snapshot_diff(old, new):
    changed = [k for k in new if k in old and new[k][2] != old[k][2]]
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    return changed, added, removed


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
    if sys.platform == "win32":
        cpath = os.path.join(target, ".aq-dlc", "config.json")
        pc = load_json(cpath, {}) or {}
        pc.setdefault("gate", {}).setdefault("env", {}).setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        save_json(cpath, pc)
    if ".gitignore" in kept and ensure_gitignore(target):
        print("  + .gitignore に標準の除外設定を追記")
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


GITIGNORE_MARK = "# --- AQ-DLC 標準 ---"


def ensure_gitignore(project):
    """Append AQ-DLC's standard ignore block once (keeps the user's own lines)."""
    path = os.path.join(project, ".gitignore")
    src = os.path.join(TEMPLATES, "project", ".gitignore")
    block = open(src, encoding="utf-8").read()
    cur = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if GITIGNORE_MARK in cur:
        return False
    with open(path, "a", encoding="utf-8") as f:
        if cur and not cur.endswith("\n"):
            f.write("\n")
        f.write(("\n" if cur else "") + block)
    return True


JUNK = re.compile(r"(^|/)(__pycache__|node_modules|\.venv|venv|\.pytest_cache|\.mypy_cache|dist|build|target)(/|$)"
                  r"|\.pyc$|(^|/)\.env($|\.)(?!example)|\.DS_Store$")


def cmd_publish(args):
    vis = pop_opt(args, "--visibility")
    dry = pop_flag(args, "--dry-run")
    project = resolve_project(args)
    cfg = config(project)
    vis = vis or cfg["github"]["default_visibility"]
    if vis not in ("private", "public"):
        die("--visibility は private か public を指定してください")
    branch = cfg["github"]["default_branch"]
    meta = load_json(os.path.join(project, ".aq-dlc", "project.json"), {})

    def sh(cmd, check=True):
        code, out = sh_run(cmd, project)
        if check and code != 0:
            die(f"失敗しました: {cmd}\n{out}")
        return code, out

    if shutil.which("git") is None:
        die("git が見つかりません")
    missing = [k for k in ("user.name", "user.email") if sh(f"git config --get {k}", check=False)[0] != 0
               or not sh(f"git config --get {k}", check=False)[1].strip()]
    if missing and not dry:
        die("git の利用者情報が設定されていないため、コミットできません。次を実行してから publish をやり直してください:\n"
            '  git config --global user.name "<名前>"\n'
            '  git config --global user.email "<メールアドレス>"\n'
            "（GitHub のメールアドレスを公開したくない場合は、GitHub の設定にある noreply アドレスを使えます）")
    fresh = not os.path.isdir(os.path.join(project, ".git"))
    if fresh:
        if ensure_gitignore(project):
            print("[aq-dlc] .gitignore に標準の除外設定を追加しました。")
        if dry:
            print("[aq-dlc] （確認のみ）git リポジトリを作成し、次のファイルを最初のコミットに含めます:")
        else:
            sh(f"git init -b {branch}")
        if not dry:
            sh("git add -A")
            _, staged = sh("git diff --cached --name-only")
            files = [f for f in staged.splitlines() if f.strip()]
            junk = [f for f in files if JUNK.search(f)]
            for f in junk:
                sh(f'git rm -r --cached --quiet -- "{f}"', check=False)
            files = [f for f in files if f not in junk]
            print(f"[aq-dlc] 最初のコミットに含めるファイル（{len(files)}件）:")
            for f in files[:50]:
                print(f"  {f}")
            if len(files) > 50:
                print(f"  …ほか {len(files) - 50} 件")
            if junk:
                print(f"[aq-dlc] 生成物・秘密情報らしいファイル {len(junk)} 件は除外しました（例: {junk[0]}）")
            if files:
                sh('git commit -q -m "初期化: AQ-DLC プロジェクトを作成"')
    else:
        # Existing repository: never stage or commit on the user's behalf.
        code, _ = sh("git rev-parse --verify HEAD", check=False)
        if code != 0:
            die("git リポジトリにコミットがありません。コミットしてから再実行してください。")
        code, dirty = sh("git status --porcelain", check=False)
        if dirty.strip():
            print("[aq-dlc] 未コミットの変更があります。publish はコミット済みの内容だけを push します。")
    if dry:
        return

    code, _ = sh("git remote get-url origin", check=False)
    if code != 0:
        code, _ = sh("gh auth status", check=False) if shutil.which("gh") else (1, "")
        if code != 0:
            die("gh が未インストールか未ログインです。`gh auth login` の後に再実行してください。"
                "（ローカルの git リポジトリはそのまま使えます）")
        sh(f'gh repo create "{meta.get("slug") or os.path.basename(project)}" --{vis} '
           f'--source . --remote origin')
        _, url = sh("gh repo view --json url -q .url", check=False)
        print(f"[aq-dlc] GitHub に {vis} リポジトリを作成しました: {url.strip()}")
    # push the default branch (if it exists locally) and the current branch
    _, cur = sh("git rev-parse --abbrev-ref HEAD", check=False)
    cur = cur.strip()
    targets = []
    if sh(f"git rev-parse --verify {branch}", check=False)[0] == 0:
        targets.append(branch)
    if cur and cur != "HEAD" and cur not in targets:
        targets.append(cur)
    for b in targets:
        if b == branch and sh(f"git ls-remote --exit-code --heads origin {branch}", check=False)[0] == 0:
            continue  # never push to an existing default branch
        sh(f"git push -u origin {b}")
        print(f"[aq-dlc] push しました: {b}")


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


def review_running(state):
    w = state.get("waiting") or {}
    return bool(w) and time.time() < w.get("until", 0) and ("レビュー" in w.get("for", "") or w.get("agents"))


def cmd_gate(args):
    force = pop_flag(args, "--force")
    after_merge = pop_flag(args, "--after-merge")
    project = resolve_project(args)
    state, spath = load_state(project)
    if not state:
        die("実行中のrunがありません。先に new を実行してください。")
    if review_running(state) and not force:
        die("サブエージェント（独立レビューなど）の実行中は gate を実行しません。同じフォルダでテストが同時に動き、"
            "結果が壊れるためです。結果が届いてから gate を実行してください（どうしても必要なら --force）。")
    cfg = config(project)
    genv = cfg["gate"].get("env") or {}
    checks = cfg.get("checks", [])
    default_to = int(cfg["gate"]["default_timeout_sec"])
    gdir = os.path.join(os.path.dirname(spath), "gate")
    os.makedirs(gdir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    results = {"checks": {}, "verifications": {}}
    print(f"[aq-dlc] gate {stamp}  ({project})")

    def record(kind, key, cmd, timeout):
        code, out, dur = run_cmd(cmd, project, timeout, genv)
        log = os.path.join(gdir, f"{stamp}-{kind}-{key}.log")
        open(log, "w", encoding="utf-8").write(f"$ {cmd}\nexit={code}\n\n{out}")
        return {"pass": code == 0, "exit": code, "sec": dur, "log": os.path.relpath(log, project)}

    for c in checks:
        r = record("check", c["name"], c["command"], int(c.get("timeout_sec", default_to)))
        results["checks"][c["name"]] = r
        print(f"  {'合格' if r['pass'] else '不合格'} チェック:{c['name']}（{r['sec']}秒）")
    for _q, _k, v in iter_verifications(state):
        vid = v.get("id", "?")
        if v.get("after_merge") and not after_merge:
            results["verifications"][vid] = {"pass": False, "skipped": "マージ後に実行"}
            print(f"  保留 検証:{vid}（マージ後に gate --after-merge で実行）")
            continue
        if v.get("command"):
            r = record("verify", vid, v["command"], int(v.get("timeout_sec", default_to)))
        elif v.get("checks"):
            missing = [n for n in v["checks"] if n not in results["checks"]]
            r = {"pass": not missing and all(results["checks"][n]["pass"] for n in v["checks"]),
                 "via_checks": v["checks"], "missing_checks": missing}
        else:
            r = {"pass": False, "error": "command も checks もありません"}
        results["verifications"][vid] = r
        print(f"  {'合格' if r['pass'] else '不合格'} 検証:{vid}")

    ok = bool(checks) and all(r["pass"] for r in results["checks"].values())
    state["gate"] = {"ran_at": now_iso(), "finished_epoch": time.time(), "stamp": stamp,
                     "checks_pass": ok, "results": results,
                     "snapshot": snapshot(project, cfg)}
    state["stop_blocks"] = 0
    if after_merge:
        state["after_merge_gate"] = True
        advance(state, "08_after_merge")
    elif state.get("review", {}).get("done") and state["review"].get("blocking_findings", 0) > 0:
        advance(state, "05_fix")
    else:
        advance(state, "04_gate")
    save_json(spath, state)
    register_active(project, state["run_id"])
    if not checks:
        print("  ! .aq-dlc/config.json に checks がありません（完了条件を満たせません）")
    print(f"[aq-dlc] gate結果: {'合格' if ok else '不合格'}")


def edit_state(args):
    project = resolve_project(args)
    state, spath = load_state(project)
    if not state:
        die("実行中のrunがありません。先に new を実行してください。")
    return project, state, spath


def find_req(state, rid):
    for r in state.get("requirements", []):
        if r.get("id") == rid:
            return r
    die(f"requirement が見つかりません: {rid}")


def find_risk(state, kid):
    for r in state.get("requirements", []):
        for k in r.get("risks", []):
            if k.get("id") == kid:
                return k
    die(f"risk が見つかりません: {kid}")


def cmd_add_check(args):
    """add-check <名前> <コマンド> [--timeout 秒]"""
    project = resolve_project(args)
    timeout = pop_opt(args, "--timeout")
    if len(args) < 2:
        die('使い方: add-check <名前> "<コマンド>" [--timeout 秒]')
    name, command = args[0], " ".join(args[1:])
    path = os.path.join(project, ".aq-dlc", "config.json")
    cfg = load_json(path, {}) or {}
    checks = [c for c in cfg.get("checks", []) if c.get("name") != name]
    entry = {"name": name, "command": command}
    if timeout:
        entry["timeout_sec"] = int(timeout)
    checks.append(entry)
    cfg["checks"] = checks
    save_json(path, cfg)
    print(f"[aq-dlc] check を登録しました: {name} → {command}")


def cmd_remove_check(args):
    project = resolve_project(args)
    if not args:
        die("使い方: remove-check <名前>")
    path = os.path.join(project, ".aq-dlc", "config.json")
    cfg = load_json(path, {}) or {}
    before = len(cfg.get("checks", []))
    cfg["checks"] = [c for c in cfg.get("checks", []) if c.get("name") != args[0]]
    save_json(path, cfg)
    print(f"[aq-dlc] {'削除しました' if len(cfg['checks']) < before else '見つかりませんでした'}: {args[0]}")


def cmd_checks(args):
    project = resolve_project(args)
    checks = config(project).get("checks", [])
    if not checks:
        print("check は登録されていません。")
    for c in checks:
        print(f"{c['name']}: {c['command']}")


def cmd_req(args):
    """req <id> "<内容>" [--critical]"""
    critical = pop_flag(args, "--critical")
    _p, state, spath = edit_state(args)
    if len(args) < 2:
        die('使い方: req <id> "<内容>" [--critical]')
    rid, text = args[0], " ".join(args[1:])
    reqs = state.setdefault("requirements", [])
    for r in reqs:
        if r["id"] == rid:
            r["text"], r["critical"] = text, critical or r.get("critical", False)
            break
    else:
        reqs.append({"id": rid, "text": text, "critical": critical, "risks": []})
    advance(state, "01_requirements")
    save_json(spath, state)
    print(f"[aq-dlc] requirement {rid}{'（重要）' if critical else ''}: {text}")


def cmd_risk(args):
    """risk <requirement id> <risk id> "<内容>" """
    _p, state, spath = edit_state(args)
    if len(args) < 3:
        die('使い方: risk <requirement id> <risk id> "<内容>"')
    req = find_req(state, args[0])
    kid, text = args[1], " ".join(args[2:])
    for k in req.setdefault("risks", []):
        if k["id"] == kid:
            k["text"] = text
            break
    else:
        req["risks"].append({"id": kid, "text": text, "verifications": []})
    advance(state, "02_failure_first")
    save_json(spath, state)
    print(f"[aq-dlc] risk {args[0]}/{kid}: {text}")


def cmd_verify(args):
    """verify <risk id> <verification id> "<方法>" [--command "..."] [--checks a,b] [--manual "..."] [--after-merge]"""
    after = pop_flag(args, "--after-merge")
    command = pop_opt(args, "--command")
    checks = pop_opt(args, "--checks")
    manual = pop_opt(args, "--manual")
    _p, state, spath = edit_state(args)
    if len(args) < 3 or not (command or checks or manual):
        die('使い方: verify <risk id> <verification id> "<方法>" --command "..." | --checks a,b | --manual "..."')
    risk = find_risk(state, args[0])
    v = {"id": args[1], "method": " ".join(args[2:])}
    if command:
        v["command"] = command
    if checks:
        v["checks"] = [c.strip() for c in checks.split(",") if c.strip()]
    if manual:
        v["manual"] = manual
    if after:
        v["after_merge"] = True
    risk["verifications"] = [x for x in risk.setdefault("verifications", []) if x.get("id") != v["id"]] + [v]
    advance(state, "02_failure_first")
    save_json(spath, state)
    print(f"[aq-dlc] verification {args[0]}/{v['id']}: {v['method']}{'（マージ後）' if after else ''}")


def cmd_confirm(args):
    """confirm <verification id> "<人間が確認した内容>" — 人間が明示的に確認したと答えた後だけ使う"""
    _p, state, spath = edit_state(args)
    if len(args) < 2:
        die('使い方: confirm <verification id> "<人間が確認した内容（人間の返答の要約）>"')
    for _q, _k, v in iter_verifications(state):
        if v.get("id") == args[0]:
            v["waived_by"] = "human"
            v["waive_reason"] = " ".join(args[1:])
            v["confirmed_at"] = now_iso()
            save_json(spath, state)
            print(f"[aq-dlc] 人間の確認として記録しました: {args[0]}")
            return
    die(f"verification が見つかりません: {args[0]}")


def cmd_phase(args):
    _p, state, spath = edit_state(args)
    if not args:
        die("使い方: phase <フェーズ名>")
    advance(state, args[0], force=True) if args[0] in PHASES else state.__setitem__("phase", args[0])
    save_json(spath, state)
    print(f"[aq-dlc] phase: {args[0]}")


def cmd_wait(args):
    """wait "<待っている理由>" [--minutes N] — バックグラウンド処理を待つ間、Stop hook で止めない"""
    minutes = int(pop_opt(args, "--minutes") or 30)
    _p, state, spath = edit_state(args)
    reason = " ".join(args) or "バックグラウンド処理"
    state["waiting"] = {"for": reason, "since": now_iso(), "until": time.time() + minutes * 60}
    save_json(spath, state)
    print(f"[aq-dlc] 待機中として記録しました（最大{minutes}分）: {reason}")


def cmd_resume(args):
    _p, state, spath = edit_state(args)
    state.pop("waiting", None)
    state["awaiting_human"] = False
    save_json(spath, state)
    print("[aq-dlc] 待機を解除しました。")


def cmd_review(args):
    """review --start | review --done --blocking N [--summary "..."]"""
    start = pop_flag(args, "--start")
    done = pop_flag(args, "--done")
    blocking = pop_opt(args, "--blocking")
    summary = pop_opt(args, "--summary")
    _p, state, spath = edit_state(args)
    if start:
        state["waiting"] = {"for": "独立レビュー", "since": now_iso(), "until": time.time() + 30 * 60}
        state["review"] = {"done": False, "blocking_findings": 0, "summary": "レビュー中"}
        advance(state, "04_review")
        print("[aq-dlc] レビュー中として記録しました（結果が届くまで Stop hook は止めません）。")
    elif done:
        if blocking is None:
            die("--blocking <重大な指摘の件数> を指定してください")
        state.pop("waiting", None)
        state["review"] = {"done": True, "blocking_findings": int(blocking), "summary": summary or ""}
        advance(state, "05_fix" if int(blocking) > 0 else "06_memory")
        print(f"[aq-dlc] レビュー結果を記録しました: 重大な指摘 {blocking} 件")
    else:
        die('使い方: review --start | review --done --blocking N [--summary "..."]')
    save_json(spath, state)


def cmd_ask(args):
    _p, state, spath = edit_state(args)
    state["awaiting_human"] = True
    state["question"] = " ".join(args)
    save_json(spath, state)
    print("[aq-dlc] 人間への確認待ちとして記録しました。")


def cmd_memory_done(args):
    _p, state, spath = edit_state(args)
    state["memory_done"] = True
    advance(state, "06_memory")
    save_json(spath, state)
    print("[aq-dlc] Memory整理を完了として記録しました。")


def has_after_merge(state):
    return any(v.get("after_merge") for _q, _k, v in iter_verifications(state))


def cmd_complete(args):
    project, state, spath = edit_state(args)
    probs = completion_problems(project, state, stage="pr")
    if probs:
        print("[aq-dlc] まだPRを出せる状態ではありません:\n  - " + "\n  - ".join(probs))
        sys.exit(1)
    final = completion_problems(project, state, stage="final")
    if final:
        state["status"] = "pr_ready"
        advance(state, "07_pr_ready")
        save_json(spath, state)
        print("[aq-dlc] PRを出せる状態です（status: pr_ready）。マージ後の確認が残っています:\n  - "
              + "\n  - ".join(final)
              + "\n→ PRを作成して報告し、マージを待ってください（この間は Stop hook は止めません）。"
                "\n→ マージ後: 既定ブランチを取り込み → gate --after-merge → 手動確認は ask でまとめて1回 → confirm → complete")
        return
    state["status"] = "complete"
    advance(state, "09_done")
    save_json(spath, state)
    print("[aq-dlc] 完了条件を満たしました。status を complete にしました。")


def cmd_ci(args):
    """ci [--wait 分] — 現在の HEAD に対する GitHub Actions の結果を確認して記録する"""
    wait_min = float(pop_opt(args, "--wait") or 8)
    project, state, spath = edit_state(args)
    if not os.path.isdir(os.path.join(project, ".github", "workflows")):
        print("[aq-dlc] .github/workflows がないため、CI の確認は不要です。")
        return
    if shutil.which("gh") is None:
        die("gh がないため CI を確認できません。")
    code, sha = sh_run("git rev-parse HEAD", project)
    sha = sha.strip()
    deadline = time.time() + wait_min * 60
    while True:
        code, out = sh_run(f"gh run list --commit {sha} --json name,status,conclusion,url --limit 20", project, 60)
        runs = []
        if code == 0:
            try:
                runs = json.loads(out or "[]")
            except json.JSONDecodeError:
                runs = []
        pending = [r for r in runs if r.get("status") != "completed"]
        if runs and not pending:
            break
        if time.time() >= deadline:
            break
        time.sleep(20)
    failed = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") not in ("success", "skipped", "neutral")]
    result = "success" if runs and not pending and not failed else ("failure" if failed else "pending")
    state["ci"] = {"sha": sha, "result": result, "checked_at": now_iso(),
                   "runs": [{k: r.get(k) for k in ("name", "status", "conclusion", "url")} for r in runs]}
    save_json(spath, state)
    label = {"success": "成功", "failure": "失敗", "pending": "未完了（実行待ち・実行中・未検出）"}[result]
    print(f"[aq-dlc] CI（{sha[:7]}）: {label}")
    for r in runs:
        print(f"  {r.get('name')}: {r.get('conclusion') or r.get('status')}  {r.get('url')}")
    if result != "success":
        sys.exit(1)


def cmd_abort(args):
    project, state, spath = edit_state(args)
    state["status"] = "aborted"
    save_json(spath, state)
    unregister_active(project)
    print("[aq-dlc] run を中断しました。")


def completion_problems(project, state, stage=None):
    """stage: "pr" = PRを出せるか（マージ後の検証を除く）, "final" = すべて, None = status から判断"""
    if stage is None:
        stage = "final" if state.get("status") in ("pr_ready",) or state.get("after_merge_gate") else "pr"
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
        old = g.get("snapshot") or {}
        changed, added, removed = snapshot_diff(old, snapshot(project, cfg, old))
        if changed or added or removed:
            names = (changed + added + removed)[:3]
            p.append(f"gate実行後に内容が変わったファイルがある（gateを再実行）: "
                     f"変更{len(changed)}・追加{len(added)}・削除{len(removed)}件（例: {', '.join(names)}）")
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
                if v.get("after_merge") and stage == "pr":
                    continue
                if v.get("manual") and not v.get("command") and not v.get("checks"):
                    p.append(f"{vid}: 人間の確認待ち（ask でまとめて確認し、返答後に confirm）")
                    continue
                r = vres.get(vid)
                if not r:
                    p.append(f"{vid}: gate結果なし（gateを再実行）")
                elif r.get("skipped"):
                    p.append(f"{vid}: マージ後の検証が未実行（gate --after-merge）")
                elif not r.get("pass"):
                    p.append(f"{vid}: 検証が失敗")
    rv = state.get("review", {})
    if not rv.get("done"):
        p.append("独立レビュー（aq-dlc:reviewer）が未実施")
    elif rv.get("blocking_findings", 0) > 0:
        p.append(f"レビューの blocking 指摘が {rv['blocking_findings']} 件残っている")
    if not state.get("memory_done"):
        p.append("Memory整理（aq-dlc:memory）が未実施")
    if stage == "pr" and os.path.isdir(os.path.join(project, ".github", "workflows")) \
            and sh_run("git remote get-url origin", project, 10)[0] == 0:
        ci = state.get("ci") or {}
        head = sh_run("git rev-parse HEAD", project, 10)[1].strip()
        if ci.get("sha") != head:
            p.append("最新のコミットに対する CI が未確認（push 後に ci を実行）")
        elif ci.get("result") != "success":
            p.append(f"CI が{'失敗' if ci.get('result') == 'failure' else '未完了'}（ci で再確認）")
    return p


def cmd_status(args):
    project = resolve_project(args)
    state, _ = load_state(project)
    print(f"プロジェクト: {project}")
    if not state:
        print("実行中のrunはありません。")
        return
    print(f"run: {state['run_id']}  状態: {state['status']}  フェーズ: {state.get('phase')}")
    if state.get("awaiting_human"):
        print(f"人間への確認待ち: {state.get('question')}")
    w = state.get("waiting")
    if w and time.time() < w.get("until", 0):
        print(f"待機中: {w.get('for')}")
    print(f"要求: {len(state.get('requirements', []))}件  "
          f"検証: {sum(1 for _ in iter_verifications(state))}件")
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
    print(f"[aq-dlc] 共通Memory: {os.path.join(HOME, 'memory')}")
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


# ------------------------------------------------------------------ hooks
def read_payload():
    try:
        return json.loads(decode(sys.stdin.buffer.read()) or "{}")
    except Exception:
        return {}


def cmd_stop_hook(args):
    payload = read_payload()
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = active_project_for(cwd)
    if not project:
        return
    state, spath = load_state(project)
    if not state or state.get("status") in ("aborted", "closed", "pr_ready"):
        return  # pr_ready: waiting for the human to merge
    if state.get("awaiting_human"):
        return
    w = state.get("waiting")
    if w and time.time() < w.get("until", 0):
        return  # waiting for a background task (e.g. the reviewer subagent)
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
                  "次の未充足条件に取り組んでください。人間の判断が必要なら ask コマンドを実行してから質問し、"
                  "サブエージェントやCIを待っているなら wait を実行してください。"
                  "中断するなら abort を実行してください:\n- " + "\n- ".join(probs[:8]))
    state["stop_blocks"] = state.get("stop_blocks", 0) + 1
    save_json(spath, state)
    reason += "\n（報告や記録はすべて日本語で書いてください）"
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def _subagent_state(payload):
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = active_project_for(cwd)
    if not project:
        return None, None
    state, spath = load_state(project)
    if not state or state.get("status") != "in_progress":
        return None, None
    return state, spath


def cmd_agent_start_hook(args):
    """PreToolUse(Agent/Task): mark the run as waiting so the Stop hook doesn't nag."""
    payload = read_payload()
    state, spath = _subagent_state(payload)
    if not state:
        return
    ti = payload.get("tool_input") or {}
    kind = ti.get("subagent_type") or "サブエージェント"
    w = state.get("waiting") or {}
    agents = int(w.get("agents", 0)) + 1
    label = "独立レビュー" if "reviewer" in kind else f"サブエージェント（{kind}）"
    state["waiting"] = {"for": label, "since": now_iso(), "until": time.time() + 45 * 60,
                        "agents": agents, "auto": True}
    if "reviewer" in kind:
        state["review"] = {"done": False, "blocking_findings": 0, "summary": "レビュー中"}
        advance(state, "04_review")
    save_json(spath, state)


def cmd_agent_stop_hook(args):
    """SubagentStop: release the automatic wait when the last subagent finishes."""
    payload = read_payload()
    state, spath = _subagent_state(payload)
    if not state:
        return
    w = state.get("waiting") or {}
    if not w.get("auto"):
        return
    agents = int(w.get("agents", 1)) - 1
    if agents <= 0:
        state.pop("waiting", None)
    else:
        w["agents"] = agents
    state["stop_blocks"] = 0
    save_json(spath, state)


LANG_RULE = ("【言語】ユーザーへの返答、質問、進捗報告、run.md、Memory、コミットメッセージ、PRのタイトルと本文、"
             "Issue、コード内のコメント、サブエージェントへの依頼と報告は、すべて日本語で書いてください。"
             "英語のエラーやログを伝えるときは、日本語で要約してから必要な部分だけ引用してください。"
             "コードの識別子やコマンドは英語のままで構いません。")


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
    if sys.platform == "win32":
        lines.append("【Windows】Playwright を使う場合は、ブラウザをプロジェクト内に入れてください"
                     "（PLAYWRIGHT_BROWSERS_PATH=0 を付けて npx playwright install）。Claude Desktop では AppData への書き込みが"
                     "別の場所に振り替えられ、gate から見えないことがあります。")
    lines.append(LANG_RULE)
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
        code, out = sh_run("git rev-parse --abbrev-ref HEAD", path, 5)
        return out.strip() if code == 0 else None
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
            has_remote_branch = sh_run(
                f"git ls-remote --exit-code --heads origin {default}", project, 15)[0] == 0
            if has_remote_branch:
                reasons.append(f"{default} への直接 push は人間の確認が必要です（作業ブランチを push して PR にしてください）")
    if reasons:
        print("AQ-DLC guard: " + " / ".join(dict.fromkeys(reasons)), file=sys.stderr)
        sys.exit(2)


def cmd_doctor(args):
    """環境の確認: Python・git・利用者情報・gh・文字コード・Windows固有の注意"""
    project = resolve_project(args, required=False) or os.getcwd()
    ok = True

    def line(good, msg, fix=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'OK ' if good else 'NG '} {msg}" + (f"\n       → {fix}" if fix and not good else ""))
    print("[aq-dlc] 環境の確認")
    line(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}", "Python 3.8 以上を入れてください")
    has_git = shutil.which("git") is not None
    line(has_git, "git", "git をインストールしてください")
    if has_git:
        for k in ("user.name", "user.email"):
            code, out = sh_run(f"git config --get {k}", project, 10)
            line(code == 0 and bool(out.strip()), f"git {k}: {out.strip() or '未設定'}",
                 f'git config --global {k} "<値>" を実行してください')
    has_gh = shutil.which("gh") is not None
    line(has_gh, "gh（GitHub CLI）", "GitHub連携を使う場合はインストールして gh auth login を実行してください")
    if has_gh:
        code, _ = sh_run("gh auth status", project, 20)
        line(code == 0, "gh のログイン", "gh auth login を実行してください")
    enc = locale.getpreferredencoding(False)
    print(f"  情報 OSの文字コード: {enc}（エンジンは子プロセスの出力を UTF-8 → {enc} → cp932 の順で読みます）")
    if sys.platform == "win32":
        print("  情報 Windows: Playwright はプロジェクト内にブラウザを入れる設定（PLAYWRIGHT_BROWSERS_PATH=0）を推奨します。"
              "init 時に gate.env に設定済みです。")
        if "WindowsApps" in sys.executable:
            print("  注意 Microsoft Store 版の Python です。AppData への書き込みが別の場所に振り替えられることがあります。")
    print("[aq-dlc] 問題は見つかりませんでした。" if ok else "[aq-dlc] NG の項目を直すと、途中で止まりにくくなります。")


# ------------------------------------------------------------------ main
COMMANDS = {
    "add-check": cmd_add_check, "remove-check": cmd_remove_check, "checks": cmd_checks,
    "req": cmd_req, "risk": cmd_risk, "verify": cmd_verify, "phase": cmd_phase,
    "wait": cmd_wait, "resume": cmd_resume, "review": cmd_review, "ask": cmd_ask,
    "memory-done": cmd_memory_done, "complete": cmd_complete, "abort": cmd_abort,
    "confirm": cmd_confirm, "ci": cmd_ci, "doctor": cmd_doctor,
    "agent-start-hook": cmd_agent_start_hook, "agent-stop-hook": cmd_agent_stop_hook,
    "setup": cmd_setup, "init": cmd_init, "create": cmd_create, "publish": cmd_publish, "new": cmd_new,
    "status": cmd_status, "gate": cmd_gate, "check": cmd_check, "memory": cmd_memory,
    "stop-hook": cmd_stop_hook, "guard-hook": cmd_guard_hook, "session-hook": cmd_session_hook,
}


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
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
            print(f"[aq-dlc] hookでエラーが発生しました（作業は継続します）: {e}", file=sys.stderr)
        return
    COMMANDS[name](list(sys.argv[2:]))


if __name__ == "__main__":
    main()
