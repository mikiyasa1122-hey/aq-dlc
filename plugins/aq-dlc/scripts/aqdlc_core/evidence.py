"""Negative Evidence Graph: 証明できた範囲と、まだ証明できていない範囲を区別して示す。

要求 → 受け入れ条件 / リスク → 設計 → 実装 → 検証（テスト）→ 通常 Gate → 反証テスト → 独立レビュー

検証（V）ごとの状態:
  verified                 その検証の契約を満たす証拠がある（通常 Gate 合格＋反証テストで欠陥を検出、
                           または人が確認する検証で、対象を指定した人間の確認がある）
  passed_but_unchallenged  通常 Gate は合格したが、反証テストが未実施（無効・未実行）
  insufficient             必要な証拠が不足（未実行・不合格・実行方法なし・欠陥を見逃した・反証が不成立）
  invalidated              変更により証拠が失効した（失効の原因を示す）
  not_applicable           反証テストを適用できない（対応外の言語・適用できる箇所がない）。通常 Gate は合格

通常 Gate の判定と反証テストの判定は別の層として記録し、混同しない。
失効は依存関係に従う: コード・チェックの定義・設定は全体を、検証条件の変更はその検証だけを失効させる。
"""
from . import counterfactual as CF
from . import gate as G
from .state import current_review, iter_verifications, review_passed

ORDER = ("insufficient", "invalidated", "passed_but_unchallenged", "not_applicable", "verified")
LABEL = {"verified": "証明済み", "passed_but_unchallenged": "合格・反証未実施", "insufficient": "証拠不足",
         "invalidated": "失効", "not_applicable": "合格・反証は対象外"}


def gate_global_stale(ctx):
    g = ctx.state.get("gate") or {}
    if not g:
        return []
    out = []
    if g.get("code_fp") != ctx.code_fp:
        out.append("コードの変更（" + (G.describe_diff(g.get("snapshot"), ctx.snap) or "内容が変化") + "）")
    if g.get("checks_fp") != ctx.checks_fp:
        out.append("チェックの定義の変更")
    if g.get("config_fp") and g.get("config_fp") != ctx.config_fp:
        out.append("設定の変更")
    if g.get("modified_during_gate"):
        out.append("gate の実行中の変更")
    st = ctx.state
    sp = st.get("spec") or {}
    li = ((st.get("impl") or {}).get("rounds") or [None])[-1]
    if sp.get("approved_hash") and sp["approved_hash"] != ctx.spec_hash:
        out.append("承認後の仕様の変更")
    elif li and li.get("spec_hash") and li["spec_hash"] != ctx.spec_hash:
        out.append("実装後の仕様の変更")
    if li and li.get("design_hash") and li["design_hash"] != ctx.design_hash:
        out.append("実装後の設計の変更")
    return out


