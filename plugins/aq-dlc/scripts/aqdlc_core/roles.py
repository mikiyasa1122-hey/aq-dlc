"""Adaptive Role Engine: 工程・技術分野に応じた Role Profile の決定論的な選択と記録。

Role Profile は、責務・判断基準・禁止事項・成果物・検証条件・引き継ぎ条件を書いた Markdown
（plugins/aq-dlc/roles/{stage,domain}/*.md）。LLM の作業方針として引き継ぎ情報に加えるだけで、
ツールの権限・人間の承認・安全上の制約は変えない（Profile に権限の項目があれば拒否する）。
同じ Claude の別の Profile を「独立したモデル」として扱わない。

選択は Python で決定論的に行う:
  1. 工程（担当）ごとの基本 Profile（stage）
  2. 状況による追加の工程 Profile（反証テストの有効時・反証テストの差し戻し時）
  3. 技術分野の追加 Profile（domain）: 要求・受け入れ条件・リスクの文と、実装・設計の対象ファイルから
     キーワードとパスで点数を付け、min_score 以上のものを優先度順に最大 N 件。競合する Profile は
     優先度の高い方を残し、同点なら両方とも外して基本 Profile だけにする（曖昧な判定は安全側）。
"""
import fnmatch
import os
import re

from .util import fingerprint, read_text, sha1_text

ROLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "roles")
REQUIRED_SECTIONS = ("責務", "判断基準", "禁止事項", "成果物", "検証条件", "引き継ぎ条件")
# 権限・実行環境に関わる項目。Profile がこれらを持てば、権限の拡大を防ぐために拒否する
FORBIDDEN_KEYS = {"tools", "allowed-tools", "allowed_tools", "allowedtools", "disallowedtools", "disallowed_tools",
                  "permissions", "permissionmode", "permission_mode", "model", "hooks", "mcpservers",
                  "mcp_servers", "env"}
ROLE_NAMES = ("requirements", "design", "implementer", "reviewer", "orchestrator")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

# 工程ごとの基本 Profile
BASE = {
    "requirements": ["requirements-analyst"],
    "design": ["software-architect"],
    "implementer": ["software-engineer", "qa-test-designer"],
    "reviewer": ["code-reviewer"],
    "orchestrator": [],
    "memory": ["reliability-engineer"],
}


def roles_dir():
    return os.environ.get("AQDLC_ROLES_DIR") or ROLE_DIR


def _parse(path):
    text = read_text(path)
    if text is None:
        return None, ["読めない"]
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.S)
    if not m:
        return None, ["先頭の --- で囲んだ項目（frontmatter）がない"]
    meta, errs = {}, []
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            errs.append(f"項目の形式が不正: {line.strip()[:40]}")
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        meta[k] = v
    body = m.group(2)
    return {"meta": meta, "body": body, "hash": sha1_text(text), "path": path}, errs


def _validate(p, kind):
    meta, errs = p["meta"], []
    bad_keys = sorted(k for k in meta if k.lower().replace("-", "_").replace("_", "") in
                      {x.replace("_", "").replace("-", "") for x in FORBIDDEN_KEYS})
    if bad_keys:
        errs.append("権限・実行環境の項目は Role Profile に書けない（権限は変えられない）: " + "、".join(bad_keys))
    pid = meta.get("id")
    if not isinstance(pid, str) or not ID_RE.match(pid):
        errs.append(f"id が不正: {pid}")
    try:
        if int(meta.get("version", "x")) < 1:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("version が 1 以上の整数ではない")
    if meta.get("kind") != kind:
        errs.append(f"kind が {kind} ではない")
    roles = meta.get("roles")
    if not isinstance(roles, list) or not roles or any(r not in ROLE_NAMES for r in roles):
        errs.append(f"roles が不正（{' / '.join(ROLE_NAMES)} の一覧）")
    heads = [ln.lstrip("#").strip() for ln in p["body"].splitlines() if re.match(r"^#{1,4}\s", ln)]
    miss = [s for s in REQUIRED_SECTIONS if not any(h.startswith(s) for h in heads)]
    if miss:
        errs.append("必須の見出しがない: " + "、".join(miss))
    if kind == "domain":
        for k in ("priority", "min_score"):
            try:
                int(meta.get(k, "x"))
            except (TypeError, ValueError):
                errs.append(f"{k} が整数ではない")
        if not isinstance(meta.get("keywords"), list) and not isinstance(meta.get("paths"), list):
            errs.append("keywords か paths の一覧が必要")
        for k in ("keywords", "paths", "conflicts"):
            if k in meta and not isinstance(meta[k], list):
                errs.append(f"{k} は [a, b] の一覧で書く")
    return errs


