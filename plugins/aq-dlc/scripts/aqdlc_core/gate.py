"""Quality Gate・コードの指紋・CI の確認。

証拠の鮮度は「内容の指紋」で判定する。
  - コード: プロジェクトのファイル内容（.gitignore と ignore_dirs を除く）
  - チェック: 名前・コマンド・種類・前提・タイムアウト・環境変数
  - 検証: コマンド・参照するチェックの指紋・手動/マージ後の区別
どれかが gate 実行時と変われば、その gate 結果は使えない。
"""
import json
import re
import os
import shutil
import time

from .util import fingerprint, info, run_cmd, sh_run, sha1_file, now_iso

KIND_ORDER = ["typecheck", "lint", "build", "unit", "integration", "other", "e2e"]
KINDS = set(KIND_ORDER)
LEVELS = {"unit", "integration", "e2e", "manual", "other"}


# ------------------------------------------------------------------ code fingerprint
# 依存関係・キャッシュ（どの階層にあってもソースではない）
DEP_DIRS = {".git", ".aq-dlc", ".claude", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", ".gradle", ".tox", ".nox"}


def source_files(project, cfg):
    """指紋の対象のファイル。

    - git で追跡しているファイルは、常に対象（ignore_dirs や .gitignore の設定で外れない）
    - 追跡していないファイルは、.gitignore 済みを除く
    - ignore_dirs（ビルド出力など）は、プロジェクト直下のフォルダ名にだけ適用する（src/build/ のような
      ソースのフォルダを誤って外さない）。依存関係・キャッシュのフォルダはどの階層でも外す
    """
    top_ignore = set(cfg["gate"]["ignore_dirs"])
    tracked, others = None, None
    if shutil.which("git") and os.path.isdir(project):
        code, out = sh_run("git rev-parse --is-inside-work-tree", project, 30)
        if code == 0 and out.strip() == "true":
            c1, o1 = sh_run("git ls-files -c -z", project, 120)
            c2, o2 = sh_run("git ls-files -o --exclude-standard -z", project, 120)
            if c1 == 0 and c2 == 0:
                tracked = [f for f in o1.split("\0") if f.strip()]
                others = [f for f in o2.split("\0") if f.strip()]
    if tracked is None:
        tracked, others = [], []
        for dirpath, dirnames, filenames in os.walk(project):
            rel_dir = os.path.relpath(dirpath, project)
            dirnames[:] = [d for d in dirnames if d not in DEP_DIRS and not (rel_dir == "." and d in top_ignore)]
            for name in filenames:
                others.append(os.path.relpath(os.path.join(dirpath, name), project))
    out = set()
    for f in tracked:
        rel = f.replace(os.sep, "/").strip()
        parts = rel.split("/")
        if parts[0] in (".aq-dlc", ".claude", ".git"):
            continue
        out.add(rel)
    for f in others:
        rel = f.replace(os.sep, "/").strip()
        parts = rel.split("/")
        if any(p in DEP_DIRS for p in parts[:-1]) or (len(parts) > 1 and parts[0] in top_ignore):
            continue
        out.add(rel)
    return sorted(out)


def _stat_key(st):
    # 更新時刻だけでなく ctime（utime で戻せない）と inode も比べる
    return [st.st_size, st.st_mtime_ns, getattr(st, "st_ctime_ns", 0), getattr(st, "st_ino", 0)]


def snapshot(project, cfg, previous=None, fresh=False):
    """{相対パス: [サイズ, 更新時刻, sha1, ctime, inode]}。

    fresh=True ならキャッシュを使わず全ファイルをハッシュし直す（gate の開始・終了、各工程の登録、完了判定）。
    fresh=False なら、サイズ・更新時刻・ctime・inode がすべて前回と同じファイルだけハッシュを再利用する。
    """
    previous = {} if fresh else (previous or {})
    snap = {}
    for rel in source_files(project, cfg):
        p = os.path.join(project, rel)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        key = _stat_key(st)
        old = previous.get(rel)
        if old and len(old) == 5 and [old[0], old[1], old[3], old[4]] == key:
            snap[rel] = old
            continue
        try:
            snap[rel] = [key[0], key[1], sha1_file(p), key[2], key[3]]
        except OSError:
            continue
    return snap


def code_fp(snap):
    return fingerprint(sorted([k, v[2]] for k, v in snap.items()))


def snapshot_diff(old, new):
    old, new = old or {}, new or {}
    changed = sorted(k for k in new if k in old and new[k][2] != old[k][2])
    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    return changed, added, removed


def describe_diff(old, new, limit=3):
    c, a, r = snapshot_diff(old, new)
    if not (c or a or r):
        return ""
    ex = (c + a + r)[:limit]
    return f"変更{len(c)}・追加{len(a)}・削除{len(r)}件（例: {', '.join(ex)}）"


# ------------------------------------------------------------------ fingerprints of conditions
def gate_env(cfg):
    return {k: str(v) for k, v in (cfg["gate"].get("env") or {}).items()}


def check_fp(c, cfg):
    return fingerprint({"name": c.get("name"), "command": c.get("command"), "kind": c.get("kind", "other"),
                        "needs": sorted(c.get("needs") or []), "timeout": c.get("timeout_sec"),
                        "env": gate_env(cfg)})


def checks_fp(cfg):
    return fingerprint(sorted(check_fp(c, cfg) for c in cfg.get("checks", [])))


PROTECTED_CLAUDE = re.compile(r"^\.claude/(settings[^/]*\.json$|agents/|hooks/|skills/|commands/)")


def protected_files_fp(project):
    """.claude の設定（hook の無効化など）・エージェント定義・hook・Skill の内容の指紋。"""
    root = os.path.join(project, ".claude")
    items = []
    if os.path.isdir(root):
        for dirpath, _d, files in os.walk(root):
            for name in files:
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, project).replace(os.sep, "/")
                if PROTECTED_CLAUDE.match(rel):
                    try:
                        items.append([rel, sha1_file(p)])
                    except OSError:
                        items.append([rel, "unreadable"])
    return fingerprint(sorted(items))


