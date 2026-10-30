"""Counterfactual Gate（反証テスト）: テストが欠陥を見つけられるかを実際に確かめる。

通常の Quality Gate の合格後に、要求・リスクに結び付いた実装ファイルへ決定論的な Mutation
（mutation.py）を1つずつ埋め込み、その検証（V）のコマンドが失敗するかを調べる。

  killed          対象のテストが欠陥を検出した（テストの失敗として確認できた）
  survived        欠陥があってもテストが通過した（テストの不足、または等価な変異）
  invalid         Mutation の不正・構文エラー・テストの起動失敗・ベースライン不合格など（検証不成立）
  timeout         時間切れ
  not_applicable  対象の技術・条件に適用できない

安全のための方針:
  - 元の作業ツリーは変更しない。プロジェクトの外（~/.aq-dlc/tmp/cf/）に作った一時コピーで実行し、
    終了時（異常終了・中断を含む）に削除する。前回の残りは次回の開始時に掃除する。
  - 実行するのは、人間が承認した設定に登録済みの検証コマンド（V の command / checks）だけ。
    Mutation は1行の決定論的な書き換えで、任意のコマンドや LLM の生成したパッチは実行しない。
  - 子プロセスには資格情報らしい環境変数を渡さない。HTTP(S) のプロキシを使えないアドレスに向け、
    外部への通信を原則として失敗させる（OS のサンドボックスではないため、完全な遮断は保証しない）。
  - 時間・Mutation の数に上限を設ける。
"""
import os
import re
import shutil
import signal
import sys
import time

from . import gate as G
from . import mutation as MU
from .util import fingerprint, now_iso, run_cmd, sha1_file, sha1_text, write_text

RESULTS = ("killed", "survived", "invalid", "timeout", "not_applicable")
DEFAULTS = {"mode": "off", "timeout_sec": 300, "max_mutations": 12, "per_verification": 2,
            "auto_plan": True, "copy_extra": []}
SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE|AUTH|SESSION|COOKIE|"
                        r"DATABASE_URL|DB_URL|CONN(ECTION)?_STR)", re.I)
SECRET_PREFIX = ("AWS_", "GH_", "GITHUB_", "AZURE_", "GOOGLE_", "GCP_", "GCLOUD_", "OPENAI", "ANTHROPIC",
                 "NPM_TOKEN", "HEROKU_", "VERCEL_", "NETLIFY_", "STRIPE_", "SLACK_", "TWILIO_", "SENTRY_")
LINK_DIRS = ("node_modules", ".venv", "venv")
# テストの起動失敗・環境の問題（欠陥の検出ではない）
INVALID_OUT = re.compile(
    r"SyntaxError|IndentationError|TabError|Cannot find module|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|"
    r"No module named|error TS\d{3,5}|command not found|is not recognized as an internal or external command|"
    r"collected 0 items|no tests ran|No tests found|No test files found|ERROR: file or directory not found|"
    r"Ran 0 tests|ImportError while importing test module|Failed to import test module|unittest\.loader\._FailedTest|"
    r"ENOENT", re.I)
# テストの失敗（期待値の違反・テスト中の例外）
FAIL_OUT = re.compile(
    r"AssertionError|assert(ion)?\s|FAILED|FAIL\b|\bfailed\b|failures=|errors=|not ok\b|✗|✕|×|Expected|"
    r"expect\(|toBe|toEqual|toThrow|Traceback|Error:", re.I)


def cf_config(cfg):
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("counterfactual") or {})
    if out["mode"] not in ("off", "advisory", "enforce"):
        out["mode"] = "off"
    return out


# ------------------------------------------------------------------ 計画（決定論的）
RISK_HINTS = [
    (re.compile(r"権限|認可|認証|ログイン|なりすまし|auth|permission|role|admin", re.I), ("guard_off", "negate_if")),
    (re.compile(r"重複|二重|冪等|同時|already|duplicate|idempot", re.I), ("guard_off", "negate_if")),
    (re.compile(r"境界|上限|下限|以上|以下|超え|未満|最大|最小|件数|limit|max|min", re.I), ("boundary",)),
    (re.compile(r"空|不正|入力|検証|バリデーション|形式|invalid|empty|valid", re.I), ("guard_off", "remove_raise")),
    (re.compile(r"エラー|例外|失敗|異常|error|fail|exception", re.I), ("remove_raise", "guard_off")),
    (re.compile(r"更新|削除|DB|データベース|整合|update|delete", re.I), ("sql_where", "eq_flip")),
]


