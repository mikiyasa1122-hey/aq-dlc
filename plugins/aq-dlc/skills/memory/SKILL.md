---
name: memory
description: AQ-DLCのrun終了時（フェーズ06）に、成功・失敗の学びを候補化し、5つの視点で選別してMemory（~/.aq-dlc/memory と プロジェクトの .aq-dlc/memory/project）に統合保存する。昇格とMemory Reviewもこの手順で行う。/aq-dlc:memory で単独実行してもよい。
---

# AQ-DLC Memory — 学びの選別・統合・保存

記憶はモデルの再学習ではなく、Markdownの外部知識。形式は `${CLAUDE_PLUGIN_ROOT}/skills/start/references/memory-format.md`。
閾値は設定の `memory`（`~/.aq-dlc/config.json`、プロジェクト側で上書き可）。

## 言語
Memoryの本文・見出し・タグ以外の記述、run.md への記録、ユーザーへの質問はすべて日本語で書く（tags と stack は検索用に英小文字でよい）。

## 保存先
- **global**（プロジェクトをまたぐ学び）：`~/.aq-dlc/memory/{failure,success,rules}/`
- **project**（このプロジェクトだけの目的・技術・規約）：`<project>/.aq-dlc/memory/project/`
- 迷ったら project に置く。global への昇格は REVIEW。

## 1. 候補を抽出する
現在のrunの `state.json`、`run.md`、gate結果、レビュー指摘から探す：
- **Failure**：gateやレビューで見つかった不具合・見落とし。条件と根本原因。
- **Code Success**：再利用できる設計・実装パターン（証拠でpassしたもの）。
- **Workflow Success**：手順として効いたこと。
- **Project**：このプロジェクトで確立した技術選択・規約。
- 読み込んだMemoryが役立ったか、外れたか。

## 2. 5つの視点で選別する（各0/1）
1. **再利用性** 次のタスクでも使える一般性がある
2. **重要性** 同じ問題が起きたときの影響が大きい
3. **証拠** gate結果・テスト・ログ・レビューで裏付けられている
4. **新規性** 既存Memoryと重複しない（重複なら統合）
5. **適用条件** どの技術・状況で有効か説明できる

- **AUTO**：合計が `auto_save_min_score` 以上、かつ「証拠」が1 → 保存または統合。
- **SKIP**：それ以外 → run.md の「保留した候補」に1行だけ残す。
- **REVIEW**：下の5に当たるもの。

## 3. 統合して保存する
1. 保存の前に、既存Memoryを tags と本文キーワードで検索する（Grep で両方の保存先を見る）。
2. 近いものがあれば追記する：根拠・証拠、適用条件、例外を足し、`updated` と `evidence` を更新する。evidence は `<プロジェクト名>/runs/<run_id>#<verification id>` の形で書く。
3. 新規の場合だけ次の連番idで作る。新規は常に `status: candidate`。
4. このrunで適用したMemoryは `applied_in` にrun idを追記する。外れた場合は「例外・注意点」に書く。
5. global Memory に、秘密情報・個人情報・社内固有の名前を書かない。

## 4. 状態の昇格（保存と適用を分ける）
- candidate → validated：このrunのgateでpassした証拠が紐付いた（自動）。
- validated → active：`applied_in` が `promote_to_active_min_runs` 以上、かつ役立った記録がある（自動）。ただし `type: rule` は REVIEW。
- active → deprecated：矛盾する証拠が出た（REVIEW）。
- active が `max_active_per_type` を超えそうなら、統合か deprecated の候補を REVIEW に出す。

## 5. 人間確認（REVIEW）
対象：Verified Rule の追加・変更・active化、active Memory との矛盾、deprecated化、project → global への昇格。
1回の質問にまとめ、**複数選択式**で聞く。各項目は1〜2行で、何を、なぜ、根拠を書く。0件なら聞かない。質問前に `ask` コマンドを実行し、返答後に `resume` を実行する。

## 6. 実行記録を閉じる
run.md の「Memory」節に、保存・統合したid、SKIP候補、REVIEWの結果、Skill改善提案を書く。最後に `memory-done` コマンドを実行する。
