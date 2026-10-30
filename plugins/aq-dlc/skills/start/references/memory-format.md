# Memory ファイル形式

場所：
- project（保存先）：`<project>/.aq-dlc/memory/<type>/<id>-<slug>.md`（failure / success / rules / project）
- 共通：`~/.aq-dlc/memory/<type>/<id>-<slug>.md`（memory-share で人間が承認したものだけが入る）

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
evidence: [runs/20261029-181000-todo-app#V1]
applied_in: []
created: 2026-10-29
updated: 2026-10-29
---
```

- **status**：`candidate` → `validated` → `active` → `deprecated`（validated 以上はエンジンが根拠を照合する）
- **scope**：`global`（全プロジェクト）または `project:<名前>`
- **stack**：有効な技術。言語に依存しないなら `[any]`
- **evidence**：裏付けとなるrunとverification
- **applied_in**：このMemoryを使ったrun（表示用。昇格の判定はエンジンの台帳 `ledger.jsonl` で行う）

E-AQDL で追加した項目（旧形式のファイルは、これらがなくても読める）:

| 項目 | 内容 |
|---|---|
| `conditions` / 本文の「適用条件」 | どの技術・状況で有効か |
| `exceptions` / 本文の「例外・注意点」 | 当てはまらない条件・有害だった事例 |
| `helped` `not_applicable` `harmful` `unknown` | 利用結果の回数（memory-done がエンジンの台帳から書き込む。手で書いても判定には使われない） |
| `deprecated_reason` | deprecated にした理由（必須） |
| `conflicts_with` | 矛盾する可能性のある Memory の ID |
| `shared_from` `share_approval` | 共通 Memory へ共有したときにエンジンが付ける（元のプロジェクトの識別子・承認 ID） |
| `updated` | 最終更新日 |

Verified Rule の承認・利用結果は `ledger.jsonl`（プロジェクト・共通それぞれ）に、共通 Memory への共有は `~/.aq-dlc/memory/audit.jsonl` に記録される。

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
