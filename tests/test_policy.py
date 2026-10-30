"""担当ごとの権限（エンジン側でも確認する）と、人間の返答の扱い。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ENGINE, Env, build_to_review, denied  # noqa: E402


class EnginePolicyTest(unittest.TestCase):
    """hook の正規表現をすり抜ける書き方（変数・関数経由）でも、エンジンが担当外のコマンドを拒否する。"""

    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def bash_then_engine(self, role, shell_text, engine_args):
        res = self.e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": shell_text},
                                       **self.e.agent_fields(role)})
        if denied(res):
            return "denied-by-hook"
        code, out = self.e.run(list(engine_args) + ["--project", self.e.project])
        return out if code != 0 else "ok"

    def test_variable_indirection(self):
        shell = f"AQ='python3 {ENGINE}'; $AQ req R9 \"統括が勝手に足す要求\""
        out = self.bash_then_engine("orchestrator", shell, ["req", "R9", "統括が勝手に足す要求"])
        self.assertNotEqual(out, "ok")
        self.assertIn("担当", out)
        self.assertNotIn("R9", [r["id"] for r in self.e.state()["requirements"]])

    def test_function_wrapper(self):
        shell = f"r(){{ python3 {ENGINE} \"$@\" --project x; }}; r spec --approve 承認"
        out = self.bash_then_engine("requirements", shell, ["spec", "--approve", "承認"])
        self.assertNotEqual(out, "ok")

    def test_reviewer_cannot_bind_via_variable(self):
        shell = f"AQ='python3 {ENGINE}'; $AQ bind V1 --command true"
        out = self.bash_then_engine("reviewer", shell, ["bind", "V1", "--command", "true"])
        self.assertNotEqual(out, "ok")

    def test_allowed_role_passes(self):
        shell = f"AQ='python3 {ENGINE}'; $AQ bind V1 --level unit"
        self.assertEqual(self.bash_then_engine("implementer", shell, ["bind", "V1", "--level", "unit"]), "ok")

    def test_manual_cli_without_hook_is_allowed(self):
        # hook が動いていない（人が端末から直接使う）場合は制限しない
        self.e.aq("trace")


class HumanReplyTest(unittest.TestCase):
    def test_user_prompt_clears_awaiting(self):
        e = Env()
        try:
            e.aq("init", "x")
            e.aq("new", "x")
            e.aq("ask", "どちらにしますか")
            self.assertTrue(e.state()["awaiting_human"])
            e.human_says("A にしてください")
            s = e.state()
            self.assertFalse(s["awaiting_human"])
            self.assertEqual(s["human_events"][-1]["kind"], "prompt")
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
