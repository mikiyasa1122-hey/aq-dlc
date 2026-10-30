"""人間の承認の判定と監査ログ。

承認の意味:
  「人間から返答があった」ことと「対象を承認した」ことを区別する。承認が必要な操作には対象 ID
  （SPEC-xxxxxxxx、CFG-xxxxxxxx、MEMSCOPE-xxxxxxxx、SHARE-xxxxxxxx など）を割り当て、提示・質問の
  後に記録された人間の返答のうち、対象 ID と明示的な決定（承認 / 却下）を1行で書いたものだけを
  決定として扱う。「OK」「いいよ」のような対象のない返答や、承認と却下が混ざった返答は決定にしない。

監査ログ:
  JSON Lines。各行は前の行のハッシュを含む（ハッシュの連鎖）。途中の行の書き換え・削除のうち、
  連鎖を作り直さない単純なものは検出できる。ファイルごと作り直す改ざんは検出できない
  （ハッシュだけで外部の改ざんへの耐性を保証するものではない）。
"""
import json
import os
import re

from .util import canon, now_iso, sha1_text

APPROVE_WORDS = ("承認", "approve")
REJECT_WORDS = ("却下", "reject")
CONFIDENTIAL_OK = "機密確認済み"


# ------------------------------------------------------------------ 対象 ID
def target_id(prefix, value):
    """prefix-xxxxxxxx（value がハッシュならその先頭8文字、ID ならそのまま）。"""
    v = str(value or "")
    if re.fullmatch(r"[0-9a-f]{8,}", v):
        v = v[:8]
    return f"{prefix}-{v}" if prefix else v


def phrase(target, decision="承認"):
    return f"{decision} {target}"


# ------------------------------------------------------------------ 返答からの決定の読み取り
_SEG_SPLIT = re.compile(r"[\n,、;；]")
ANY_DECISION = re.compile(r"(承認|却下|approve|reject)\s*[:：]?\s*[A-Za-z]+-[A-Za-z0-9_.@-]+", re.I)
NEGATIVE = re.compile(r"承認しない|承認できない|承認せず|却下|修正したい|修正して|直して|やめ|見送|保留|待って|不可|反対|"
                      r"\bnot\b|\bno\b|reject|don't|do not", re.I)


def decisions_in(text, target):
    """text の中で target に対する明示的な決定を探す。

    戻り値: (decision, flags) — decision は "approve" / "reject" / None（なし・曖昧）、
            flags は追加の確認語（"機密確認済み" など）の集合。
    1つの区切り（行・読点・カンマ）全体が「承認 TARGET」「却下 TARGET」（選択肢の
    「<説明>（承認 TARGET）」の形も可）のときだけ決定とみなす。
    """
    if not text or not target:
        return None, set()
    t = re.escape(target)
    found, flags, negative = set(), set(), False
    pat = re.compile(
        rf"^(?:[^\n]{{0,60}}?[（(]\s*)?(承認|却下|approve|reject)\s*[:：]?\s*{t}(?![A-Za-z0-9_.-])"
        rf"\s*({CONFIDENTIAL_OK})?\s*[)）]?\s*[。.!！]?$", re.I)
    for seg in _SEG_SPLIT.split(text):
        s = seg.strip().strip("「」『』\"'`*").strip()
        if not s or target not in s:
            continue
        m = pat.match(s)
        if not m:
            continue
        word = m.group(1).lower()
        found.add("approve" if word in ("承認", "approve") else "reject")
        prefix = s[:m.start(1)]
        if NEGATIVE.search(prefix):
            negative = True  # 「却下したい（承認 …）」のような前置き
        if m.group(2):
            flags.add(CONFIDENTIAL_OK)
    if len(found) != 1:
        return None, flags  # 決定なし、または承認と却下が混在（曖昧）
    d = found.pop()
    for seg in _SEG_SPLIT.split(text):
        if ANY_DECISION.search(seg):
            continue  # 他の対象への決定（「共有しない（却下 SHARE-…）」など）は除く
        if NEGATIVE.search(seg):
            negative = True
    if d == "approve" and negative:
        return None, flags  # 「修正したい」「承認しない」などの否定・保留と一緒に書かれた承認は曖昧として扱う
    return d, flags


