# Quality Gate — 言語別の check 例

設計担当が技術スタックを決めたら `add-check` で登録する。種類（`--kind`）に応じて、型チェック → Lint → ビルド → 単体 → 結合 → E2E の順に実行される。使えないものは登録しない。存在しないツールを追加するより、そのスタックで標準的なものを選ぶ。

```bash
$AQ add-check typecheck "npx tsc --noEmit"      --kind typecheck
$AQ add-check lint      "npx eslint ."          --kind lint
$AQ add-check build     "npm run build"         --kind build
$AQ add-check unit      "npx vitest run"        --kind unit
$AQ add-check e2e       "npx playwright test"   --kind e2e     # build の後に実行。build が不合格なら実行しない
```

| 種類 | 意味 |
|---|---|
| typecheck / lint | 機械的な不整合 |
| build | 成果物を作る。E2E と、レベル e2e の検証の前提になる |
| unit / integration | 単体・結合テスト |
| e2e | ブラウザなどを通したテスト。必要な場合だけ |
| other | その他（依存関係の脆弱性チェックなど） |

`--needs a,b` で、前提のチェックを明示できる（前提が不合格なら実行しない）。

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
- E2E は本番と同じビルド成果物に対して実行する。`--kind e2e` のチェックと、レベル e2e の検証は、ビルドが不合格なら実行されない（古い成果物での合格を防ぐ）。
- Webアプリの E2E は Playwright を候補にする（すべてのプロジェクトに強制しない）。Windows ではブラウザをプロジェクト内に入れる（`PLAYWRIGHT_BROWSERS_PATH=0`）。
- テストファイルが0件のとき失敗するテストランナー（Vitest など）は、最初のテストを書いてから check を登録する。

## 順序の原則
型チェックが利用できる言語では、機械的な不整合（型・Lint・ビルド）を解消してから独立レビューに渡す。

## セキュリティ観点（採用機能に関係するものだけ）
認証、認可（他利用者データ）、入力検証、秘密情報のハードコード、依存パッケージの既知脆弱性（`npm audit` / `pip-audit` / `cargo audit` 等が使えるなら check に追加）。
