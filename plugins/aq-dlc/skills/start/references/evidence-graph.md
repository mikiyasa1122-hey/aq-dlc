# Evidence Graph（state.json の書き方）

要求 → リスク → 検証 → 証拠 を `state.json` で結ぶ。証拠の `result` はエンジン（`gate`）だけが書く。

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
- 手動確認しかできないもの：`"manual": "確認手順"` を書き、人間に確認を依頼する。人間がOKしたら `"waived_by": "human", "waive_reason": "..."` を追記する。AIが自分でこれを書いてはいけない。
- critical な requirement は、少なくとも1つのriskと1つのverificationが必要。
- gate実行後に対象ファイルを変更すると、gate結果は stale（古い）扱いになり、完了判定は通らない。

## 完了条件（`check` が見るもの）

1. `config.json` に1つ以上の check が登録されている
2. 最新のgateで全checkがpass、かつgate後にファイル変更がない
3. 全verificationがpass（またはhuman waive）
4. critical requirementにrisk/verificationがある
5. `review.done == true` かつ `review.blocking_findings == 0`
6. `memory_done == true`
