---
name: start
description: 新規アプリをAQ-DLC方式で開発する。開いているフォルダをプロジェクトとして初期化し、GitHubリポジトリを用意して、Failure First → 実装 → Quality Gate → 独立レビュー → 証拠で完了判定 → Memory整理まで自動で進める。空のフォルダやAQ-DLCプロジェクトで、アプリ・ツール・機能の作成や修正を頼まれたときは、ユーザーがコマンドを打たなくても使う。/aq-dlc:start で明示的に呼んでもよい。
---

# AQ-DLC — 証拠で完了する開発ループ

「コードを生成したから完了」ではなく、「要求された振る舞いを証拠で確認できたから完了」。

このSkillは**手順（固定）**を定める。学び（可変）は Memory にある。プラグインのファイルは書き換えない。改善が必要だと思ったら、run.md の「Skill改善提案」に書く。

## 自動で進める（既定の動き）
- 依頼を受けたら、計画の承認を求めずに 00 から完了まで続けて進める。途中経過の報告は区切りごとに1〜2行でよい。
- 決まっていないこと（技術スタック、画面構成、データ構造、命名、GitHubの公開範囲など）は、妥当な既定値を自分で選び、run.md の「仮定」に書いて進める。公開範囲の既定は設定の `github.default_visibility`。
- 止まって聞くのは「人間に確認するとき」に挙げたものだけ。それ以外で迷ったら、取り消しやすい方を選んで進める。
- ツールの許可を求められて止まった場合は、何の許可が必要かを1行で伝える。
- 進行中のrunがあるフォルダで再開するときは、`status` で未充足条件を確認し、続きから進める。

## エンジン

すべてのコマンドに、作成したプロジェクトの絶対パスを `--project` で渡す（Bashのカレントディレクトリに依存しないため）。

```bash
PL='python3 "${CLAUDE_PLUGIN_ROOT}/scripts/aqdlc.py"'
$PL setup                                   # 初回のみ。~/.aq-dlc を初期化
$PL init "<app名>"                           # 開いているフォルダを初期化し、絶対パスを出力
$PL create "<app名>" [--parent DIR]          # 明示されたときだけ: サブフォルダを作って初期化
$PL publish --project P [--visibility V]    # git init → GitHubリポジトリ作成 → push
$PL new "<run名>" --project P                # runを開始
$PL memory --tags a,b [--stack x] --project P
$PL gate --project P                         # Quality Gate を実行し記録
$PL status --project P
$PL check --project P                        # 完了判定（Stop hook と同じ）
```

役割分担：あなた（AI）は仕様理解・リスク提案・設計・実装・テスト・学習候補の抽出を担当する。エンジンは状態管理・gate実行・証拠照合・完了判定を担当する。完了は**自己申告で決めない**。

## 00 プロジェクトの準備
基本は**Claude Codeで開いているフォルダ**をそのままプロジェクトにする。
1. `$PL setup` を実行する（何度実行してもよい）。
2. `$PL init "<app名>"` で、開いているフォルダを初期化する（既存ファイルは上書きしない）。出力の最終行の絶対パスを以後 `P` とする。
   - ホームフォルダで開かれている、または既存ファイルが多く新規開発に見えない場合は、初期化せずに、アプリ用の空フォルダを開き直すよう伝える。
   - ユーザーが「新しいフォルダを作って」と明示した場合だけ、`$PL create "<app名>"` でサブフォルダを作り、その出力を `P` とする。
3. ファイル作成・編集は `P` の中で行う。Bashで別のフォルダにいる可能性がある場合は `cd "P" && ...` の形にする。
4. `$PL publish --project P` でGitHubリポジトリを作る。公開範囲は、ユーザーの指定 → 全体設定の `github.default_visibility`（初期値 private）。gh が未ログインなら、その旨を伝えてローカルのgitだけで進める。
5. `git -C "P" switch -c aq-dlc/<run名>` で作業ブランチを作る。

