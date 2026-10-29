# Quality Gate — 言語別の check 例

技術スタックが決まったら `add-check` コマンドで登録する（例：`add-check test "pytest -q"`）。登録結果は `.aq-dlc/config.json` に保存される。使えないものは登録しない。存在しないツールを追加するより、そのスタックで標準的なものを選ぶ。

```json
"checks": [
  { "name": "typecheck", "command": "npx tsc --noEmit", "timeout_sec": 300 },
  { "name": "lint",      "command": "npx eslint .",      "timeout_sec": 300 },
  { "name": "test",      "command": "npm test",          "timeout_sec": 600 }
]
```

| スタック | typecheck | lint | test | build |
|---|---|---|---|---|
| TypeScript/Node | `npx tsc --noEmit` | `npx eslint .` | `npm test` | `npm run build` |
| Python | `mypy .` / `pyright` | `ruff check .` | `pytest -q` | — |
| Go | （build に含む） | `go vet ./...` | `go test ./...` | `go build ./...` |
| Rust | （build に含む） | `cargo clippy -- -D warnings` | `cargo test` | `cargo build` |
| Swift | — | `swiftlint` | `swift test` | `swift build` |
| Kotlin/Gradle | — | `./gradlew lint` | `./gradlew test` | `./gradlew build` |

## 検証の粒度
verification の `command` は、そのリスクだけを確かめるようにテスト名で絞る（`vitest -t`、`playwright -g`、`pytest -k`、`go test -run`、`cargo test <名前>`）。ファイル全体を指すと、どの証拠がどの要求を支えているのかが曖昧になり、別のファイルを指していても気づけない。

## E2E テストの注意
- E2E は本番と同じビルド成果物に対して実行する。ビルドが失敗したら E2E を実行しない（古い成果物で合格してしまうため）。check は「build → e2e」の順に登録し、e2e のコマンドを `npm run build && npx playwright test` のようにつなげる。
- テストファイルが0件のとき失敗するテストランナー（Vitest など）は、最初のテストを書いてから check を登録する。

## 順序の原則
型チェックが利用できる言語では、機械的な不整合（型・Lint・ビルド）を解消してから独立レビューに渡す。

## セキュリティ観点（採用機能に関係するものだけ）
認証、認可（他利用者データ）、入力検証、秘密情報のハードコード、依存パッケージの既知脆弱性（`npm audit` / `pip-audit` / `cargo audit` 等が使えるなら check に追加）。