def _preferred_ops(text):
    pref = []
    for rx, ops in RISK_HINTS:
        if rx.search(text or ""):
            pref += [o for o in ops if o not in pref]
    return pref


def targets_of(state):
    """(要求, 親（AC/K）, 検証) のうち、反証の対象にできるもの（自動の検証で、マージ後でないもの）。"""
    out = []
    for r in state.get("requirements", []):
        for p in r.get("risks", []) + r.get("acceptance", []):
            for v in p.get("verifications", []):
                if G.is_automated(v) and not v.get("after_merge"):
                    out.append((r, p, v))
    # 重要な要求 → リスク → 受け入れ条件の順
    out.sort(key=lambda t: (not t[0].get("critical"), t[1] not in t[0].get("risks", []), t[0]["id"], t[2]["id"]))
    return out


def _read_lines(project, rel):
    path = os.path.join(project, rel)
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read().splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None


def plan(project, state, cfg, source=None):
    """反証テストの計画。戻り値: (mutations, notes)。

    mutations: [{id, verification, parent, requirement, risk, operator, category, file, line, original,
                 mutated, expected, origin}]
    """
    cc = cf_config(cfg)
    source = set(source if source is not None else G.source_files(project, cfg))
    vmap = {v["id"]: (r, p, v) for r, p, v in targets_of(state)}
    muts, notes, used = [], [], set()

    def add(r, p, v, rel, i, op, lines, origin):
        lang = MU.language(rel)
        new = MU.apply(op, lines, i, lang)
        if new is None:
            return False
        key = (v["id"], rel, i, op)
        if key in used:
            return False
        used.add(key)
        is_risk = p in r.get("risks", [])
        muts.append({"verification": v["id"], "parent": p["id"], "requirement": r["id"],
                     "risk": p["id"] if is_risk else None, "critical": bool(r.get("critical")),
                     "operator": op, "category": MU.category(op, lines[i]), "file": rel, "line": i + 1,
                     "original": lines[i].rstrip("\r\n"), "mutated": new.rstrip("\r\n"),
                     "original_hash": sha1_text(lines[i]),
                     "expected": f"{v['id']} の検証（{p['id']}: {(p.get('text') or '')[:60]}）が失敗する",
                     "origin": origin})
        return True

    # 1) 明示的に追加された対象（cf-add）
    for t in (state.get("counterfactual") or {}).get("targets", []):
        if t["verification"] not in vmap:
            notes.append(f"{t['id']}: 検証 {t['verification']} が反証の対象外（自動の検証ではない・存在しない）")
            continue
        r, p, v = vmap[t["verification"]]
        lines = _read_lines(project, t["file"])
        if lines is None or t["file"] not in source:
            notes.append(f"{t['id']}: {t['file']} を読めない（存在しない・シンボリックリンク・対象外）")
            continue
        if not add(r, p, v, t["file"], t["line"] - 1, t["operator"], lines, t["id"]):
            notes.append(f"{t['id']}: {t['file']}:{t['line']} に {t['operator']} を適用できない")
    # 2) 自動の計画
    if cc.get("auto_plan", True):
        per_v = int(cc["per_verification"])
        impl_map = (state.get("impl") or {}).get("map", {})
        for r, p, v in targets_of(state):
            files = [f for f in impl_map.get(r["id"], []) if MU.language(f) and not MU.is_test_file(f) and f in source]
            if not files:
                continue
            pref = _preferred_ops(" ".join([p.get("text") or "", v.get("method") or ""]))
            ops = pref + [o for o in MU.OPERATORS if o not in pref]
            count = sum(1 for m in muts if m["verification"] == v["id"])
            for op in ops:
                if count >= per_v:
                    break
                for rel in files:
                    lines = _read_lines(project, rel)
                    if lines is None:
                        continue
                    done = False
                    for i, o in MU.sites(lines, MU.language(rel), (op,)):
                        if any(m["file"] == rel and m["line"] == i + 1 and m["verification"] == v["id"] for m in muts):
                            continue
                        if add(r, p, v, rel, i, o, lines, "auto"):
                            count += 1
                            done = True
                            break
                    if done:
                        break
    limit = int(cc["max_mutations"])
    if len(muts) > limit:
        notes.append(f"Mutation の数が上限（{limit}）を超えたため、{len(muts) - limit} 件を実行しない")
        muts = muts[:limit]
    for n, m in enumerate(muts, 1):
        m["id"] = f"M{n}"
    return muts, notes


