"""マルチエージェント・ワークフローの結合テスト（hook への入力を模擬）。

依頼の完了条件との対応:
  T1  4つのサブエージェントが正しく呼び出される              test_full_workflow / test_spawn_order
  T2  仕様整理からレビューまで自動で引き継がれる              test_full_workflow（各 brief と next）
  T3  仕様承認前に実装へ進まない                              test_spawn_order / test_spec_approval_needs_human
  T4  設計書が Implementation Agent へ正確に渡される          test_design_handoff
  T5  Reviewer が実装コードを変更しない                       test_reviewer_cannot_modify
  T6  レビュー指摘後に修正・再検証が行われる                  test_full_workflow
  T7  コードや検証条件を変えると古い証拠が無効になる          test_invalidation.py
  T8  検証IDの衝突で誤った合格判定が起きない                  test_invalidation.py
  T9  既存の Memory 機能が動く                                test_misc.py
  T10 Stop hook・GitHub 連携・完了判定が破綻しない            test_misc.py / test_guard.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (REVIEW_FAIL, REVIEW_PASS, MEMO_V2, Env, blocked, build_to_review, denied,  # noqa: E402
                     implement, read_file, write_file)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_full_workflow(self):
        e = self.e
        build_to_review(e)
        self.assertIn("独立レビュー", e.next())
        # レビュー: 要修正
        res = e.spawn("reviewer")
        self.assertFalse(denied(res), res)
        self.assertIs(res["hookSpecificOutput"]["updatedInput"]["run_in_background"], False)
        ctx = e.start("reviewer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("判定: 合格 または 要修正", ctx)
        self.assertIsNone(e.stop("reviewer", REVIEW_FAIL))
        s = e.state()
        self.assertEqual(s["reviews"][-1]["blocking"], 1)
        self.assertTrue(s["reviews"][-1]["valid"])
        self.assertTrue(os.path.exists(os.path.join(e.arts, "review-1.md")))
        nxt = e.next()
        self.assertIn("実装", nxt)
        self.assertIn("要修正", nxt)
        # 修正: 指摘への対応がないと終了できない
        self.assertFalse(denied(e.spawn("implementer")))
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[重大1]", brief)
        implement(e, MEMO_V2, extra="（修正版）")
        res = e.stop("implementer", "修正")
        self.assertTrue(blocked(res), res)
        self.assertIn("指摘への対応", res["reason"])
        implement(e, MEMO_V2, extra="## 指摘への対応\n- [重大1] strip して判定するよう修正\n")
        self.assertIsNone(e.stop("implementer", "修正完了"))
        self.assertEqual(e.state()["fix_iterations"], 1)
        # 修正後: gate も レビューも古くなる
        self.assertIn("gate 後に変わったものがある", e.next())
        self.assertTrue(denied(e.spawn("reviewer")))  # gate 前はレビューできない
        e.aq("gate")
        nxt = e.next()
        self.assertIn("独立レビュー", nxt)
        self.assertIn("再レビュー", nxt)
        e.spawn("reviewer")
        e.start("reviewer")
        self.assertIsNone(e.stop("reviewer", REVIEW_PASS))
        self.assertIn("Memory", e.next())
        e.aq("memory-done")
        out = e.aq("complete")
        self.assertIn("complete", out)
        self.assertIsNone(e.hook("stop", {}))
        self.assertEqual(e.state()["status"], "closed")
        trace = e.aq("trace")
        self.assertIn("| R1 |", trace)
        self.assertIn("合格", trace)

    def test_spawn_order(self):
        e = self.e
        e.aq("init", "x")
        e.aq("new", "x")
        for role in ("design", "implementer", "reviewer"):
            self.assertTrue(denied(e.spawn(role)), role)
        self.assertFalse(denied(e.spawn("requirements")))
        e.start("requirements")
        # 実行中は他のサブエージェントを起動できない（順次実行）
        res = e.spawn("design")
        self.assertTrue(denied(res))
        self.assertIn("順番", res["hookSpecificOutput"]["permissionDecisionReason"])
        # 成果物がないと終了できない
        res = e.stop("requirements", "終わり")
        self.assertTrue(blocked(res))
        self.assertIn("仕様書", res["reason"])

    def test_spec_approval_needs_human(self):
        e = self.e
        e.aq("init", "x")
        e.aq("new", "x")
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "req", "R1", "a")
        e.as_role("requirements", "accept", "R1", "AC1", "b")
        e.as_role("requirements", "verify", "AC1", "V1", "c", "--level", "unit")
        e.as_role("requirements", "question", "Q1", "どちら？")
        from helpers import SPEC_MD
        e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project),
                   SPEC_MD.replace("R2", "R1").replace("AC2", "AC1"))
        self.assertIsNone(e.stop("requirements", "ok"))
        # 質問への回答は、人間の返答が記録されてから
        e.aq("answer", "Q1", "A", ok=False)
        e.aq("spec", "--present", ok=False)  # 未回答の質問がある
        e.human_answers({"Q1": "A"})
        e.aq("answer", "Q1", "A")
        self.assertIn("要求整理", e.next())  # 回答を仕様に反映する
        e.spawn("requirements")
        e.start("requirements")
        e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project),
                   SPEC_MD.replace("R2", "R1").replace("AC2", "AC1") + "\nQ1→A\n")
        e.stop("requirements", "ok")
        e.aq("spec", "--approve", "OK", ok=False)  # 未提示
        e.aq("spec", "--present")
        e.aq("spec", "--approve", "OK", ok=False)  # 提示後の人間の返答がない
        self.assertTrue(denied(e.spawn("design")))
        self.assertTrue(denied(e.spawn("implementer")))
        e.aq("gate", ok=False)  # 承認前は gate も動かない
        e.human_says("それでお願いします")
        e.aq("spec", "--approve", "ユーザーが承認")
        self.assertFalse(denied(e.spawn("design")))
        # 承認後に仕様が変わると、再承認まで設計・実装に進めない
        e.start("design")
        e.stop("design", "")  # 成果物なし → block されるが、テストでは無視して上限まで
        s = e.state()
        s["agents"]["running"] = {}
        e.save_state(s)
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "req", "R1", "変更後の要求")
        e.stop("requirements", "変更")
        self.assertTrue(denied(e.spawn("design")))
        self.assertIn("人間の確認", e.next())

    def test_design_handoff(self):
        e = self.e
        build_to_review(e, with_question=False)
        s = e.state()
        dh = s["design"]["registered_hash"]
        # 実装担当への引き継ぎに、設計書の場所と指紋が入る
        e.spawn("reviewer")  # gate 合格後なので可
        e.start("reviewer")
        e.stop("reviewer", REVIEW_FAIL)
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn(os.path.join(e.arts, "design.md"), brief)
        self.assertIn(dh[:8], brief)
        # 古い設計書の指紋を書いた報告では終了できない
        implement(e, MEMO_V2, extra="## 指摘への対応\n- [重大1] 修正\n")
        p = os.path.join(e.arts, "impl.md")
        write_file(p, read_file(p).replace(dh[:8], "deadbeef"))
        res = e.stop("implementer", "x")
        self.assertTrue(blocked(res))
        self.assertIn("設計書", res["reason"])

    def test_reviewer_cannot_modify(self):
        e = self.e
        build_to_review(e, with_question=False)
        e.spawn("reviewer")
        e.start("reviewer")
        self.assertTrue(denied(e.write_as("reviewer", "src/memo.py", "x = 1\n")))
        res = e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": "git commit -am fix"},
                                  **e.agent_fields("reviewer")})
        self.assertTrue(denied(res))
        res = e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": "sed -i s/a/b/ src/memo.py"},
                                  **e.agent_fields("reviewer")})
        self.assertTrue(denied(res))
        # それでもコードが変わった場合は、レビューを無効にする
        with open(os.path.join(e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
            f.write("# changed\n")
        self.assertIsNone(e.stop("reviewer", REVIEW_PASS))
        rv = e.state()["reviews"][-1]
        self.assertFalse(rv["valid"])
        self.assertIn("レビュー中にコードが変更された", rv["invalid_reason"])
        self.assertIn("無効", e.aq("status"))

    def test_orchestrator_does_not_implement(self):
        e = self.e
        build_to_review(e, with_question=False)
        self.assertTrue(denied(e.write_as("orchestrator", "src/memo.py", "x")))
        self.assertTrue(denied(e.write_as("design", "src/memo.py", "x")))
        self.assertIsNone(e.write_as("orchestrator", os.path.relpath(os.path.join(e.run_dir, "run.md"),
                                                                     e.project), "# メモ\n"))
        res = e.as_role("orchestrator", "req", "R9", "勝手な要求", ok=None)
        self.assertTrue(denied(res))
        res = e.as_role("requirements", "spec", "--approve", "x", ok=None)
        self.assertTrue(denied(res))
        res = e.as_role("reviewer", "bind", "V1", "--command", "true", ok=None)
        self.assertTrue(denied(res))
        # state.json の直接編集は誰もできない
        for role in ("orchestrator", "implementer", "requirements"):
            self.assertTrue(denied(e.write_as(role, os.path.relpath(os.path.join(e.run_dir, "state.json"),
                                                                    e.project), "{}")), role)
        # hook の偽装はできない
        res = e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {
            "command": f'echo {{}} | python3 aqdlc.py hook user-prompt --project "{e.project}"'}})
        self.assertTrue(denied(res))


if __name__ == "__main__":
    unittest.main()