def find_decision(state, target, after_seq):
    """after_seq より後の人間の返答（解析できたもの）から、target への決定を探す。

    戻り値: (decision, event, flags)。新しい返答を優先する。
    """
    events = [e for e in state.get("human_events", [])
              if e.get("seq", 0) > (after_seq or 0) and e.get("parsed", True)]
    for e in reversed(events):
        d, flags = decisions_in(e.get("text", ""), target)
        if d:
            return d, e, flags
    return None, None, set()


def record(state, target, decision, event, run_dir=None, note="", kind=""):
    """承認の記録を state と run の監査ログに残す。"""
    rec = {"target": target, "decision": decision, "kind": kind, "at": now_iso(),
           "run": state.get("run_id"), "event_seq": (event or {}).get("seq"),
           "source": (event or {}).get("kind"), "excerpt": _excerpt((event or {}).get("text") or "", target),
           "note": (note or "")[:300]}
    state.setdefault("approvals", []).append(rec)
    if run_dir:
        audit_append(os.path.join(run_dir, "audit.jsonl"), "approval", **rec)
    return rec


def _excerpt(text, target):
    for seg in _SEG_SPLIT.split(text):
        if target in seg:
            return seg.strip()[:200]
    return text[:120]


def approved(state, target):
    """target が承認され、その後に却下されていないか。"""
    last = None
    for a in state.get("approvals", []):
        if a.get("target") == target:
            last = a.get("decision")
    return last == "approve"


# ------------------------------------------------------------------ 監査ログ
def _last_hash(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for ln in reversed(tail):
        try:
            return json.loads(ln).get("h", "")
        except ValueError:
            continue
    return ""


def audit_append(path, event, **fields):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prev = _last_hash(path)
        rec = {"at": now_iso(), "event": event}
        rec.update(fields)
        h = sha1_text(prev + canon(rec))
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"rec": rec, "prev": prev, "h": h}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def audit_verify(path):
    """(行数, 問題の一覧)。ハッシュの連鎖が切れている行を返す。"""
    errs, n, prev = [], 0, ""
    try:
        with open(path, encoding="utf-8") as f:
            for i, ln in enumerate(f, 1):
                n += 1
                try:
                    obj = json.loads(ln)
                except ValueError:
                    errs.append(f"{i} 行目: JSON として読めない")
                    prev = ""
                    continue
                if obj.get("prev") != prev:
                    errs.append(f"{i} 行目: 前の行との連鎖が一致しない（削除・並べ替えの可能性）")
                if sha1_text(obj.get("prev", "") + canon(obj.get("rec"))) != obj.get("h"):
                    errs.append(f"{i} 行目: 内容とハッシュが一致しない（書き換えの可能性）")
                prev = obj.get("h", "")
    except FileNotFoundError:
        return 0, []
    return n, errs


def audit_read(path, limit=50):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln).get("rec", {}))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return out[-limit:]


# ------------------------------------------------------------------ AskUserQuestion の回答の取り出し
_ANS_STR = re.compile(r'"([^"]*)"\s*=\s*"([^"]*)"')


def parse_ask_response(resp):
    """AskUserQuestion の結果から、人間が選んだ・入力した回答だけを取り出す。

    戻り値: (回答のテキスト, 解析できたか)。選択肢の一覧（選ばれなかった選択肢を含む）は
    承認の判定に使わない。
    """
    if isinstance(resp, dict):
        ans = resp.get("answers")
        if isinstance(ans, dict):
            vals = []
            for v in ans.values():
                vals.extend(v if isinstance(v, list) else [v])
            return "\n".join(str(x) for x in vals), True
        if isinstance(ans, list):
            return "\n".join(str(x) for x in ans), True
        for k in ("content", "result", "text"):
            if isinstance(resp.get(k), str):
                return parse_ask_response(resp[k])
        return json.dumps(resp, ensure_ascii=False), False
    if isinstance(resp, list):
        texts = [c.get("text", "") for c in resp if isinstance(c, dict)]
        return parse_ask_response("\n".join(texts))
    if isinstance(resp, str):
        pairs = _ANS_STR.findall(resp)
        if pairs:
            return "\n".join(a for _q, a in pairs), True
        return resp, False
    return "", False