def definition_fp(state, cfg, muts):
    cc = cf_config(cfg)
    return fingerprint({"version": MU.VERSION, "cfg": {k: cc[k] for k in sorted(cc) if k != "mode"},
                        "targets": (state.get("counterfactual") or {}).get("targets", []),
                        "muts": [[m["verification"], m["file"], m["line"], m["operator"], m["original_hash"]]
                                 for m in muts]})


# ------------------------------------------------------------------ 隔離環境
def sandbox_root():
    from .project import home
    return os.path.join(home(), "tmp", "cf")


def _rmtree(path):
    def onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)


def cleanup_stale(max_age=6 * 3600):
    root = sandbox_root()
    removed = 0
    if not os.path.isdir(root):
        return 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        marker = os.path.join(d, ".aqdlc-cf-owner")
        alive = False
        try:
            pid = int(open(marker, encoding="utf-8").read().strip() or 0)
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    alive = True
                except (OSError, SystemError, ValueError):
                    alive = False
        except (OSError, ValueError):
            pass
        try:
            old = time.time() - os.path.getmtime(d) > max_age
        except OSError:
            old = True
        if not alive or old:
            _unlink_dep_links(d)
            _rmtree(d)
            removed += 1
    return removed


def _unlink_dep_links(d):
    """依存フォルダへのリンクを先に外す（リンク先の実体を消さないため）。"""
    for name in LINK_DIRS:
        p = os.path.join(d, name)
        try:
            if os.path.islink(p):
                os.unlink(p)
            elif os.name == "nt" and os.path.isdir(p) and _is_junction(p):
                os.rmdir(p)
        except OSError:
            pass


def _is_junction(p):
    try:
        return bool(os.readlink(p))
    except (OSError, ValueError, AttributeError):
        return False


