---
name: start
description: 新規アプリをAQ-DLC方式で開発する。開いているフォルダをプロジェクトとして初期化し、GitHubリポジトリを用意して、Failure First → 実装 → Quality Gate → 独立レビュー → 証拠で完了判定 → Memory整理まで自動で進める。空のフォルダやAQ-DLCプロジェクトで、アプリ・ツール・機能の作成や修正を頼まれたときは、ユーザーがコマンドを打たなくても使う。/aq-dlc:start で明示的に呼んでもよい。
---

# AQ-DLC — 証拠で完了する開発ループ

「コードを生成したから完了」ではなく、「要求された振る舞いを証拠で確認できたから完了」。

このSkillは**手順（固定）**を定める。学び（可変）は Memory にある。プラグインのファイルは書き換えない。改善が必要だと思ったら、run.md の「Skill改善提案」に書く。

## 言語
ユーザーへの返答・質問・進捗報告、run.md、Memory、コミットメッセージ、PRのタイトルと本文、Issue、コード内のコメント、サブエージェントへの依頼は、**すべて日本語**で書く。英語のエラーやログは日本語で要約してから、必要な部分だけ引用する。コードの識別子やコマンドは英語のままでよい。

## Windows で使うとき
エンジンの入出力は UTF-8 に統一してある。Playwright を使う場合は、ブラウザをプロジェクト内に入れる（`PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install`）。init 時に gate の環境変数にも同じ設定が入る。Claude Desktop では AppData への書き込みが別の場所に振り替えられ、gate から見えないことがあるため。テストやビルドのコマンドも UTF-8 で出力されるようにする（Python は `PYTHONUTF8=1` がエンジンから渡される。Node 等で文字化けする場合は `chcp 65001` を含めたコマンドにする）。パスは `/` 区切りで渡してよい。

## 自動で進める（既定の動き）
- 依頼を受けたら、計画の承認を求めずに 00 から完了まで続けて進める。途中経過の報告は区切りごとに1〜2行でよい。
- 決まっていないこと（技術スタック、画面構成、データ構造、命名、GitHubの公開範囲など）は、妥当な既定値を自分で選び、run.md の「仮定」に書いて進める。公開範囲の既定は設定の `github.default_visibility`。
- 止まって聞くのは「人間に確認するとき」に挙げたものだけ。それ以外で迷ったら、取り消しやすい方を選んで進める。
- ツールの許可を求められて止まった場合は、何の許可が必要かを1行で伝える。
- 進行中のrunがあるフォルダで再開するときは、`status` で未充足条件を確認し、続きから進める。

## エンジン

すべてのコマンドに、プロジェクトの絶対パスを `--project` で渡す（Bashのカレントディレクトリに依存しないため）。`state.json` と `config.json` は**手で編集せず、次のコマンドで更新する**（JSONのエスケープ事故を防ぐため）。

```bash
PL='python3 "${CLAUDE_PLUGIN_ROOT}/scripts/aqdlc.py"'
# 準備
$PL setup                                    # ~/.aq-dlc を初期化（何度でも可）
$PL init "<app名>"                            # 開いているフォルダを初期化し、絶対パスを出力
$PL create "<app名>" [--parent DIR]           # 明示されたときだけ: サブフォルダを作って初期化
$PL publish --project P [--visibility V] [--dry-run]
# run と Evidence Graph
$PL new "<run名>" --project P
$PL req R1 "<受け入れ条件>" [--critical] --project P
$PL risk R1 K1 "<失敗・リスク>" --project P
$PL verify K1 V1 "<確かめ方>" --command "<実行コマンド>" --project P   # または --checks test / --manual "<手順>"
$PL phase <フェーズ名> --project P
# Quality Gate
$PL add-check <名前> "<コマンド>" [--timeout 秒] --project P   # remove-check / checks
$PL gate --project P
# レビュー・待機・確認
$PL review --done --blocking <重大な指摘の件数> --summary "<要約>" --project P
$PL wait "<理由>" [--minutes N] --project P    # サブエージェント以外の処理を待つとき。resume で解除（サブエージェントは自動）
$PL ask "<質問>" --project P                   # 人間に確認するとき。返答後に resume
# 仕上げ
$PL memory --tags a,b [--stack x] --project P
$PL memory-done --project P
$PL status --project P
$PL complete --project P                       # 完了条件を判定（マージ後の検証が残れば pr_ready）
$PL ci --wait 8 --project P                    # push 後に CI を確認して記録
$PL verify K1 V9 "<確認>" --command "..." --after-merge --project P   # マージ後にしか確かめられない検証
$PL gate --after-merge --project P             # マージ後の検証も含めて実行
$PL confirm V9 "<人間の返答の要約>" --project P  # 人間が確認したと答えた後だけ
$PL doctor                                     # 環境の確認（最初に1回）
$PL abort --project P                          # 中断を指示されたとき
```

