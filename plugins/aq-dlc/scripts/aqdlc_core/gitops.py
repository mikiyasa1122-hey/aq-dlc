"""GitHub への公開と、危険な Git / GitHub 操作のガード。"""
import os
import re
import shlex
import shutil

from .project import config, ensure_gitignore, resolve_project
from .util import die, info, load_json, pop_flag, pop_opt, sh_run

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
    missing = [k for k in ("user.name", "user.email") if not sh(f"git config --get {k}", check=False)[1].strip()]
    if missing and not dry:
        die("git の利用者情報が設定されていないため、コミットできません。次を実行してから publish をやり直してください:\n"
            '  git config --global user.name "<名前>"\n'
            '  git config --global user.email "<メールアドレス>"\n'
            "（GitHub のメールアドレスを公開したくない場合は、GitHub の設定にある noreply アドレスを使えます）")
    fresh = not os.path.isdir(os.path.join(project, ".git"))
    if fresh:
        if ensure_gitignore(project):
            info(".gitignore に標準の除外設定を追加しました。")
        if dry:
            info("（確認のみ）git リポジトリを作成し、最初のコミットを作ります。")
            return
        sh(f"git init -b {branch}")
        sh("git add -A")
        _, staged = sh("git diff --cached --name-only")
        files = [f for f in staged.splitlines() if f.strip()]
        junk = [f for f in files if JUNK.search(f)]
        for f in junk:
            sh(f'git rm -r --cached --quiet -- "{f}"', check=False)
        files = [f for f in files if f not in junk]
        info(f"最初のコミットに含めるファイル（{len(files)}件）:")
        for f in files[:50]:
            print(f"  {f}")
        if len(files) > 50:
            print(f"  …ほか {len(files) - 50} 件")
        if junk:
            info(f"生成物・秘密情報らしいファイル {len(junk)} 件は除外しました（例: {junk[0]}）")
        if files:
            sh('git commit -q -m "初期化: AQ-DLC プロジェクトを作成"')
    else:
        if sh("git rev-parse --verify HEAD", check=False)[0] != 0:
            die("git リポジトリにコミットがありません。コミットしてから再実行してください。")
        if sh("git status --porcelain", check=False)[1].strip():
            info("未コミットの変更があります。publish はコミット済みの内容だけを push します。")
    if dry:
        return
    if sh("git remote get-url origin", check=False)[0] != 0:
        code = sh("gh auth status", check=False)[0] if shutil.which("gh") else 1
        if code != 0:
            die("gh が未インストールか未ログインです。`gh auth login` の後に再実行してください。"
                "（ローカルの git リポジトリはそのまま使えます）")
        sh(f'gh repo create "{meta.get("slug") or os.path.basename(project)}" --{vis} --source . --remote origin')
        _, url = sh("gh repo view --json url -q .url", check=False)
        info(f"GitHub に {vis} リポジトリを作成しました: {url.strip()}")
    _, cur = sh("git rev-parse --abbrev-ref HEAD", check=False)
    cur = cur.strip()
    targets = []
    if sh(f"git rev-parse --verify {branch}", check=False)[0] == 0:
        targets.append(branch)
    if cur and cur != "HEAD" and cur not in targets:
        targets.append(cur)
    for b in targets:
        if b == branch and sh(f"git ls-remote --exit-code --heads origin {branch}", check=False)[0] == 0:
            continue  # 既に存在する既定ブランチには push しない
        sh(f"git push -u origin {b}")
        info(f"push しました: {b}")


