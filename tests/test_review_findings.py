"""独立コードレビュー（v0.5.1 作業中）で再現された誤った合格の経路の回帰テスト。"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (Env, bash_as, blocked, build_to_review, denied, edit_json, read_file,  # noqa: E402
                     write_file)
from test_p0 import MAJOR, MINOR, review  # noqa: E402


def finish(e):
    e.aq("memory-done", "--skipped", "テスト用の run")


class VerdictParsingTest(unittest.TestCase):
    """重大1: 判定行の後ろに文字が続く・報告形式の例をそのまま写した判定を合格として読まない。"""

    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def reviewed(self, msg):
        e = self.e
        self.assertFalse(denied(e.spawn("reviewer")))
        e.start("reviewer")
        for _ in range(4):
            if not blocked(e.stop("reviewer", msg)):
                break
        return e.state()["reviews"][-1]

    def assert_blocked_completion(self):
        finish(self.e)
        self.e.aq("check", ok=False)
        self.e.aq("complete", ok=False)

    def test_trailing_text(self):
        rv = self.reviewed(review("判定: 合格 ではなく要修正", 0, 1, MINOR))
        self.assertFalse(rv.get("passed"))
        self.assert_blocked_completion()

    def test_both_words(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "plugins", "aq-dlc", "scripts"))
        from aqdlc_core.agents import parse_review
        for line in ("判定: 合格 / 要修正", "判定: 合格 または 要修正", "判定: <合格 か 要修正 の1語だけ>",
                     "判定: 合格 ではなく要修正", "判定: 合格（ただし要確認）", "判定: たぶん合格"):
            with self.subTest(line=line):
                rec, errs = parse_review(review(line, 0, 0))
                self.assertFalse(rec["passed"])
                self.assertTrue(errs)
        rec, errs = parse_review(review("判定: 合格", 0, 0))
        self.assertTrue(rec["passed"])
        self.assertFalse(errs)

    def test_template_line_is_not_a_pass(self):
        rv = self.reviewed(review("判定: <合格 か 要修正 の1語だけ>", 0, 0))
        self.assertFalse(rv["valid"])
        self.assert_blocked_completion()

    def test_conflicting_verdict_lines(self):
        rv = self.reviewed(review("判定: 合格\n判定: 要修正", 0, 1, MINOR))
        self.assertFalse(rv["valid"])
        self.assert_blocked_completion()

    def test_bold_verdict_is_accepted(self):
        rv = self.reviewed(review("**判定**: 合格", 0, 0))
        self.assertTrue(rv["valid"])
        self.assertTrue(rv["passed"])


class FindingNotationTest(unittest.TestCase):
    """重大2: 太字・番号付き・全角の「重大」も数え、「合格」との矛盾として扱う。"""

    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def test_variants(self):
        e = self.e
        variants = ["- **[重大1]** 区分: 実装 / 対象: R1 / 内容: 空白が保存される",
                    "1. [重大1] 区分: 実装 / 対象: R1 / 内容: 空白が保存される",
                    "- ［重大1］ 区分: 実装 / 対象: R1 / 内容: 空白が保存される",
                    "- 【重大1】 区分: 実装 / 対象: R1 / 内容: 空白が保存される"]
        for i, f in enumerate(variants):
            with self.subTest(f=f):
                self.assertFalse(denied(e.spawn("reviewer")), i)
                e.start("reviewer")
                res = e.stop("reviewer", review("判定: 合格", 0, 0, f))
                self.assertTrue(blocked(res))
                for _ in range(3):
                    e.stop("reviewer", review("判定: 合格", 0, 0, f))
                rv = e.state()["reviews"][-1]
                self.assertFalse(rv["valid"])
                self.assertGreaterEqual(rv["blocking"], 1)


class FingerprintTest(unittest.TestCase):
    """重大3: サイズと更新時刻を保った書き換えも検出する。"""

    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    SAME_SIZE = ("import os; p='src/memo.py'; st=os.stat(p); s=open(p).read().replace('==','!='); "
                 "open(p,'w').write(s); os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))")

    def test_orchestrator_same_size_edit_is_detected(self):
        e = self.e
        pre, post = bash_as(e, "orchestrator", f"\"{sys.executable}\" -c \"{self.SAME_SIZE}\"")
        self.assertIn("!=", read_file(os.path.join(e.project, "src", "memo.py")))
        self.assertTrue(e.state().get("violations"))
        self.assertTrue(denied(e.spawn("reviewer")))

    def test_hookless_same_size_edit_blocks_completion(self):
        e = self.e
        e.spawn("reviewer")
        e.start("reviewer")
        from helpers import REVIEW_PASS
        self.assertIsNone(e.stop("reviewer", REVIEW_PASS))
        finish(e)
        e.aq("check")
        subprocess.run([sys.executable, "-c", self.SAME_SIZE], cwd=e.project, check=True)
        out = e.aq("check", ok=False)
        self.assertIn("src/memo.py", out)
        e.aq("complete", ok=False)


class ConfigTamperTest(unittest.TestCase):
    """重大4・5: 設定の改ざん（指紋の対象外にする・自動化の境界を変える）を検出し、全体設定は書かせない。"""

    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def test_bash_config_tamper(self):
        e = self.e
        cmd = (f"\"{sys.executable}\" -c \"import json; p='.aq-dlc/config.json'; c=json.load(open(p)); "
               "c.setdefault('gate',{})['ignore_dirs']=['src','tests']; json.dump(c,open(p,'w'))\"")
        bash_as(e, "orchestrator", cmd)
        v = e.state()["violations"][-1]
        self.assertIn("設定", v.get("note", ""))
        self.assertFalse(v.get("files"))  # 対象を外しても、ファイルが消えたことにはならない
        out = e.aq("check", ok=False)
        self.assertIn("設定", out)
        for role in ("design", "implementer", "reviewer"):
            self.assertTrue(denied(e.spawn(role)), role)
        # ignore を変えた後にコードを壊しても、完了できない
        with open(os.path.join(e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
            f.write("raise SystemExit\n")
        e.aq("complete", ok=False)

    def test_enforce_roles_tamper_is_detected(self):
        e = self.e
        edit_json(os.path.join(e.home, "config.json"), lambda c: c.setdefault("automation", {}).update(
            enforce_roles=False))
        self.assertIn("設定", e.aq("check", ok=False))

    def test_global_config_write_is_denied(self):
        e = self.e
        for role in ("orchestrator", "implementer", "requirements"):
            res = e.hook("pre-write", {"tool_name": "Write", "tool_input": {
                "file_path": os.path.join(e.home, "config.json"), "content": "{}"}, **e.agent_fields(role)})
            self.assertTrue(denied(res), role)
        # Memory は統括だけが書ける
        res = e.hook("pre-write", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(e.home, "memory", "success", "W-9.md"), "content": "x"}})
        self.assertFalse(denied(res))

    def test_agent_definition_impersonation(self):
        e = self.e
        res = e.hook("pre-write", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(e.project, ".claude", "agents", "implementer.md"), "content": "x"}})
        self.assertTrue(denied(res))
        # 接頭辞のない同名のエージェントは実装担当として扱わない
        res = e.hook("pre-write", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(e.project, "src", "memo.py"), "content": "x"},
            "agent_type": "implementer", "agent_id": "fake"})
        self.assertTrue(denied(res))


class ViolationTest(unittest.TestCase):
    """中程度6・7: 記録された担当外の変更は取り込むまで進めない。検出の記録を消しても見逃さない。"""

    def tearDown(self):
        self.e.cleanup()

    def test_violation_before_first_implementation(self):
        self.e = e = Env()
        e.aq("init", "x")
        e.aq("new", "x")
        os.makedirs(os.path.join(e.project, "src"), exist_ok=True)
        bash_as(e, "orchestrator", "echo 'x = 1' > src/early.py")
        self.assertTrue(e.state()["violations"])
        self.assertTrue(e.state()["violations"][-1].get("resolved") is None)
        from helpers import implement, SPEC_MD, DESIGN_MD  # noqa: F401
        # 仕様・設計を通して実装担当まで進める
        build_rest(e)
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("src/early.py", brief)
        implement(e, "def add(m, t):\n    m.append(t)\n    return m\n")
        res = e.stop("implementer", "")
        self.assertTrue(blocked(res))
        self.assertIn("src/early.py", res["reason"])
        p = os.path.join(e.arts, "impl.md")
        write_file(p, read_file(p) + "\n## 指摘への対応\n- [担当外1] src/early.py は不要なので削除した\n")
        os.remove(os.path.join(e.project, "src", "early.py"))
        self.assertIsNone(e.stop("implementer", ""))
        self.assertTrue(e.state()["violations"][-1]["resolved"])

    def test_deleting_the_record_is_detected(self):
        self.e = e = Env()
        build_to_review(e, with_question=False)
        tmp = os.path.join(e.home, "tmp").replace(os.sep, "/")
        bash_as(e, "orchestrator", f"rm -rf '{tmp}'; echo '# evil' >> src/memo.py")
        v = e.state()["violations"][-1]
        self.assertEqual(v["kind"], "tamper")
        self.assertTrue(denied(e.spawn("reviewer")))


class SourceSetTest(unittest.TestCase):
    """中程度8: git で追跡しているファイルは ignore_dirs の名前でも指紋の対象に含める。"""

    def test_tracked_build_dir_is_fingerprinted(self):
        e = Env(git=True)
        try:
            build_to_review(e, with_question=False)
            os.makedirs(os.path.join(e.project, "src", "build"), exist_ok=True)
            write_file(os.path.join(e.project, "src", "build", "helper.py"), "X = 1\n")
            e.git("add", "-A")
            e.git("commit", "-q", "-m", "x")
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            "plugins", "aq-dlc", "scripts"))
            from aqdlc_core import gate as G
            from aqdlc_core.project import DEFAULTS
            files = G.source_files(e.project, DEFAULTS)
            self.assertIn("src/build/helper.py", files)
            os.makedirs(os.path.join(e.project, "dist"), exist_ok=True)
            write_file(os.path.join(e.project, "dist", "out.js"), "x")  # 追跡していないビルド出力は対象外
            self.assertNotIn("dist/out.js", G.source_files(e.project, DEFAULTS))
        finally:
            e.cleanup()


class LoopCapTest(unittest.TestCase):
    """中程度9: 担当外の変更による修正ループも、修正回数の上限で人間に回す。"""

    def test_unowned_respects_fix_limit(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            s = e.state()
            s["fix_iterations"] = 99
            e.save_state(s)
            with open(os.path.join(e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
                f.write("# x\n")
            self.assertIn("人間の確認", e.next())
        finally:
            e.cleanup()


class LegacyReviewTest(unittest.TestCase):
    def test_legacy_review_without_passed_key(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "plugins", "aq-dlc", "scripts"))
        from aqdlc_core.state import review_passed
        self.assertFalse(review_passed({"valid": True, "verdict": "要修正", "blocking": 0}))
        self.assertTrue(review_passed({"valid": True, "verdict": "合格", "blocking": 0}))
        self.assertFalse(review_passed({"valid": True, "verdict": "合格", "blocking": 1}))
        self.assertFalse(review_passed({"valid": False, "verdict": "合格", "blocking": 0, "passed": True}))
        self.assertFalse(review_passed(None))


def build_rest(e):
    """init / new 済みのプロジェクトで、仕様の承認と設計まで進める。"""
    import helpers
    py = f'"{sys.executable}"'
    e.spawn("requirements")
    e.start("requirements")
    e.as_role("requirements", "req", "R1", "メモを追加できる", "--critical")
    e.as_role("requirements", "req", "R2", "1秒以内に応答する", "--kind", "nonfunctional")
    e.as_role("requirements", "accept", "R1", "AC1", "追加したメモが一覧に出る")
    e.as_role("requirements", "accept", "R2", "AC2", "応答が1秒以内")
    e.as_role("requirements", "risk", "R1", "K1", "空のメモが保存される")
    e.as_role("requirements", "verify", "AC1", "V1", "追加", "--level", "unit")
    e.as_role("requirements", "verify", "K1", "V2", "空", "--level", "unit")
    e.as_role("requirements", "verify", "AC2", "V3", "全体", "--level", "unit")
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project), helpers.SPEC_MD)
    assert e.stop("requirements", "") is None
    e.aq("spec", "--present")
    e.human_says("承認します")
    e.aq("spec", "--approve", "承認")
    e.spawn("design")
    e.start("design")
    e.as_role("design", "design-map", "R1", "src/memo.py")
    e.as_role("design", "design-map", "R2", "src/memo.py")
    e.as_role("design", "add-check", "unit", f"{py} -m unittest discover -s tests -q", "--kind", "unit")
    e.write_as("design", os.path.relpath(os.path.join(e.arts, "design.md"), e.project),
               helpers.DESIGN_MD.format(spec=e.state()["spec"]["registered_hash"][:8]))
    assert e.stop("design", "") is None


if __name__ == "__main__":
    unittest.main()
