"""証拠の鮮度（T7）と ID の衝突（T8）、E2E と古いビルド。"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import REVIEW_PASS, Env, build_to_review, edit_json, read_file, write_file  # noqa: E402


def finish_review(e):
    e.spawn("reviewer")
    e.start("reviewer")
    assert e.stop("reviewer", REVIEW_PASS) is None
    e.aq("memory-done")


class InvalidationTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)
        finish_review(self.e)
        self.e.aq("check")  # すべて揃った状態から始める

    def tearDown(self):
        self.e.cleanup()

    def assert_stale(self, words):
        out = self.e.aq("check", ok=False)
        for w in words:
            self.assertIn(w, out)

    def test_code_change(self):
        with open(os.path.join(self.e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
            f.write("# x\n")
        # 実装担当以外の変更は、まず実装担当の取り込みが必要
        self.assert_stale(["実装担当以外の操作でコードが変わった", "src/memo.py"])
        e = self.e
        e.spawn("implementer")
        brief = e.start("implementer")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("src/memo.py", brief)
        p = os.path.join(e.arts, "impl.md")
        write_file(p, read_file(p) + "\n## 指摘への対応\n- [担当外1] 追加された行を確認して取り込んだ\n")
        self.assertIsNone(e.stop("implementer", "取り込み"))
        # 取り込み後: gate は古い → gate をやり直しても、レビューは古いまま
        self.assert_stale(["gate 後に変わったものがある", "コード"])
        e.aq("gate")
        self.assert_stale(["再レビュー", "コード"])

    def test_mtime_only_change_is_not_stale(self):
        p = os.path.join(self.e.project, "src", "memo.py")
        t = time.time() + 5
        os.utime(p, (t, t))
        self.e.aq("check")

    def test_verification_command_change(self):
        s = self.e.state()
        for r in s["requirements"]:
            for p in r["acceptance"] + r["risks"]:
                for v in p["verifications"]:
                    if v["id"] == "V2":
                        v["command"] = "true"  # 検証を弱める
        self.e.save_state(s)
        self.assert_stale(["検証条件", "V2"])
        self.e.aq("gate")
        self.assert_stale(["再レビュー", "検証条件"])

    def test_check_definition_change(self):
        p = os.path.join(self.e.project, ".aq-dlc", "config.json")
        edit_json(p, lambda cfg: cfg["checks"][0].update(command="true"))
        self.assert_stale(["チェックの定義"])

    def test_gate_env_change(self):
        p = os.path.join(self.e.project, ".aq-dlc", "config.json")
        edit_json(p, lambda cfg: cfg.setdefault("gate", {}).update(env={"NODE_ENV": "test"}))
        self.assert_stale(["チェックの定義"])

    def test_spec_doc_change(self):
        p = os.path.join(self.e.arts, "spec.md")
        with open(p, "a", encoding="utf-8") as f:
            f.write("追記\n")
        self.assert_stale(["仕様"])

    def test_design_doc_change(self):
        p = os.path.join(self.e.arts, "design.md")
        with open(p, "a", encoding="utf-8") as f:
            f.write("追記\n")
        self.assert_stale(["設計書が登録後に変更された"])

    def test_new_verification_has_no_evidence(self):
        e = self.e
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "risk", "R1", "K9", "巨大なメモ")
        e.as_role("requirements", "verify", "K9", "V9", "巨大なメモの単体テスト", "--level", "unit")
        e.stop("requirements", "リスク追加")
        out = e.aq("check", ok=False)
        self.assertIn("V9", out)


class IdCollisionTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def test_verify_rejects_duplicate_id(self):
        e = self.e
        e.spawn("reviewer")
        e.start("reviewer")
        e.stop("reviewer", REVIEW_PASS)
        e.spawn("requirements")
        e.start("requirements")
        out = e.as_role("requirements", "verify", "AC2", "V1", "別の検証に同じID", ok=False)
        self.assertIn("既に使われています", out)
        out = e.as_role("requirements", "risk", "R2", "AC1", "受け入れ条件と同じID", ok=False)
        self.assertIn("既に使われています", out)

    def test_hand_edited_duplicate_blocks_gate_and_completion(self):
        e = self.e
        s = e.state()
        # V2（失敗する検証）を、合格している V1 と同じ ID にする
        for r in s["requirements"]:
            for p in r["risks"]:
                for v in p["verifications"]:
                    if v["id"] == "V2":
                        v["id"] = "V1"
                        v["command"] = "exit 1"
        e.save_state(s)
        out = e.aq("gate", ok=False)
        self.assertIn("重複", out)
        out = e.aq("check", ok=False)
        self.assertIn("IDが重複", out)


class E2EBuildTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()

    def tearDown(self):
        self.e.cleanup()

    def test_e2e_not_run_after_failed_build(self):
        e = self.e
        build_to_review(e, with_question=False)
        marker = os.path.join(e.project, "e2e-ran.txt")
        py = f'"{sys.executable}"'
        e.spawn("reviewer")
        e.start("reviewer")
        e.stop("reviewer", REVIEW_PASS)
        e.spawn("design")
        e.start("design")
        e.as_role("design", "add-check", "build", f"{py} -c \"import sys; sys.exit(1)\"", "--kind", "build")
        e.as_role("design", "add-check", "e2e", f"{py} -c \"open(r'{marker}','w').write('x')\"", "--kind", "e2e")
        p = os.path.join(e.arts, "design.md")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\nE2E を追加\n")
        e.stop("design", "")
        out = e.aq("gate")
        self.assertIn("中止 チェック:e2e", out)
        self.assertFalse(os.path.exists(marker))
        g = e.state()["gate"]
        self.assertFalse(g["results"]["checks"]["e2e"]["pass"])
        self.assertFalse(g["checks_pass"])


class RunIdTest(unittest.TestCase):
    def test_runs_do_not_collide(self):
        e = Env()
        try:
            e.aq("init", "x")
            e.aq("new", "same")
            first = e.run_dir
            e.aq("new", "same", ok=False)  # 進行中の run があると止める
            e.aq("new", "same", "--force")
            e.aq("new", "same", "--force")
            runs = [d for d in os.listdir(os.path.join(e.project, ".aq-dlc", "runs")) if d != "CURRENT"]
            self.assertEqual(len(runs), 3)
            self.assertTrue(os.path.exists(os.path.join(first, "state.json")))
        finally:
            e.cleanup()


if __name__ == "__main__":
    unittest.main()
