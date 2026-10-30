"""run の状態、Evidence Graph、証拠の鮮度判定、次の工程の決定。

Evidence Graph:
  要求(R) ─┬─ 受け入れ条件(AC) ── 検証(V) ── テスト(command/checks/test) ── gate 結果
           └─ リスク(K)        ── 検証(V) ── ...
  要求(R) ── 設計要素（design-map） ── 実装ファイル（impl-map）

各工程の成果物（仕様書・設計書・実装報告・レビュー結果）は、作成時点の
仕様・設計・コード・検証条件の指紋と一緒に記録する。指紋が今の状態と一致する
ものだけを証拠として扱う。
"""
import datetime as _dt
import os
import re
import time

from . import gate as G
from .project import config, register_active
from .util import (die, fingerprint, load_json, now_iso, read_text, save_json, sha1_text,
                   short, slugify)

SCHEMA = 2
ROLES = ("requirements", "design", "implementer", "reviewer")
ROLE_LABEL = {"requirements": "要求分析（aq-dlc:requirements）", "design": "設計（aq-dlc:design）",
              "implementer": "実装（aq-dlc:implementer）", "reviewer": "独立レビュー（aq-dlc:reviewer）"}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


# ------------------------------------------------------------------ paths
def runs_dir(project):
    return os.path.join(project, ".aq-dlc", "runs")


def run_dir(project, run_id=None):
    if not run_id:
        run_id = (read_text(os.path.join(runs_dir(project), "CURRENT"), "") or "").strip()
    if not run_id:
        return None
    d = os.path.join(runs_dir(project), run_id)
    return d if os.path.isdir(d) else None


def artifacts_dir(rd):
    return os.path.join(rd, "artifacts")


def doc_path(rd, kind):
    return os.path.join(artifacts_dir(rd), {"spec": "spec.md", "design": "design.md", "impl": "impl.md"}[kind])


def doc_hash(rd, kind):
    t = read_text(doc_path(rd, kind))
    return sha1_text(t) if t is not None else None


# ------------------------------------------------------------------ state io
def empty_state(rid, title):
    return {
        "schema_version": SCHEMA, "run_id": rid, "title": title, "status": "in_progress",
        "created": now_iso(), "seq": 0,
        "awaiting_human": False, "question": "", "asked_seq": 0, "waiting": None,
        "agents": {"running": {}, "history": []},
        "requirements": [], "assumptions": [], "out_of_scope": [], "questions": [],
        "decisions": [], "escalations": [],
        "spec": {}, "design": {"map": {}}, "impl": {"rounds": [], "map": {}},
        "gate": None, "reviews": [], "fix_iterations": 0,
        "human_events": [], "history": [],
        "memory_done": False, "ci": None, "after_merge_gate": False, "stop_blocks": 0,
    }


def migrate(state):
    base = empty_state(state.get("run_id", "?"), state.get("title", ""))
    for k, v in base.items():
        state.setdefault(k, v)
    state["design"].setdefault("map", {})
    state["impl"].setdefault("rounds", [])
    state["impl"].setdefault("map", {})
    state["agents"].setdefault("running", {})
    state["agents"].setdefault("history", [])
    for r in state["requirements"]:
        r.setdefault("acceptance", [])
        r.setdefault("risks", [])
        r.setdefault("kind", "functional")
    if state.get("schema_version", 1) < SCHEMA:
        state["legacy"] = True
        state["schema_version"] = SCHEMA
    return state


def load_state(project):
    d = run_dir(project)
    if not d:
        return None, None
    p = os.path.join(d, "state.json")
    s = load_json(p, None)
    return (migrate(s), p) if s else (None, None)


def save_state(spath, state):
    save_json(spath, state)


def require_state(project):
    s, p = load_state(project)
    if not s:
        die("実行中のrunがありません。先に new を実行してください。")
    return s, p


def bump(state):
    state["seq"] = int(state.get("seq", 0)) + 1
    return state["seq"]


