# Memory ファイル形式

場所：
- global：`~/.aq-dlc/memory/<type>/<id>-<slug>.md`（failure / success / rules）
- project：`<project>/.aq-dlc/memory/project/<id>-<slug>.md`

| type | フォルダ | id接頭辞 | 内容 |
|---|---|---|---|
| failure | failure/ | F- | 失敗の条件・根本原因・影響・再発防止・再現/検証方法 |
| success (code) | success/ | S- | うまくいった設計・実装 |
| success (workflow) | success/ | W- | うまくいった手順 |
| project | project/ | P- | プロジェクトの目的、技術、設計制約、確立した規約 |
| rule | rules/ | R- | 検証済みで、将来の判断や品質チェックに使うルール |

## frontmatter（1行1項目。リストは [a, b] 形式）

```markdown
---
id: F-0001
type: failure
status: candidate
scope: global
tags: [auth, authorization, repository]
stack: [any]
importance: high
evidence: [todo-app/runs/20261029-1810-todo-app#V1]
applied_in: []
created: 2026-10-29
updated: 2026-10-29
---
```

- **status**：`candidate` → `validated` → `active` → `deprecated`
- **scope**：`global`（全プロジェクト）または `project:<名前>`
- **stack**：有効な技術。言語に依存しないなら `[any]`
- **evidence**：裏付けとなるrunとverification
- **applied_in**：このMemoryを使ったrun。昇格判定に使う

## 本文の見出し

```markdown
## 適用条件
## 学んだ行動
## 根拠・証拠
## 例外・注意点
## 元の実行記録
```

failure の場合は「学んだ行動」の代わりに、次の見出しを使ってよい：「失敗の条件 / 根本原因 / 影響 / 再発防止 / 再現・検証方法」。

## 統合の原則
似た内容のファイルを増やさない。既存Memoryに証拠・適用条件・例外を追記して `updated` を更新する。
