"""GitHub 操作ガード（T10）: 既存の検出漏れを含む。"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Env, denied  # noqa: E402

BLOCK = [
    "git push --force origin feature",
    "git push -f",
    "git push -fu origin feature",            # 短いオプションの組み合わせ（旧版の検出漏れ）
    "git push -uf origin feature",
    "git push origin +feature",
    "git push --force-with-lease",
    "git push --mirror",                      # 旧版の検出漏れ
    "git push --prune origin",                # 旧版の検出漏れ
    "git push origin --delete feature",
    "git push origin :feature",
    "git push -d origin feature",
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin feature:refs/heads/main",
    "git push --all origin",                  # 旧版の検出漏れ
    "git -C . push -f",
    "cd . && git push --force",
    "gh pr merge 3 --squash",
    "gh pr merge --auto 3",
    "gh repo delete owner/x --yes",
    "gh repo edit --visibility public",
    "gh release delete v1",
    "gh api -X DELETE repos/o/r",              # 旧版の検出漏れ
    "gh api repos/o/r/pulls/1/merge -X PUT",   # 旧版の検出漏れ
    "gh api --method PATCH repos/o/r -f visibility=public",
    "gh api graphql -f query='mutation { mergePullRequest(input:{}) { clientMutationId } }'",
    "curl -X DELETE https://api.github.com/repos/o/r",
    "git filter-branch --tree-filter x HEAD",
    "git config alias.p 'push --force'",
]
ALLOW = [
    "git push -u origin feature",
    "git push origin aq-dlc/run-1",
    "git status",
    "git commit -m 'fix: main 画面の修正'",
    "gh pr create --title t --body b",
    "gh pr view 3",
    "gh api repos/o/r/pulls",
    "echo push",
]


class GuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = Env(git=True)
        e = cls.e
        e.aq("init", "x")
        e.git("add", "-A")
        e.git("commit", "-q", "-m", "init")
        cls.remote = os.path.join(e.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", cls.remote], check=True, capture_output=True)
        e.git("remote", "add", "origin", cls.remote)
        e.git("push", "-q", "origin", "main")
        e.git("switch", "-q", "-c", "feature")

    @classmethod
    def tearDownClass(cls):
        cls.e.cleanup()

    def check(self, cmd):
        return self.e.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd}})

    def test_blocked(self):
        for cmd in BLOCK:
            with self.subTest(cmd=cmd):
                self.assertTrue(denied(self.check(cmd)), cmd)

    def test_allowed(self):
        for cmd in ALLOW:
            with self.subTest(cmd=cmd):
                self.assertFalse(denied(self.check(cmd)), cmd)

    def test_bare_push_on_main(self):
        e = self.e
        e.git("switch", "-q", "main")
        try:
            for cmd in ("git push", "git push -u origin", "git push origin HEAD"):
                with self.subTest(cmd=cmd):
                    self.assertTrue(denied(self.check(cmd)), cmd)
        finally:
            e.git("switch", "-q", "feature")

    def test_outside_project_untouched(self):
        other = os.path.join(self.e.tmp, "other")
        os.makedirs(other, exist_ok=True)
        res = self.e.hook("pre-bash", {"cwd": other, "tool_name": "Bash",
                                       "tool_input": {"command": "git push --force"}})
        self.assertFalse(denied(res))


if __name__ == "__main__":
    unittest.main()
