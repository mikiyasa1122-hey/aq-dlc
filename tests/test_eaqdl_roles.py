"""E-AQDL Phase 2: Adaptive Role Engine。"""
import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (REVIEW_PASS, ROOT, Env, build_to_review, denied, edit_json, implement, MEMO_V2,  # noqa: E402
                     read_file, write_file)

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import agents as A  # noqa: E402
from aqdlc_core import roles as RL  # noqa: E402

ROLES_SRC = os.path.join(ROOT, "plugins", "aq-dlc", "roles")


def ctx_text(res):
    return res["hookSpecificOutput"]["additionalContext"]


class TestStageProfiles(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def test_each_stage_gets_its_profile_and_is_recorded(self):
        e = self.e
        hist = e.state()["roles"]["history"]
        by = {h["agent"]: h for h in hist}
        self.assertEqual([x["id"] for x in by["requirements"]["stage"]], ["requirements-analyst"])
        self.assertEqual([x["id"] for x in by["design"]["stage"]], ["software-architect"])
        self.assertEqual([x["id"] for x in by["implementer"]["stage"]], ["software-engineer", "qa-test-designer"])
        for h in hist:
            for x in h["stage"]:
                self.assertTrue(x["version"] >= 1 and len(x["hash"]) == 12)
        self.assertEqual(by["requirements"]["trigger"], "start")
        e.spawn("reviewer")
        ctx = ctx_text(e.start("reviewer"))
        self.assertIn("Principal Code Reviewer", ctx)
        self.assertIn("独立した別のモデルではない", ctx)
        self.assertIn("使えるツール・人間の承認・安全上の制約は変わらない", ctx)

    def test_sendback_trigger_and_transition(self):
        e = self.e
        e.spawn("reviewer")
        e.start("reviewer")
        from helpers import REVIEW_FAIL
        e.stop("reviewer", REVIEW_FAIL)
        e.spawn("implementer")
        e.start("implementer")
        hist = e.state()["roles"]["history"]
        self.assertEqual(hist[-1]["agent"], "implementer")
        self.assertEqual(hist[-1]["trigger"], "sendback")

    def test_selection_is_deterministic(self):
        e = self.e
        os.environ["AQDLC_HOME"] = e.home
        from aqdlc_core.state import Ctx, load_state
        s, _ = load_state(e.project)
        a = A.role_selection("implementer", Ctx(e.project, s))
        b = A.role_selection("implementer", Ctx(e.project, s))
        self.assertEqual(RL.public(a), RL.public(b))

    def test_profile_change_invalidates_review(self):
        e = self.e
        tmp = os.path.join(e.tmp, "roles")
        shutil.copytree(ROLES_SRC, tmp)
        e.env["AQDLC_ROLES_DIR"] = tmp
        e.spawn("reviewer")
        e.start("reviewer")
        assert e.stop("reviewer", REVIEW_PASS) is None
        self.assertNotIn("Role Profile", e.aq("next"))
        p = os.path.join(tmp, "stage", "code-reviewer.md")
        write_file(p, read_file(p).replace("version: 1", "version: 2"))
        self.assertIn("Role Profile", e.aq("next"))


class TestDomainSelection(unittest.TestCase):
    def state(self, texts, files=(), critical=True):
        reqs = []
        for i, t in enumerate(texts, 1):
            reqs.append({"id": f"R{i}", "text": t, "critical": critical and i == 1,
                         "acceptance": [{"id": f"AC{i}", "text": t, "verifications": []}], "risks": []})
        return {"requirements": reqs, "design": {"map": {}}, "impl": {"map": {"R1": list(files)}}}

    def test_security_profile_for_auth_requirements(self):
        st = self.state(["ログインした利用者だけが自分のメモを編集できる（権限のない操作は拒否）"], ["src/auth/session.py"])
        sel = RL.select("implementer", st, {})
        ids = [x["id"] for x in sel["domain"]]
        self.assertIn("security", ids)
        sec = next(x for x in sel["domain"] if x["id"] == "security")
        self.assertIn("R1", sec["related"])
        self.assertTrue(any("キーワード" in r for r in sec["reasons"]))

    def test_weak_signal_falls_back_to_base(self):
        st = self.state(["メモを一覧に表示する"], critical=False)  # 「表示」だけでは frontend の根拠が不足（点数 1）
        sel = RL.select("design", st, {})
        self.assertEqual(sel["domain"], [])
        self.assertTrue(sel["fallback"])
        self.assertTrue(any(d["id"] == "frontend" for d in sel["dropped"]))

    def test_priority_limit_and_conflict(self):
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_roles")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(ROLES_SRC, tmp)
        os.environ["AQDLC_ROLES_DIR"] = tmp
        try:
            st = self.state(["ログインと権限、トークンの検証、同時更新とリトライ、DB のトランザクション、"
                             "API のエンドポイント、応答時間 1 秒以内、画面のフォーム"])
            sel = RL.select("implementer", st, {"roles": {"max_domain_profiles": 2}})
            self.assertEqual([x["id"] for x in sel["domain"]], ["security", "concurrency"])  # 優先度順に2件
            self.assertTrue(any("上限" in d["why"] for d in sel["dropped"]))
            # 競合: 優先度・点数が同じなら両方とも外す（曖昧な判定は基本 Profile だけ）
            for name, other in (("performance", "frontend"),):
                p = os.path.join(tmp, "domain", f"{name}.md")
                write_file(p, read_file(p).replace("conflicts: []", f"conflicts: [{other}]")
                           .replace("priority: 55", "priority: 50"))
            st2 = self.state(["応答時間 1 秒以内の画面のフォーム"])
            a = RL.select("implementer", st2, {})
            b = RL.select("implementer", st2, {})
            self.assertEqual(RL.public(a), RL.public(b))
            ids = [x["id"] for x in a["domain"]]
            self.assertNotIn("performance", ids)
            self.assertNotIn("frontend", ids)
            self.assertTrue(any("判定できない" in d["why"] for d in a["dropped"]))
        finally:
            os.environ.pop("AQDLC_ROLES_DIR", None)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_or_permission_profiles_are_rejected(self):
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_roles2")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(ROLES_SRC, tmp)
        os.environ["AQDLC_ROLES_DIR"] = tmp
        try:
            p = os.path.join(tmp, "domain", "security.md")
            write_file(p, read_file(p).replace("kind: domain", "kind: domain\ntools: Write, Bash"))
            write_file(os.path.join(tmp, "domain", "broken.md"), "見出しも frontmatter もない")
            profiles, rejected = RL.load_profiles({})
            self.assertNotIn("security", profiles)
            names = {os.path.basename(r["path"]) for r in rejected}
            self.assertEqual(names, {"security.md", "broken.md"})
            self.assertTrue(any("権限" in e for r in rejected for e in r["errors"]))
        finally:
            os.environ.pop("AQDLC_ROLES_DIR", None)
            shutil.rmtree(tmp, ignore_errors=True)


class TestPermissionsUnchanged(unittest.TestCase):
    def test_security_profile_does_not_expand_reviewer_permissions(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            s = e.state()
            s["requirements"][0]["text"] = "ログインと権限の検証、トークン"  # security を適用させる
            e.spawn("reviewer")
            res = e.start("reviewer")
            # 役割の追加は引き継ぎ文だけ。書き込み・担当外のコマンドは従来どおり拒否される
            self.assertTrue(denied(e.write_as("reviewer", "src/memo.py", "x")))
            self.assertTrue(denied(e.as_role("reviewer", "bind", "V1", "--level", "unit", ok=None)))
            self.assertTrue(denied(e.as_role("reviewer", "cf", ok=None)))
            self.assertTrue(res)
            for role in ("requirements", "design", "implementer", "reviewer"):
                self.assertEqual(A.ROLE_COMMANDS[role] - A.COMMON_READ,
                                 {"requirements": {"req", "accept", "risk", "verify", "assume", "question", "drop"},
                                  "design": {"design-map", "decision", "add-check", "remove-check", "bind"},
                                  "implementer": {"bind", "impl-map", "add-check", "gate", "cf"},
                                  "reviewer": set()}[role])
        finally:
            e.cleanup()

    def test_agent_definitions_have_no_permission_from_profiles(self):
        out = os.popen(f'"{sys.executable}" "{os.path.join(ROOT, "plugins", "aq-dlc", "scripts", "aqdlc.py")}" roles --check').read()
        self.assertIn("OK", out)
        self.assertNotIn("NG", out)


if __name__ == "__main__":
    unittest.main()
