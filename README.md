# AQ-DLC

証拠で完了する、個人開発向けの Claude Code プラグイン。

> 「コードを生成したから完了」ではなく、「要求された振る舞いを証拠で確認できたから完了」。

新規アプリの開発をAIに任せながら、成功と失敗をMarkdownのMemoryに蓄積し、次の開発に活かします。人間の操作は、重要な例外の判断だけにします。

## できること

- Claude Code で開いたフォルダで `/aq-dlc:start` を実行すると、そのフォルダをプロジェクトとして初期化し、GitHubリポジトリを用意して開発を始める
- Failure First：実装前に失敗・リスクを洗い出し、検証方法を決める
- Evidence Graph：要求 → リスク → 検証 → 証拠を `state.json` で結ぶ
- Quality Gate：言語に応じたテスト・型チェック・Lint をエンジンが実行して記録する
- 独立レビュー：コードを変更できないレビュー専用サブエージェント
- 証拠に基づく完了：条件を満たすまで、Stop hook が Claude の停止をブロックする
- Success / Failure Memory：学びを選別・統合して `~/.aq-dlc/memory/` に保存し、次のプロジェクトで関連するものだけ読む
- GitHub操作は自由。ただし force push、main への直接 push、PRのマージ、削除系は hook で止める

## インストール

必要なもの：Claude Code、Python 3.8 以上、git。GitHub連携を使うなら `gh`（`gh auth login` 済み）。

Claude Code で次を実行します。

```
/plugin marketplace add mikiyasa1122-hey/aq-dlc
/plugin install aq-dlc@aq-dlc
```

インストール後、Claude Code を再起動するか `/reload-plugins` を実行します。

## 使い方

1. アプリ用の空フォルダを作り、Claude Code で開く（デスクトップアプリでフォルダを選ぶ、またはそのフォルダで `claude` を起動）
2. 作りたいものを頼む。コマンドは不要です（`/aq-dlc:start` で明示してもよい）

```
題材：<作りたいアプリを1〜3行で>
必須機能：
- <機能1>
- <機能2>
完成条件：
- <何ができたら完了か>
技術スタック：おまかせ
GitHub：private（または public）
```

Claude がそのフォルダを初期化し、その中で最後まで自動で開発します。題材以外の項目は省略してよく、決まっていないことは Claude が既定値を選んで run.md に記録します。完了するとPRが作られ、マージはあなたが行います。

親フォルダ（例：`~/dev`）で開いて「新しいフォルダを作って」と頼むと、サブフォルダを作ってそこで開発することもできます。

## 自動で動く仕組み

- **SessionStart hook**：空のフォルダか AQ-DLC プロジェクトを開くと、開発依頼を AQ-DLC で進めるよう Claude に伝えます。進行中の run があれば、その状態も伝えて再開させます。既存の別プロジェクトでは何もしません。
- **Stop hook**：完了条件を満たすまで、Claude に作業を続けさせます（上限 `max_auto_continues`）。
- **許可の自動化**：初期化時に、プロジェクトの `.claude/settings.json` を作ります。ファイル編集と一般的な開発コマンド（git、gh、npm、python、go、cargo など）は確認なしで実行でき、`sudo`、force push、PRのマージ、リポジトリ削除は拒否します。初期化直後のセッションで許可を求められた場合は、Claude Code を開き直すと反映されます。
- **仕様は必ず確認します**：実装の前に、要求・想定する失敗・決めたこと・作らないものを仕様シートにまとめ、答えによって作るものが変わる点を選択式で質問します。承認されるまで実装に入らず、承認後に仕様が変わったら再確認します。
- 仕様の承認後に人間が判断するのは、取り消せない操作・検証の免除・Memoryの重要な変更など、例外だけです。

## 更新履歴

### 0.4.0
- 実装前の「仕様の確認」を必須の工程にした。仕様シート（`spec`）を見せ、答えによって作るものが変わる点を最大4問の選択式で質問し、承認（`spec --approve`）を得てから実装する
- 利用者に見える振る舞い・データ・範囲は仮定で済ませず質問する。内部実装の判断は `assume` で記録して自動で進める
- 承認前は gate を実行せず、承認後に要求・仮定・対象外が変わったら再承認まで完了にしない