def actor_hint(project):
    """PreToolUse hook が書いた「直前にエンジンを呼んだ役割」。"""
    rd = run_dir(project)
    if not rd:
        return "orchestrator"
    a = load_json(os.path.join(rd, "actor.json"), {}) or {}
    if time.time() - a.get("at", 0) < 60:
        return a.get("role", "orchestrator")
    return "orchestrator"


def log(state, project, action, detail):
    state["history"].append({"seq": bump(state), "at": now_iso(), "actor": actor_hint(project),
                             "action": action, "detail": detail})


def human_event(state, kind, text):
    seq = bump(state)
    state["human_events"].append({"seq": seq, "at": now_iso(), "kind": kind, "text": (text or "")[:500]})
    state["human_events"] = state["human_events"][-50:]
    return seq


def last_human_seq(state):
    return max([e["seq"] for e in state.get("human_events", [])] or [0])


def new_run(project, title):
    """run を作成する。ID は秒まで含め、衝突したら連番を付ける（既存の run を上書きしない）。"""
    base = _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + (slugify(title) or "run")
    os.makedirs(runs_dir(project), exist_ok=True)
    rid, n = base, 1
    while True:
        d = os.path.join(runs_dir(project), rid)
        try:
            os.makedirs(d)
            break
        except FileExistsError:
            n += 1
            rid = f"{base}-{n}"
    os.makedirs(os.path.join(d, "gate"), exist_ok=True)
    os.makedirs(artifacts_dir(d), exist_ok=True)
    save_state(os.path.join(d, "state.json"), empty_state(rid, title))
    tmpl = read_text(os.path.join(project, ".aq-dlc", "run-template.md"), "# {title}\n")
    with open(os.path.join(d, "run.md"), "w", encoding="utf-8") as f:
        f.write(tmpl.replace("{title}", title).replace("{run_id}", rid).replace("{date}", now_iso()))
    with open(os.path.join(runs_dir(project), "CURRENT"), "w", encoding="utf-8") as f:
        f.write(rid + "\n")
    register_active(project, rid)
    return rid, d


# ------------------------------------------------------------------ graph
def iter_nodes(state):
    for r in state.get("requirements", []):
        yield "requirement", r, None
        for a in r.get("acceptance", []):
            yield "acceptance", a, r["id"]
            for v in a.get("verifications", []):
                yield "verification", v, a["id"]
        for k in r.get("risks", []):
            yield "risk", k, r["id"]
            for v in k.get("verifications", []):
                yield "verification", v, k["id"]
    for key, kind in (("questions", "question"), ("assumptions", "assumption"),
                      ("out_of_scope", "out_of_scope"), ("decisions", "decision"),
                      ("escalations", "escalation")):
        for n in state.get(key, []):
            yield kind, n, None


def id_index(state):
    idx, dups = {}, []
    for kind, node, parent in iter_nodes(state):
        nid = node.get("id")
        if nid in idx:
            dups.append(nid)
        else:
            idx[nid] = (kind, node, parent)
    return idx, sorted(set(dups))


def iter_verifications(state):
    for r in state.get("requirements", []):
        for p in r.get("acceptance", []) + r.get("risks", []):
            for v in p.get("verifications", []):
                yield r, p, v


def find(state, nid, kinds=None):
    idx, dups = id_index(state)
    if nid in dups:
        die(f"ID {nid} が重複しているため特定できません。重複を解消してください。")
    if nid not in idx or (kinds and idx[nid][0] not in kinds):
        die(f"{'・'.join(kinds) if kinds else 'ノード'} が見つかりません: {nid}")
    return idx[nid]


def ensure_new_id(state, nid, same_place=None):
    """ID は run 全体で一意。same_place（kind, parent）と一致する既存ノードの更新だけ許す。"""
    if not ID_RE.match(nid or ""):
        die(f"ID の形式が不正です: {nid}（英字で始まる英数字・._- の32文字以内）")
    idx, _ = id_index(state)
    if nid in idx:
        kind, _node, parent = idx[nid]
        if same_place and (kind, parent) == same_place:
            return idx[nid][1]
        die(f"ID {nid} は既に使われています（{kind}{'、親 ' + parent if parent else ''}）。"
            "検証結果が上書きされるのを防ぐため、別のIDを付けてください。")
    return None


