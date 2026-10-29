---
id: W-0002
type: workflow-success
status: candidate
scope: global
tags: [workflow, typecheck, lint, review]
stack: [any]
importance: medium
evidence: []
applied_in: []
created: 2026-10-29
updated: 2026-10-29
---
# 機械的な不整合を解消してから独立レビューに渡す

設計段階の例であり、まだ実際のrunで検証されていない。

## 適用条件
型チェック・Lint・ビルドが利用できる言語。

## 学んだ行動
型エラー・Lint・ビルドエラーを先にすべて解消してから、独立レビューを行う。レビューの注意を、仕様逸脱や検証不足に集中させるため。

## 根拠・証拠
未検証。

## 例外・注意点
動的言語で型チェックがない場合は、テストとLintで代替する。

## 元の実行記録
なし（設計段階の例）