### 0.3.0（FlowBoard の試運用レポートを反映）
- サブエージェント（独立レビューなど）の実行中は、エンジンが自動で待機扱いにし、Stop hook が止めないようにした。待機中は gate も実行しない（テストの同時実行で結果が壊れるのを防ぐ）
- レビュー担当は、テスト結果を一時フォルダに出すようにした
- push のたびに `ci` で GitHub Actions の結果を確認する手順を追加。最新コミットの CI が通っていないと完了にできない
- マージ後にしか確かめられない検証（`verify --after-merge`）を追加。PR作成時点では `pr_ready` になり、マージ後に `gate --after-merge` → 人間の確認をまとめて1回 → `complete`
- フェーズを、要求・リスク・gate・レビューなどの操作に合わせて自動で進めるようにした
- Memory の読み込みを改善：手順・ルール・プロジェクト情報はタグに関係なく読み込み、未検証のものも「参考」として表示
- publish の前に git の利用者情報を確認し、未設定なら設定方法を表示
- `doctor` コマンド（環境の確認）を追加。Windows では Playwright のブラウザをプロジェクト内に入れる設定を初期化時に追加
- 検証コマンドをテスト名で絞る指針と、E2E を古いビルドに対して実行しない指針を追加

### 0.2.0（初回試運用のフィードバックを反映）
- レビュー待ちの間は Stop hook が止めないようにした（`review --start` / `wait`）
- Windows（日本語環境）の文字コード問題を修正：子プロセスの出力は UTF-8 → OSの文字コード → cp932 の順で読み、エンジンの出力は UTF-8 に統一
- gate 後の変更判定を、更新日時ではなく内容（ハッシュ）で行うようにした。ブランチ切り替えだけではやり直しにならない
- publish を安全にした：既存のリポジトリでは新しいコミットを作らず、main にも push しない。初回コミットでは生成物や `.env` を除外し、含めるファイルを表示する
- `add-check`、`req`、`risk`、`verify`、`review`、`ask`、`complete` などのコマンドを追加し、JSON の手書きを不要にした
- `.gitignore` の雛形に、言語ごとの生成物（`__pycache__/`、`node_modules/` など）を追加

## 保存される場所

| 場所 | 内容 |
|---|---|
| `~/.aq-dlc/config.json` | 全体設定（作業フォルダ、GitHubの公開範囲、自動化の境界、Memoryの閾値） |
| `~/.aq-dlc/memory/` | プロジェクトをまたぐ学び（failure / success / rules） |
| `<project>/.aq-dlc/config.json` | そのプロジェクトの checks と、全体設定の上書き |
| `<project>/.aq-dlc/memory/project/` | そのプロジェクト固有の学び |
| `<project>/.aq-dlc/runs/<run>/` | run ごとの `state.json`（Evidence Graph）と `run.md`（実行記録） |

プラグイン本体には個人の情報や学びを含みません。Memory は各自のPCにだけ保存されます。

## 設定（`~/.aq-dlc/config.json`）

| キー | 初期値 | 意味 |
|---|---|---|
| `workspace_dir` | `""` | 「新しいフォルダを作って」と頼んだときの親フォルダ。空なら開いているフォルダ |
| `github.default_visibility` | `private` | 新規リポジトリの公開範囲 |
| `automation.auto_start` | `true` | 空のフォルダやプロジェクトで、コマンドなしで AQ-DLC を始める |
| `automation.spec_review` | `true` | 実装前に仕様を人間に確認する（false で省略） |
| `automation.max_auto_continues` | `20` | Stop hook が自動で作業を続けさせる上限 |
| `automation.max_fix_iterations` | `8` | 修正ループの上限 |
| `automation.block_*` | `true` | force push、main への push、PRマージ、削除系を止める |
| `memory.auto_save_min_score` | `3` | 5つの視点のうち何点以上で自動保存するか |
| `memory.promote_to_active_min_runs` | `2` | 何回役立ったら active にするか |

初期値は試運用のための仮の値です。

## 構成

```
.claude-plugin/marketplace.json
plugins/aq-dlc/
  .claude-plugin/plugin.json
  skills/start/              /aq-dlc:start（手順とリファレンス）
  skills/memory/             /aq-dlc:memory（学びの選別・統合・保存）
  agents/reviewer.md         aq-dlc:reviewer（独立レビュー）
  hooks/hooks.json           SessionStart（自動開始）、Stop（完了判定）、PreToolUse（GitHub操作のガード）
  scripts/aqdlc.py       エンジン（標準ライブラリのみ）
  templates/                 新規プロジェクトと ~/.aq-dlc の雛形
```

hook は、AQ-DLC のプロジェクトで作業しているときだけ働きます。他のプロジェクトでの作業には影響しません。

## 注意

1回の試運用は、エンジンが一通り動くことの確認であり、「記憶によって品質が改善した」ことの証明ではありません。効果を測るには、比較対象と複数回の検証が必要です。

## License

MIT