# ------------------------------------------------------------------ guard
SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")
GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def tokens(seg):
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def git_subcommand(toks):
    """['git', '-C', 'x', 'push', ...] → ('push', [...引数], cwd)"""
    if not toks:
        return None, [], None
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1  # VAR=value の前置
    if i >= len(toks) or os.path.basename(toks[i]) not in ("git", "git.exe"):
        return None, [], None
    i += 1
    cwd = None
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] in GIT_GLOBAL_OPTS_WITH_VALUE and i + 1 < len(toks):
            if toks[i] == "-C":
                cwd = toks[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(toks):
        return None, [], cwd
    return toks[i], toks[i + 1:], cwd


def analyze_push(args):
    flags, pos = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("-"):
            flags.append(a)
            if a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec") and i + 1 < len(args):
                i += 1
        else:
            pos.append(a)
        i += 1
    short = "".join(f[1:] for f in flags if f.startswith("-") and not f.startswith("--"))
    force = any(f.startswith(("--force", "--force-with-lease", "--force-if-includes")) for f in flags) \
        or "f" in short or any(p.startswith("+") for p in pos[1:])
    delete = "--delete" in flags or "d" in short or any(p.startswith(":") for p in pos[1:])
    mirror = "--mirror" in flags
    prune = "--prune" in flags
    all_ = "--all" in flags or "--branches" in flags
    refspecs = pos[1:]
    return {"force": force, "delete": delete, "mirror": mirror, "prune": prune, "all": all_, "refspecs": refspecs}


def push_targets_default(info_, default, current):
    names = {default, "master"}
    for r in info_["refspecs"]:
        dst = r.lstrip("+").split(":")[-1]
        dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
        if dst in names or (dst == "HEAD" and current in names):
            return True
    if info_["all"]:
        return True
    if not info_["refspecs"] and current in names:
        return True
    return False


GH_API_DANGER = re.compile(r"mergePullRequest|deleteRepository|updateRepository|archiveRepository|deleteRef|"
                           r"enablePullRequestAutoMerge|transferRepository", re.I)


def analyze_gh(toks):
    """gh の危険な操作。戻り値: (設定キー, 理由) のリスト"""
    out = []
    if not toks or os.path.basename(toks[0]) not in ("gh", "gh.exe"):
        return out
    rest = [t for t in toks[1:]]
    words = [t for t in rest if not t.startswith("-")]
    joined = " ".join(rest)
    if words[:2] == ["pr", "merge"]:
        out.append(("block_pr_merge", "PR のマージは人間が行います"))
    if words[:2] in (["repo", "delete"], ["repo", "archive"], ["repo", "rename"], ["release", "delete"]):
        out.append(("block_destructive_gh", "リポジトリ・リリースの削除やアーカイブ、名前の変更は人間の確認が必要です"))
    if words[:2] == ["repo", "edit"] and "--visibility" in joined:
        out.append(("block_destructive_gh", "公開範囲の変更は人間の確認が必要です"))
    if words[:1] == ["api"]:
        method = None
        for i, t in enumerate(rest):
            if t in ("-X", "--method") and i + 1 < len(rest):
                method = rest[i + 1].upper()
            elif t.startswith("--method="):
                method = t.split("=", 1)[1].upper()
            elif t.startswith("-X") and len(t) > 2:
                method = t[2:].upper()
        path = next((w for w in words[1:] if "/" in w or w == "graphql"), "")
        if method == "DELETE":
            out.append(("block_destructive_gh", "GitHub API での削除は人間の確認が必要です"))
        if re.search(r"/pulls/\d+/merge\b|/merges\b", path) and method in ("PUT", "POST", None):
            out.append(("block_pr_merge", "API 経由の PR マージ・ブランチのマージは人間が行います"))
        if method == "PATCH" and re.fullmatch(r"/?repos/[^/]+/[^/]+/?", path):
            out.append(("block_destructive_gh", "リポジトリ設定（公開範囲など）の変更は人間の確認が必要です"))
        if re.search(r"/git/refs/heads/", path) and method in ("PATCH", "DELETE"):
            out.append(("block_force_push", "API でのブランチの書き換え・削除は人間の確認が必要です"))
        if path == "graphql" and GH_API_DANGER.search(joined):
            out.append(("block_destructive_gh", "GraphQL での削除・マージ・設定変更は人間の確認が必要です"))
    return out


def analyze_http(seg):
    if not re.search(r"\b(curl|wget|http|https|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b", seg, re.I):
        return []
    if "api.github.com" not in seg:
        return []
    m = re.search(r"(?:-X|--request|-Method)\s*['\"]?(DELETE|PUT|PATCH|POST)", seg, re.I)
    method = m.group(1).upper() if m else None
    if method == "DELETE" or (method in ("PUT", "POST") and re.search(r"/merge", seg)) \
            or (method == "PATCH" and re.search(r"api\.github\.com/repos/[^/\s]+/[^/\s'\"]+['\"\s]", seg + " ")):
        return [("block_destructive_gh", "GitHub API の破壊的な操作（削除・マージ・設定変更）は人間の確認が必要です")]
    return []


def guard_reasons(cmd, project, current_branch_fn, remote_has_branch_fn):
    cfg = config(project)
    auto = cfg["automation"]
    default = cfg["github"]["default_branch"]
    reasons = []
    for seg in SEGMENT_SPLIT.split(cmd):
        seg = seg.strip()
        if not seg:
            continue
        toks = tokens(seg)
        while toks and toks[0] in ("sudo", "command", "exec", "time", "nohup", "env"):
            toks = toks[1:]
        sub, args, gcwd = git_subcommand(toks)
        if sub == "push":
            pi = analyze_push(args)
            if pi["force"]:
                reasons.append(("block_force_push", "force push（--force / -f / +refspec）は人間の確認が必要です"))
            if pi["mirror"]:
                reasons.append(("block_force_push", "push --mirror はリモートを上書き・削除するため人間の確認が必要です"))
            if pi["delete"] or pi["prune"]:
                reasons.append(("block_destructive_gh", "リモートブランチの削除（--delete / --prune / :branch）は人間の確認が必要です"))
            where = os.path.join(project, gcwd) if gcwd and not os.path.isabs(gcwd) else (gcwd or project)
            if push_targets_default(pi, default, current_branch_fn(where)) and remote_has_branch_fn(where, default):
                reasons.append(("block_push_to_default_branch",
                                f"{default} への直接 push は人間の確認が必要です（作業ブランチを push して PR にしてください）"))
        elif sub in ("filter-branch", "filter-repo"):
            reasons.append(("block_destructive_gh", "履歴の書き換えは人間の確認が必要です"))
        elif sub == "config" and any(re.match(r"^alias\.", a) for a in args) and re.search(r"push|merge", seg):
            reasons.append(("block_force_push", "push / merge を含む git の別名の設定は人間の確認が必要です"))
        reasons += analyze_gh(toks)
        reasons += analyze_http(seg)
    return [msg for key, msg in reasons if auto.get(key, True)]


def current_branch(path):
    try:
        code, out = sh_run("git rev-parse --abbrev-ref HEAD", path, 5)
        return out.strip() if code == 0 else None
    except Exception:  # noqa: BLE001
        return None


def remote_has_branch(path, branch):
    try:
        return sh_run(f"git ls-remote --exit-code --heads origin {branch}", path, 15)[0] == 0
    except Exception:  # noqa: BLE001
        return True  # 確認できないときは安全側に倒す


def dangerous_words(cmd):
    return bool(re.search(r"\bpush\b|\bmerge\b|\bdelete\b|\bfilter-(branch|repo)\b|-X\s*DELETE|--visibility", cmd))