def config_fp(cfg, project=None):
    """実際に使われる設定の指紋: 全体設定＋プロジェクト設定、および .claude の設定・定義。

    指紋の対象・自動化の境界・hook の無効化などの改ざんを検出する。
    """
    from .project import DEFAULTS, EAQDL_SECTIONS
    # E-AQDL で追加した設定は、既定値のままなら含めない（更新前に始まった run の指紋を変えないため）
    eff = {k: v for k, v in cfg.items() if not k.startswith("_")
           and not (k in EAQDL_SECTIONS and v == DEFAULTS.get(k))}
    return fingerprint({"cfg": eff, "claude": protected_files_fp(project) if project else None})


def verification_fp(v, cfg):
    by_name = {c["name"]: c for c in cfg.get("checks", [])}
    refs = []
    for n in v.get("checks") or []:
        refs.append(check_fp(by_name[n], cfg) if n in by_name else f"missing:{n}")
    return fingerprint({"command": v.get("command"), "checks": refs, "manual": v.get("manual"),
                        "after_merge": bool(v.get("after_merge")), "level": v.get("level"),
                        "timeout": v.get("timeout_sec"),
                        "env": gate_env(cfg) if v.get("command") else None})


def is_automated(v):
    return bool(v.get("command") or v.get("checks"))


# ------------------------------------------------------------------ gate execution
def ordered_checks(checks):
    """種類の順（型→Lint→ビルド→単体→結合→E2E）に並べ、needs の前提を先に置く。"""
    by_name = {c["name"]: c for c in checks}
    base = sorted(checks, key=lambda c: KIND_ORDER.index(c.get("kind", "other"))
                  if c.get("kind", "other") in KINDS else KIND_ORDER.index("other"))
    order, seen, visiting = [], set(), set()

    def visit(c):
        n = c["name"]
        if n in seen:
            return
        if n in visiting:
            return  # 循環は実行時に「前提が未実行」として失敗させる
        visiting.add(n)
        for dep in implicit_needs(c, checks):
            if dep in by_name:
                visit(by_name[dep])
        visiting.discard(n)
        seen.add(n)
        order.append(c)

    for c in base:
        visit(c)
    return order