役割分担：あなた（AI）は仕様理解・リスク提案・設計・実装・テスト・学習候補の抽出を担当する。エンジンは状態管理・gate実行・証拠照合・完了判定を担当する。完了は**自己申告で決めない**。

## 00 プロジェクトの準備
基本は**Claude Codeで開いているフォルダ**をそのままプロジェクトにする。
1. `$PL setup` と `$PL doctor` を実行する。doctor で NG（git の利用者情報、gh のログインなど）があれば、開発を始める前に1回の質問でまとめて解決する。
2. `$PL init "<app名>"` で、開いているフォルダを初期化する（既存ファイルは上書きしない）。出力の最終行の絶対パスを以後 `P` とする。
   - ホームフォルダで開かれている、または既存ファイルが多く新規開発に見えない場合は、初期化せずに、アプリ用の空フォルダを開き直すよう伝える。
   - ユーザーが「新しいフォルダを作って」と明示した場合だけ、`$PL create "<app名>"` でサブフォルダを作り、その出力を `P` とする。
3. ファイル作成・編集は `P` の中で行う。Bashで別のフォルダにいる可能性がある場合は `cd "P" && ...` の形にする。
4. `$PL publish --project P` でGitHubリポジトリを作る。公開範囲は、ユーザーの指定 → 全体設定の `github.default_visibility`（初期値 private）。gh が未ログインなら、その旨を伝えてローカルのgitだけで進める。
5. `git -C "P" switch -c aq-dlc/<run名>` で作業ブランチを作る。
6. publish は**最初のコミット前**に実行する。既存のgitリポジトリでは publish は新しいコミットを作らず、main への push もしない（コミット済みの作業ブランチだけを push する）。

## 01 run開始・Memory・要求
1. `$PL new "<短い名前>" --project P`。
2. 目的・想定技術・リスク領域からタグを決め、`$PL memory --tags ... --project P` で**関連するものだけ**読む。全件は読まない。`active` のRuleは従い、`validated` は参考にする。読んだidは run.md の「読み込んだMemory」に書く。
3. 作る機能・受け入れ条件・重要な制約を `$PL req` で登録する。重要なものは `--critical`。新規開発なので既存実装の調査は不要。
4. 作り直しが大きくなる曖昧さだけユーザーに確認する（後述の手順で）。それ以外は妥当な判断で進め、run.md に「仮定」として残す。

## 02 Failure First
各requirementについて、失敗・リスクを先に洗い出し `$PL risk` で登録する。観点：不正入力、権限・認可、境界値、データ不整合、エラー時の振る舞い、秘密情報、並行性（該当時）。各riskに `$PL verify` で検証を最低1つ付ける（`references/evidence-graph.md`）。

## 03 設計・実装・テスト
1. 技術スタックを決めたら、`$PL add-check` でその言語の検証コマンドを登録する（`references/quality-gate.md`）。選んだ理由は run.md と Project Memory に残す。
2. 実装とテストを書く。verificationの `command` から実行できる形にする。
3. 区切りごとにコミットし、作業ブランチを push してよい。**push したら毎回 `$PL ci --wait 8 --project P` で CI を確認する**（Bash のタイムアウトは10分に設定する）。CI が失敗していたら、次の作業に進む前に直す。「完了」「済み」と報告・記録するのは、ローカルの gate と CI の両方が通ってから。
4. 検証コマンドは、できるだけ**そのリスクだけ**を確かめる粒度にする（テストファイル全体ではなく、テスト名で絞る。例：`npx vitest run -t "インポート失敗時"`、`npx playwright test -g "保存失敗"`、`pytest -k "isolation"`）。