def _link_dir(src, dst):
    try:
        os.symlink(src, dst, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(src, dst)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def make_sandbox(project, cfg, files, stamp):
    """プロジェクトのコピーを作る。戻り値: (path, notes)"""
    root = sandbox_root()
    os.makedirs(root, exist_ok=True)
    d = os.path.join(root, f"{stamp}-{os.getpid()}")
    os.makedirs(d)
    write_text(os.path.join(d, ".aqdlc-cf-owner"), str(os.getpid()))
    notes = []
    real_project = os.path.realpath(project)
    cc = cf_config(cfg)
    extra = []
    for pat in cc.get("copy_extra") or []:
        import glob
        for p in glob.glob(os.path.join(project, pat), recursive=True):
            rel = os.path.relpath(p, project).replace(os.sep, "/")
            if not rel.startswith("..") and os.path.isfile(p):
                extra.append(rel)
    for rel in list(files) + extra:
        if ".." in rel.split("/"):
            notes.append(f"{rel}: パスが不正なのでコピーしない")
            continue
        src = os.path.join(project, rel)
        real = os.path.realpath(src)
        if not (real == real_project or real.startswith(real_project.rstrip(os.sep) + os.sep)):
            notes.append(f"{rel}: プロジェクトの外を指すシンボリックリンクなのでコピーしない")
            continue
        if not os.path.isfile(real):
            continue
        dst = os.path.join(d, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copyfile(real, dst)
            shutil.copymode(real, dst)
        except OSError as e:
            notes.append(f"{rel}: コピーできない（{e}）")
    for name in LINK_DIRS:
        src = os.path.join(project, name)
        if os.path.isdir(src) and not os.path.exists(os.path.join(d, name)):
            if not _link_dir(os.path.realpath(src), os.path.join(d, name)):
                notes.append(f"{name}: 隔離環境にリンクできない（テストが依存関係を読めない可能性）")
    return d, notes


def safe_env(cfg):
    env = {}
    for k, v in os.environ.items():
        if SECRET_ENV.search(k) or k.upper().startswith(SECRET_PREFIX):
            continue
        env[k] = v
    env.update({"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "localhost,127.0.0.1,::1",
                "no_proxy": "localhost,127.0.0.1,::1", "AQDLC_COUNTERFACTUAL": "1", "CI": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(G.gate_env(cfg))  # 人間が設定したテスト用の環境変数（テスト用 DB など）
    return env


def env_fp(cfg):
    return fingerprint({"gate_env": G.gate_env(cfg), "platform": sys.platform,
                        "python": sys.version.split()[0]})


def mask(text, env_removed):
    out = text or ""
    for v in env_removed:
        if v and len(v) >= 6:
            out = out.replace(v, "＊＊＊")
    out = re.sub(r"(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})",
                 "＊＊＊", out)
    return out


# ------------------------------------------------------------------ 実行
def commands_of(v, cfg):
    if v.get("command"):
        return [v["command"]]
    by = {c["name"]: c for c in cfg.get("checks", [])}
    return [by[n]["command"] for n in v.get("checks") or [] if n in by]


def run_commands(cmds, cwd, timeout, env):
    """戻り値: (exit, output, sec, timed_out)"""
    out, total = [], 0.0
    for c in cmds:
        code, o, sec = run_cmd_env(c, cwd, timeout, env)
        total += sec
        out.append(f"$ {c}\nexit={code}\n{o}")
        if code == 124 and "タイムアウト" in o:
            return code, "\n".join(out), total, True
        if code != 0:
            return code, "\n".join(out), total, False
    return 0, "\n".join(out), total, False


def run_cmd_env(cmd, cwd, timeout, env):
    import subprocess
    from .util import decode
    start = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=timeout, env=env)
        return p.returncode, decode(p.stdout) + decode(p.stderr), round(time.time() - start, 2)
    except subprocess.TimeoutExpired:
        return 124, f"タイムアウト（{timeout}秒）", round(time.time() - start, 2)


def classify(code, out, timed_out):
    if timed_out:
        return "timeout", "時間切れ"
    if code == 0:
        return "survived", "欠陥があってもテストが通過した"
    if code in (126, 127):
        return "invalid", "テストを起動できない（コマンドが見つからない・実行できない）"
    m = INVALID_OUT.search(out or "")
    if m:
        return "invalid", f"テストの起動・読み込みの失敗（{m.group(0)}）。欠陥の検出とは扱わない"
    m = FAIL_OUT.search(out or "")
    if m:
        return "killed", f"テストが失敗した（{m.group(0).strip()}）"
    return "invalid", "失敗の内容がテストの期待値の違反として確認できない"


class _Interrupted(Exception):
    pass


def execute(project, state, cfg, muts, log_dir, stamp, only=None):
    """反証テストを実行する。戻り値: 実行の記録（dict）。"""
    cc = cf_config(cfg)
    rec = {"stamp": stamp, "at": now_iso(), "mode": cc["mode"], "mutations": [], "notes": [],
           "status": "running"}
    vmap = {v["id"]: (r, p, v) for r, p, v in targets_of(state)}
    files = G.source_files(project, cfg)
    removed_env = [v for k, v in os.environ.items() if SECRET_ENV.search(k) or k.upper().startswith(SECRET_PREFIX)]
    env = safe_env(cfg)
    os.makedirs(log_dir, exist_ok=True)
    cleanup_stale()
    before = G.snapshot(project, cfg, fresh=True)
    sandbox = None
    prev_term = None

    def on_term(_sig, _frm):
        raise _Interrupted()
    try:
        prev_term = signal.signal(signal.SIGTERM, on_term)
    except (ValueError, OSError, AttributeError):
        prev_term = None
    try:
        sandbox, notes = make_sandbox(project, cfg, files, stamp)
        rec["notes"] += notes
        tmax = int(cc["timeout_sec"])
        baselines = {}
        todo = [m for m in muts if not only or m["id"] in only]
        for m in todo:
            m = dict(m)
            vid = m["verification"]
            r, p, v = vmap.get(vid, (None, None, None))
            log = os.path.join(log_dir, f"{stamp}-{m['id']}.log")
            m.update(result=None, reason="", log=os.path.relpath(log, project), sec=0)
            if v is None:
                m.update(result="not_applicable", reason="検証が反証の対象外になった")
                rec["mutations"].append(m)
                continue
            cmds = commands_of(v, cfg)
            m["command"] = " && ".join(cmds)
            if not cmds:
                m.update(result="not_applicable", reason="実行するコマンドがない")
                rec["mutations"].append(m)
                continue
            if vid not in baselines:
                code, out, sec, to = run_commands(cmds, sandbox, tmax, env)
                baselines[vid] = {"pass": code == 0 and not to, "sec": sec, "exit": code}
                write_text(os.path.join(log_dir, f"{stamp}-baseline-{vid}.log"), mask(out, removed_env))
            base = baselines[vid]
            if not base["pass"]:
                m.update(result="invalid", reason=f"ベースライン（欠陥を入れる前）で {vid} が不合格（exit {base['exit']}）。"
                                                  "隔離環境で検証が成立しない")
                rec["mutations"].append(m)
                continue
            target = os.path.join(sandbox, m["file"])
            try:
                with open(target, encoding="utf-8", newline="") as f:
                    lines = f.read().splitlines(keepends=True)
            except (OSError, UnicodeDecodeError) as e:
                m.update(result="invalid", reason=f"対象のファイルを読めない（{e}）")
                rec["mutations"].append(m)
                continue
            i = m["line"] - 1
            if i >= len(lines) or sha1_text(lines[i]) != m["original_hash"]:
                m.update(result="invalid", reason="対象の行が計画の時点から変わった")
                rec["mutations"].append(m)
                continue
            lang = MU.language(m["file"])
            new = MU.apply(m["operator"], lines, i, lang)
            if new is None:
                m.update(result="not_applicable", reason=f"{m['operator']} をこの行に適用できない")
                rec["mutations"].append(m)
                continue
            mutated = lines[:i] + [new] + lines[i + 1:]
            text = "".join(mutated)
            ok, why = MU.syntax_ok(text, lang, m["file"])
            if not ok:
                m.update(result="invalid", reason=why)
                rec["mutations"].append(m)
                continue
            original = "".join(lines)
            try:
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                to_sec = min(tmax, max(30, int(base["sec"] * 3) + 10))
                code, out, sec, to = run_commands(cmds, sandbox, to_sec, env)
            finally:
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(original)
                _clear_pycache(sandbox, m["file"])
            res, why = classify(code, out, to)
            m.update(result=res, reason=why, exit=code, sec=sec)
            write_text(log, f"# {m['id']} {m['operator']} {m['file']}:{m['line']}\n- {m['original']}\n+ {m['mutated']}\n\n"
                       + mask(out, removed_env))
            rec["mutations"].append(m)
            print(f"  {m['id']} {res:9} {m['file']}:{m['line']} {m['category']}（{vid}）")
        # 終了時にベースラインをもう一度確かめる（環境の不調で killed になったものを除くため）
        for vid, base in baselines.items():
            if not base["pass"] or not any(x["verification"] == vid and x["result"] == "killed" for x in rec["mutations"]):
                continue
            code, out, _sec, to = run_commands(commands_of(vmap[vid][2], cfg), sandbox, tmax, env)
            if code != 0 or to:
                for x in rec["mutations"]:
                    if x["verification"] == vid and x["result"] == "killed":
                        x.update(result="invalid", reason="終了時のベースラインの再確認で不合格（環境の不調の可能性）")
        rec["status"] = "done"
    except (KeyboardInterrupt, _Interrupted):
        rec["status"] = "interrupted"
        rec["notes"].append("中断された（結果は使わない）")
    finally:
        if prev_term is not None:
            try:
                signal.signal(signal.SIGTERM, prev_term)
            except (ValueError, OSError):
                pass
        if sandbox:
            _unlink_dep_links(sandbox)
            _rmtree(sandbox)
            rec["cleaned"] = not os.path.exists(sandbox)
    after = G.snapshot(project, cfg, fresh=True)
    if G.code_fp(before) != G.code_fp(after):
        rec["status"] = "modified"
        rec["notes"].append("反証テストの実行中に元の作業ツリーが変わった（結果は使わない）: "
                            + G.describe_diff(before, after))
    rec["code_fp"] = G.code_fp(before)
    return rec


def _clear_pycache(sandbox, rel):
    d = os.path.join(sandbox, os.path.dirname(rel), "__pycache__")
    if os.path.isdir(d):
        _rmtree(d)


# ------------------------------------------------------------------ 集計・鮮度
def summarize(rec):
    c = {k: 0 for k in RESULTS}
    eq = 0
    for m in (rec or {}).get("mutations", []):
        if m.get("equivalent"):
            eq += 1
            continue
        if m.get("result") in c:
            c[m["result"]] += 1
    valid = c["killed"] + c["survived"]
    score = round(c["killed"] / valid, 3) if valid else None
    return {"counts": c, "equivalent": eq, "valid": valid, "score": score,
            "excluded": c["invalid"] + c["timeout"] + c["not_applicable"] + eq}


def current(state):
    return (state.get("counterfactual") or {}).get("latest")


def record_fp(state):
    """最新の反証テストの記録の指紋（レビューがどの結果を見たかの確認に使う）。"""
    rec = current(state)
    if not rec:
        return None
    return fingerprint({"stamp": rec.get("stamp"), "fp": rec.get("fp"),
                        "res": [[m.get("id"), m.get("result"), bool(m.get("equivalent"))]
                                for m in rec.get("mutations", [])]})


def mutation_key(m):
    return f"{m['verification']}|{m['file']}|{m['line']}|{m['operator']}|{m['original_hash'][:12]}"


def fp_now(ctx, muts=None):
    """反証テストの結果を使える条件の指紋（コード・チェック・検証条件・Mutation の定義・環境）。"""
    if muts is None:
        muts, _ = plan(ctx.project, ctx.state, ctx.cfg, source=list(ctx.snap))
    return fingerprint({"code": ctx.code_fp, "checks": ctx.checks_fp, "graph": ctx.graph_fp,
                        "def": definition_fp(ctx.state, ctx.cfg, muts), "env": env_fp(ctx.cfg)})


def stale_reasons(ctx, rec=None):
    rec = rec or current(ctx.state)
    if not rec:
        return ["未実行"]
    out = []
    if rec.get("status") != "done":
        out.append({"interrupted": "中断された", "modified": "実行中に作業ツリーが変わった"}.get(rec.get("status"),
                                                                                              "完了していない"))
    if rec.get("code_fp") != ctx.code_fp:
        out.append("コード")
    if rec.get("checks_fp") != ctx.checks_fp:
        out.append("チェックの定義")
    if rec.get("graph_fp") != ctx.graph_fp:
        out.append("検証条件")
    muts, _ = plan(ctx.project, ctx.state, ctx.cfg, source=list(ctx.snap))
    if rec.get("def_fp") != definition_fp(ctx.state, ctx.cfg, muts):
        out.append("Mutation の定義")
    if rec.get("env_fp") != env_fp(ctx.cfg):
        out.append("実行環境")
    return out


def by_verification(rec):
    out = {}
    for m in (rec or {}).get("mutations", []):
        out.setdefault(m["verification"], []).append(m)
    return out


def critical_gaps(state, rec):
    """重要な要求の検証のうち、反証テストで検出を確かめられていないもの。

    戻り値: [(V, 理由, survived の Mutation ID の一覧)]
    """
    bv = by_verification(rec)
    gaps = []
    for r, p, v in targets_of(state):
        if not r.get("critical"):
            continue
        ms = [m for m in bv.get(v["id"], []) if not m.get("equivalent")]
        surv = [m["id"] for m in ms if m.get("result") == "survived"]
        if surv:
            gaps.append((v["id"], f"欠陥を見逃した（{', '.join(surv)}）", surv))
        elif ms and not any(m.get("result") == "killed" for m in ms) \
                and not all(m.get("result") == "not_applicable" for m in ms):
            gaps.append((v["id"], "反証テストが成立しなかった（invalid / timeout のみ）", []))
    return gaps