def compute(ctx):
    s, cfg = ctx.state, ctx.cfg
    g = s.get("gate") or {}
    vres = (g.get("results") or {}).get("verifications", {})
    gstale = gate_global_stale(ctx)
    cc = CF.cf_config(cfg)
    crec = CF.current(s)
    incomplete = bool(crec) and crec.get("status") != "done" and cc["mode"] != "off"
    cstale = [x for x in CF.stale_reasons(ctx, crec) if x not in ("中断された", "実行中に作業ツリーが変わった",
                                                                   "完了していない")] \
        if (crec and cc["mode"] != "off") else []
    bv = CF.by_verification(crec) if crec else {}
    rv = current_review(s)
    review = "未実施"
    if rv:
        review = "合格" if review_passed(rv) else "要修正"
        if rv.get("code_fp") != ctx.code_fp or rv.get("graph_fp") != ctx.graph_fp:
            review += "（古い）"
    rows = []
    for r, p, v in iter_verifications(s):
        res = vres.get(v["id"])
        causes, basis = [], []
        # ---- 通常 Gate の層
        if v.get("manual") and not G.is_automated(v):
            layer_g = "人の確認済み" if v.get("confirmed_by") == "human" else "人の確認待ち"
        elif v.get("after_merge") and not (res and not res.get("pending_after_merge")):
            layer_g = "マージ後に確認" if v.get("confirmed_by") != "human" else "人の確認済み"
        elif not G.is_automated(v):
            layer_g = "実行方法なし"
        elif res is None:
            layer_g = "未実行"
        else:
            layer_g = "合格" if res.get("pass") else "不合格"
            if res.get("fp") != G.verification_fp(v, cfg):
                causes.append("検証条件の変更")
            causes += gstale
        # ---- 反証テストの層
        ms = [m for m in bv.get(v["id"], []) if not m.get("equivalent")]
        eq = [m for m in bv.get(v["id"], []) if m.get("equivalent")]
        if cc["mode"] == "off":
            layer_c = "無効"
        elif not G.is_automated(v) or v.get("after_merge"):
            layer_c = "対象外"
        elif not crec:
            layer_c = "未実行"
        elif incomplete:
            layer_c = f"未完了（{crec.get('status')}）"
        elif cstale:
            layer_c = "失効"
        elif not ms:
            layer_c = "未実施（Mutation の上限）" if v["id"] in crec.get("unrun", []) else "対象外"
        else:
            kinds = [m["result"] for m in ms]
            if "survived" in kinds:
                layer_c = "見逃しあり（" + ", ".join(m["id"] for m in ms if m["result"] == "survived") + "）"
            elif "killed" in kinds:
                layer_c = "検出（" + ", ".join(m["id"] for m in ms if m["result"] == "killed") + "）"
            elif all(k == "not_applicable" for k in kinds):
                layer_c = "対象外"
            else:
                layer_c = "不成立（" + "/".join(sorted(set(kinds))) + "）"
        if cstale and cc["mode"] != "off" and G.is_automated(v) and not v.get("after_merge"):
            causes += [f"反証テスト後の{x}" for x in cstale if x not in ("未実行",)]
        if eq:
            basis.append("等価として除外: " + ", ".join(m["id"] for m in eq))
        # ---- 総合
        if layer_g in ("人の確認済み",):
            state = "verified"
            basis.append("対象を指定した人間の確認")
        elif layer_g in ("人の確認待ち", "マージ後に確認", "実行方法なし", "未実行", "不合格"):
            state = "insufficient"
            basis.append(f"通常 Gate: {layer_g}")
        elif causes:
            state = "invalidated"
        elif layer_c.startswith("見逃し") or layer_c.startswith("不成立") or layer_c.startswith("未実施"):
            state = "insufficient"
            basis.append(f"反証テスト: {layer_c}")
        elif layer_c.startswith("検出"):
            state = "verified"
            basis.append(f"通常 Gate 合格（{g.get('stamp')}）・反証テスト {layer_c}")
        elif layer_c == "対象外" and cc["mode"] != "off":
            state = "not_applicable"
            basis.append(f"通常 Gate 合格（{g.get('stamp')}）。反証テストを適用できる箇所がない")
        else:
            state = "passed_but_unchallenged"
            basis.append(f"通常 Gate 合格（{g.get('stamp')}）。反証テスト: {layer_c}")
        rows.append({"requirement": r["id"], "critical": bool(r.get("critical")), "parent": p["id"],
                     "parent_kind": "risk" if p in r.get("risks", []) else "acceptance", "verification": v["id"],
                     "kind": ("人の確認" if v.get("manual") and not G.is_automated(v) else
                              v.get("level") or "自動"),
                     "gate": layer_g, "counterfactual": layer_c, "review": review, "state": state,
                     "basis": basis, "causes": causes})
    return rows


def by_node(ctx, rows=None):
    """受け入れ条件・リスクごとの総合（最も弱い検証の状態）。検証のないものは insufficient。"""
    rows = rows if rows is not None else compute(ctx)
    out = {}
    for r in ctx.state.get("requirements", []):
        for kind, nodes in (("acceptance", r.get("acceptance", [])), ("risk", r.get("risks", []))):
            for n in nodes:
                vs = [x for x in rows if x["parent"] == n["id"]]
                st = min((x["state"] for x in vs), key=ORDER.index) if vs else "insufficient"
                out[n["id"]] = {"requirement": r["id"], "kind": kind, "critical": bool(r.get("critical")),
                                "state": st, "verifications": [x["verification"] for x in vs]}
    return out


def summary(ctx):
    rows = compute(ctx)
    nodes = by_node(ctx, rows)
    counts = {k: 0 for k in ORDER}
    for n in nodes.values():
        counts[n["state"]] += 1
    return rows, nodes, counts