## 01 run開始・Memory・要求
1. `$PL new "<短い名前>" --project P`。
2. 目的・想定技術・リスク領域からタグを決め、`$PL memory --tags ... --project P` で**関連するものだけ**読む。全件は読まない。`active` のRuleは従い、`validated` は参考にする。読んだidは `state.json` の `memory_loaded` に書く。
3. 作る機能・受け入れ条件・重要な制約を `state.json` の `requirements` に書く。重要なものは `"critical": true`。新規開発なので既存実装の調査は不要。
4. 作り直しが大きくなる曖昧さだけユーザーに確認する（後述の手順で）。それ以外は妥当な判断で進め、run.md に「仮定」として残す。

## 02 Failure First
各requirementについて、失敗・リスクを先に洗い出し `risks` に書く。観点：不正入力、権限・認可、境界値、データ不整合、エラー時の振る舞い、秘密情報、並行性（該当時）。各riskに `verifications` を最低1つ付ける（`references/evidence-graph.md`）。

## 03 設計・実装・テスト
1. 技術スタックを決めたら、`P/.aq-dlc/config.json` の `checks` にその言語の検証コマンドを登録する（`references/quality-gate.md`）。選んだ理由は run.md と Project Memory に残す。
2. 実装とテストを書く。verificationの `command` から実行できる形にする。
3. 区切りごとにコミットし、作業ブランチを push してよい。

## 04 Quality Gate ＋ 独立レビュー
1. `$PL gate --project P`。
2. 機械的な不整合（型・Lint・ビルド）を先に解消する。
3. **aq-dlc:reviewer サブエージェント**にレビューを依頼する。渡すもの：プロジェクトパス、requirements と risks、変更ファイル一覧、gate結果。実装の経緯や自己評価は渡さない。
4. 結果を `state.json` の `review`（`done`、`blocking_findings`、`summary`）に記録する。

## 05 修正ループ
型エラー → 修正、テスト不足 → verification追加、仕様逸脱 → 実装修正、リスク漏れ → risk追加。修正後は必ず gate をやり直す（gate後の変更はstale扱い）。上限は `automation.max_fix_iterations`。超えたら人間に判断を仰ぐ。

## 06 Memory整理
**aq-dlc:memory Skill** の手順で、学びを選別・統合・保存する。終わったら `memory_done: true`。

## 完了
1. `state.json` の `status` を `"complete"` にし、`$PL check --project P` を通す。NGなら直して戻る。
2. 作業ブランチを push し、`gh pr create` でPRを作る。PR本文には requirement → 証拠（gate結果）の対応表、残った注意点、保存したMemoryを書く。マージは人間が行う。
3. ユーザーへの報告は短く：作ったもの、確かめた方法、PRのURL、残った注意点、確認待ちのMemory。

## 人間に確認するとき（例外だけ）
- 対象：作り直しが大きい要求の曖昧さ、verificationの免除、秘密情報・課金・GitHub以外の外部サービスへの書き込み、プロジェクト外のファイル変更、テストや検証を弱める変更、修正ループ上限到達、Memory Review。
- 手順：`state.json` に `"awaiting_human": true` と `"question"` を書いてから質問する。Stop hookはこの状態なら停止を許可する。返答を受けたら `false` に戻す。質問は1回にまとめ、可能なら複数選択式（AskUserQuestion）。
- 中断を指示されたら `status` を `"aborted"` にする。

## してはいけないこと
- テストや検証コマンドを弱める・消すことで gate を通す。
- 失敗した結果を書き換える。結果はエンジンだけが記録する。
- AIの判断だけで `waived_by: "human"` を書く。
- force push・履歴の書き換え、main への直接 push、PRのマージ、リポジトリ・ブランチ・リリースの削除、公開範囲の変更（hookでも止まる）。
- プラグインのファイル（Skill・agent・hook・エンジン）を書き換える。

## 参照
- `references/evidence-graph.md` — state.json の書き方と完了条件
- `references/quality-gate.md` — 言語別の check 例
- `references/memory-format.md` — Memoryファイルの形式
