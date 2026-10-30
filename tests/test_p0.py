"""外部レビュー（v0.5.0 / 3a5a0c0）で指摘された P0 の回帰テスト。

P0-1 独立レビューの判定: 「合格」かつ重大0件のときだけ完了できる
P0-2 gate 実行中のコード変更: gate を不合格にし、実際に検証したコードと証拠を一致させる
P0-3 Bash 経由の担当外の変更: 検出して記録し、実装担当の取り込み・gate・レビューをやり直すまで先に進めない
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (REVIEW_PASS, Env, bash_as, blocked, build_to_review, denied, edit_json,  # noqa: E402
                     read_file, write_file)

REVIEW_TMPL = """## レビュー結果
{verdict}
重大な指摘: {blocking}
軽微な指摘: {minor}
レビュー対象: 仕様 x / 設計 y / コード z
### 指摘
{findings}
### 受け入れ条件ごとの検証の妥当性
- AC1: V1 は追加後の一覧を確認しており妥当
- AC2: V3 は全テストの実行で妥当
- K1: V2 は空のメモの拒否を確認しており妥当
### E2E の要否
- 不要（CLI を持たないライブラリのため）
### 不足しているリスク
- なし
### 確認したこと
- python3 -m unittest
"""
MAJOR = "- [重大1] 区分: 実装 / 対象: R1 / 内容: 空白だけのメモが保存される / 根拠: src/memo.py:2 / 提案: strip"
MINOR = "- [軽微1] 区分: テスト / 対象: V1 / 内容: テスト名が分かりにくい"


def review(verdict="判定: 合格", blocking=0, minor=0, findings="- なし"):
    return REVIEW_TMPL.format(verdict=verdict, blocking=blocking, minor=minor, findings=findings)


def finish_memory(e):
    e.aq("memory-done", "--end", "project", "--skipped", "テスト用の小さな run のため保存する学びなし")


class P01ReviewVerdictTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def run_review(self, message, attempts=4):
        e = self.e
        self.assertFalse(denied(e.spawn("reviewer")))
        e.start("reviewer")
        res = None
        for _ in range(attempts):
            res = e.stop("reviewer", message)
            if not blocked(res):
                break
        return res

    def assert_not_completable(self):
        finish_memory(self.e)
        out = self.e.aq("check", ok=False)
        self.assertIn("レビュー", out)
        self.e.aq("complete", ok=False)

    def test_pass_zero_blocking_completes(self):
        self.assertIsNone(self.run_review(review()))
        finish_memory(self.e)
        self.e.aq("check")

    def test_needs_fix_zero_blocking_is_not_completable(self):
        res = self.run_review(review("判定: 要修正", 0, 1, MINOR))
        self.assertIsNone(res)
        self.assert_not_completable()
        self.assertIn("実装", self.e.next())  # 修正ループへ

    def test_needs_fix_one_blocking_is_not_completable(self):
        self.run_review(review("判定: 要修正", 1, 0, MAJOR))
        self.assert_not_completable()

    def test_pass_with_blocking_is_contradiction(self):
        self.assertFalse(denied(self.e.spawn("reviewer")))
        self.e.start("reviewer")
        res = self.e.stop("reviewer", review("判定: 合格", 1, 0, MAJOR))
        self.assertTrue(blocked(res))
        self.assertIn("矛盾", res["reason"])
        for _ in range(3):
            self.e.stop("reviewer", review("判定: 合格", 1, 0, MAJOR))
        rv = self.e.state()["reviews"][-1]
        self.assertFalse(rv["valid"])
        self.assert_not_completable()

    def test_missing_verdict_is_not_completable(self):
        self.run_review(review("（判定の行なし）", 0, 0))
        self.assertFalse(self.e.state()["reviews"][-1]["valid"])
        self.assert_not_completable()

    def test_invalid_verdict_is_not_completable(self):
        self.run_review(review("判定: たぶん大丈夫", 0, 0))
        self.assertFalse(self.e.state()["reviews"][-1]["valid"])
        self.assert_not_completable()

    def test_blocking_count_mismatch_is_not_completable(self):
        self.run_review(review("判定: 要修正", 2, 0, MAJOR))
        self.assertFalse(self.e.state()["reviews"][-1]["valid"])
        self.assert_not_completable()

    def test_invalid_latest_review_overrides_older_pass(self):
        self.assertIsNone(self.run_review(review()))
        # コードを変えずに再レビューし、矛盾した結果（無効）になった場合、前回の合格は使わない
        self.run_review(review("判定: 合格", 1, 0, MAJOR))
        self.assertFalse(self.e.state()["reviews"][-1]["valid"])
        self.assert_not_completable()


class P02GateIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def add_check(self, name, code, kind="other"):
        py = f'"{sys.executable}"'
        self.e.aq("add-check", name, f"{py} -c \"{code}\"", "--kind", kind)

    def test_source_change_during_gate_fails(self):
        self.add_check("mutate", "open('src/memo.py','a').write('# x\\\\n')")
        out = self.e.aq("gate")
        self.assertIn("不合格", out)
        g = self.e.state()["gate"]
        self.assertFalse(g["checks_pass"])
        self.assertIn("src/memo.py", " ".join(g["modified_during_gate"]["files"]))
        self.assertNotEqual(g["code_fp"], g["code_fp_after"])
        self.assertTrue(denied(self.e.spawn("reviewer")))
        out = self.e.aq("check", ok=False)
        self.assertIn("実装担当以外の操作でコードが変わった", out)  # gate が書き換えたコードは担当外の変更として扱う
        self.assertIn("gate の実行中", self.e.aq("status"))

    def test_test_change_during_gate_fails(self):
        self.add_check("mutate", "open('tests/test_memo.py','a').write('# x\\\\n')")
        self.e.aq("gate")
        self.assertFalse(self.e.state()["gate"]["checks_pass"])
        self.assertTrue(denied(self.e.spawn("reviewer")))

    def test_build_output_is_not_a_false_positive(self):
        self.add_check("build", "import os; os.makedirs('dist', exist_ok=True); open('dist/out.js','w').write('x')",
                       kind="build")
        self.e.aq("gate")
        g = self.e.state()["gate"]
        self.assertTrue(g["checks_pass"], g.get("modified_during_gate"))
        self.assertFalse(g.get("modified_during_gate"))

    def test_rerun_after_fix_passes(self):
        self.add_check("mutate", "open('src/memo.py','a').write('# x\\\\n')")
        self.e.aq("gate")
        self.e.aq("remove-check", "mutate")
        # gate が書き換えたコードは実装担当が取り込む（担当外の変更）→ gate → レビュー
        self.assertIn("実装", self.e.next())
        self.e.spawn("implementer")
        self.e.start("implementer")
        p = os.path.join(self.e.arts, "impl.md")
        write_file(p, read_file(p) + "\n## 指摘への対応\n- [担当外1] gate が書き換えた行を確認し、元に戻した\n")
        with open(os.path.join(self.e.project, "src", "memo.py"), encoding="utf-8") as f:
            src = f.read().replace("# x\n", "")
        write_file(os.path.join(self.e.project, "src", "memo.py"), src)
        self.assertIsNone(self.e.stop("implementer", "取り込み"))
        self.e.aq("gate")
        self.assertTrue(self.e.state()["gate"]["checks_pass"])
        self.assertFalse(denied(self.e.spawn("reviewer")))

    def test_gate_result_records_the_code_it_ran_on(self):
        before = self.e.state()["impl"]["rounds"][-1]["code_fp"]
        self.e.aq("gate")
        g = self.e.state()["gate"]
        self.assertEqual(g["code_fp"], before)


class P03BashBypassTest(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def assert_violation(self, role, cmd):
        pre, post = bash_as(self.e, role, cmd)
        if denied(pre):
            return  # 実行前に止まった
        s = self.e.state()
        self.assertTrue(s.get("violations"), f"{role}: {cmd} の変更が記録されていない")
        self.assertEqual(s["violations"][-1]["role"], role)
        self.assertIsNotNone(post)
        self.assertIn("担当外", json_text(post))
        # 担当外の変更があると、レビュー・完了に進めない
        self.assertTrue(denied(self.e.spawn("reviewer")))
        self.assertIn("実装", self.e.next())

    def test_orchestrator_redirect(self):
        self.assert_violation("orchestrator", "echo '# x' >> src/memo.py")

    def test_orchestrator_python_write(self):
        self.assert_violation("orchestrator",
                              f"\"{sys.executable}\" -c \"open('src/memo.py','a').write('#x')\"")

    def test_design_script_write(self):
        write_file(os.path.join(self.e.tmp, "w.sh"), "echo '# y' >> \"$1\"\n")
        script = os.path.join(self.e.tmp, "w.sh").replace(os.sep, "/")  # bash に渡すため / 区切り
        self.assert_violation("design", f"sh '{script}' src/memo.py")

    def test_requirements_variable_expansion(self):
        self.assert_violation("requirements", "F=src/memo.py; T=$F; printf x >> \"$T\"")

    def test_reviewer_absolute_path(self):
        e = self.e
        e.spawn("reviewer")
        e.start("reviewer")
        self.assert_violation_reviewer(os.path.join(e.project, "tests", "test_memo.py"))

    def assert_violation_reviewer(self, path):
        pre, post = bash_as(self.e, "reviewer", f"printf '# z' >> '{path}'")
        if not denied(pre):
            self.assertTrue(self.e.state().get("violations"))
        self.e.stop("reviewer", REVIEW_PASS)
        self.assertFalse(self.e.state()["reviews"][-1]["valid"])

    def test_implementer_may_change_code(self):
        self.e.spawn("implementer")
        self.e.start("implementer")
        _pre, post = bash_as(self.e, "implementer", "echo '# ok' >> src/memo.py")
        self.assertFalse(self.e.state().get("violations"))

    def test_symlink_write_into_project(self):
        e = self.e
        link = os.path.join(e.tmp, "outside-link")
        try:
            os.symlink(os.path.join(e.project, "src"), link)
        except (OSError, NotImplementedError):
            self.skipTest("シンボリックリンクを作れない環境")
        res = e.hook("pre-write", {"tool_name": "Write",
                                   "tool_input": {"file_path": os.path.join(link, "memo.py"), "content": "x"}})
        self.assertTrue(denied(res))

    def test_relative_path_write(self):
        res = self.e.hook("pre-write", {"tool_name": "Write",
                                        "tool_input": {"file_path": "src/memo.py", "content": "x"}})
        self.assertTrue(denied(res))

    def test_external_edit_is_caught_by_ownership(self):
        # hook を通らない変更（別プロセスなど）も、実装担当の登録時のコードとの差で検出する
        with open(os.path.join(self.e.project, "src", "memo.py"), "a", encoding="utf-8") as f:
            f.write("# outside\n")
        self.e.aq("gate")
        self.assertTrue(denied(self.e.spawn("reviewer")))
        self.assertIn("実装担当", self.e.next())


def json_text(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
