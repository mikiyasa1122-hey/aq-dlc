"""外部レビューの P1（テストの妥当性・Memory の信頼性・運用の計測）の回帰テスト。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import REVIEW_PASS, Env, blocked, build_to_review, denied, read_file, write_file  # noqa: E402

MEM = """---
id: {id}
type: {type}
status: {status}
scope: global
tags: [workflow]
stack: [any]
evidence: [{evidence}]
applied_in: [{applied}]
{extra}---
# {title}
本文
"""


class TestQualityTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def test_signals_are_given_and_must_be_addressed(self):
        e = self.e
        e.spawn("implementer")
        e.start("implementer")  # 実装担当の作業として追加する（担当外の変更にしない）
        write_file(os.path.join(e.project, "tests", "test_extra.py"),
                   "import unittest\n\nclass X(unittest.TestCase):\n    @unittest.skip('later')\n"
                   "    def test_a(self):\n        pass\n")
        p = os.path.join(e.arts, "impl.md")
        write_file(p, read_file(p) + "\n（テスト追加）\n")
        self.assertIsNone(e.stop("implementer", "追加"))
        e.aq("gate")
        e.spawn("reviewer")
        brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("tests/test_extra.py", brief)
        self.assertIn("skip", brief)
        res = e.stop("reviewer", REVIEW_PASS)  # 兆候のあるファイルに触れていない
        self.assertTrue(blocked(res))
        self.assertIn("tests/test_extra.py", res["reason"])
        ok = REVIEW_PASS.replace("### E2E の要否", "- tests/test_extra.py の skip は将来の機能用で、受け入れ条件とは無関係\n### E2E の要否")
        self.assertIsNone(e.stop("reviewer", ok))

    def test_review_must_cover_each_acceptance_criterion(self):
        e = self.e
        e.spawn("reviewer")
        e.start("reviewer")
        msg = REVIEW_PASS.replace("- AC2: V3 は全テストの実行で妥当\n", "")
        res = e.stop("reviewer", msg)
        self.assertTrue(blocked(res))
        self.assertIn("AC2", res["reason"])

    def test_web_app_without_e2e_is_flagged(self):
        e = self.e
        e.spawn("implementer")
        e.start("implementer")
        write_file(os.path.join(e.project, "package.json"), '{"dependencies": {"react": "18.3.1"}}')
        p = os.path.join(e.arts, "impl.md")
        write_file(p, read_file(p) + "\n（package.json 追加）\n")
        self.assertIsNone(e.stop("implementer", ""))
        e.aq("gate")
        self.assertFalse(denied(e.spawn("reviewer")))
        brief = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Web アプリに見えるが、E2E のチェック・検証がない", brief)
        res = e.stop("reviewer", REVIEW_PASS.replace("### E2E の要否\n- 不要（画面を持たないライブラリのため）\n", ""))
        self.assertTrue(blocked(res))
        self.assertIn("E2E の要否", res["reason"])


class MemoryReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.aq("init", "x")
        self.e.aq("new", "x")
        self.e.aq("memory-scope", "project")
        self.run_id = self.e.state()["run_id"]
        # E-AQDL: 保存はプロジェクトの Memory（共通 Memory へは memory-share で人間の承認を得て共有する）
        self.mem_dir = os.path.join(self.e.project, ".aq-dlc", "memory", "success")
        os.makedirs(self.mem_dir, exist_ok=True)

    def tearDown(self):
        self.e.cleanup()

    def write_mem(self, mid, status="candidate", typ="workflow-success", evidence=None, applied="", extra="",
                  title=None):
        write_file(os.path.join(self.mem_dir, f"{mid}.md"), MEM.format(
            id=mid, type=typ, status=status, evidence=evidence if evidence is not None else f"x/runs/{self.run_id}#V1",
            applied=applied, extra=extra, title=title or f"題名 {mid}"))

    def test_memory_done_requires_a_report(self):
        out = self.e.aq("memory-done", "--end", "project", ok=False)
        self.assertIn("--skipped", out)
        self.e.aq("memory-done", "--end", "project", "--skipped", "再利用できる学びがなかった")
        s = self.e.state()
        self.assertTrue(s["memory_done"])
        self.assertEqual(s["memory"]["report"]["skipped"], "再利用できる学びがなかった")

    def test_saved_memory_must_exist_and_cite_this_run(self):
        self.e.aq("memory-done", "--end", "project", "--saved", "W-0100", ok=False)  # 存在しない
        self.write_mem("W-0100", evidence="other/runs/old#V1")
        out = self.e.aq("memory-done", "--end", "project", "--saved", "W-0100", ok=False)
        self.assertIn("この run", out)
        self.write_mem("W-0100")
        self.e.aq("memory-done", "--end", "project", "--saved", "W-0100")

    def test_unverified_memory_cannot_be_active(self):
        self.write_mem("W-0101", status="active")
        out = self.e.aq("memory-done", "--end", "project", "--saved", "W-0101", ok=False)
        self.assertIn("根拠を確認できない", out)  # 実在する検証・gate の合格がない
        self.assertIn("役立った記録", out)  # 根拠の run 以外での helped が足りない
        self.write_mem("W-0102", status="active", typ="rule", applied="a, b, c")
        out = self.e.aq("memory-done", "--end", "project", "--saved", "W-0102", ok=False)
        self.assertIn("人間の承認", out)

    def test_duplicate_title_must_be_merged(self):
        self.write_mem("W-0103", title="同じ題名")
        self.write_mem("W-0104", title="同じ題名")
        out = self.e.aq("memory-done", "--end", "project", "--saved", "W-0104", ok=False)
        self.assertIn("統合", out)

    def test_loaded_memory_needs_a_result(self):
        self.write_mem("W-0105", status="validated")
        out = self.e.aq("memory", "--tags", "workflow", "--record")
        self.assertIn("W-0105", out)
        self.assertIn("W-0105", self.e.state()["memory"]["loaded"])
        out = self.e.aq("memory-done", "--end", "project", "--skipped", "なし", ok=False)
        self.assertIn("memory-use", out)
        self.e.aq("memory-use", "W-0105", "--result", "bogus", ok=False)
        self.e.aq("memory-use", "W-0105", "--result", "helped", "--note", "境界値の洗い出しに使えた")
        self.e.aq("memory-done", "--end", "project", "--skipped", "なし")
        self.assertEqual(self.e.state()["memory"]["used"]["W-0105"]["result"], "helped")

    def test_listing_respects_limit(self):
        for i in range(30):
            self.write_mem(f"W-02{i:02d}", status="validated")
        out = self.e.aq("memory", "--tags", "workflow")
        shown = [ln for ln in out.splitlines() if ln.startswith("W-02")]
        self.assertEqual(len(shown), 10)
        self.assertEqual([ln.split()[0] for ln in shown], sorted(ln.split()[0] for ln in shown))  # 順序が安定


class MetricsTest(unittest.TestCase):
    def test_metrics(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            e.spawn("reviewer")
            e.start("reviewer")
            e.stop("reviewer", REVIEW_PASS)
            e.aq("ask", "確認")
            e.human_says("OK")
            m = json.loads(e.aq("metrics", "--json"))
            self.assertEqual(m["roles"]["requirements"]["runs"], 1)
            self.assertEqual(m["roles"]["reviewer"]["runs"], 1)
            self.assertGreaterEqual(m["gate"]["runs"], 1)
            self.assertEqual(m["reviews"]["passed"], 1)
            self.assertEqual(m["human"]["asks"], 1)
            self.assertGreaterEqual(m["human"]["replies"], 2)
            self.assertIn("Quality Gate", e.aq("metrics"))
        finally:
            e.cleanup()

    def test_transcript_tokens(self):
        e = Env()
        try:
            e.aq("init", "x")
            e.aq("new", "x")
            e.aq("memory-scope", "project")
            tp = os.path.join(e.tmp, "t.jsonl")
            write_file(tp, "\n".join(json.dumps(x) for x in [
                {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}},
                {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}},
                {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 7, "output_tokens": 3,
                                                                        "cache_read_input_tokens": 100}}}]))
            e.hook("stop", {"transcript_path": tp})
            m = json.loads(e.aq("metrics", "--json"))
            self.assertEqual(m["orchestrator_tokens"]["input"], 17)
            self.assertEqual(m["orchestrator_tokens"]["output"], 8)
            self.assertEqual(m["orchestrator_tokens"]["cache_read"], 100)
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
