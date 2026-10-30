"""E-AQDL Phase 3・4: Counterfactual Gate（反証テスト）と Negative Evidence Graph。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (IMPL_MD, MEMO_V1, REVIEW_PASS, ROOT, TEST_MEMO, Env, build_to_review, denied,  # noqa: E402
                     read_file, write_file)

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import counterfactual as CF  # noqa: E402
from aqdlc_core import mutation as MU  # noqa: E402

PY = f'"{sys.executable}"'


class TestOperators(unittest.TestCase):
    def ap(self, op, src, i=0, lang="python"):
        lines = src.splitlines(keepends=True)
        return MU.apply(op, lines, i, lang)

    def test_python_operators(self):
        src = 'if text == "":\n    raise ValueError("x")\n'
        self.assertEqual(self.ap("negate_if", src), 'if not (text == ""):\n')
        self.assertEqual(self.ap("guard_off", src), 'if False:\n')
        self.assertEqual(self.ap("eq_flip", src), 'if text != "":\n')
        self.assertIn("pass", self.ap("remove_raise", src, 1))
        self.assertEqual(self.ap("boundary", "    if n <= 10:\n"), "    if n < 10:\n")
        self.assertEqual(self.ap("and_or", "    if a and b:\n"), "    if a or b:\n")
        self.assertEqual(self.ap("return_bool", "    return True\n"), "    return False\n")
        self.assertIsNone(self.ap("guard_off", "if x:\n    y = 1\n"))  # ガード節ではない
        self.assertIsNone(self.ap("eq_flip", 'msg = "a == b"\n'))  # 文字列の中は変えない
        self.assertIsNone(self.ap("negate_if", "# if x:\n"))  # コメント
        self.assertIsNone(self.ap("boundary", "def f(x) -> int:\n"))

    def test_js_operators(self):
        js = "  if (!user || user.role !== 'admin') { throw new Error('forbidden'); }\n"
        self.assertEqual(self.ap("guard_off", js, lang="js"), "  if (false) { throw new Error('forbidden'); }\n")
        self.assertIn("!(!user", self.ap("negate_if", js, lang="js"))
        self.assertIn("user.role === 'admin'", self.ap("eq_flip", js, lang="js"))
        self.assertIn("&&", self.ap("and_or", js, lang="js"))
        self.assertIn("void 0;", self.ap("remove_raise", js, lang="js"))
        self.assertEqual(MU.category("guard_off", js), "認可条件の除去")
        self.assertIsNone(self.ap("boundary", "const f = (a) => a;\n", lang="js"))
        self.assertIsNone(self.ap("boundary", "let x: Array<string> = [];\n", lang="js"))
        sql = "  db.run('UPDATE notes SET body = ? WHERE id = ? AND owner = ?', [b, id, me]);\n"
        self.assertIn("WHERE id = ? OR owner = ?", self.ap("sql_where", sql, lang="js"))

    def test_classification(self):
        self.assertEqual(CF.classify(0, "OK", False)[0], "survived")
        self.assertEqual(CF.classify(1, "FAILED (failures=1)\nAssertionError: 1 != 2", False)[0], "killed")
        self.assertEqual(CF.classify(1, "SyntaxError: invalid syntax", False)[0], "invalid")
        self.assertEqual(CF.classify(1, "error TS2304: Cannot find name 'x'", False)[0], "invalid")
        self.assertEqual(CF.classify(127, "sh: vitest: command not found", False)[0], "invalid")
        self.assertEqual(CF.classify(1, "something odd", False)[0], "invalid")  # 期待値の違反と確認できない
        self.assertEqual(CF.classify(124, "タイムアウト", True)[0], "timeout")
        self.assertEqual(CF.classify(2, "ERROR: tests/x.py\nImportError while importing test module", False)[0],
                         "invalid")

    def test_secret_env_is_not_passed(self):
        os.environ["GITHUB_TOKEN"] = "ghp_dummy_value_for_test_123456"
        os.environ["MY_API_KEY"] = "dummy"
        os.environ["DATABASE_URL"] = "postgres://prod"
        try:
            env = CF.safe_env({"gate": {"env": {"DATABASE_URL": "sqlite:///test.db"}}})
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("MY_API_KEY", env)
            self.assertEqual(env["DATABASE_URL"], "sqlite:///test.db")  # 人間が設定したテスト用の値だけ
            self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:9")
            self.assertIn("＊＊＊", CF.mask("token ghp_dummy_value_for_test_123456", ["ghp_dummy_value_for_test_123456"]))
        finally:
            for k in ("GITHUB_TOKEN", "MY_API_KEY", "DATABASE_URL"):
                os.environ.pop(k, None)


class TestCounterfactualGate(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False,
                        config={"counterfactual": {"mode": "enforce", "timeout_sec": 60}})

    def tearDown(self):
        self.e.cleanup()

    def cf_dir(self):
        return os.path.join(self.e.home, "tmp", "cf")

    def test_full_cycle_killed_survived_and_fix(self):
        e = self.e
        self.assertIn("反証テスト", e.next())
        self.assertTrue(denied(e.spawn("reviewer")))  # 反証テストの前にレビューしない
        before = read_file(os.path.join(e.project, "src", "memo.py"))
        out = e.aq("cf")
        self.assertIn("killed", out)
        self.assertEqual(read_file(os.path.join(e.project, "src", "memo.py")), before)  # 元のコードは変えない
        self.assertEqual(os.listdir(self.cf_dir()), [])  # 隔離環境は削除済み
        rec = e.state()["counterfactual"]["latest"]
        self.assertEqual(rec["status"], "done")
        self.assertGreater(rec["summary"]["counts"]["killed"], 0)
        for m in rec["mutations"]:
            for k in ("id", "verification", "requirement", "operator", "file", "line", "expected", "result",
                      "command", "log", "original_hash"):
                self.assertIn(k, m)
        # V1（AC1: 追加の単体テスト）は空のメモの拒否を確かめていない → raise を消しても通過する
        m1 = next(m for m in rec["mutations"] if m["verification"] == "V1" and m["result"] == "survived")
        self.assertEqual(m1["operator"], "remove_raise")
        ev = json.loads(e.aq("evidence", "--json"))
        st = {r["verification"]: r["state"] for r in ev["verifications"]}
        self.assertEqual(st["V2"], "verified")  # 通常 Gate 合格＋欠陥を検出
        self.assertEqual(st["V1"], "insufficient")  # 見逃しあり
        nxt = e.next()
        self.assertIn("見逃した", nxt)
        self.assertIn("実装", nxt)
        self.assertIn("insufficient", e.aq("evidence"))
        self.assertTrue(denied(e.spawn("reviewer")))
        # 実装担当がテストを強化する
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"反証{m1['id']}", brief)
        self.assertIn("Adversarial Test Engineer", brief)  # 差し戻しで反証の専門性が付く
        e.write_as("implementer", "tests/test_memo.py", TEST_MEMO.replace(
            'self.assertEqual(memo.add([], "a"), ["a"])',
            'self.assertEqual(memo.add([], "a"), ["a"])\n        with self.assertRaises(ValueError):\n'
            '            memo.add([], "")'))
        s = e.state()
        e.write_as("implementer", os.path.relpath(os.path.join(e.arts, "impl.md"), e.project), IMPL_MD.format(
            design=s["design"]["registered_hash"][:8], spec=s["spec"]["registered_hash"][:8],
            extra=f"## 指摘への対応\n- 反証{m1['id']}: test_add で空のメモが拒否されることも確認した"))
        self.assertIsNone(e.stop("implementer", "テストを強化"))
        self.assertIn("gate", e.next())
        e.aq("gate")
        e.aq("cf")
        rec = e.state()["counterfactual"]["latest"]
        self.assertEqual(rec["summary"]["counts"]["survived"], 0)
        self.assertIn("独立レビュー", e.next())
        m = json.loads(e.aq("metrics", "--json"))
        self.assertEqual(m["counterfactual"]["runs"], 2)
        self.assertIn("killed", m["counterfactual"]["score_definition"])
        self.assertGreaterEqual(m["role_profiles"]["by_trigger"]["sendback"], 1)
        self.assertEqual(m["memory"]["scope"], "project")
        self.assertEqual(m["conditions"]["engine"], "E-AQDL")
        self.assertIn("E-AQDL", e.aq("metrics", "--runs"))

    def test_reviewer_must_address_survived_in_advisory(self):
        e = self.e
        from helpers import edit_json
        edit_json(os.path.join(e.project, ".aq-dlc", "config.json"),
                  lambda c: c["counterfactual"].update(mode="advisory"))
        self._accept_config()
        e.aq("gate")  # 設定が変わったので gate からやり直す
        e.aq("cf-add", "V1", "--file", "src/memo.py", "--line", "2", "--op", "guard_off")
        e.aq("cf")
        surv = [m["id"] for m in e.state()["counterfactual"]["latest"]["mutations"] if m["result"] == "survived"]
        self.assertIn("M1", surv)
        self.assertIn("独立レビュー", e.next())  # advisory では見逃しでも止めない
        e.spawn("reviewer")
        brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("survived M1", brief)
        res = e.stop("reviewer", REVIEW_PASS)
        self.assertIn("M1", res["reason"])  # survived に触れていない
        note = "".join(f"\n- {i}: test_add が空のメモを確かめていない（軽微）" for i in surv)
        e.stop("reviewer", REVIEW_PASS.replace("- python3 -m unittest", "- python3 -m unittest" + note))
        self.assertTrue(e.state()["reviews"][-1]["passed"])

    def _accept_config(self):
        import re
        e = self.e
        target = re.search(r"承認 (CFG-[0-9a-f]{8})", e.next()).group(1)
        e.aq("ask", "設定の変更を受け入れますか")
        e.human_says(f"承認 {target}")
        e.aq("config-accept", "受け入れ")

    def test_mode_off_and_stale(self):
        e = self.e
        e.aq("cf")
        e.write_as("implementer", "src/memo.py", MEMO_V1 + "\n")  # 担当外の変更 → 反証テストも失効
        self.assertTrue(CF.stale_reasons(self._ctx()))
        self.assertIn("invalidated", e.aq("evidence"))

    def _ctx(self):
        os.environ["AQDLC_HOME"] = self.e.home
        from aqdlc_core.state import Ctx, load_state
        s, _ = load_state(self.e.project)
        return Ctx(self.e.project, s)

    def test_cf_add_rejects_bad_paths(self):
        e = self.e
        e.aq("cf-add", "V1", "--file", "../outside.py", "--line", "1", "--op", "negate_if", ok=False)
        try:
            os.symlink(os.path.join(e.project, "src", "memo.py"), os.path.join(e.project, "src", "link.py"))
        except (OSError, NotImplementedError):
            link = False  # Windows で権限がない場合
        else:
            link = True
        if link:
            e.aq("cf-add", "V1", "--file", "src/link.py", "--line", "2", "--op", "negate_if", ok=False)
        e.aq("cf-add", "V1", "--file", "src/memo.py", "--line", "4", "--op", "negate_if", ok=False)  # 適用不可
        e.aq("cf-add", "V1", "--file", "src/memo.py", "--line", "1", "--op", "rm -rf /", ok=False)
        self.assertTrue(denied(e.as_role("implementer", "cf-add", "V1", "--file", "src/memo.py", "--line", "1",
                                         "--op", "negate_if", ok=None)))

    def test_baseline_failure_and_timeout_are_not_detection(self):
        e = self.e
        ctx = self._ctx()
        s = ctx.state
        cfg = dict(ctx.cfg, counterfactual={"mode": "enforce", "timeout_sec": 3, "max_mutations": 2,
                                            "per_verification": 1})
        for _r, _p, v in CF.targets_of(s):
            v.pop("checks", None)
            if v["id"] == "V1":
                v["command"] = f'{PY} -c "import sys; sys.exit(1)"'  # 隔離環境で最初から失敗
            else:
                v["command"] = (f'{PY} -c "import time; t=open(\'src/memo.py\').read(); '
                                f'time.sleep(20 if (\'False\' in t or \'pass\' in t or \'not (\' in t) else 0)"')
        muts, _ = CF.plan(e.project, s, cfg)
        rec = CF.execute(e.project, s, cfg, muts, os.path.join(e.tmp, "cflog"), "t1")
        res = {m["verification"]: m for m in rec["mutations"]}
        self.assertEqual(res["V1"]["result"], "invalid")
        self.assertIn("ベースライン", res["V1"]["reason"])
        others = [(m["id"], m["result"], m["reason"]) for m in rec["mutations"] if m["verification"] != "V1"]
        self.assertIn("timeout", {r for _i, r, _w in others}, others)
        self.assertEqual(os.listdir(self.cf_dir()), [])

    def test_stale_sandbox_is_cleaned(self):
        d = os.path.join(self.cf_dir(), "old-1")
        os.makedirs(d)
        write_file(os.path.join(d, ".aqdlc-cf-owner"), "999999")
        os.environ["AQDLC_HOME"] = self.e.home
        self.assertEqual(CF.cleanup_stale(), 1)
        self.assertFalse(os.path.exists(d))

    def test_equivalent_needs_human_approval(self):
        e = self.e
        e.aq("cf-add", "V1", "--file", "src/memo.py", "--line", "2", "--op", "guard_off")
        e.aq("cf")
        rec = e.state()["counterfactual"]["latest"]
        stamp = rec["stamp"]
        others = [m["id"] for m in rec["mutations"] if m["result"] == "survived" and m["id"] != "M1"]
        e.aq("cf-mark", "M1", "--equivalent", "等価", ok=False)
        e.aq("ask", "M1 は等価ですか")
        e.human_says("はい")
        e.aq("cf-mark", "M1", "--equivalent", "等価", ok=False)
        e.human_says(f"承認 EQ-{stamp}-M1")
        e.aq("cf-mark", "M1", "--equivalent", "振る舞いが変わらないと人間が判断")
        rec = e.state()["counterfactual"]["latest"]
        self.assertEqual(rec["summary"]["equivalent"], 1)
        self.assertTrue(rec["mutations"][0]["equivalent"]["approval"].startswith("EQ-"))
        if not others:
            self.assertNotIn("見逃した", e.next())


class TestModeOff(unittest.TestCase):
    def test_off_is_distinguished_from_verified(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            out = e.aq("cf", ok=False)
            self.assertIn("off", out)
            self.assertNotIn("反証テスト", e.next())
            ev = e.aq("evidence")
            self.assertIn("passed_but_unchallenged", ev)
            self.assertNotIn("| verified", ev)
            self.assertIn("V1", e.aq("cf", "--plan"))  # 計画の確認はできる
            # 検証条件の変更は、その検証だけを失効させる（過剰に失効させない）
            s = e.state()
            s["requirements"][0]["acceptance"][0]["verifications"][0]["timeout_sec"] = 99
            e.save_state(s)
            rows = {r["verification"]: r for r in json.loads(e.aq("evidence", "--json"))["verifications"]}
            self.assertEqual(rows["V1"]["state"], "invalidated")
            self.assertIn("検証条件の変更", rows["V1"]["causes"])
            self.assertEqual(rows["V2"]["state"], "passed_but_unchallenged")
            self.assertIn("失効の原因", e.aq("trace"))
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