def implicit_needs(c, checks):
    needs = list(c.get("needs") or [])
    if c.get("kind") == "e2e":
        needs += [x["name"] for x in checks if x.get("kind") == "build" and x["name"] != c["name"]]
    return list(dict.fromkeys(needs))


def run_gate(project, state, cfg, iter_verifications, log_dir, stamp, after_merge=False):
    checks = cfg.get("checks", [])
    env = gate_env(cfg)
    default_to = int(cfg["gate"]["default_timeout_sec"])
    results = {"checks": {}, "verifications": {}}
    os.makedirs(log_dir, exist_ok=True)
    build_ok = True

    def record(kind, key, cmd, timeout):
        code, out, dur = run_cmd(cmd, project, timeout, env)
        log = os.path.join(log_dir, f"{stamp}-{kind}-{key}.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write(f"$ {cmd}\nexit={code}\n\n{out}")
        return {"pass": code == 0, "exit": code, "sec": dur, "log": os.path.relpath(log, project)}

    for c in ordered_checks(checks):
        fp = check_fp(c, cfg)
        needs = implicit_needs(c, checks)
        blocked = [n for n in needs if n not in results["checks"] or not results["checks"][n]["pass"]]
        if blocked:
            r = {"pass": False, "skipped": f"前提のチェック（{', '.join(blocked)}）が不合格・未登録のため実行しない"}
            print(f"  中止 チェック:{c['name']}（前提 {', '.join(blocked)} が不合格）")
        else:
            r = record("check", c["name"], c["command"], int(c.get("timeout_sec", default_to)))
            print(f"  {'合格' if r['pass'] else '不合格'} チェック:{c['name']}（{r['sec']}秒）")
        r["fp"] = fp
        r["kind"] = c.get("kind", "other")
        results["checks"][c["name"]] = r
        if c.get("kind") == "build" and not r["pass"]:
            build_ok = False

    for _req, _parent, v in iter_verifications(state):
        vid = v.get("id", "?")
        vfp = verification_fp(v, cfg)
        if v.get("after_merge") and not after_merge:
            r = {"pass": False, "pending_after_merge": True}
            print(f"  保留 検証:{vid}（マージ後に gate --after-merge で実行）")
        elif not is_automated(v):
            r = {"pass": False, "manual": bool(v.get("manual")),
                 "error": None if v.get("manual") else "実行方法（command / checks）が未設定"}
            print(f"  {'人の確認' if v.get('manual') else '未設定'} 検証:{vid}")
        elif v.get("command"):
            if v.get("level") == "e2e" and not build_ok:
                r = {"pass": False, "skipped": "ビルドが不合格のため E2E を実行しない（古い成果物での合格を防ぐ）"}
                print(f"  中止 検証:{vid}（ビルド不合格）")
            else:
                r = record("verify", vid, v["command"], int(v.get("timeout_sec", default_to)))
                print(f"  {'合格' if r['pass'] else '不合格'} 検証:{vid}")
        else:
            missing = [n for n in v["checks"] if n not in results["checks"]]
            ok = not missing and all(results["checks"][n]["pass"] for n in v["checks"])
            r = {"pass": ok, "via_checks": v["checks"], "missing_checks": missing}
            print(f"  {'合格' if ok else '不合格'} 検証:{vid}（チェック {', '.join(v['checks'])}）")
        r["fp"] = vfp
        results["verifications"][vid] = r

    checks_pass = bool(checks) and all(r["pass"] for r in results["checks"].values())
    return results, checks_pass


# ------------------------------------------------------------------ CI
def check_ci(project, wait_min):
    """現在の HEAD に対する GitHub Actions の結果。戻り値: (record, message)"""
    if not os.path.isdir(os.path.join(project, ".github", "workflows")):
        return None, ".github/workflows がないため、CI の確認は不要です。"
    if shutil.which("gh") is None:
        return None, "gh がないため CI を確認できません。"
    _code, sha = sh_run("git rev-parse HEAD", project, 30)
    sha = sha.strip()
    deadline = time.time() + wait_min * 60
    runs, pending = [], []
    while True:
        code, out = sh_run(f"gh run list --commit {sha} --json name,status,conclusion,url --limit 20", project, 60)
        runs = []
        if code == 0:
            try:
                runs = json.loads(out or "[]")
            except json.JSONDecodeError:
                runs = []
        pending = [r for r in runs if r.get("status") != "completed"]
        if (runs and not pending) or time.time() >= deadline:
            break
        time.sleep(20)
    failed = [r for r in runs if r.get("status") == "completed"
              and r.get("conclusion") not in ("success", "skipped", "neutral")]
    result = "success" if runs and not pending and not failed else ("failure" if failed else "pending")
    rec = {"sha": sha, "result": result, "checked_at": now_iso(),
           "runs": [{k: r.get(k) for k in ("name", "status", "conclusion", "url")} for r in runs]}
    return rec, None


def print_ci(rec):
    label = {"success": "成功", "failure": "失敗", "pending": "未完了（実行待ち・実行中・未検出）"}[rec["result"]]
    info(f"CI（{rec['sha'][:7]}）: {label}")
    for r in rec["runs"]:
        print(f"  {r.get('name')}: {r.get('conclusion') or r.get('status')}  {r.get('url')}")


# ------------------------------------------------------------------ テストの兆候（判断材料。合否は決めない）
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e)/|(_test|\.test|\.spec|Test|Tests)\.[A-Za-z]+$"
                       r"|(^|/)test_[^/]+\.py$")