def req_by_id(state, rid):
    for r in state["requirements"]:
        if r["id"] == rid:
            return r
    die(f"要求が見つかりません: {rid}")


# ------------------------------------------------------------------ fingerprints
def spec_hash(state, rd):
    return fingerprint({
        "doc": doc_hash(rd, "spec"),
        "req": [[r["id"], r.get("text"), r.get("kind"), bool(r.get("critical")),
                 [[a["id"], a.get("text")] for a in r.get("acceptance", [])]]
                for r in state.get("requirements", [])],
        "assume": [[a["id"], a.get("text")] for a in state.get("assumptions", [])],
        "out": [[o["id"], o.get("text")] for o in state.get("out_of_scope", [])],
    })


def graph_fp(state):
    def vdef(v):
        return {k: v.get(k) for k in ("id", "method", "command", "checks", "manual", "after_merge",
                                       "level", "test", "timeout_sec")}
    return fingerprint([[r["id"], r.get("text"), bool(r.get("critical")),
                         [[a["id"], a.get("text"), [vdef(v) for v in a.get("verifications", [])]]
                          for a in r.get("acceptance", [])],
                         [[k["id"], k.get("text"), [vdef(v) for v in k.get("verifications", [])]]
                          for k in r.get("risks", [])]]
                        for r in state.get("requirements", [])])


class Ctx:
    """現在の仕様・設計・コード・検証条件の指紋をまとめて計算する。"""

    def __init__(self, project, state, cfg=None):
        self.project, self.state = project, state
        self.cfg = cfg or config(project)
        self.rd = run_dir(project, state.get("run_id"))
        self.spec_hash = spec_hash(state, self.rd)
        self.design_hash = doc_hash(self.rd, "design")
        self.graph_fp = graph_fp(state)
        self.checks = self.cfg.get("checks", [])
        self.checks_fp = G.checks_fp(self.cfg)
        self._snap = None

    @property
    def snap(self):
        if self._snap is None:
            prev = (self.state.get("gate") or {}).get("snapshot")
            self._snap = G.snapshot(self.project, self.cfg, prev)
        return self._snap

    @property
    def code_fp(self):
        return G.code_fp(self.snap)


def gate_status(ctx):
    """(鮮度, 合否)。鮮度: 今のコード・チェック定義・各検証条件で実行された結果か。"""
    s = ctx.state
    g = s.get("gate")
    if not g:
        return False, False
    vres = (g.get("results") or {}).get("verifications", {})
    fresh = g.get("code_fp") == ctx.code_fp and g.get("checks_fp") == ctx.checks_fp
    passing = bool(g.get("checks_pass"))
    for _r, _p, v in iter_verifications(s):
        r = vres.get(v["id"])
        if r is None or r.get("fp") != G.verification_fp(v, ctx.cfg):
            fresh = False
            continue
        if v.get("after_merge") or (v.get("manual") and not G.is_automated(v)):
            continue
        if not r.get("pass"):
            passing = False
    return fresh, passing


# ------------------------------------------------------------------ problems / next
def P(code, msg, route):
    return {"code": code, "msg": msg, "route": route}


def latest_review(state):
    rv = [r for r in state.get("reviews", []) if r.get("valid")]
    return rv[-1] if rv else None


def last_impl(state):
    rounds = state["impl"].get("rounds", [])
    return rounds[-1] if rounds else None


def graph_issues(state):
    out = []
    for r in state["requirements"]:
        if not r.get("acceptance"):
            out.append(f"{r['id']} に受け入れ条件がない")
        for a in r.get("acceptance", []):
            if not a.get("verifications"):
                out.append(f"{a['id']} に検証がない")
        if r.get("critical") and not any(k.get("verifications") for k in r.get("risks", [])):
            out.append(f"{r['id']}（重要）に検証付きのリスクがない")
        for k in r.get("risks", []):
            if not k.get("verifications"):
                out.append(f"{k['id']} に検証がない")
    return out


