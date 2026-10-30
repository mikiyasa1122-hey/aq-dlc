# E-AQDL P0 実装レポート（Phase 0〜5）

対象: `feat/e-aqdl`（基準: main = v0.5.1 マージ済み）。指示書: E-AQDL 統合拡張実装指示書（2026-10-30）。
今回の範囲は P0（Phase 0〜5）。P1・P2（Failure Time Machine・Adaptive Quality Budget・Design Tournament、§7〜9）は別の PR で扱う。

## Phase 0: 現状調査の結果と計画

| 項目 | 調査結果 | 方針 |
|---|---|---|
| コード | `aqdlc_core`（約4,200行、標準ライブラリのみ）。状態は run ごとの `state.json`、証拠は指紋で鮮度を判定、hook で担当と操作を制御 | 同じ構造を保ち、新しい概念は新しいモジュール（approval / roles / mutation / counterfactual / evidence）に分け、既存のモジュールには統合点だけを足す |
| テスト | 93 件（Python 3.8 / 3.12・3 OS の CI で合格） | 既存のテストは削除しない。承認・終了時の選択など仕様の変更に合わせた更新は、より厳しくする方向だけにする |
| 互換性のリスク | ① 設定の指紋（config_fp）に新しい設定が入ると、進行中の run が config_changed になる ② 新しい承認・同意の条件を古い run に求めると止まる ③ Memory の保存先・形式の変更 ④ ID・コマンド・保存場所の改名 | ① E-AQDL の設定セクションは既定値のままなら指紋に含めない ② state に機能の版（`eaqdl`）を持たせ、v0.5 の run には新しい条件を求めない ③ 旧形式の Memory は読める。v0.5 の run の `memory-done` は `--end` 省略を許す ④ 改名は表示だけ |
| 安全性の既知の弱点 | pre-bash 以外の PreToolUse は例外時に許可していた。承認は「返答があった」ことしか見ていなかった。state.json の保存は検証なし | Phase 1 で対処 |

## AC の状態

| AC | 状態 | 根拠 |
|---|---|---|
| AC-01 名称変更と移行経路 | テスト済み | README（改名の理由・移行手順）、plugin.json の表示名。`test_eaqdl_safety.TestCompatibility` |
| AC-02 既存4エージェントと主要フローの回帰 | テスト済み | 既存テスト（承認の書き方と Memory の終了時の選択に合わせて更新）がすべて合格。実際の Claude Code での確認は下の「実環境での確認」 |
| AC-03 工程ごとの Role Profile | テスト済み | `test_eaqdl_roles.TestStageProfiles` |
| AC-04 技術分野・リスクに応じた追加 | テスト済み | `TestDomainSelection`（根拠・関連 ID・優先度・上限・競合・決定論） |
| AC-05 役割で権限が拡大しない | テスト済み | `TestPermissionsUnchanged`、権限の項目を持つ Profile の拒否 |
| AC-06 隔離環境での Mutation 実行 | テスト済み | `test_eaqdl_cf.TestCounterfactualGate`（元のコードの不変・一時コピーの削除・古いコピーの掃除） |
| AC-07 見逃しを survived と判定 | テスト済み | 同上（V1 が空のメモの拒否を確かめていないことを survived として検出） |
| AC-08 通常 Gate と Mutation の区別 | テスト済み | `TestModeOff`（off では passed_but_unchallenged）、evidence の層の分離 |
| AC-09 未検証の要求・リスクの追跡 | テスト済み | `evidence` / `trace`、受け入れ条件・リスクごとの集計 |
| AC-10 変更に応じた失効 | テスト済み | コード・検証条件（その検証だけ）・Mutation の定義・Role Profile による失効 |
| AC-11 Memory の有効性を実在の証拠と照合 | テスト済み | `test_eaqdl_memory.TestLifecycle`（存在しない run・検証、gate 不合格の参照を拒否） |
| AC-12 証拠不足の Memory を active にしない | テスト済み | 台帳（別の run での helped、重複の排除、harmful）による判定 |
| AC-13 開始時の利用範囲の選択 | テスト済み | `TestScopeConsent`（なし・プロジェクトのみ・共通・拒否・設定） |
| AC-14 終了時の共有候補の選別・承認 | テスト済み | `TestShare`（候補ごとの承認・却下、曖昧な返答、機密の確認、提示後の変更） |
| AC-15 未承認の横断利用・共有が起きない | テスト済み | 未選択・非対話での共通 Memory の拒否、共通 Memory への直接書き込みの拒否 |
| AC-16 Failure Time Machine | 未実装 | P1。別の PR |
| AC-17 Quality Budget | 未実装 | P1。別の PR |
| AC-18 Design Tournament | 未実装 | P2。別の PR |
| AC-19 Metrics | 実装済み（シナリオを除く）・テスト済み | `metrics --json` に Role・Mutation（検出率の定義と除外件数）・Memory・承認・トークン、`metrics --runs`。障害シナリオは P1 のため未実装 |
| AC-20 従来のコード・設定・Memory・Run との互換 | テスト済み | 設定の指紋の不変、v0.5 の run の継続、旧形式の Memory の読み込み、ID・コマンドの維持 |

## 設計判断

- **承認の対象化**: 承認が必要な操作に対象 ID（`SPEC-<仕様の指紋>` など）を割り当て、「承認 <ID>」「却下 <ID>」を1行（または選択肢の「…（承認 <ID>）」）で書いた返答だけを決定にした。AskUserQuestion の結果からは回答の部分だけを取り出す（選ばれなかった選択肢を承認に使わない）。自然言語の意図推定はしない（曖昧な返答で確定しないため）。
- **状態の検証**: 保存のたびに整合性を確かめ、新たな違反を持ち込む保存は拒否、既存の違反は `state_invalid` として人間に知らせる。保存はロックと rev で同時更新を拒否する。監査ログはハッシュの連鎖だが、作り直す改ざんへの耐性は主張しない。
- **Role Engine**: 新しいサブエージェントを増やさず、既存の4担当の引き継ぎ情報に Profile を加える。選択は Python で決定論的に行い、根拠が弱い・競合が解決できないときは基本 Profile だけにする。
- **反証テスト**: git worktree ではなく、追跡対象のファイルのコピーを使う（未コミットの変更も含めて gate と同じ内容を対象にするため）。実行するコマンドは設定済みの検証だけ。既定は off（既存のプロジェクトで重い検証を勝手に始めない）。
- **Memory**: 判定に使う利用結果はエンジンの台帳に記録し、Markdown の数字は表示用にした。共通 Memory への書き込みはエンジンの共有コマンドだけに限定した。

## 安全性の確認（テストで確かめたこと）

hook の入力不正・状態の破損時の拒否、承認の偽装（対象のない返答・古い版・選ばれなかった選択肢・state.json への直接の書き込み）、パストラバーサル・シンボリックリンク・不正な Operator（コマンドインジェクションの代わりに使われる引数）の拒否、資格情報らしい環境変数を子プロセスに渡さないこと、ログのトークンの伏せ字、一時コピーの削除と古いコピーの掃除、元の作業ツリーの不変。

## 既知の制約（未解決）

README の「残る制約」を参照。主なもの: 反証テストは OS のサンドボックスではない、機密の検出は簡易検査、Bash による `~/.aq-dlc/memory` の書き換えは hook では止められない、監査ログ・台帳はファイルであり改ざんへの耐性は保証しない、Memory の効果の比較は材料を並べるだけで統計的な判定はしない。