def load_profiles(cfg=None):
    """(profiles, rejected)。profiles: {id: profile}, rejected: [{path, errors}]"""
    disabled = set((cfg or {}).get("roles", {}).get("disabled", []) or [])
    profiles, rejected, seen = {}, [], {}
    for kind in ("stage", "domain"):
        d = os.path.join(roles_dir(), kind)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(d, name)
            p, errs = _parse(path)
            if p:
                errs += _validate(p, kind)
            if errs:
                rejected.append({"path": path, "errors": errs})
                continue
            pid = p["meta"]["id"]
            if pid in seen:
                rejected.append({"path": path, "errors": [f"id {pid} が {seen[pid]} と重複している"]})
                profiles.pop(pid, None)
                continue
            seen[pid] = path
            if pid in disabled:
                continue
            p["id"], p["kind"], p["version"] = pid, kind, int(p["meta"]["version"])
            p["title"] = p["meta"].get("title", pid)
            p["roles"] = p["meta"]["roles"]
            if kind == "domain":
                p["priority"] = int(p["meta"]["priority"])
                p["min_score"] = int(p["meta"]["min_score"])
                p["keywords"] = p["meta"].get("keywords") or []
                p["paths"] = p["meta"].get("paths") or []
                p["conflicts"] = p["meta"].get("conflicts") or []
            profiles[pid] = p
    # 重複した ID は両方とも使わない
    dup = {r["errors"][0].split()[1] for r in rejected if "重複" in r["errors"][0]}
    for pid in dup:
        profiles.pop(pid, None)
    return profiles, rejected