## 04 Quality Gate ＋ 独立レビュー
1. `$PL gate --project P`。
2. 機械的な不整合（型・Lint・ビルド）を先に解消する。
3. **aq-dlc:reviewer サブエージェント**にレビューを依頼する。サブエージェントの実行中は、エンジンが自動で「待機中」として扱い、Stop hook は止めない。結果を待つ間はターンを終えてよい（sleep で待たない）。待機中は gate を実行しない（同じフォルダでテストが衝突するため）。渡すもの：プロジェクトパス、requirements と risks、変更ファイル一覧、gate結果。実装の経緯や自己評価は渡さない。
4. 結果が届いたら `$PL review --done --blocking <重大な指摘の件数> --summary "..."` で記録する。

## 05 修正ループ
型エラー → 修正、テスト不足 → verification追加、仕様逸脱 → 実装修正、リスク漏れ → risk追加。修正後は必ず gate をやり直す（gate後に内容が変わったファイルがあると完了にできない。ブランチ切り替えなどで更新日時だけ変わった場合は影響しない）。上限は `automation.max_fix_iterations`。超えたら人間に判断を仰ぐ。

## 06 Memory整理
**aq-dlc:memory Skill** の手順で、学びを選別・統合・保存する。終わったら `$PL memory-done`。

## 完了
1. 作業ブランチを push し、`$PL ci --wait 8` で CI が通ったことを確認する。
2. `$PL complete --project P` を実行する。満たしていない条件が表示されたら、直して戻る。
3. `gh pr create` でPRを作る。PR本文には requirement → 証拠（gate結果）の対応表、残った注意点、保存したMemoryを書く。マージは人間が行う。
4. ユーザーへの報告は短く：作ったもの、確かめた方法、PRのURL、残った注意点、確認待ちのMemory。

### マージ後にしか確かめられないもの
デプロイ後の公開URL、本番環境のスモークテストなどは、`verify ... --after-merge` で登録する。
- PR 作成前の `complete` は、マージ後の検証を除いて判定し、状態を `pr_ready` にする。この間は Stop hook は止めない（マージ待ち）。
- マージ後：既定ブランチに切り替えて取り込み → `$PL gate --after-merge` → 自動化できない確認は**まとめて1回だけ** `ask` で人間に確認 → 返答に応じて `confirm <id> "<人間の返答の要約>"` → `$PL ci` → `$PL complete`。
- 人間に「確認しました」と言われていないものを confirm してはいけない。

## 人間に確認するとき（例外だけ）
- 対象：作り直しが大きい要求の曖昧さ、verificationの免除、秘密情報・課金・GitHub以外の外部サービスへの書き込み、プロジェクト外のファイル変更、テストや検証を弱める変更、修正ループ上限到達、Memory Review。
- 手順：`$PL ask "<質問>"` を実行してから質問する。手動で確認してほしい項目は、1つずつではなく**まとめて1回**で聞く。Stop hookはこの状態なら停止を許可する。返答を受けたら `$PL resume`。質問は1回にまとめ、可能なら複数選択式（AskUserQuestion）。
- 中断を指示されたら `$PL abort`。

## してはいけないこと
- テストや検証コマンドを弱める・消すことで gate を通す。
- 失敗した結果を書き換える。結果はエンジンだけが記録する。
- 人間の返答なしに `confirm` を実行する。
- force push・履歴の書き換え、main への直接 push、PRのマージ、リポジトリ・ブランチ・リリースの削除、公開範囲の変更（hookでも止まる）。
- プラグインのファイル（Skill・agent・hook・エンジン）を書き換える。

## 参照
- `references/evidence-graph.md` — state.json の書き方と完了条件
- `references/quality-gate.md` — 言語別の check 例
- `references/memory-format.md` — Memoryファイルの形式