def problems(ctx, stage="pr"):
    """未充足の条件を、対応すべき順に返す。stage: pr = PR前（マージ後の検証を除く） / final = すべて"""
    s, cfg = ctx.state, ctx.cfg
    auto = cfg["automation"]
    out = []
    _idx, dups = id_index(s)
    if dups:
        out.append(P("dup_ids", f"IDが重複している: {', '.join(dups)}（結果が上書きされるため判定できない）",
                     "requirements"))
    spec = s.get("spec") or {}
    if not s["requirements"] or not spec.get("registered_hash"):
        out.append(P("spec_missing", "仕様が未作成（aq-dlc:requirements が要求・受け入れ条件・リスクを整理する）",
                     "requirements"))
        return out
    gi = graph_issues(s)
    if gi:
        out.append(P("graph_incomplete", "Evidence Graph が不完全: " + "、".join(gi[:5]), "requirements"))
    if spec.get("registered_hash") != ctx.spec_hash:
        out.append(P("spec_unregistered", "仕様が要求整理の完了後に変更された（aq-dlc:requirements で整理し直す）",
                     "requirements"))
    openq = [q["id"] for q in s["questions"] if q.get("status") != "answered"]
    if openq:
        out.append(P("questions_open", f"未回答の質問: {', '.join(openq)}", "human"))
    unapplied = [q["id"] for q in s["questions"]
                 if q.get("status") == "answered" and q.get("answered_seq", 0) > spec.get("registered_seq", 0)]
    if unapplied and not openq:
        out.append(P("answers_unapplied", f"人間の回答（{', '.join(unapplied)}）が仕様に未反映", "requirements"))
    if auto.get("spec_review", True) and not openq and not unapplied:
        if spec.get("approved_hash") != ctx.spec_hash:
            if spec.get("presented_hash") != ctx.spec_hash:
                out.append(P("spec_unpresented", "仕様を人間に提示していない", "human"))
            else:
                out.append(P("spec_unapproved", "仕様が人間に未承認", "human"))
    if out and out[-1]["route"] in ("human", "requirements"):
        return out
    esc = [e["id"] for e in s["escalations"] if e.get("status") != "resolved"]
    if esc:
        out.append(P("escalation", f"未対応の差し戻し: {', '.join(esc)}", "orchestrator"))
        return out

    d = s["design"]
    if not d.get("registered_hash"):
        out.append(P("design_missing", "設計が未作成", "design"))
    elif d.get("registered_hash") != ctx.design_hash:
        out.append(P("design_changed", "設計書が登録後に変更された（aq-dlc:design で登録し直す）", "design"))
    elif d.get("spec_hash") != ctx.spec_hash:
        out.append(P("design_stale", "承認済み仕様の変更後に設計が更新されていない", "design"))
    else:
        unmapped = [r["id"] for r in s["requirements"] if r["id"] not in d.get("map", {})]
        if unmapped:
            out.append(P("design_unmapped", f"設計に対応付いていない要求: {', '.join(unmapped)}", "design"))
        if not ctx.checks:
            out.append(P("no_checks", "Quality Gate のチェックが未登録", "design"))
    if out:
        return out

    max_fix = int(auto.get("max_fix_iterations", 8))

    def fix_route(route):
        if route == "implementer" and s.get("fix_iterations", 0) >= max_fix:
            return "human"
        return route

    li = last_impl(s)
    if not li:
        out.append(P("impl_missing", "実装が未着手", "implementer"))
        return out
    if li.get("design_hash") != ctx.design_hash or li.get("spec_hash") != ctx.spec_hash:
        out.append(P("impl_stale", "仕様・設計の変更後に実装が更新されていない", "implementer"))
        return out
    untraced = []
    for r in s["requirements"]:
        files = s["impl"]["map"].get(r["id"]) or []
        missing = [f for f in files if not os.path.exists(os.path.join(ctx.project, f))]
        if not files:
            untraced.append(f"{r['id']}（実装ファイル未登録）")
        elif missing:
            untraced.append(f"{r['id']}（存在しないファイル: {', '.join(missing[:2])}）")
    if untraced:
        out.append(P("impl_untraced", "要求と実装の対応が不足: " + "、".join(untraced[:5]), "implementer"))
    unbound = [v["id"] for _r, _p, v in iter_verifications(s) if not G.is_automated(v) and not v.get("manual")]
    if unbound:
        out.append(P("verif_unbound", f"実行方法が未設定の検証: {', '.join(unbound)}（bind で設定）", "implementer"))
    if out:
        return out

    rv = latest_review(s)
    if rv and rv.get("blocking", 0) > 0:
        cats = {f.get("category") for f in rv.get("findings", []) if f.get("severity") == "重大"}
        if "仕様" in cats and spec.get("registered_seq", 0) < rv["seq"]:
            out.append(P("review_fix", f"レビュー#{rv['n']} の重大な指摘（仕様）が未対応", "requirements"))
        elif "設計" in cats and d.get("registered_seq", 0) < rv["seq"]:
            out.append(P("review_fix", f"レビュー#{rv['n']} の重大な指摘（設計）が未対応", "design"))
        elif li["seq"] < rv["seq"]:
            out.append(P("review_fix", f"レビュー#{rv['n']} の重大な指摘 {rv['blocking']} 件が未対応",
                         fix_route("implementer")))
        if out:
            return out

    g = s.get("gate")
    if not g:
        out.append(P("gate_missing", "Quality Gate が未実行", "gate"))
        return out
    stale = []
    diff = G.describe_diff(g.get("snapshot"), ctx.snap)
    if g.get("code_fp") != ctx.code_fp:
        stale.append("コード（" + (diff or "内容が変化") + "）")
    if g.get("checks_fp") != ctx.checks_fp:
        stale.append("チェックの定義")
    vres = (g.get("results") or {}).get("verifications", {})
    changed_v, new_v = [], []
    for _r, _p, v in iter_verifications(s):
        r = vres.get(v["id"])
        if r is None:
            new_v.append(v["id"])
        elif r.get("fp") != G.verification_fp(v, cfg):
            changed_v.append(v["id"])
    if changed_v:
        stale.append(f"検証条件（{', '.join(changed_v[:5])}）")
    if new_v:
        stale.append(f"結果のない検証（{', '.join(new_v[:5])}）")
    if stale:
        out.append(P("gate_stale", "gate 後に変わったものがある: " + "、".join(stale), "gate"))
        return out
    failed = [n for n, r in g["results"]["checks"].items() if not r["pass"]]
    vfail = []
    for _r, _p, v in iter_verifications(s):
        r = vres.get(v["id"], {})
        if v.get("after_merge") or (not G.is_automated(v) and v.get("manual")):
            continue
        if not r.get("pass"):
            vfail.append(v["id"])
    if failed or vfail:
        what = "、".join(([f"チェック {', '.join(failed)}"] if failed else []) +
                        ([f"検証 {', '.join(vfail[:6])}"] if vfail else []))
        route = "gate" if li["seq"] > g["seq"] else fix_route("implementer")
        out.append(P("gate_fail", f"gate が不合格: {what}", route))
        return out

    if not rv:
        bad = [r for r in s.get("reviews", []) if not r.get("valid")]
        why = f"（前回のレビューは無効: {bad[-1].get('invalid_reason')}）" if bad else ""
        out.append(P("review_missing", "独立レビューが未実施" + why, "reviewer"))
        return out
    rs = []
    if rv.get("code_fp") != ctx.code_fp:
        rs.append("コード")
    if rv.get("graph_fp") != ctx.graph_fp:
        rs.append("検証条件")
    if rv.get("checks_fp") != ctx.checks_fp:
        rs.append("チェックの定義")
    if rv.get("spec_hash") != ctx.spec_hash:
        rs.append("仕様")
    if rv.get("design_hash") != ctx.design_hash:
        rs.append("設計")
    if rs:
        out.append(P("review_stale", f"レビュー#{rv['n']} の後に {'・'.join(rs)} が変わった（再レビューが必要）",
                     "reviewer"))
        return out
    if rv.get("blocking", 0) > 0:
        out.append(P("review_blocking", f"レビュー#{rv['n']} の重大な指摘 {rv['blocking']} 件が残っている",
                     fix_route("implementer")))
        return out

    manual_pre = [v["id"] for _r, _p, v in iter_verifications(s)
                  if v.get("manual") and not G.is_automated(v) and not v.get("after_merge")
                  and v.get("confirmed_by") != "human"]
    if manual_pre:
        out.append(P("manual_unconfirmed", f"人間の確認待ちの検証: {', '.join(manual_pre)}", "human"))
    if not s.get("memory_done"):
        out.append(P("memory", "Memory 整理が未実施", "memory"))
    if out:
        return out

    if stage == "pr":
        from .util import sh_run
        if os.path.isdir(os.path.join(ctx.project, ".github", "workflows")) \
                and sh_run("git remote get-url origin", ctx.project, 10)[0] == 0:
            ci = s.get("ci") or {}
            head = sh_run("git rev-parse HEAD", ctx.project, 10)[1].strip()
            if ci.get("sha") != head:
                out.append(P("ci", "最新のコミットに対する CI が未確認", "ci"))
            elif ci.get("result") != "success":
                out.append(P("ci", f"CI が{'失敗' if ci.get('result') == 'failure' else '未完了'}",
                             "ci" if ci.get("result") != "failure" else fix_route("implementer")))
        return out

    # final
    am = []
    for _r, _p, v in iter_verifications(s):
        if not v.get("after_merge"):
            continue
        if v.get("confirmed_by") == "human":
            continue
        r = vres.get(v["id"], {})
        if not G.is_automated(v):
            am.append((v["id"], "human"))
        elif not s.get("after_merge_gate") or r.get("pending_after_merge"):
            am.append((v["id"], "after_merge"))
        elif not r.get("pass"):
            am.append((v["id"], "after_merge_fail"))
    if any(k == "after_merge" for _i, k in am):
        out.append(P("after_merge", "マージ後の検証が未実行: " + ", ".join(i for i, k in am if k == "after_merge"),
                     "after_merge"))
    elif any(k == "after_merge_fail" for _i, k in am):
        out.append(P("after_merge_fail", "マージ後の検証が不合格: "
                     + ", ".join(i for i, k in am if k == "after_merge_fail"), fix_route("implementer")))
    if any(k == "human" for _i, k in am):
        out.append(P("manual_unconfirmed", "マージ後に人間の確認が必要: "
                     + ", ".join(i for i, k in am if k == "human"), "human"))
    return out