# ------------------------------------------------------------------ 技術分野の判定
def _kw_regex(kw):
    if re.fullmatch(r"[A-Za-z0-9_/+.-]+\*?", kw):
        prefix = kw.endswith("*")
        core = re.escape(kw.rstrip("*"))
        return re.compile(rf"(?<![A-Za-z0-9]){core}{'[A-Za-z0-9_-]*' if prefix else ''}(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(kw))


def _texts(state, prompt=""):
    """(ID, 文, 重要か) の一覧。要求・受け入れ条件・リスク、および依頼文。"""
    out = []
    for r in state.get("requirements", []):
        crit = bool(r.get("critical"))
        out.append((r["id"], r.get("text") or "", crit))
        for a in r.get("acceptance", []):
            out.append((a["id"], a.get("text") or "", crit))
        for k in r.get("risks", []):
            out.append((k["id"], k.get("text") or "", crit))
        d = (state.get("design") or {}).get("map", {}).get(r["id"])
        if d:
            out.append((r["id"], d, crit))
    if prompt:
        out.append(("依頼文", prompt, False))
    return out


def _files(state):
    files = []
    for fs in ((state.get("impl") or {}).get("map") or {}).values():
        files += [f for f in fs if f not in files]
    return files


def _path_match(path, pattern):
    path = path.replace("\\", "/")
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    return "/" not in pattern and fnmatch.fnmatch(os.path.basename(path), pattern)


def score_domain(p, texts, files):
    kws, related, crit = set(), set(), False
    for kw in p["keywords"]:
        rx = _kw_regex(kw)
        for nid, text, c in texts:
            if rx.search(text):
                kws.add(kw)
                if nid != "依頼文":
                    related.add(nid)
                crit = crit or c
    pfiles = sorted({f for f in files for pat in p["paths"] if _path_match(f, pat)})
    score = len(kws) + (2 if pfiles else 0) + (1 if crit and kws else 0)
    reasons = []
    if kws:
        reasons.append("キーワード: " + "、".join(sorted(kws)[:6]))
    if pfiles:
        reasons.append("対象ファイル: " + "、".join(pfiles[:3]))
    if crit and kws:
        reasons.append("重要な要求に関係する")
    return score, reasons, sorted(related)


def select(role, state, cfg, prompt="", extra_stage=()):
    """担当 role に適用する Profile を決める（同じ入力なら同じ結果）。

    戻り値: {"stage": [...], "domain": [...], "candidates": [...], "dropped": [...], "rejected": [...],
             "fallback": bool, "fp": 指紋}
    """
    rcfg = (cfg or {}).get("roles", {}) or {}
    profiles, rejected = load_profiles(cfg)
    if not rcfg.get("enabled", True):
        return {"stage": [], "domain": [], "candidates": [], "dropped": [], "rejected": rejected,
                "fallback": True, "enabled": False, "fp": fingerprint(["disabled"])}
    stage, missing = [], []
    for pid in list(BASE.get(role, [])) + [x for x in extra_stage if x not in BASE.get(role, [])]:
        p = profiles.get(pid)
        if p and p["kind"] == "stage":
            stage.append(p)
        else:
            missing.append(pid)
    domain, cands, dropped = [], [], []
    if role in ("requirements", "design", "implementer", "reviewer"):
        texts, files = _texts(state, prompt), _files(state)
        for p in sorted((p for p in profiles.values() if p["kind"] == "domain" and role in p["roles"]),
                        key=lambda p: p["id"]):
            score, reasons, related = score_domain(p, texts, files)
            if score <= 0:
                continue
            c = {"p": p, "score": score, "reasons": reasons, "related": related}
            if score < p["min_score"]:
                dropped.append({"id": p["id"], "why": f"根拠が不足（点数 {score} < {p['min_score']}）", "score": score})
                continue
            cands.append(c)
        cands.sort(key=lambda c: (-c["p"]["priority"], -c["score"], c["p"]["id"]))
        chosen = []
        for c in cands:
            clash = [x for x in chosen if c["p"]["id"] in x["p"]["conflicts"] or x["p"]["id"] in c["p"]["conflicts"]]
            if clash:
                x = clash[0]
                if (x["p"]["priority"], x["score"]) == (c["p"]["priority"], c["score"]):
                    chosen.remove(x)
                    dropped.append({"id": x["p"]["id"], "why": f"{c['p']['id']} と競合し、優先度・点数が同じため判定できない"})
                    dropped.append({"id": c["p"]["id"], "why": f"{x['p']['id']} と競合し、優先度・点数が同じため判定できない"})
                else:
                    dropped.append({"id": c["p"]["id"], "why": f"{x['p']['id']} と競合（優先度の高い方を適用）"})
                continue
            chosen.append(c)
        limit = int(rcfg.get("max_domain_profiles", 3))
        for c in chosen[limit:]:
            dropped.append({"id": c["p"]["id"], "why": f"上限（{limit} 件）を超えた"})
        domain = chosen[:limit]
    rec_stage = [{"id": p["id"], "version": p["version"], "hash": p["hash"][:12], "title": p["title"]} for p in stage]
    rec_domain = [{"id": c["p"]["id"], "version": c["p"]["version"], "hash": c["p"]["hash"][:12],
                   "title": c["p"]["title"], "score": c["score"], "reasons": c["reasons"], "related": c["related"]}
                  for c in domain]
    fp = fingerprint({"stage": [[x["id"], x["version"], x["hash"]] for x in rec_stage],
                      "domain": [[x["id"], x["version"], x["hash"]] for x in rec_domain]})
    return {"stage": rec_stage, "domain": rec_domain, "dropped": dropped, "missing": missing,
            "rejected": [{"path": os.path.basename(r["path"]), "errors": r["errors"]} for r in rejected],
            "fallback": not domain, "enabled": True, "fp": fp,
            "_bodies": _merge({p["id"]: p["body"] for p in stage}, {c["p"]["id"]: c["p"]["body"] for c in domain})}


def _merge(a, b):
    out = dict(a)
    out.update(b)
    return out


def brief_section(sel):
    """引き継ぎ情報に加える節。"""
    if not sel.get("enabled", True):
        return []
    lines = ["", "## 適用する役割（Role Profile。エンジンが工程と技術分野から自動で選んだ作業方針）",
             "役割は判断の観点を与えるもので、使えるツール・人間の承認・安全上の制約は変わらない。"
             "別の役割になっても、あなたは同じ Claude であり独立した別のモデルではない。"]
    for x in sel["stage"]:
        lines.append(f"- 工程: {x['title']}（{x['id']} v{x['version']}）")
    for x in sel["domain"]:
        lines.append(f"- 追加の専門性: {x['title']}（{x['id']} v{x['version']}）理由: {' / '.join(x['reasons'])}"
                     + (f"（関係: {', '.join(x['related'][:6])}）" if x["related"] else ""))
    if sel.get("fallback"):
        lines.append("- 追加の専門性: なし（根拠が十分な分野がないため、工程の基本 Profile だけを使う）")
    for pid, body in sel.get("_bodies", {}).items():
        text = re.sub(r"^#\s.*\n", "", body.strip() + "\n", count=1).strip()
        text = re.sub(r"^## ", "#### ", text, flags=re.M)
        lines += ["", f"### {pid}", text]
    return lines


def public(sel):
    """state に記録する形（本文を除く）。"""
    return {k: v for k, v in sel.items() if not k.startswith("_")}


def trigger_of(role, state, sendback):
    hist = [h for h in (state.get("roles") or {}).get("history", []) if h.get("agent") == role]
    if sendback:
        return "sendback"
    if not hist:
        return "start"
    return "transition"


def record(state, role, sel, trigger, seq, agent_id=None):
    r = state.setdefault("roles", {"history": []})
    rec = dict(public(sel), agent=role, trigger=trigger, seq=seq, agent_id=agent_id)
    r.setdefault("history", []).append(rec)
    r["history"] = r["history"][-100:]
    return rec
