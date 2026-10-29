# Evidence Graph（state.json の書き方）

要求 → リスク → 検証 → 証拠 を `state.json` で結ぶ。登録はエンジンの `req` / `risk` / `verify` コマンドで行い、JSONは手で編集しない。下は結果の形の説明。証拠の `result` はエンジン（`gate`）だけが書く。

```json
{
  "run_id": "20261029-1810-todo-app",
  "title": "todo-app",
  "status": "in_progress",
  "phase": "02_failure_first",
  "awaiting_human": false,
  "question": "",
  "memory_loaded": ["F-0001", "W-0001"],
  "requirements": [
    {
      "id": "R1",
      "text": "利用者は自分のデータのみ閲覧できる",
      "critical": true,
      "risks": [
        {
          "id": "K1",
          "text": "他利用者のデータにアクセスできてしまう",
          "verifications": [
            {
              "id": "V1",
              "method": "権限の異なる利用者2人で統合テスト",
              "command": "npm test -- tests/auth/isolation.test.ts",
              "checks": ["test"]
            }
          ]
        }
      ]
    }
  ],
  "review": { "done": false, "blocking_findings": 0, "summary": "" },
  "memory_done": false
}
```

## ルール

- **command**：そのverificationだけを確かめるコマンド。gateが実行し、終了コード0ならpass。できるだけ書く。
- **checks**：configのcheck名（例 `"test"`, `"typecheck"`）。gateでそのcheckがpassならpassとして扱う。commandが書けない場合の代替。
- verificationには `command` か `checks` のどちらかが必要。
- マージ後にしか確かめられないもの（デプロイ後の公開URLなど）：`--after-merge` を付ける。PR作成前の判定からは外れ、マージ後に `gate --after-merge` で実行する。
- 手動確認しかできないもの：`verify ... --manual "確認手順"` で登録し、他の手動確認とまとめて1回で人間に依頼する。人間が確認したと答えたら `confirm <id> "<返答の要約>"` で記録する。人間の返答なしに confirm してはいけない。
- critical な requirement は、少なくとも1つのriskと1つのverificationが必要。
- gate実行後にファイルの**内容**が変わると、gate結果は古い扱いになり、完了判定は通らない（更新日時だけの変化は無視する）。

## 完了条件（`check` が見るもの）

1. `config.json` に1つ以上の check が登録されている
2. 最新のgateで全checkがpass、かつgate後にファイル変更がない
3. 全verificationがpass（または人間の confirm）。マージ後の検証は、PR前の判定では除外
7. GitHub Actions がある場合、最新コミットの CI が成功（`ci` で記録）
4. critical requirementにrisk/verificationがある
5. `review.done == true` かつ `review.blocking_findings == 0`
6. `memory_done == true`