PHASE_OF_ROUTE = {"requirements": "要求整理", "human": "人間の確認", "orchestrator": "差し戻しの対応",
                  "design": "設計", "implementer": "実装・修正", "gate": "Quality Gate",
                  "reviewer": "独立レビュー", "memory": "Memory 整理", "ci": "CI の確認",
                  "after_merge": "マージ後の確認"}


def running_agents(state):
    now = time.time()
    return {k: v for k, v in state["agents"].get("running", {}).items()
            if now - v.get("epoch", now) < 3 * 3600}


def next_action(ctx):
    """(フェーズ名, 次にすること, 未充足の一覧)"""
    s = ctx.state
    st = s.get("status")
    if st in ("complete", "closed"):
        return "完了", "この run は完了しています。新しい開発は new で別の run を始めてください。", []
    if st == "aborted":
        return "中断", "この run は中断されています。", []
    run = running_agents(s)
    if run:
        roles = "、".join(ROLE_LABEL.get(v["role"], v["role"]) for v in run.values())
        return "サブエージェント実行中", f"{roles} の完了を待つ（結果はエンジンが自動で記録する）。", []
    if s.get("awaiting_human"):
        return "人間の返答待ち", f"人間の返答を待つ: {s.get('question')}（返答後に resume）", []
    stage = "final" if st == "pr_ready" else "pr"
    probs = problems(ctx, stage)
    if not probs:
        if st == "pr_ready":
            return "完了判定", "complete を実行する（すべての証拠が揃っている）。", []
        return "完了判定", "complete を実行し、PR を作成する。", []
    p = probs[0]
    return PHASE_OF_ROUTE.get(p["route"], p["route"]), instruction(p, ctx), probs


