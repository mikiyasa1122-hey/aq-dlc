"""全体設定・プロジェクトの初期化・アクティブなrunの登録。"""
import os
import shutil
import sys
import time

from .util import (deep_merge, die, info, load_json, norm, now_iso, pop_opt, read_text, save_json,
                   slugify, is_within)

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(PKG_DIR)
PLUGIN_ROOT = os.path.dirname(SCRIPTS_DIR)
ENGINE = os.path.join(SCRIPTS_DIR, "aqdlc.py")
TEMPLATES = os.path.join(PLUGIN_ROOT, "templates")


def home():
    return os.path.abspath(os.path.expanduser(os.environ.get("AQDLC_HOME", "~/.aq-dlc")))


def active_path():
    return os.path.join(home(), "active.json")


def engine_cmd():
    return f'python3 "{ENGINE}"'


def plugin_version():
    return (load_json(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), {}) or {}).get("version")


DEFAULTS = {
    "workspace_dir": "",
    "github": {"default_visibility": "private", "default_branch": "main"},
    "checks": [],
    "gate": {
        "default_timeout_sec": 600,
        "env": {},
        "ignore_dirs": [".git", ".aq-dlc", ".claude", "node_modules", ".venv", "venv",
                        "__pycache__", "dist", "build", "target", ".next", ".turbo", ".vite",
                        ".pytest_cache", ".mypy_cache", ".ruff_cache", "coverage", ".gradle",
                        "test-results", "playwright-report", "blob-report", ".nyc_output",
                        "htmlcov", ".cache", ".parcel-cache", ".svelte-kit"],
    },
    "automation": {
        "auto_start": True,
        "spec_review": True,
        "enforce_roles": True,
        "orchestrator_edits_code": False,
        "force_foreground_agents": True,
        "max_agent_stop_blocks": 3,
        "max_auto_continues": 30,
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


def ensure_home(verbose=False):
    created = []
    h = home()
    os.makedirs(h, exist_ok=True)
    cfg_path = os.path.join(h, "config.json")
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
    dst = os.path.join(h, "memory")
    for dirpath, _d, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for name in files:
            t = os.path.join(dst, rel, name)
            if not os.path.exists(t):
                shutil.copy2(os.path.join(dirpath, name), t)
                created.append(t)
    if verbose:
        info(f"home: {h}")
        for c in created:
            print(f"  + {c}")
        if not created:
            print("  （既に初期化済み）")


def config(project=None):
    cfg = deep_merge(DEFAULTS, load_json(os.path.join(home(), "config.json"), {}) or {})
    if project:
        cfg = deep_merge(cfg, load_json(os.path.join(project, ".aq-dlc", "config.json"), {}) or {})
    return cfg


def project_config_path(project):
    return os.path.join(project, ".aq-dlc", "config.json")


# ------------------------------------------------------------------ project discovery
def find_project(start):
    d = os.path.abspath(os.path.expanduser(start))
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


def register_active(project, run_id):
    a = load_json(active_path(), {}) or {}
    a.setdefault("projects", {})[project] = {"run_id": run_id, "updated": time.time()}
    save_json(active_path(), a)


def unregister_active(project):
    a = load_json(active_path(), {}) or {}
    if project in a.get("projects", {}):
        del a["projects"][project]
        save_json(active_path(), a)


def active_project_for(cwd):
    """セッションのフォルダに関係する、最近更新されたプロジェクト。"""
    if not cwd:
        return None
    p = find_project(cwd)
    if p:
        return p
    a = load_json(active_path(), {}) or {}
    best, best_t = None, -1
    for proj, inf in a.get("projects", {}).items():
        if os.path.isdir(proj) and (is_within(proj, cwd) or is_within(cwd, proj)) \
                and inf.get("updated", 0) > best_t:
            best, best_t = proj, inf.get("updated", 0)
    return best


# ------------------------------------------------------------------ init / create
GITIGNORE_MARK = "# --- AQ-DLC 標準 ---"


def ensure_gitignore(project):
    """AQ-DLC 標準の除外設定を1回だけ追記する（利用者の行は残す）。"""
    path = os.path.join(project, ".gitignore")
    block = read_text(os.path.join(TEMPLATES, "project", ".gitignore"), "")
    cur = read_text(path, "") or ""
    if GITIGNORE_MARK in cur:
        return False
    with open(path, "a", encoding="utf-8") as f:
        if cur and not cur.endswith("\n"):
            f.write("\n")
        f.write(("\n" if cur else "") + block)
    return True


def scaffold(target, name):
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
            text = read_text(os.path.join(dirpath, f), "")
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(text.replace("{app_name}", name))
            added.append(os.path.relpath(dst, target))
    save_json(os.path.join(target, ".aq-dlc", "project.json"), {
        "name": name, "slug": slugify(name) or "app", "created": now_iso(),
        "aqdlc_version": plugin_version(),
    })
    return added, kept


def cmd_setup(args):
    ensure_home(verbose=True)


def cmd_init(args):
    ensure_home()
    target = pop_opt(args, "--project") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(target):
        die(f"フォルダがありません: {target}")
    if norm(target) in (norm("~"), norm(os.sep)):
        die("ホームフォルダやルートは初期化できません。アプリ用のフォルダを開いてください。")
    existing = find_project(target)
    if existing and norm(existing) == norm(target):
        info(f"初期化済み: {target}")
        print(target)
        return
    if existing:
        die(f"既存のAQ-DLCプロジェクトの内側です: {existing}")
    name = " ".join(args).strip() or os.path.basename(target)
    others = [e for e in os.listdir(target) if not e.startswith(".")]
    added, kept = scaffold(target, name)
    if sys.platform == "win32":
        cpath = project_config_path(target)
        pc = load_json(cpath, {}) or {}
        pc.setdefault("gate", {}).setdefault("env", {}).setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
        save_json(cpath, pc)
    if ".gitignore" in kept and ensure_gitignore(target):
        print("  + .gitignore に標準の除外設定を追記")
    info(f"初期化しました: {target}")
    for a in added:
        print(f"  + {a}")
    for k in kept:
        print(f"  = {k}（既存のため変更なし）")
    if others:
        print(f"  ! 既存のファイルが {len(others)} 件あります。新規開発の前提と合うか確認してください。")
    print(target)


def cmd_create(args):
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
