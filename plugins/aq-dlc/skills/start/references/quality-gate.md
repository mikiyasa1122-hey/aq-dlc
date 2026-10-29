# Quality Gate — 言語別の check 例

技術スタックが決まったら `.aq-dlc/config.json` の `checks` に登録する。使えないものは登録しない。存在しないツールを追加するより、そのスタックで標準的なものを選ぶ。

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

## 順序の原則
型チェックが利用できる言語では、機械的な不整合（型・Lint・ビルド）を解消してから独立レビューに渡す。

## セキュリティ観点（採用機能に関係するものだけ）
認証、認可（他利用者データ）、入力検証、秘密情報のハードコード、依存パッケージの既知脆弱性（`npm audit` / `pip-audit` / `cargo audit` 等が使えるなら check に追加）。
