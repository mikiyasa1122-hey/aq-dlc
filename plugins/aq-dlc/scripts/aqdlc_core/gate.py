"""Quality Gate・コードの指紋・CI の確認。

証拠の鮮度は「内容の指紋」で判定する。
  - コード: プロジェクトのファイル内容（.gitignore と ignore_dirs を除く）
  - チェック: 名前・コマンド・種類・前提・タイムアウト・環境変数
  - 検証: コマンド・参照するチェックの指紋・手動/マージ後の区別
どれかが gate 実行時と変われば、その gate 結果は使えない。
"""
import json
import os
import shutil
import time

from .util import fingerprint, info, run_cmd, sh_run, sha1_file, now_iso

KIND_ORDER = ["typecheck", "lint", "build", "unit", "integration", "other", "e2e"]
KINDS = set(KIND_ORDER)
LEVELS = {"unit", "integration", "e2e", "manual", "other"}


# ------------------------------------------------------------------ code fingerprint
def source_files(project, cfg):
    ignore = set(cfg["gate"]["ignore_dirs"]) | {".aq-dlc", ".claude", ".git"}
    files = None
    if shutil.which("git") and os.path.isdir(project):
        code, out = sh_run("git rev-parse --is-inside-work-tree", project, 30)
        if code == 0 and out.strip() == "true":
            code, out = sh_run("git ls-files -co --exclude-standard -z", project, 120)
            if code == 0:
                files = [f for f in out.split("\0") if f.strip()]
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(project):
            dirnames[:] = [d for d in dirnames if d not in ignore]
            for name in filenames:
                files.append(os.path.relpath(os.path.join(dirpath, name), project))
    out = set()
    for f in files:
        rel = f.replace(os.sep, "/").strip()
        parts = rel.split("/")
        if any(p in ignore for p in parts[:-1]):
            continue
        out.add(rel)
    return sorted(out)


def snapshot(project, cfg, previous=None):
    """{相対パス: [サイズ, 更新時刻, sha1]}。サイズと更新時刻が同じなら前回のハッシュを再利用する。"""
    previous = previous or {}
    snap = {}
    for rel in source_files(project, cfg):
        p = os.path.join(project, rel)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        old = previous.get(rel)
        if old and old[0] == st.st_size and abs(old[1] - st.st_mtime) < 1e-6:
            snap[rel] = old
            continue
        try:
            snap[rel] = [st.st_size, st.st_mtime, sha1_file(p)]
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
