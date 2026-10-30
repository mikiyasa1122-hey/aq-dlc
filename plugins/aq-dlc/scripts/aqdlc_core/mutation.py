"""Mutation Operator（反証テスト用に、意図的な欠陥を1行に埋め込む）。

対応言語: Python（.py）と JavaScript / TypeScript（.js .jsx .mjs .cjs .ts .tsx .mts .cts）。
すべて「1行の決定論的な書き換え」で、LLM が生成したパッチは実行しない。各 Operator は
適用できる場所かどうかを形式的に確かめ（apply が None を返せば適用できない）、書き換え後の
Python は構文を確かめる。

Operator と、指示書の初期 Mutation との対応:
  negate_if        条件分岐の反転                 if c → if not (c)
  boundary         境界値の変更                   <= ↔ < 、 >= ↔ >
  eq_flip          条件の反転（等価比較）          == ↔ != 、 === ↔ !==
  and_or           条件の反転（論理演算）          and ↔ or 、 && ↔ ||
  guard_off        入力検証・認可条件・冪等性（重複防止）の除去
                   （例外・return で抜けるガード節の条件を偽にする）
  remove_raise     エラーハンドリング・入力検証の削除（raise / throw を消す）
  return_bool      戻り値の真偽の反転             return True ↔ return False
  sql_where        DB 更新条件の変更              UPDATE / DELETE の WHERE の AND → OR
"""
import re

VERSION = 1
PY_EXT = (".py",)
JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
OPERATORS = ("negate_if", "boundary", "eq_flip", "and_or", "guard_off", "remove_raise", "return_bool", "sql_where")
CATEGORY = {
    "negate_if": "条件分岐の反転", "boundary": "境界値の変更", "eq_flip": "条件の反転（等価比較）",
    "and_or": "条件の反転（論理演算）", "guard_off": "ガード条件の除去", "remove_raise": "エラー処理・入力検証の削除",
    "return_bool": "戻り値の反転", "sql_where": "DB 更新条件の変更",
}
AUTH_RE = re.compile(r"auth|role|permission|perm\b|admin|owner|token|login|session|allow|forbid|権限|認可|ログイン", re.I)
IDEM_RE = re.compile(r"exist|already|duplicat|processed|idempot|seen|used|done|lock|重複|処理済|既に", re.I)


def language(path):
    p = path.lower()
    if p.endswith(PY_EXT):
        return "python"
    if p.endswith(JS_EXT):
        return "js"
    return None


def is_test_file(path):
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    return bool(re.search(r"(^|/)(tests?|__tests__|spec|e2e)/", p) or name.startswith("test_")
                or re.search(r"(_test\.py|\.test\.[a-z]+|\.spec\.[a-z]+|conftest\.py)$", name))


def _skip_line(line, lang):
    t = line.strip()
    if not t:
        return True
    if lang == "python":
        return t.startswith(("#", '"""', "'''"))
    return t.startswith(("//", "/*", "*", "import ", "export {"))


def _in_string(line, pos, lang="python"):
    """pos がコードではない（文字列リテラルの中、または行コメントの後）か（1行の中での簡易な判定）。"""
    q = None
    i = 0
    while i < pos and i < len(line):
        ch = line[i]
        if q:
            if ch == "\\":
                i += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'`":
            q = ch
        elif ch == "#" and lang == "python":
            return True
        elif ch == "/" and line[i:i + 2] == "//" and lang == "js":
            return True
        i += 1
    return q is not None


_MASK_CACHE = {}


