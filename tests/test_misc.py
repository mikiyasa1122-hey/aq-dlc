"""Memory（T9）、Stop hook・SessionStart・完了判定・publish（T10）。"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import REVIEW_PASS, Env, blocked, build_to_review, denied, read_file, write_file  # noqa: E402


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.aq("init", "x")

    def tearDown(self):
        self.e.cleanup()

    def test_setup_and_listing(self):
        e = self.e
        out = e.aq("memory", "--all")
        for mid in ("F-0001", "W-0001", "W-0002"):
            self.assertIn(mid, out)
        # 手順（workflow）はタグが合わなくても参考として表示される
        out = e.aq("memory", "--tags", "react,ui")
        self.assertIn("W-0001", out)
        self.assertNotIn("F-0001", out)
        # validated にすると「読み込むMemory」に入る
        p = os.path.join(e.home, "memory", "failure", "F-0001-contract-scope-leak.md")
        write_file(p, read_file(p).replace("status: candidate", "status: validated"))
        out = e.aq("memory", "--tags", "authorization")
        self.assertIn("読み込むMemory", out)
        self.assertIn("F-0001", out)

    def test_crlf_frontmatter(self):
        e = self.e
        p = os.path.join(e.home, "memory", "success", "W-0009-crlf.md")
        write_file(p, "---\r\nid: W-0009\r\ntype: workflow-success\r\nstatus: active\r\ntags: [workflow]\r\n---\r\n# 改行コード CRLF\r\n", newline="")
        self.assertIn("W-0009", e.aq("memory", "--tags", "x"))


class HookTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_session_start(self):
        e = self.e
        res = e.hook("session-start", {})
        self.assertIn("空です", res["hookSpecificOutput"]["additionalContext"])
        with open(os.path.join(e.project, "a.txt"), "w") as f:
            f.write("x")
        self.assertIsNone(e.hook("session-start", {}))  # 既存の別プロジェクトには関与しない
        e.aq("init", "x")
        e.aq("new", "x")
        ctx = e.hook("session-start", {})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("統括", ctx)
        self.assertIn("aq-dlc:requirements", ctx)
        self.assertIn("日本語", ctx)

    def test_stop_hook(self):
        e = self.e
        e.aq("init", "x")
        e.aq("new", "x")
        res = e.hook("stop", {})
        self.assertTrue(blocked(res))
        self.assertIn("aq-dlc:requirements", res["reason"])
        e.aq("ask", "どちらにしますか")
        self.assertIsNone(e.hook("stop", {}))  # 人間の返答待ちは止めない
        e.aq("resume")
        e.spawn("requirements")
        e.start("requirements")
        self.assertIsNone(e.hook("stop", {}))  # サブエージェントの実行中は止めない
        # 自動継続の上限
        s = e.state()
        s["agents"]["running"] = {}
        s["stop_blocks"] = 999
        e.save_state(s)
        self.assertIsNone(e.hook("stop", {}))

    def test_legacy_hook_names(self):
        e = self.e
        e.aq("init", "x")
        e.aq("new", "x")
        code, out = e.run(["stop-hook"], stdin=json.dumps({"cwd": e.project}))
        self.assertEqual(code, 0)
        self.assertIn("block", out)
        code, out = e.run(["agent-start-hook"], stdin="{}")
        self.assertEqual(code, 0)

    def test_hooks_ignore_non_project(self):
        e = self.e
        other = os.path.join(e.tmp, "other")
        os.makedirs(other)
        for name in ("stop", "pre-write", "subagent-start", "subagent-stop", "user-prompt"):
            self.assertIsNone(e.hook(name, {"cwd": other, "tool_input": {"file_path": os.path.join(other, "a")},
                                            "agent_type": "aq-dlc:implementer", "agent_id": "z"}), name)
        res = e.hook("pre-agent", {"cwd": other, "tool_input": {"subagent_type": "aq-dlc:design"}})
        self.assertTrue(denied(res))
        self.assertIsNone(e.hook("pre-agent", {"cwd": other, "tool_input": {"subagent_type": "Explore"}}))


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_after_merge_and_manual(self):
        e = self.e
        build_to_review(e, with_question=False)
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "risk", "R1", "K2", "公開URLで動かない")
        e.as_role("requirements", "verify", "K2", "V8", "公開URLを開く", "--after-merge", "--manual", "URLを開く")
        e.stop("requirements", "")
        e.aq("gate")
        e.spawn("reviewer")
        e.start("reviewer")
        e.stop("reviewer", REVIEW_PASS)
        e.aq("memory-done")
        out = e.aq("complete")
        self.assertIn("pr_ready", out)
        self.assertIsNone(e.hook("stop", {}))  # マージ待ちは止めない
        e.aq("confirm", "V8", "見ました", ok=False)  # ask の後の人間の返答がない
        e.aq("ask", "公開URLを開いて確認してください")
        e.human_says("開けました")
        e.aq("confirm", "V8", "ユーザーが公開URLを確認")
        e.aq("gate", "--after-merge")
        e.aq("complete")
        self.assertEqual(e.state()["status"], "complete")

    def test_gate_failure_routes_to_implementer(self):
        e = self.e
        build_to_review(e, with_question=False)
        os.remove(os.path.join(e.project, "src", "memo.py"))
        e.aq("gate")
        nxt = e.next()
        self.assertIn("実装", nxt)
        self.assertTrue(denied(e.spawn("reviewer")))
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("gate", brief)
        self.assertIn("不合格", brief)

    def test_escalation_blocks(self):
        e = self.e
        build_to_review(e, with_question=False)
        e.spawn("implementer")
        e.start("implementer")
        p = os.path.join(e.arts, "impl.md")
        write_file(p, read_file(p).replace("## 差し戻し\n- なし", "## 差し戻し\n- R2 の1秒は測定できない") + "\n")
        e.stop("implementer", "")
        self.assertIn("差し戻し", e.next())
        self.assertTrue(denied(e.spawn("reviewer")))
        e.aq("escalation", "E1", "--resolve", "人間と相談し計測方法を変更")
        self.assertNotIn("差し戻し", e.next())


class PublishTest(unittest.TestCase):
    def test_identity_check(self):
        e = Env()
        try:
            e.aq("init", "x")
            env = dict(e.env, HOME=e.tmp, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.path.join(e.tmp, "nogit"))
            for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
                env.pop(k, None)
            from helpers import ENGINE
            p = subprocess.run([sys.executable, ENGINE, "publish", "--project", e.project], env=env,
                               capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("利用者情報", p.stdout + p.stderr)
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