AGENT_CALL = ("Agent ツールで {agent} を呼ぶ（subagent_type: \"{agent}\"）。prompt には目的を1〜2行で書けばよい。"
              "仕様・設計・指摘などの引き継ぎ情報はエンジンが自動で渡し、終了時に成果物を検証して記録する。")


def instruction(p, ctx):
    r, c = p["route"], p["code"]
    head = p["msg"] + "。"
    if r in ROLES:
        hint = AGENT_CALL.format(agent=f"aq-dlc:{r}")
        if r == "requirements" and c == "spec_missing":
            hint += " prompt にはユーザーの依頼文をそのまま含める。"
        return head + "\n→ " + hint
    if r == "human":
        if c == "questions_open":
            return (head + "\n→ show <質問ID> で内容を確認し、AskUserQuestion で人間に聞く（1回で最大4問）。"
                    "回答を answer <質問ID> \"<回答>\" で記録する。")
        if c == "spec_unpresented":
            return (head + "\n→ spec --present で仕様シートを出し、要点を短く見せてから、AskUserQuestion で"
                    "「この仕様で進めてよいか」を確認する。")
        if c == "spec_unapproved":
            return (head + "\n→ 人間が承認したら spec --approve \"<人間の返答の要約>\"。修正の要望なら "
                    "answer ではなく aq-dlc:requirements に修正を依頼する（依頼内容は question で記録してもよい）。")
        if c == "manual_unconfirmed":
            return (head + "\n→ ask \"<確認してほしいこと（まとめて）>\" を実行してから人間に確認し、"
                    "確認できたものを confirm <検証ID> \"<人間の返答の要約>\" で記録する。")
        return head + "\n→ 修正ループが上限に達した。状況を要約し、ask を実行してから人間に方針を確認する。"
    if r == "orchestrator":
        return (head + "\n→ escalation で内容を確認する。仕様の変更が必要なら人間に確認して aq-dlc:requirements、"
                "設計の変更なら aq-dlc:design に依頼する。対応したら escalation <ID> --resolve \"<対応>\"。")
    if r == "gate":
        return head + "\n→ gate を実行する。"
    if r == "memory":
        return head + "\n→ aq-dlc:memory Skill の手順で学びを整理し、memory-done を実行する。"
    if r == "ci":
        return head + "\n→ 作業ブランチを commit・push し、ci --wait 8 で結果を確認する。"
    if r == "after_merge":
        return (head + "\n→ PR がマージされたら既定ブランチを取り込み、gate --after-merge を実行する。")
    return head


