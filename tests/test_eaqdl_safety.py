"""E-AQDL Phase 1: 改名の互換・承認の意味づけ・Hook の安全側拒否・状態の整合性と監査ログ。"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ENGINE, ROOT, Env, build_to_review, denied, edit_json, read_file, write_file  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "plugins", "aq-dlc", "scripts"))
from aqdlc_core import approval as AP  # noqa: E402


class TestDecisionParsing(unittest.TestCase):
    T = "SPEC-1a2b3c4d"

    def test_explicit_line_is_decision(self):
        self.assertEqual(AP.decisions_in("承認 SPEC-1a2b3c4d", self.T)[0], "approve")
        self.assertEqual(AP.decisions_in("この仕様で進める（承認 SPEC-1a2b3c4d）", self.T)[0], "approve")
        self.assertEqual(AP.decisions_in("却下 SPEC-1a2b3c4d", self.T)[0], "reject")

    def test_ambiguous_or_untargeted_is_not_decision(self):
        for text in ("OK", "いいよ", "承認します", "承認 SPEC-ffffffff",
                     "承認 SPEC-1a2b3c4d\n却下 SPEC-1a2b3c4d",  # 承認と却下が混在
                     "承認する場合は「承認 SPEC-1a2b3c4d」と返信してくださいと言われたが、まだ決めていない"):
            self.assertIsNone(AP.decisions_in(text, self.T)[0], text)
        # ID の先頭一致だけでは決定にしない
        self.assertIsNone(AP.decisions_in("承認 SPEC-1a2b3c4d9", self.T)[0])

    def test_ask_response_uses_only_answers(self):
        resp = {"questions": [{"question": "進めてよいか", "options": [
            {"label": "この仕様で進める（承認 SPEC-1a2b3c4d）"}, {"label": "修正したい"}]}],
            "answers": {"進めてよいか": "修正したい"}}
        text, parsed = AP.parse_ask_response(resp)
        self.assertTrue(parsed)
        self.assertIsNone(AP.decisions_in(text, self.T)[0])
        text, parsed = AP.parse_ask_response('User has answered your questions: "進めてよいか"="この仕様で進める（承認 SPEC-1a2b3c4d）".')
        self.assertTrue(parsed)
        self.assertEqual(AP.decisions_in(text, self.T)[0], "approve")
        _t, parsed = AP.parse_ask_response({"something": 1})
        self.assertFalse(parsed)


class TestApprovalBinding(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        e = self.e
        e.aq("init", "x")
        e.aq("new", "x")
        e.aq("memory-scope", "project")
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "req", "R1", "メモを追加できる")
        e.as_role("requirements", "accept", "R1", "AC1", "一覧に出る")
        e.as_role("requirements", "verify", "AC1", "V1", "単体テスト", "--level", "unit")
        e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project),
                   "# 仕様\n## 目的\nx\n## 機能要件\nR1\n## 非機能要件\nなし\n## 受け入れ条件\nAC1\n"
                   "## リスク\nなし\n## 不明点\nなし\n## 対象外\nなし\n")
        assert e.stop("requirements", "完了") is None

    def tearDown(self):
        self.e.cleanup()

    def _target(self):
        import re
        return re.search(r"承認 (SPEC-[0-9a-f]{8})", self.e.aq("spec", "--present")).group(1)

    def test_vague_reply_is_not_approval(self):
        e = self.e
        self._target()
        e.human_says("OK")
        out = e.aq("spec", "--approve", "OK と返答", ok=False)
        self.assertIn("明示的な承認がありません", out)
        self.assertNotIn("approved_hash", e.state()["spec"])

    def test_approval_binds_target_time_run(self):
        e = self.e
        t = self._target()
        e.human_says(f"承認 {t}")
        e.aq("spec", "--approve", "承認")
        s = e.state()
        a = s["approvals"][-1]
        self.assertEqual((a["target"], a["decision"], a["run"], a["kind"]), (t, "approve", s["run_id"], "spec"))
        self.assertTrue(a["at"] and a["event_seq"])
        self.assertTrue(os.path.exists(os.path.join(e.run_dir, "audit.jsonl")))

    def test_reject_is_recorded_not_approved(self):
        e = self.e
        t = self._target()
        e.human_says(f"却下 {t}")
        e.aq("spec", "--approve", "x", ok=False)
        s = e.state()
        self.assertEqual(s["approvals"][-1]["decision"], "reject")
        self.assertNotIn("approved_hash", s["spec"])

    def test_approval_of_old_version_does_not_carry(self):
        e = self.e
        t = self._target()
        e.human_says(f"承認 {t}")
        # 承認の前に仕様が変わった（別の版）→ 古い版への承認は新しい版の承認にならない
        e.spawn("requirements")
        e.start("requirements")
        e.as_role("requirements", "accept", "R1", "AC1", "一覧に表示される")
        assert e.stop("requirements", "修正") is None
        t2 = self._target()
        self.assertNotEqual(t, t2)
        e.human_says("はい")
        e.aq("spec", "--approve", "x", ok=False)

    def test_forged_approval_in_state_is_detected(self):
        e = self.e
        self._target()
        s = e.state()
        s["spec"]["approved_hash"] = s["spec"]["presented_hash"]
        s["spec"]["approved_seq"] = s["seq"]
        e.save_state(s)
        out = e.aq("status")
        self.assertIn("state.json の整合性が取れていない", out)
        self.assertTrue(denied(e.spawn("design")))


class TestFailClosedHooks(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.e.aq("init", "x")
        self.e.aq("new", "x")

    def tearDown(self):
        self.e.cleanup()

    def _raw_hook(self, name, raw):
        p = subprocess.run([sys.executable, ENGINE, "hook", name], cwd=self.e.project, env=self.e.env,
                           input=raw, capture_output=True, text=True, encoding="utf-8")
        lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
        return json.loads(lines[-1]) if lines else None

    def test_unreadable_payload_is_denied_in_project(self):
        for name in ("pre-write", "pre-agent", "pre-bash"):
            self.assertTrue(denied(self._raw_hook(name, "{not json")), name)

    def test_unreadable_payload_outside_project_is_not_blocked(self):
        out = os.path.join(self.e.tmp, "other")
        os.makedirs(out)
        p = subprocess.run([sys.executable, ENGINE, "hook", "pre-write"], cwd=out, env=self.e.env,
                           input="{bad", capture_output=True, text=True, encoding="utf-8")
        self.assertNotIn("deny", p.stdout)

    def test_broken_state_denies_write_and_agent(self):
        e = self.e
        write_file(os.path.join(e.run_dir, "state.json"), "{broken")
        res = e.hook("pre-write", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(e.project, "a.py")}})
        self.assertTrue(denied(res))
        res = e.hook("pre-agent", {"tool_name": "Agent", "tool_input": {"subagent_type": "aq-dlc:design"}})
        self.assertTrue(denied(res))
        from aqdlc_core import approval
        log = os.path.join(e.home, "audit.jsonl")
        self.assertTrue(any(r["event"] == "hook.error" for r in approval.audit_read(log)))


class TestStateIntegrity(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        build_to_review(self.e, with_question=False)

    def tearDown(self):
        self.e.cleanup()

    def test_forged_gate_pass_is_detected(self):
        e = self.e
        s = e.state()
        for r in s["gate"]["results"]["checks"].values():
            r["pass"] = False
        s["gate"]["checks_pass"] = True
        e.save_state(s)
        self.assertIn("gate が合格になっているが", e.aq("check", ok=False))

    def test_engine_refuses_to_introduce_invalid_state(self):
        from aqdlc_core import state as S
        s, p = S.load_state(self.e.project)
        s["status"] = "unknown-status"
        with self.assertRaises(S.StateError):
            S.save_state(p, s)

    def test_concurrent_update_is_rejected(self):
        os.environ["AQDLC_HOME"] = self.e.home
        from aqdlc_core import state as S
        a, p = S.load_state(self.e.project)
        b, _ = S.load_state(self.e.project)
        a["title"] = "A"
        S.save_state(p, a)
        b["title"] = "B"
        with self.assertRaises(S.StateError):
            S.save_state(p, b)
        self.assertEqual(json.loads(read_file(p))["title"], "A")

    def test_audit_chain_detects_edit(self):
        e = self.e
        log = os.path.join(e.run_dir, "audit.jsonl")
        n, errs = AP.audit_verify(log)
        self.assertGreater(n, 0)
        self.assertEqual(errs, [])
        lines = read_file(log).splitlines()
        obj = json.loads(lines[0])
        obj["rec"]["decision"] = "reject"
        lines[0] = json.dumps(obj, ensure_ascii=False)
        write_file(log, "\n".join(lines) + "\n")
        self.assertTrue(AP.audit_verify(log)[1])
        self.assertNotEqual(e.run(["audit", "--verify", "--project", e.project])[0], 0)


class TestCompatibility(unittest.TestCase):
    def test_new_sections_at_default_do_not_change_config_fp(self):
        from aqdlc_core import gate as G
        from aqdlc_core.project import DEFAULTS, EAQDL_SECTIONS
        cfg = json.loads(json.dumps(DEFAULTS))
        legacy = {k: v for k, v in cfg.items() if k not in EAQDL_SECTIONS}
        self.assertEqual(G.config_fp(cfg), G.config_fp(legacy))
        cfg["counterfactual"]["mode"] = "enforce"
        self.assertNotEqual(G.config_fp(cfg), G.config_fp(legacy))

    def test_legacy_run_keeps_working(self):
        e = Env()
        try:
            build_to_review(e, with_question=False)
            s = e.state()
            # v0.5 の run を模擬: E-AQDL の項目がない
            for k in ("eaqdl", "approvals", "rev", "memory_scope", "roles"):
                s.pop(k, None)
            e.save_state(s)
            out = e.aq("status")
            self.assertNotIn("整合性", out)
            self.assertNotIn("Memory の利用範囲", out)
            self.assertIn("独立レビュー", out)
            self.assertFalse(denied(e.spawn("reviewer")))
        finally:
            e.cleanup()

    def test_ids_and_names_are_kept(self):
        plugin = json.loads(read_file(os.path.join(ROOT, "plugins", "aq-dlc", ".claude-plugin", "plugin.json")))
        self.assertEqual(plugin["name"], "aq-dlc")
        self.assertIn("E-AQDL", plugin["displayName"])
        for role in ("requirements", "design", "implementer", "reviewer"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "plugins", "aq-dlc", "agents", f"{role}.md")))
        self.assertTrue(os.path.exists(ENGINE))


if __name__ == "__main__":
    unittest.main()