SKIP_RE = re.compile(r"\.skip\s*\(|\bxit\s*\(|\bxdescribe\s*\(|\bxtest\s*\(|@(unittest\.)?skip|pytest\.mark\.(skip|xfail)"
                     r"|\bt\.Skip\(|#\[ignore\]|@Disabled\b|@Ignore\b|\.todo\s*\(|\.fixme\s*\(")
ONLY_RE = re.compile(r"\b(it|test|describe)\.only\s*\(|\bfit\s*\(|\bfdescribe\s*\(")
ASSERT_RE = re.compile(r"assert|expect\s*\(|\.should\b|\.to(Be|Equal|Have|Contain|Match|Throw)|require\.|"
                       r"\bt\.(Error|Fatal|Fail)|XCTAssert|check\s*\(|verify\s*\(", re.I)
TEST_DEF_RE = re.compile(r"\bdef\s+test_|\b(it|test)\s*\(|\bfunc\s+Test|#\[test\]|@Test\b|\bfn\s+test_")
CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb",
            ".php", ".cs", ".vue", ".svelte")


def test_signals(project, files):
    """skip・.only・アサーションのないテストファイルを機械的に検出する（意味の判断はレビュー担当が行う）。"""
    out = []
    for rel in files:
        if not rel.endswith(CODE_EXT) or not TEST_FILE.search(rel):
            continue
        try:
            with open(os.path.join(project, rel), encoding="utf-8", errors="replace") as f:
                text = f.read(400000)
        except OSError:
            continue
        if SKIP_RE.search(text):
            out.append({"file": rel, "issue": "skip / todo / xfail されたテストがある"})
        if ONLY_RE.search(text):
            out.append({"file": rel, "issue": ".only などで一部のテストだけを実行する指定がある"})
        if TEST_DEF_RE.search(text) and not ASSERT_RE.search(text):
            out.append({"file": rel, "issue": "テストの定義はあるが、アサーションが見当たらない"})
    return out


WEB_DEPS = re.compile(r'"(react|react-dom|vue|svelte|next|nuxt|@angular/core|vite|solid-js|preact|astro)"\s*:')


def looks_like_web_app(project, files):
    if "package.json" in files:
        try:
            with open(os.path.join(project, "package.json"), encoding="utf-8") as f:
                if WEB_DEPS.search(f.read()):
                    return True
        except OSError:
            pass
    return any(f.endswith(("index.html", ".html")) and not f.startswith(("node_modules/", "dist/")) for f in files)
