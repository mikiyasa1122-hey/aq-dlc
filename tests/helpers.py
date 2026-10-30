"""テスト用の補助: 一時プロジェクト、エンジンの実行、hook の模擬入力。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "plugins", "aq-dlc", "scripts", "aqdlc.py")


class Env:
    def __init__(self, git=False):
        self.tmp = tempfile.mkdtemp(prefix="aqdlc-test-")
        self.home = os.path.join(self.tmp, "home")
        self.project = os.path.join(self.tmp, "app")
        os.makedirs(self.project)
        self.env = dict(os.environ, AQDLC_HOME=self.home, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        self.env.pop("CLAUDE_PROJECT_DIR", None)
        # Claude Code の会話記録（人間の返答の照合に使う）を模擬する
        self.claude_dir = os.path.join(self.tmp, "claude-config")
        self.env["CLAUDE_CONFIG_DIR"] = self.claude_dir
        self.transcript = os.path.join(self.claude_dir, "projects", "app", "session.jsonl")
        os.makedirs(os.path.dirname(self.transcript), exist_ok=True)
        self._ask_n = 0
        self.env.pop("AQDLC_PROJECT", None)
        self.agent_ids = {}
        if git:
            self.git("init", "-q", "-b", "main")

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ process helpers
    def run(self, args, stdin=None, cwd=None, split=False):
        p = subprocess.run([sys.executable, ENGINE] + list(args), cwd=cwd or self.project, env=self.env,
                           input=stdin, capture_output=True, text=True, encoding="utf-8")
        if split:
            return p.returncode, p.stdout, p.stderr
        return p.returncode, p.stdout + p.stderr

    def aq(self, *args, ok=True):
        code, out = self.run(list(args) + ["--project", self.project])
        if ok is True and code != 0:
            raise AssertionError(f"aq {args} failed ({code}):\n{out}")
        if ok is False and code == 0:
            raise AssertionError(f"aq {args} should fail:\n{out}")
        return out

    def hook(self, name, payload=None):
        payload = dict(payload or {})
        payload.setdefault("cwd", self.project)
        code, out, err = self.run(["hook", name], stdin=json.dumps(payload, ensure_ascii=False), split=True)
        if code != 0 or "Traceback" in err:
            raise AssertionError(f"hook {name} exited {code}: {out}{err}")
        out = out.strip()
        if not out:
            return None
        lines = [ln for ln in out.splitlines() if ln.startswith("{")]
        return json.loads(lines[-1]) if lines else {"raw": out}

    def git(self, *args, cwd=None):
        return subprocess.run(["git"] + list(args), cwd=cwd or self.project, env=self.env,
                              capture_output=True, text=True, check=True).stdout

    # ------------------------------------------------------------ role simulation
    def as_role(self, role, *args, ok=True):
        """サブエージェント（role）が Bash でエンジンを呼ぶ流れ: PreToolUse(Bash) → 実行。"""
        cmd = f'python3 "{ENGINE}" ' + " ".join(f'"{a}"' for a in args) + f' --project "{self.project}"'
        res = self.hook("pre-bash", {"tool_name": "Bash", "tool_input": {"command": cmd},
                                     **self.agent_fields(role)})
        if res and res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            if ok is True:
                raise AssertionError(f"{role} denied: {res}")
            return res
        return self.aq(*args, ok=ok)

    def agent_fields(self, role):
        if role in (None, "orchestrator"):
            return {}
        return {"agent_type": f"aq-dlc:{role}", "agent_id": self.agent_ids.get(role, f"id-{role}")}

    def spawn(self, role, background=None):
        ti = {"subagent_type": f"aq-dlc:{role}", "description": role, "prompt": "作業してください"}
        if background is not None:
            ti["run_in_background"] = background
        return self.hook("pre-agent", {"tool_name": "Agent", "tool_input": ti})

    def start(self, role):
        n = len(self.agent_ids) + 1
        self.agent_ids[role] = f"agent-{role}-{n}"
        return self.hook("subagent-start", {"agent_type": f"aq-dlc:{role}", "agent_id": self.agent_ids[role]})

    def stop(self, role, message=""):
        return self.hook("subagent-stop", {"agent_type": f"aq-dlc:{role}", "agent_id": self.agent_ids[role],
                                           "last_assistant_message": message, "stop_hook_active": False})

    def write_as(self, role, rel, text):
        path = os.path.join(self.project, rel)
        res = self.hook("pre-write", {"tool_name": "Write", "tool_input": {"file_path": path, "content": text},
                                      **self.agent_fields(role)})
        if res and res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
            return res
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(text))
        return None

    def _tx(self, obj):
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def human_says(self, text="はい、それで進めてください"):
        self._tx({"type": "user", "message": {"role": "user", "content": text}})
        self.hook("user-prompt", {"prompt": text, "transcript_path": self.transcript})

    def spec_target(self):
        import re
        out = self.aq("spec")
        return re.search(r"仕様 ([0-9a-f]{8})", out) and "SPEC-" + re.search(r"仕様 ([0-9a-f]{8})", out).group(1)

    def approve_spec(self, note="ユーザーが仕様を承認"):
        """仕様を提示し、人間が対象を指定して承認する流れ。"""
        import re
        out = self.aq("spec", "--present")
        target = re.search(r"承認 (SPEC-[0-9a-f]{8})", out).group(1)
        self.human_says(f"承認 {target}")
        return self.aq("spec", "--approve", note)

    def human_answers(self, answers):
        self._ask_n += 1
        tid = f"toolu_ask{self._ask_n}"
        self._tx({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": {"questions": []}}]}})
        text = "User has answered your questions: " + ", ".join(f'"{k}"="{v}"' for k, v in answers.items()) + "."
        self._tx({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text}]}, "toolUseResult": {"answers": answers}})
        self.hook("post-ask", {"tool_name": "AskUserQuestion", "tool_input": {"questions": []},
                               "tool_response": {"answers": answers}, "transcript_path": self.transcript})

    # ------------------------------------------------------------ state
    @property
    def run_dir(self):
        with open(os.path.join(self.project, ".aq-dlc", "runs", "CURRENT"), encoding="utf-8") as f:
            rid = f.read().strip()
        return os.path.join(self.project, ".aq-dlc", "runs", rid)

    @property
    def arts(self):
        return os.path.join(self.run_dir, "artifacts")

    def state(self):
        with open(os.path.join(self.run_dir, "state.json"), encoding="utf-8") as f:
            return json.load(f)

    def save_state(self, s):
        with open(os.path.join(self.run_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)

    def next(self):
        return self.aq("next")


def denied(res):
    return bool(res) and res.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def blocked(res):
    return bool(res) and res.get("decision") == "block"


SPEC_MD = """# 仕様書
## 目的
メモを保存する。
## 機能要件
- R1 メモを追加できる
## 非機能要件
- R2 1秒以内に応答する
## 受け入れ条件
- AC1 追加したメモが一覧に出る
- AC2 応答が1秒以内
## リスク
- K1 空のメモが保存される
## 不明点・矛盾点
- Q1 メモの最大文字数
## 対象外
- ログイン
"""

DESIGN_MD = """# 設計書
仕様: {spec}
## 技術スタック
Python 標準ライブラリ
## アーキテクチャ
単一モジュール
## ディレクトリ構成
src/ と tests/
## データモデル
メモはリスト
## エラーハンドリング
空のメモは ValueError
## セキュリティ
外部入力なし
## テスト方針
unittest で単体テスト
## 要求との対応
R1 → src/memo.py、R2 → src/memo.py
## 設計判断とトレードオフ
最小構成
## 差し戻し
- なし
"""

IMPL_MD = """# 実装報告
設計書: {design}
仕様: {spec}
## 実施内容
メモの追加を実装した。
## 検証結果
python3 -m unittest で合格。
{extra}
## 差し戻し
- なし
"""

REVIEW_FAIL = """## レビュー結果
判定: 要修正
重大な指摘: 1
軽微な指摘: 1
レビュー対象: 仕様 x / 設計 y / コード z
### 指摘
- [重大1] 区分: 実装 / 対象: R1, V2 / 内容: 空白だけのメモが保存される / 根拠: src/memo.py:3 / 提案: strip して判定
- [軽微1] 区分: テスト / 対象: V1 / 内容: テスト名が分かりにくい
### 受け入れ条件ごとの検証の妥当性
- AC1: V1 は追加後の一覧を確認しており妥当
- AC2: V3 は全テストの実行で妥当
- K1: V2 は空のメモの拒否を確認しており妥当
### E2E の要否
- 不要（画面を持たないライブラリのため）
### 不足しているリスク
- なし
### 確認したこと
- python3 -m unittest
"""

REVIEW_PASS = """## レビュー結果
判定: 合格
重大な指摘: 0
軽微な指摘: 0
レビュー対象: 仕様 x / 設計 y / コード z
### 指摘
- なし
### 受け入れ条件ごとの検証の妥当性
- AC1: V1 は追加後の一覧を確認しており妥当
- AC2: V3 は全テストの実行で妥当
- K1: V2 は空のメモの拒否を確認しており妥当
### E2E の要否
- 不要（画面を持たないライブラリのため）
### 不足しているリスク
- なし
### 確認したこと
- python3 -m unittest
"""

MEMO_V1 = '''def add(memos, text):
    if text == "":
        raise ValueError("空のメモ")
    memos.append(text)
    return memos
'''

MEMO_V2 = '''def add(memos, text):
    if not text.strip():
        raise ValueError("空のメモ")
    memos.append(text)
    return memos
'''

TEST_MEMO = '''import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import memo  # noqa: E402


class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(memo.add([], "a"), ["a"])

    def test_empty(self):
        with self.assertRaises(ValueError):
            memo.add([], "")


if __name__ == "__main__":
    unittest.main()
'''


def build_to_review(e, with_question=True, config=None):
    """仕様承認 → 設計 → 実装 → gate 合格まで進める（各テストの前提）。config: プロジェクト設定に足す項目。"""
    py = f'"{sys.executable}"'
    e.aq("init", "メモ")
    if config:
        edit_json(os.path.join(e.project, ".aq-dlc", "config.json"), lambda c: c.update(config))
    e.aq("new", "メモ")
    e.aq("memory-scope", "project")
    assert not denied(e.spawn("requirements"))
    e.start("requirements")
    e.as_role("requirements", "req", "R1", "メモを追加できる", "--critical")
    e.as_role("requirements", "req", "R2", "1秒以内に応答する", "--kind", "nonfunctional")
    e.as_role("requirements", "accept", "R1", "AC1", "追加したメモが一覧に出る")
    e.as_role("requirements", "accept", "R2", "AC2", "応答が1秒以内")
    e.as_role("requirements", "risk", "R1", "K1", "空のメモが保存される")
    e.as_role("requirements", "verify", "AC1", "V1", "追加の単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "K1", "V2", "空のメモを拒否する単体テスト", "--level", "unit")
    e.as_role("requirements", "verify", "AC2", "V3", "全テストが通る", "--level", "unit")
    if with_question:
        e.as_role("requirements", "question", "Q1", "メモの最大文字数は？", "--options", "制限なし（推奨）|100文字")
    e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project), SPEC_MD)
    assert e.stop("requirements", "要求2件を整理") is None
    if with_question:
        e.human_answers({"Q1": "制限なし"})
        e.aq("answer", "Q1", "制限なし")
        e.spawn("requirements")
        e.start("requirements")
        e.write_as("requirements", os.path.relpath(os.path.join(e.arts, "spec.md"), e.project),
                   SPEC_MD + "\n（Q1: 制限なしで確定）\n")
        assert e.stop("requirements", "回答を反映") is None
    e.approve_spec("ユーザーが『この仕様で進めて』と返答")
    assert not denied(e.spawn("design"))
    e.start("design")
    s = e.state()
    from_spec = s["spec"]["registered_hash"]
    e.as_role("design", "design-map", "R1", "src/memo.py の add")
    e.as_role("design", "design-map", "R2", "src/memo.py（計算量 O(1)）")
    e.as_role("design", "add-check", "unit", f"{py} -m unittest discover -s tests -q", "--kind", "unit")
    e.write_as("design", os.path.relpath(os.path.join(e.arts, "design.md"), e.project),
               DESIGN_MD.format(spec=from_spec[:8]))
    assert e.stop("design", "設計完了") is None
    assert not denied(e.spawn("implementer"))
    e.start("implementer")
    implement(e, MEMO_V1)
    assert e.stop("implementer", "実装完了") is None
    e.aq("gate")


def implement(e, memo_src, extra=""):
    py = f'"{sys.executable}"'
    e.write_as("implementer", "src/memo.py", memo_src)
    e.write_as("implementer", "tests/test_memo.py", TEST_MEMO)
    e.as_role("implementer", "impl-map", "R1", "--files", "src/memo.py")
    e.as_role("implementer", "impl-map", "R2", "--files", "src/memo.py")
    e.as_role("implementer", "bind", "V1", "--command", f"{py} -m unittest tests.test_memo.T.test_add",
              "--test", "tests/test_memo.py::test_add")
    e.as_role("implementer", "bind", "V2", "--command", f"{py} -m unittest tests.test_memo.T.test_empty",
              "--test", "tests/test_memo.py::test_empty")
    e.as_role("implementer", "bind", "V3", "--checks", "unit")
    s = e.state()
    design_hash = s["design"]["registered_hash"]
    spec_hash = s["spec"]["registered_hash"]
    e.write_as("implementer", os.path.relpath(os.path.join(e.arts, "impl.md"), e.project),
               IMPL_MD.format(design=design_hash[:8], spec=spec_hash[:8], extra=extra))


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_file(path, text, newline=None):
    with open(path, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)


def edit_json(path, fn):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    fn(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def bash_as(e, role, cmd, tool_use_id="t1"):
    """Bash ツールの流れを模擬する: PreToolUse(Bash) → シェルで実行 → PostToolUse(Bash)。

    戻り値: (pre の結果, post の結果)。pre で拒否されたらシェルは実行しない。
    """
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": tool_use_id,
               **e.agent_fields(role)}
    pre = e.hook("pre-bash", payload)
    if denied(pre):
        return pre, None
    subprocess.run([bash_path(), "-c", cmd], cwd=e.project, env=e.env, capture_output=True)
    post = e.hook("post-bash", dict(payload, tool_response={"stdout": "", "stderr": ""}))
    return pre, post


def bash_path():
    """Claude Code の Bash ツールと同じく bash で実行する（Windows では Git Bash）。"""
    import shutil
    if os.name == "nt":
        for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if os.path.exists(c):
                return c
    return shutil.which("bash") or "bash"