# ------------------------------------------------------------------ display helpers
def spec_sheet(state):
    lines = [f"# 仕様の確認: {state.get('title')}", ""]
    for kind, label in (("functional", "機能要件"), ("nonfunctional", "非機能要件")):
        reqs = [r for r in state["requirements"] if r.get("kind", "functional") == kind]
        if not reqs:
            continue
        lines += [f"## {label}"]
        for r in reqs:
            lines.append(f"- {r['id']}{' 【重要】' if r.get('critical') else ''} {r.get('text')}")
            for a in r.get("acceptance", []):
                lines.append(f"    - 受け入れ条件 {a['id']}: {a.get('text')}")
            for k in r.get("risks", []):
                vs = "、".join(v.get("method", "") + ("（マージ後）" if v.get("after_merge") else "")
                              + ("（人が確認）" if v.get("manual") and not G.is_automated(v) else "")
                              for v in k.get("verifications", []))
                lines.append(f"    - 想定する失敗 {k['id']}: {k.get('text')}" + (f" → 確かめ方: {vs}" if vs else ""))
        lines.append("")
    if state.get("assumptions"):
        lines += ["## こちらで決めたこと（仮定）"] + [f"- {a['id']} {a['text']}" for a in state["assumptions"]] + [""]
    if state.get("out_of_scope"):
        lines += ["## 今回は作らないもの"] + [f"- {o['id']} {o['text']}" for o in state["out_of_scope"]] + [""]
    qs = state.get("questions", [])
    if qs:
        lines += ["## 質問と回答"]
        for q in qs:
            lines.append(f"- {q['id']} {q['text']} → {q.get('answer') or '（未回答）'}")
    return "\n".join(lines).rstrip() + "\n"