def masked_lines(lines, lang):
    """複数行の文字列（docstring）・ブロックコメントの中にある行の番号の集合。"""
    key = (lang, hash(tuple(lines)))
    if key in _MASK_CACHE:
        return _MASK_CACHE[key]
    out, inside = set(), None
    for i, ln in enumerate(lines):
        if inside:
            out.add(i)
            if inside in ln:
                inside = None
            continue
        if lang == "python":
            for d in ('"""', "'''"):
                if ln.count(d) % 2 == 1:
                    out.add(i)
                    inside = d
                    break
                if ln.strip().startswith(d):
                    out.add(i)  # 1行の docstring
        else:
            t = ln.strip()
            if t.startswith("/*") or t.startswith("*"):
                out.add(i)
            if "/*" in ln and "*/" not in ln[ln.index("/*"):]:
                out.add(i)
                inside = "*/"
    if len(_MASK_CACHE) > 64:
        _MASK_CACHE.clear()
    _MASK_CACHE[key] = out
    return out


def _js_if_cond(line):
    """`if (` の位置と、対応する `)` の位置。"""
    m = re.search(r"\bif\s*\(", line)
    if not m or _in_string(line, m.start(), "js"):
        return None
    start = m.end() - 1
    depth = 0
    for i in range(start, len(line)):
        ch = line[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return start, i
    return None


PY_IF = re.compile(r"^(\s*)(if|elif)\s+(.+?):\s*(#.*)?$")
PY_IF_INLINE = re.compile(r"^(\s*)(if|elif)\s+(.+?):\s*(raise\b.*|return\b.*|continue|break)$")


def _guard_body(lines, i, lang):
    """i 行目の if がガード節（例外・return で抜ける）か。"""
    line = lines[i]
    if lang == "python":
        if PY_IF_INLINE.match(line):
            return True
        indent = len(line) - len(line.lstrip())
        for j in range(i + 1, min(i + 4, len(lines))):
            nxt = lines[j]
            if not nxt.strip():
                continue
            if len(nxt) - len(nxt.lstrip()) <= indent:
                return False
            if re.match(r"\s*(raise|return|continue|break)\b", nxt):
                return True
        return False
    rng = _js_if_cond(line)
    if not rng:
        return False
    rest = line[rng[1] + 1:]
    if re.search(r"\b(throw|return|continue|break)\b", rest):
        return True
    for j in range(i + 1, min(i + 4, len(lines))):
        t = lines[j].strip()
        if not t or t == "{":
            continue
        return bool(re.match(r"(throw|return|continue|break)\b", t))
    return False


def apply(op, lines, i, lang):
    """lines[i] に op を適用した新しい行（適用できなければ None）。"""
    if i < 0 or i >= len(lines):
        return None
    line = lines[i]
    if _skip_line(line, lang):
        return None
    if i in masked_lines(lines, lang) and not (op == "sql_where" and lang == "python"):
        return None  # docstring・ブロックコメント（Python の三重引用符の SQL 文字列だけは対象にする）
    nl = "\n" if line.endswith("\n") else ""
    body = line[:-1] if nl else line
    new = None
    if op in ("negate_if", "guard_off"):
        if op == "guard_off" and not _guard_body(lines, i, lang):
            return None
        if lang == "python":
            m = PY_IF.match(body) or PY_IF_INLINE.match(body)
            if m:
                cond = m.group(3)
                rest = body[m.end(3):]
                repl = "False" if op == "guard_off" else f"not ({cond})"
                new = f"{m.group(1)}{m.group(2)} {repl}{rest}"
        else:
            rng = _js_if_cond(body)
            if rng:
                s, e = rng
                cond = body[s + 1:e]
                repl = "false" if op == "guard_off" else f"!({cond})"
                new = body[:s + 1] + repl + body[e:]
    elif op == "boundary":
        for m in re.finditer(r"(?<=[\w)\]'\"])(\s+)(<=|>=|<|>)(\s+)", body):
            if _in_string(body, m.start(2), lang):
                continue
            if lang == "js" and body[m.start(2) - 1:m.start(2)] in ("=", "-"):
                continue
            swap = {"<=": "<", "<": "<=", ">=": ">", ">": ">="}[m.group(2)]
            new = body[:m.start(2)] + swap + body[m.end(2):]
            break
    elif op == "eq_flip":
        pats = [("===", "!=="), ("!==", "==="), ("==", "!="), ("!=", "==")] if lang == "js" else [("==", "!="), ("!=", "==")]
        best = None
        for a, b in pats:
            for m in re.finditer(re.escape(a), body):
                if _in_string(body, m.start(), lang):
                    continue
                if lang == "js" and a in ("==", "!=") and (body[m.end():m.end() + 1] == "=" or
                                                           body[m.start() - 1:m.start()] in ("=", "!")):
                    continue
                if lang == "python" and body[m.end():m.end() + 1] == "=":
                    continue
                if best is None or m.start() < best[0]:
                    best = (m.start(), m.end(), b)
                break
        if best:
            new = body[:best[0]] + best[2] + body[best[1]:]
    elif op == "and_or":
        pats = [(r"&&", "||"), (r"\|\|", "&&")] if lang == "js" else [(r"(?<=\s)and(?=\s)", "or"), (r"(?<=\s)or(?=\s)", "and")]
        best = None
        for pat, b in pats:
            for m in re.finditer(pat, body):
                if _in_string(body, m.start(), lang):
                    continue
                if best is None or m.start() < best[0]:
                    best = (m.start(), m.end(), b)
                break
        if best:
            new = body[:best[0]] + best[2] + body[best[1]:]
    elif op == "remove_raise":
        if lang == "python":
            m = re.match(r"^(\s*)raise\b.*$", body)
            if m:
                new = f"{m.group(1)}pass  # E-AQDL mutation: raise を削除"
            else:
                m = PY_IF_INLINE.match(body)
                if m and m.group(4).startswith("raise"):
                    new = f"{m.group(1)}{m.group(2)} {m.group(3)}: pass"
        else:
            m = re.search(r"\bthrow\b[^;]*;?", body)
            if m and not _in_string(body, m.start(), lang):
                new = body[:m.start()] + "void 0;" + body[m.end():]
    elif op == "return_bool":
        pairs = {"True": "False", "False": "True"} if lang == "python" else {"true": "false", "false": "true"}
        m = re.search(r"\breturn\s+(True|False|true|false)\b", body)
        if m and m.group(1) in pairs:
            new = body[:m.start(1)] + pairs[m.group(1)] + body[m.end(1):]
    elif op == "sql_where":
        ctx = "".join(lines[max(0, i - 5):i]) + body  # 複数行の SQL（直前の数行に UPDATE / DELETE）
        if re.search(r"\b(UPDATE|DELETE)\b", ctx, re.I) and re.search(r"\bWHERE\b", body, re.I):
            m = re.search(r"(?<=\s)AND(?=\s)", body[re.search(r"\bWHERE\b", body, re.I).end():], re.I)
            if m:
                off = re.search(r"\bWHERE\b", body, re.I).end()
                new = body[:off + m.start()] + "OR" + body[off + m.end():]
    if new is None or new == body:
        return None
    return new + nl


def category(op, line):
    if op == "guard_off":
        if AUTH_RE.search(line):
            return "認可条件の除去"
        if IDEM_RE.search(line):
            return "冪等性・重複実行防止の除去"
        return "入力検証・ガード条件の除去"
    return CATEGORY.get(op, op)


def sites(lines, lang, ops=OPERATORS):
    """適用できる (行番号0始まり, op) の一覧（ファイル順・op 順）。"""
    out = []
    for i in range(len(lines)):
        for op in ops:
            if apply(op, lines, i, lang) is not None:
                out.append((i, op))
    return out


def syntax_ok(text, lang, path="<mutant>"):
    """書き換え後の構文の確認（Python のみ。JS/TS は実行結果で判定する）。"""
    if lang != "python":
        return True, ""
    try:
        compile(text, path, "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"構文エラー: {e.msg}（{e.lineno} 行目）"
