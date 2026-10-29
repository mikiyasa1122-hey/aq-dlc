---
id: W-0001
type: workflow-success
status: candidate
scope: global
tags: [workflow, testing, acceptance-criteria, boundary]
stack: [any]
importance: medium
evidence: []
applied_in: []
created: 2026-10-29
updated: 2026-10-29
---
# 実装前に異常系・境界値・受け入れ条件を定義する

設計段階の例であり、まだ実際のrunで検証されていない。

## 適用条件
入力やデータ状態によって振る舞いが変わる機能全般。

## 学んだ行動
実装に入る前に、各要求について受け入れ条件、異常系（不正入力・権限なし・存在しないID）、境界値（0件・上限・空文字・最大長）を列挙し、それぞれに検証方法を割り当てる。

## 根拠・証拠
未検証。

## 例外・注意点
探索的なプロトタイプでは、列挙を最小限にしてよい。

## 元の実行記録
なし（設計段階の例）