def trace_rows(ctx):
    s = ctx.state
    g = s.get("gate") or {}
    vres = (g.get("results") or {}).get("verifications", {})
    fresh_code = g.get("code_fp") == ctx.code_fp and g.get("checks_fp") == ctx.checks_fp
    rows = []
    for r in s["requirements"]:
        design = s["design"]["map"].get(r["id"], "")
        files = ", ".join(s["impl"]["map"].get(r["id"], []))
        nodes = [("受入", a) for a in r.get("acceptance", [])] + [("リスク", k) for k in r.get("risks", [])]
        if not nodes:
            rows.append([r["id"], "", design, files, "", "", "", ""])
        for label, n in nodes:
            vs = n.get("verifications", []) or [{}]
            for v in vs:
                res = vres.get(v.get("id"), {}) if v else {}
                if not v:
                    status = "検証なし"
                elif v.get("confirmed_by") == "human":
                    status = "人が確認済み"
                elif res.get("pending_after_merge"):
                    status = "マージ後"
                elif not res:
                    status = "未実行"
                else:
                    ok = res.get("pass")
                    fresh = fresh_code and res.get("fp") == G.verification_fp(v, ctx.cfg)
                    status = ("合格" if ok else "不合格") + ("" if fresh else "（古い）")
                rows.append([r["id"], f"{label} {n['id']}", design, files,
                             f"{v.get('id', '')}" + (f"（{v.get('level')}）" if v.get("level") else ""),
                             v.get("test") or v.get("command") or ",".join(v.get("checks") or [])
                             or (v.get("manual") and "人が確認") or "",
                             status, short(g.get("stamp", ""))[:15] if g else ""])
    return rows


def first_line(s, n=60):
    s = (s or "").splitlines()[0] if s else ""
    return s if len(s) <= n else s[:n - 1] + "…"


def describe_state(ctx):
    s = ctx.state
    spec, d = s.get("spec") or {}, s["design"]
    li, rv = last_impl(s), latest_review(s)
    lines = [
        f"仕様: {short(ctx.spec_hash)}  登録 v{spec.get('version', 0)}"
        f"  承認{'済み' if spec.get('approved_hash') == ctx.spec_hash else 'なし' if not spec.get('approved_hash') else '（古い）'}",
        f"設計: {short(ctx.design_hash) if ctx.design_hash else '未作成'}"
        f"  登録{'済み' if d.get('registered_hash') and d.get('registered_hash') == ctx.design_hash else 'なし'}",
        f"実装: {('第' + str(li['n']) + '回') if li else '未着手'}  修正回数: {s.get('fix_iterations', 0)}",
        f"コード: {short(ctx.code_fp)}  チェック: {len(ctx.checks)}件",
        f"レビュー: {('#' + str(rv['n']) + ' ' + rv['verdict'] + ' 重大' + str(rv['blocking'])) if rv else '未実施'}",
    ]
    bad = [r for r in s.get("reviews", []) if not r.get("valid")]
    if bad and (not rv or bad[-1]["n"] > rv["n"]):
        lines.append(f"  ! 最後のレビュー#{bad[-1]['n']} は無効: {bad[-1].get('invalid_reason')}")
    return lines
