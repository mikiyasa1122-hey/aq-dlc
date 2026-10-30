"""共通処理: 文字コード、子プロセス、JSON、ハッシュ、引数の解析。"""
import datetime as _dt
import hashlib
import json
import locale
import os
import re
import subprocess
import sys
import time


def decode(b):
    """子プロセスの出力を読む。UTF-8 → OSの文字コード → cp932 の順に試す。"""
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
    """シェルコマンドを実行し (終了コード, 出力) を返す。"""
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=timeout,
                       env=child_env(env))
    return p.returncode, decode(p.stdout) + decode(p.stderr)


def run_cmd(cmd, cwd, timeout, env=None):
    start = time.time()
    try:
        code, out = sh_run(cmd, cwd, timeout, env)
    except subprocess.TimeoutExpired:
        code, out = 124, f"タイムアウト（{timeout}秒）"
    return code, out, round(time.time() - start, 2)


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def info(msg):
    print(f"[E-AQDL] {msg}")


def die(msg, code=1):
    print(f"[E-AQDL] {msg}", file=sys.stderr)
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
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


class FileLock:
    """複数のプロセス（hook とコマンド）が同じファイルを同時に書き換えないための排他ロック。

    ロックファイルを O_EXCL で作る。古いロック（stale 秒以上前）は異常終了の残りとして消す。
    """

    def __init__(self, path, timeout=5.0, stale=30.0):
        self.path, self.timeout, self.stale = path + ".lock", timeout, stale
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError(f"ロックを取得できません: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
            os.remove(self.path)
        except OSError:
            pass
        return False


def read_text(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, IsADirectoryError):
        return default
    except UnicodeDecodeError:
        with open(path, "rb") as f:
            return decode(f.read())


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def slugify(s, n=40):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-").lower()[:n]


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


def split_list(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(obj):
    return hashlib.sha1(canon(obj).encode("utf-8")).hexdigest()


def sha1_text(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def short(h):
    return (h or "--------")[:8]


def norm(p):
    return os.path.normcase(os.path.abspath(os.path.expanduser(p)))


def is_within(path, root):
    path, root = norm(path), norm(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def setup_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
