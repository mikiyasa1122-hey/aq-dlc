# E-AQDL（旧 AQ-DLC）

**Evidence-driven Adaptive Quality Development Lifecycle — 証拠駆動の適応型品質開発ライフサイクル**

証拠で完了する、個人開発向けのマルチエージェント開発プラグイン（Claude Code）。

> 「コードが動いたから完了」ではなく、「要求を満たしたこと、その判断に使った証拠の有効性、証拠の限界まで説明できるから完了」。

仕様整理・設計・実装・レビューを独立した担当（サブエージェント）に分け、それぞれの成果物をエンジンが証拠に基づいて検証します。工程と技術分野に応じて担当の判断基準（Role Profile）を切り替え、テスト自身が欠陥を検出できるかを反証テストで確かめ、まだ証明できていない範囲を見える化します。学びは Markdown の Memory に蓄積し、効果を記録しながら再利用します。人間の操作は、仕様の確認と重要な例外の判断だけです。

## AQ-DLC から E-AQDL への改名

| 語 | 意味 |
|---|---|
| Evidence-driven | 証拠（機械的に再実行できる結果）で判定する |
| Adaptive | 工程・技術分野・リスク・反証テストの結果に応じて、役割と検証を変える |
| Quality | 正確性・安全性・保守性・テストの有効性 |
| Development Lifecycle | 要求から改善（Memory）までの一連の工程 |

改名の理由: v0.5 までの AQ-DLC は「証拠で完了判定する」仕組みでしたが、0.6.0 で「役割の適応」「テストの欠陥検出能力の反証」「未証明範囲の管理」「効果を検証する Memory」が加わり、品質の判定から品質を改善し続けるライフサイクルへ範囲が広がったためです。

**互換性**: 改名は表示（README・プラグインの表示名・Skill と Agent の説明・CLI の表示）だけです。次はそのまま使えます。
- プラグイン ID `aq-dlc`、インストール名 `aq-dlc@aq-dlc`、エージェント ID `aq-dlc:requirements|design|implementer|reviewer`、Skill `/aq-dlc:start` `/aq-dlc:memory`
- エンジン `scripts/aqdlc.py` とすべての従来のコマンド、`~/.aq-dlc/`、`<project>/.aq-dlc/`、既存の run・設定・Memory
- GitHub リポジトリは人間が `mikiyasa1122-hey/e-aqdl` に改名しました（旧 URL `mikiyasa1122-hey/aq-dlc` は GitHub が転送します）。マーケットプレイス名とプラグイン ID は `aq-dlc` のままです

## 体制

```
                 ┌──────────────── 統括（Main Orchestrator：メインの Claude Code）────────────────┐
                 │ 進行管理・人間への仕様確認と承認・Quality Gate の実行・修正ループの管理・PR       │
                 └───┬──────────────┬──────────────┬──────────────┬──────────────────────────────┘
                     ▼              ▼              ▼              ▼
              ① 要求分析        ② 設計         ③ 実装         ④ 独立レビュー
              requirements      design         implementer    reviewer
              spec.md           design.md      コード・テスト  レビュー結果
                                               impl.md        （コード変更不可）
                     └──────────────┴──── E-AQDL エンジン（hook）────┴──────────────┘
                       起動条件の確認・Role Profile の選択・引き継ぎ情報の注入・成果物の検証・
                       証拠の鮮度判定・反証テスト・承認の照合・状態の検証・完了判定
```

| 担当 | 責務 | 成果物 |
|---|---|---|
| 統括 | 進行管理、サブエージェントの呼び出し、人間への仕様確認と承認、Quality Gate、修正ループ、完了確認、PR。**実装は直接しない** | 承認の記録、gate の結果、PR |
| ① 要求分析 | 依頼の分析、機能・非機能要件、受け入れ条件、Failure First のリスク、不明点・矛盾点、対象外、要求と検証の対応付け。曖昧な点を勝手に確定しない | 仕様書、要求・受け入れ条件・リスク・検証・質問 |
| ② 設計 | 技術スタックの選定、アーキテクチャ、ディレクトリ構成、データモデル、セキュリティ・エラー処理、単体・結合・E2E の方針、Quality Gate のチェック。仕様の変更は差し戻す | 設計書、要求と設計の対応、チェック |
| ③ 実装 | 仕様と設計に基づく実装、単体・結合・E2E テスト、検証の実行方法、指摘の修正。仕様・設計を独断で変えない。検証を弱めない | コード・テスト、実装報告、要求と実装の対応 |
| ④ 独立レビュー | 仕様・設計との整合、バグ・セキュリティ・認可・データ整合性・異常系、テスト品質、検証の空洞化・改ざん。実装を変更しない | 重大度付きの指摘と判定 |

## 流れ

```
依頼 → Memory の利用範囲（人間が選ぶ）→ ① 要求分析 → 統括が人間に仕様を確認・承認（SPEC-…）→ ② 設計 → ③ 実装
     → Quality Gate → 反証テスト（有効時）→ ④ 独立レビュー
          ↑      不合格 │   見逃し（enforce）│      要修正 │
          └─────────────┴───────────────────┴─────────────┘
→ Memory 整理（共有は候補ごとに人間が承認）→ CI → PR（マージは人間）→（マージ後の確認）
```

サブエージェントは順番に1つずつ動きます。エンジンが次のことを保証します。

- **起動の順序**：仕様が承認されるまで設計・実装に進めない。gate を通過していない実装はレビューできない。同時に2つは起動できない（前景で実行させる）
- **引き継ぎ**：各担当の起動時に、仕様書・設計書の場所と指紋、修正依頼（レビュー指摘・gate の不合格）、未設定の検証などをエンジンが渡す。実装報告には、読んだ設計書・仕様の指紋を書かせて照合する
- **成果物の検証**：各担当の終了時に成果物を検証し、不足があれば終了させない。レビュー結果は、レビュー担当の最後の報告からエンジンが直接読み取って記録する（統括や実装担当の申告では記録できない）
- **権限**：コードとテストを書けるのは実装担当だけ。レビュー担当はファイルを変更できない（変更されたらレビューを無効にする）。要求・設計・実装の登録コマンドは担当ごとに制限
- **人間の承認**：仕様の承認・設定変更の受け入れ・人の確認・共通 Memory の利用と共有などは、提示の後に、**対象 ID（SPEC-… / CFG-… / 検証ID / MEMSCOPE-… / SHARE-… など）と決定（承認・却下）を1行で書いた人間の返答**がないと受け付けない。「OK」などの対象のない返答・承認と却下が混ざった返答・AskUserQuestion で選ばれなかった選択肢は承認にしない。決定は対象・時刻・run・元の返答と一緒に記録する

## E-AQDL の機能

### Adaptive Role Engine（工程・技術分野に応じた役割）
サブエージェントの起動時（工程の開始・遷移・差し戻し）に、エンジンが Role Profile を決定論的に選び、引き継ぎ情報に加えます。Role Profile は `plugins/aq-dlc/roles/` の Markdown で、責務・判断基準・禁止事項・成果物・検証条件・引き継ぎ条件を書きます（ID・版・ハッシュ・適用理由を run の履歴に記録）。

| 工程 | 基本 Profile |
|---|---|
| 要求分析 | Senior Requirements Analyst |
| 設計 | Principal Software Architect |
| 実装 | Senior Software Engineer ＋ Senior QA Engineer（テスト設計） |
| 独立レビュー | Principal Code Reviewer（反証テストの有効時は Adversarial Test Engineer も） |
| 反証テストの差し戻し | 実装担当に Adversarial Test Engineer を追加 |
| Memory 整理 | Software Reliability Engineer（統括への指示に含める） |

技術分野（Security / Backend API / Database / Frontend / Concurrency / QA / Performance / Infrastructure）は、要求・受け入れ条件・リスクの文と対象ファイルからキーワードとパスで点数を付け、根拠が十分なもの（`min_score` 以上）だけを優先度順に最大3件（`roles.max_domain_profiles`）適用します。競合する Profile は優先度の高い方を残し、優先度と点数が同じなら両方とも外して基本 Profile だけにします。`$AQ roles` で選ばれた理由を確認できます。

役割は LLM の作業方針であり、**ツールの権限・人間の承認・安全上の制約は変えません**（権限の項目を持つ Profile は拒否します）。同じモデルの別の Profile を「独立したモデル」とは扱いません。レビュー担当の Profile が変わると、そのレビューは使えなくなります。

### Counterfactual Gate（反証テスト）
通常の Quality Gate の合格後に、要求・リスクに結び付いた実装ファイルへ欠陥を1つずつ埋め込み、その検証のテストが失敗するかを確かめます（`$AQ cf`）。対応言語は Python と JavaScript / TypeScript。

- Mutation Operator: 条件分岐の反転、境界値の変更、等価比較・論理演算の反転、ガード条件（入力検証・認可・重複防止）の除去、raise / throw の削除、戻り値の反転、UPDATE / DELETE の WHERE の変更。1行の決定論的な書き換えで、適用できる箇所かを形式的に確かめる（LLM の生成したパッチは実行しない）
- 結果: `killed`（検出）/ `survived`（見逃し）/ `invalid`（構文エラー・テストの起動失敗・ベースライン不合格など。検出とは扱わない）/ `timeout` / `not_applicable`
- 安全: プロジェクトの外の一時コピーで実行し、元の作業ツリーは変えない（実行前後で確認）。中断・異常終了でも後片付けし、残りは次回に掃除する。実行するのは設定済みの検証コマンドだけ。資格情報らしい環境変数は渡さず、HTTP(S) のプロキシを使えないアドレスに向けて外部通信を原則失敗させる（OS のサンドボックスではないため完全な遮断は保証しません）。時間と Mutation の数に上限がある
- モード（`counterfactual.mode`）: `off`（既定。既存のプロジェクトで重い検証を勝手に始めない）/ `advisory`（実行して報告し、レビューで評価させる）/ `enforce`（重要な要求の検証の見逃し・反証の不成立があるとレビュー・完了に進めない）
- 検出率 = killed ÷（killed＋survived）。invalid・timeout・対象外・人間が等価と判断したもの（`cf-mark`、「承認 EQ-…」が必要）は母集団から除外し、件数を別に示します

### Negative Evidence Graph（未証明範囲の管理）
`$AQ evidence`（と `trace`）で、検証ごとに通常 Gate・反証テスト・独立レビューの層を分けて示し、総合の状態を `verified` / `passed_but_unchallenged` / `insufficient` / `invalidated` / `not_applicable` で表示します。失効したものは原因（コード・チェック・設定・その検証の条件・Mutation の定義など）を示します。

### Evolutionary Memory と同意制
- run の開始時に利用範囲を人間が選ぶ: 使わない / プロジェクト内だけ / 共通 Memory も使う（「承認 MEMSCOPE-…」か設定が必要）。未選択・非対話では共通 Memory を読まない
- 終了時の保存先を選ぶ: プロジェクト内だけ / 共有候補を1件ずつ承認 / 保存しない。共有候補には内容・適用条件・根拠・再利用価値・機密性のリスク（簡易検査）を示し、承認された候補だけをエンジンが共通 Memory に書き込む。エージェントは共通 Memory に直接書けない
- ライフサイクル `candidate → validated → active → deprecated`: validated は根拠の run・検証が実在し gate で合格していることを照合、active は根拠以外の異なる2つ以上の run で役立った記録（エンジンの台帳）と有害の記録がないこと、Verified Rule は人間の承認が必要、deprecated は理由を残す
- 利用結果は `helped / not_applicable / harmful / unknown` で台帳に記録。昇格の判定に数えるのは、このプロジェクトの run の state（読み込みと利用結果の記録、helped なら記録時点の gate の合格）で裏付けられる記録だけです。台帳・監査ログは統括も Write / Edit で書けません。根拠を照合できない validated / active の Memory は、読み込み時に未検証として扱います。Verified Rule の承認（`MEM-<ID>-<内容の指紋>`）は内容の版に結び付き、内容を変えたら承認し直します。エンジンの規則や承認を変えようとする文を含む Memory は読み込まず、矛盾する Memory は競合として表示します
- `metrics --runs` で run ごとの条件（Memory の利用範囲・反証テストのモード・エンジン）と結果を並べられます。比較の材料であり、少ない試行の差を改善の証明とはしません

## 証拠の鮮度

| 変わったもの | 使えなくなる証拠 |
|---|---|
| コード（内容。更新日時だけの変化は無視） | gate の結果、レビュー |
| 検証の条件（コマンド・参照するチェック・レベルなど） | その検証の gate 結果、レビュー |
| チェックの定義・gate の環境変数 | gate の結果、レビュー |
| 仕様（要求・受け入れ条件・仮定・対象外・仕様書） | 承認、設計、実装、レビュー |
| 設計書 | 設計の登録、実装、レビュー |
| Mutation の定義（対象・Operator・元の行）・反証テストの設定 | 反証テストの結果、レビュー |
| レビュー担当の Role Profile | レビュー |

検証などの ID は run 全体で一意です。重複は登録時に拒否し、手で重複させた場合も gate と完了判定が拒否します（結果の上書きによる誤った合格を防ぐ）。

## 権限制御で保証できる範囲と、残る制約

hook だけで任意のシェル操作を完全に封じることはできません。E-AQDL は「事前に止める」「事後に検出する」「証拠を無効にする」を重ねて、**エンジンが検出できる範囲の担当外の変更を含んだまま、完了・レビューに進めない**ようにしています。検出できない経路は下の「残る制約」に挙げます。

| 層 | 仕組み | できること |
|---|---|---|
| ツールの制限 | エージェント定義の tools / disallowedTools | レビュー担当は Write / Edit を持たない（Bash は検証に必要なため残す） |
| 事前に止める | PreToolUse（Write / Edit / Bash / Agent） | 担当外の Write・Edit（シンボリックリンクは実体で判定）、Write・Edit による `~/.aq-dlc` の設定・記録や `.claude` のエージェント定義・設定の書き換え、担当外のエンジンのコマンド（変数・関数経由も含む）、危険な Git / GitHub 操作、レビュー担当の代表的な変更コマンド。担当は `aq-dlc:` の付いたプラグインのエージェントだけを認める |
| 事後に検出する | PostToolUse / PostToolUseFailure（Bash）で、実行前後のファイル内容と設定（config.json、`.claude` の設定・エージェント定義・hook・Skill）を比較 | 実装担当以外の Bash による変更を、書き方（リダイレクト・Python・スクリプト・変数展開・絶対パス）に関係なく検出して記録し、実行したエージェントに伝える。実行前の記録はプロジェクトの外に置き、記録が消えていたら改ざんとして記録する。検出処理のエラーも確認が必要な変更として記録する |
| 証拠を無効にする | 内容の指紋（判定と登録の時点ではキャッシュを使わず全ファイルをハッシュし直す） | 今のコードが実装担当の最後の登録と違う、未対応の担当外の変更の記録がある、設定がエンジン以外で変わった、のどれかなら、取り込み（設定なら人間の確認）→ gate → 独立レビューまで完了できない。hook を通らない変更（別プロセス・人の手作業）も、コードなら検出する |
| 成果物の保護 | 仕様書・設計書の指紋 | 担当外が書き換えると、再登録（仕様は人間の再承認）まで先に進めない |

**残る制約（保証していないこと）**
- `.aq-dlc/runs/*/state.json` をエンジン以外（Bash の Python など）で書き換える改ざんは、検出も防止もできません。エンジンの記録は、エージェントが悪意なく従うことを前提にしています。
- 指紋の対象は、git で追跡しているファイルと、追跡していない .gitignore 外のファイルです。`.env` などの .gitignore 済みのファイルの変更は検出しません。
- バックグラウンドで起動された Bash の、起動後の変更は事後の検出から漏れます。実装担当の登録の**後**に届いた変更は登録との比較で検出しますが、実装担当の**実行中**に届いた変更は、実装担当のコードとして登録されます（独立レビューの対象にはなります）。
- 事後の検出は、hook の時間制限（60秒）内に終わる前提です。非常に大きなリポジトリで制限を超えると、その Bash の検出が漏れることがあります（記録は残るので、次の判定時に改ざんとして扱われることはありません）。Windows では ctime が書き換えを表さないため、事後の検出でも毎回全ファイルをハッシュし直します（大きなリポジトリでは Bash のたびに時間がかかります）。
- 人間の返答は、hook が記録したものを、Claude Code の会話記録（`~/.claude/projects/` の下の transcript）にある人間の発言・AskUserQuestion の回答と照合してから承認に使います（E-AQDL で始めた run）。hook を Bash から直接呼んで返答を記録しても、会話記録にない返答は承認になりません。Bash の実行中に記録された返答は偽装として扱い、人間の確認（`audit --resolve`）まで先に進めません。ただし、エージェントが会話記録のファイルそのものを書き換える偽装は防げません。
- 承認は「対象 ID と決定を1行で書いた返答」で判定します。否定・保留の言葉（「修正したい」「承認しない」など）と一緒に書かれた承認は、曖昧として扱います。人間が内容を理解せずに選択肢を選んだ場合までは判定できません。AskUserQuestion の回答を取り出せない形式の結果は、承認の判定に使いません（選択肢を示して聞き直します）。
- state.json の保存時の検証・監査ログ（ハッシュの連鎖）は、エンジンの不具合や単純な書き換えを見つけるためのものです。ファイルごと作り直す改ざんへの耐性は保証しません。
- 反証テストは OS のサンドボックスではありません。テストのコード自体が外部に通信したり、プロジェクトの外を書き換えたりすることは防げません。依存関係（node_modules・.venv）は隔離コピーからリンクで参照するため、テストランナーのキャッシュなどが元の node_modules・.venv に書き込まれることがあります（指紋の対象外なので検出しません）。.venv に対象のパッケージを editable install している場合、テストは元の src を読み込むため、反証テストの結果が survived に偏ります。時間切れのときは、子だけでなく孫のプロセスも止めます。.gitignore 済みのファイル（テスト用の設定など）はコピーされないため、必要なら `counterfactual.copy_extra` に指定します。
- 機密情報の検出は正規表現による簡易検査です。検出されなくても機密がないとは限らないため、共有候補は人間が内容を確認します。
- Bash でエンジン以外から `~/.aq-dlc/memory` を書き換えることは、hook では止められません（Write / Edit は止めます）。
- v0.5.1 より前に始まった run と、プラグインの更新で設定の既定値が変わった場合は、設定の確認（`config-accept`）が1回必要になります。
- 実行の途中で変わって元に戻った変更（A→B→A）は、gate の前後比較では検出しません。
- 「直前の Bash を実行した担当」による判定と事後の検出は、hook が動いていることが前提です。hook を無効にした環境では効きません。
- より強い隔離が必要な場合は、Claude Code のサンドボックス機能（Bash の書き込み先・ネットワークの制限）や、コンテナなどの隔離された実行環境を併用してください。担当ごとの隔離（git worktree など）は今後の改善候補です。
- マージ後に既定ブランチを取り込んで内容が変わると、担当外の変更として扱われ、実装担当の取り込み → gate → 再レビューが必要になります。

## できること（ほかの機能）

- Failure First、Evidence Graph（要求 → 受け入れ条件・リスク → 設計 → 実装 → 検証 → テスト → 結果。`trace` で一覧）
- Quality Gate：型チェック → Lint → ビルド → 単体 → 結合 → E2E の順に実行。E2E はビルドが不合格なら実行しない
- Stop hook：完了条件を満たすまで作業を続けさせる（人間の返答待ち・サブエージェント実行中・マージ待ちは止めない）
- Success / Failure Memory：学びを選別・統合してプロジェクトの Memory に保存し、人間が承認したものだけを共通 Memory（`~/.aq-dlc/memory/`）に共有する
- GitHub 連携：リポジトリ作成、作業ブランチ、push ごとの CI 確認、PR 作成。force push、main への直接 push、PR のマージ、削除・公開範囲の変更は hook で止める
- マージ後の検証：デプロイ後にしか確かめられないものは、マージ後に gate --after-merge と人間の確認で完了させる

## インストール

必要なもの：Claude Code、Python 3.8 以上、git。GitHub 連携を使うなら `gh`（`gh auth login` 済み）。

```
/plugin marketplace add mikiyasa1122-hey/e-aqdl
/plugin install aq-dlc@aq-dlc
```

インストール後、Claude Code を再起動するか `/reload-plugins` を実行します。更新は `/plugin` から行います。

## 使い方

1. アプリ用の空フォルダを作り、Claude Code で開く
2. 作りたいものを頼む（コマンドは不要。`/aq-dlc:start` で明示してもよい）

```
題材：<作りたいアプリを1〜3行で>
必須機能：
- <機能1>
完成条件：
- <何ができたら完了か>
GitHub：private（または public）
```

要求分析担当が仕様をまとめ、答えによって作るものが変わる点を選択式で質問します。仕様シートを承認すると、設計・実装・gate・レビュー・修正ループ・PR 作成まで自動で進みます。マージはあなたが行います。

エンジンの状態は、プロジェクトで次のように確認できます（Claude に「状況は？」と聞いても同じ）。

```
python3 <プラグイン>/scripts/aqdlc.py next     # 今のフェーズと次にすること
python3 <プラグイン>/scripts/aqdlc.py status   # 仕様・設計・実装・コード・レビューの状態
python3 <プラグイン>/scripts/aqdlc.py trace    # 要求から証拠までの対応表
python3 <プラグイン>/scripts/aqdlc.py evidence # 証明済みと、まだ証明できていない範囲
python3 <プラグイン>/scripts/aqdlc.py roles    # 適用される Role Profile と理由
python3 <プラグイン>/scripts/aqdlc.py audit --verify  # 承認・同意・共有の監査ログ
```

## 既存の AQ-DLC からの移行

1. `/plugin` からプラグインを更新する（ID は `aq-dlc` のまま）。`~/.aq-dlc` とプロジェクトの `.aq-dlc` はそのまま使えます。移行スクリプトは不要です。
2. 進行中の run（v0.5 で始まったもの）は、そのまま続けられます（Memory の利用範囲の選択・承認の記録の形式は求めません）。新しい run からは、開始時に Memory の利用範囲を選び、承認は対象 ID 付きで行います。
3. 設定の既定値はそのままでは指紋に影響しません（設定の確認 `config-accept` は不要）。反証テストを使う場合は、人間がプロジェクトの `.aq-dlc/config.json` に `"counterfactual": {"mode": "advisory"}` を加えます（設定の変更として、進行中の run では `config-accept` が必要です）。
4. これまで `~/.aq-dlc/memory` に直接保存していた学びは、そのまま読めます（読むには run の開始時に「共通 Memory も使う」を選ぶか、設定 `memory_policy.scope_default` を `all` にします）。今後の共有は `memory-share` で行います。
5. `memory-done` には `--end project|share|none` が必要になりました（v0.5 で始まった run は省略時に project として扱います）。

## 更新履歴

### 0.6.0（E-AQDL へ改名・P0 機能）
- 名称を E-AQDL（Evidence-driven Adaptive Quality Development Lifecycle）に変更（表示のみ。ID・コマンド・保存場所は互換）
- 承認の意味づけ: 対象 ID と明示的な決定を結び付け、曖昧な返答・選ばれなかった選択肢を承認にしない。承認・同意・共有の監査ログ
- 重要な PreToolUse hook（Bash・Write/Edit・Agent）の異常時は拒否（入力・状態を読めない場合を含む）
- state.json の保存時の整合性検証（スキーマ・状態・参照 ID・gate とレビューと承認）、同時更新の拒否、`state_invalid`
- Adaptive Role Engine（工程・技術分野の Role Profile の自動選択と記録、権限は不変、Profile の変更でレビューを失効）
- Counterfactual Gate（Python・JS/TS の Mutation、隔離コピーでの実行、off / advisory / enforce）
- Negative Evidence Graph（`evidence`、`trace` の拡張、依存関係に基づく失効と原因）
- Evolutionary Memory と同意制（開始時の利用範囲、終了時の候補ごとの共有承認、根拠の照合、台帳による昇格、機密の簡易検査）
- metrics に Role Profile・反証テスト・Memory・承認と、run ごとの条件の一覧（`metrics --runs`）を追加
- P1 / P2（Failure Time Machine・Adaptive Quality Budget・Design Tournament）は未実装（別の PR で扱う）

### 0.5.1（外部コードレビューの指摘を修正）
- **P0-1 レビュー判定**: 「合格」かつ重大0件かつ形式が正しいときだけ完了できる。「要修正」は重大0件でも完了不可で修正ループへ。判定なし・不正な判定・「合格」と重大の矛盾・件数の不一致はレビューを無効にする。最新のレビューが無効なら、前の合格も使わない
- **P0-2 gate の整合性**: gate の結果を、実行開始時のコード・チェック定義・検証条件に紐付ける。実行中にソース・テスト・定義が変わったら gate を不合格にし、変わったファイルをログに残す（.gitignore 済みのビルド出力は対象外）
- **P0-3 担当外の変更**: 実装担当以外の Bash の前後でファイル内容を比べて検出・記録する。実装担当が最後に登録したコードと違えば、取り込み・gate・レビューをやり直すまで進めない。Write / Edit はシンボリックリンクの実体で判定する
- **P1 テストの妥当性**: レビュー報告に「受け入れ条件ごとの検証の妥当性」（全受け入れ条件と重要なリスク）と「E2E の要否」を必須にした。skip・.only・アサーションのないテストをエンジンが検出してレビュー担当に渡し、言及を必須にした。Web アプリで E2E がない場合は要否と理由を書かせる
- **P1 Memory**: 読み込んだ Memory を記録し（`memory --record`）、利用結果を記録する（`memory-use`）。`memory-done` は保存・統合した ID か見送りの理由が必要で、この run の証拠があるか、未検証の学びを active にしていないか、重複がないかを確かめる
- **P1 運用の計測**: `metrics` で、担当ごとの実行回数・再実行・所要時間・トークン、gate の回数と不合格、レビューの結果、修正ループ、人の介入、Stop hook の継続回数、担当外の変更を表示する
- `memory-done` は引数（`--saved` / `--merged` / `--skipped`）が必要になった（互換性の変更）
- 独立コードレビューの指摘を反映: 判定行を行全体で照合（「合格 または 要修正」などは不正）、太字・番号付き・全角の「重大」も数える、判定と登録の時点で全ファイルをハッシュし直す（更新時刻を戻した書き換えも検出）、設定全体の指紋（エンジン以外の変更は人間の確認 `config-accept` まで先に進めない）、`~/.aq-dlc` と `.claude` の定義の保護、`aq-dlc:` の付いたエージェントだけを担当として認める、担当外の変更の記録は取り込むまで先に進めない、検出の記録をプロジェクトの外に置く、git で追跡しているファイルは常に指紋の対象、担当外の変更による修正ループにも上限、run 全体の自動継続の上限
- プロジェクトの .gitignore の雛形で、ビルド出力の除外をプロジェクト直下だけにした（`src/build/` などのソースを無視しない）

### 0.5.0（マルチエージェント構成）
- 統括1つ＋サブエージェント4つ（要求分析・設計・実装・独立レビュー）の構成に改修。統括は実装を直接担当しない（hook で制限）
- サブエージェントの起動順序の強制、同時起動の拒否、前景での実行。起動時の引き継ぎ情報の注入、終了時の成果物の検証
- レビュー結果を、レビュー担当の最後の報告からエンジンが直接記録。レビュー中にコードが変わったらレビューを無効にする
- 証拠の鮮度を強化：検証条件・チェックの定義・gate の環境変数の変更で gate 結果が無効に。レビューは、コード・検証条件・チェック・仕様・設計の変更で無効に
- 検証 ID などの重複を禁止（登録時に拒否。手で重複させた場合も gate と完了判定が拒否）
- Evidence Graph を拡張：受け入れ条件、要求と設計・実装ファイルの対応、検証のテストレベルとテストの場所、変更履歴（`history`）、対応表（`trace`）
- 仕様の承認・質問への回答・人の確認は、hook が記録した人間の返答が必要に
- 差し戻し（設計・実装から仕様・設計への）を記録し、対応するまで先に進めない
- GitHub ガードの検出漏れを修正：`-fu` などの短いオプションの組み合わせ、`--mirror`、`--prune`、`--all`、main 上の `git push -u origin`、`gh api` での削除・マージ・設定変更、GraphQL、curl での GitHub API 操作、push を含む git の別名。判定に失敗したときは安全側に止める
- run ID を秒単位にし、衝突したら連番を付ける（既存の run を上書きしない）。進行中の run があるときの new は確認を求める
- E2E チェックはビルドが不合格なら実行しない（古い成果物での合格を防ぐ）。gate 中にテストがファイルを書き換えたら警告
- 担当外のコマンドは、hook に加えてエンジン自身も拒否する（`$AQ req` のような変数・関数経由の呼び出しも検出）。Bash から hook を直接呼ぶ偽装も拒否
- サブエージェントはブランチを作成・切り替えできない（統括が作った作業ブランチで作業する）
- 人間が発言したら、確認待ちを自動で解除する
- エンジンを責務ごとのモジュールに分割し、自動テスト（`tests/`、39件。Python 3.8 と 3.13 で確認）を追加

### 0.4.0
- 実装前の「仕様の確認」を必須の工程にした。仕様シート（`spec`）を見せ、答えによって作るものが変わる点を最大4問の選択式で質問し、承認（`spec --approve`）を得てから実装する
- 利用者に見える振る舞い・データ・範囲は仮定で済ませず質問する。内部実装の判断は `assume` で記録して自動で進める
- 承認前は gate を実行せず、承認後に要求・仮定・対象外が変わったら再承認まで完了にしない

### 0.3.0（FlowBoard の試運用レポートを反映）
- サブエージェント（独立レビューなど）の実行中は、エンジンが自動で待機扱いにし、Stop hook が止めないようにした。待機中は gate も実行しない（テストの同時実行で結果が壊れるのを防ぐ）
- レビュー担当は、テスト結果を一時フォルダに出すようにした
- push のたびに `ci` で GitHub Actions の結果を確認する手順を追加。最新コミットの CI が通っていないと完了にできない
- マージ後にしか確かめられない検証（`verify --after-merge`）を追加。PR作成時点では `pr_ready` になり、マージ後に `gate --after-merge` → 人間の確認をまとめて1回 → `complete`
- フェーズを、要求・リスク・gate・レビューなどの操作に合わせて自動で進めるようにした
- Memory の読み込みを改善：手順・ルール・プロジェクト情報はタグに関係なく読み込み、未検証のものも「参考」として表示
- publish の前に git の利用者情報を確認し、未設定なら設定方法を表示
- `doctor` コマンド（環境の確認）を追加。Windows では Playwright のブラウザをプロジェクト内に入れる設定を初期化時に追加
- 検証コマンドをテスト名で絞る指針と、E2E を古いビルドに対して実行しない指針を追加

### 0.2.0（初回試運用のフィードバックを反映）
- レビュー待ちの間は Stop hook が止めないようにした（`review --start` / `wait`）
- Windows（日本語環境）の文字コード問題を修正：子プロセスの出力は UTF-8 → OSの文字コード → cp932 の順で読み、エンジンの出力は UTF-8 に統一
- gate 後の変更判定を、更新日時ではなく内容（ハッシュ）で行うようにした。ブランチ切り替えだけではやり直しにならない
- publish を安全にした：既存のリポジトリでは新しいコミットを作らず、main にも push しない。初回コミットでは生成物や `.env` を除外し、含めるファイルを表示する
- `add-check`、`req`、`risk`、`verify`、`review`、`ask`、`complete` などのコマンドを追加し、JSON の手書きを不要にした
- `.gitignore` の雛形に、言語ごとの生成物（`__pycache__/`、`node_modules/` など）を追加

## 保存される場所

| 場所 | 内容 |
|---|---|
| `~/.aq-dlc/config.json` | 全体設定（作業フォルダ、GitHubの公開範囲、自動化の境界、Memoryの閾値） |
| `~/.aq-dlc/memory/` | プロジェクトをまたぐ学び（failure / success / rules）。`ledger.jsonl`（利用結果）、`audit.jsonl`（共有の記録） |
| `~/.aq-dlc/audit.jsonl` | hook の異常の記録 |
| `~/.aq-dlc/tmp/cf/` | 反証テストの一時コピー（終了時に削除） |
| `<project>/.aq-dlc/config.json` | そのプロジェクトの checks と、全体設定の上書き |
| `<project>/.aq-dlc/memory/` | そのプロジェクトの学び（エージェントの保存先）と `ledger.jsonl` |
| `<project>/.aq-dlc/runs/<run>/` | run ごとの `state.json`（Evidence Graph・指紋・履歴）と `run.md`（実行記録） |
| `<project>/.aq-dlc/runs/<run>/artifacts/` | 仕様書・設計書・実装報告（各回）・レビュー結果（各回）・各担当の最後の報告 |
| `<project>/.aq-dlc/runs/<run>/audit.jsonl` | 承認・Memory の同意・状態の保存拒否の監査ログ |
| `<project>/.aq-dlc/runs/<run>/cf/` | 反証テストのログ |

プラグイン本体には個人の情報や学びを含みません。Memory は各自のPCにだけ保存されます。

## 設定（`~/.aq-dlc/config.json`）

| キー | 初期値 | 意味 |
|---|---|---|
| `workspace_dir` | `""` | 「新しいフォルダを作って」と頼んだときの親フォルダ。空なら開いているフォルダ |
| `github.default_visibility` | `private` | 新規リポジトリの公開範囲 |
| `automation.auto_start` | `true` | 空のフォルダやプロジェクトで、コマンドなしで E-AQDL を始める |
| `automation.spec_review` | `true` | 実装前に仕様を人間に確認する（false で省略） |
| `automation.enforce_roles` | `true` | 担当ごとの書き込み先・コマンドの制限 |
| `automation.orchestrator_edits_code` | `false` | 統括がコードを書くことを許すか |
| `automation.force_foreground_agents` | `true` | サブエージェントを前景（順番）で実行させる |
| `automation.max_agent_stop_blocks` | `3` | 成果物が不完全なサブエージェントを終了させない回数 |
| `automation.max_auto_continues` | `30` | Stop hook が自動で作業を続けさせる上限 |
| `automation.max_fix_iterations` | `8` | 修正ループの上限 |
| `automation.block_*` | `true` | force push、main への push、PRマージ、削除系を止める |
| `memory.auto_save_min_score` | `3` | 5つの視点のうち何点以上で自動保存するか |
| `memory.promote_to_active_min_runs` | `2` | 根拠の run 以外の何 run で役立ったら active にできるか |
| `memory_policy.scope_default` | `ask` | run の開始時の Memory の利用範囲。`ask`（人間に聞く）/ `none` / `project` / `all`（共通 Memory も使う。明示的な設定として扱う） |
| `roles.enabled` / `roles.max_domain_profiles` / `roles.disabled` | `true` / `3` / `[]` | Role Profile の適用、追加の専門性の上限、使わない Profile の ID |
| `counterfactual.mode` | `off` | 反証テスト。`off` / `advisory` / `enforce` |
| `counterfactual.timeout_sec` / `max_mutations` / `per_verification` | `300` / `12` / `2` | 1回の実行時間の上限、Mutation の総数、検証ごとの数 |
| `counterfactual.copy_extra` | `[]` | 隔離コピーに追加でコピーするファイル（.gitignore 済みのテスト用の設定など。glob） |

初期値は試運用のための仮の値です。

## 構成

```
.claude-plugin/marketplace.json
plugins/aq-dlc/
  .claude-plugin/plugin.json
  skills/start/              /aq-dlc:start（手順とリファレンス）
  skills/memory/             /aq-dlc:memory（学びの選別・統合・保存）
  agents/                    requirements / design / implementer / reviewer（サブエージェント）
  roles/stage/ roles/domain/ Role Profile（工程・技術分野）
  hooks/hooks.json           SessionStart・UserPromptSubmit・PreToolUse（Bash / Write / Agent）・PostToolUse（AskUserQuestion）
                             SubagentStart・SubagentStop・Stop
  scripts/aqdlc.py           エンジンの入口（標準ライブラリのみ）
  scripts/aqdlc_core/        util / project / state（Evidence Graph・鮮度・状態の検証・次の工程）/ gate / agents / gitops /
                             approval（承認・監査ログ）/ roles / mutation / counterfactual / evidence / memory / cli
  templates/                 新規プロジェクトと ~/.aq-dlc の雛形
tests/                       エンジンと hook の自動テスト（python3 -m unittest discover -s tests）
```

hook は、E-AQDL（AQ-DLC）のプロジェクトで作業しているときだけ働きます。他のプロジェクトでの作業には影響しません。

## 注意

1回の試運用は、エンジンが一通り動くことの確認であり、「記憶・役割・反証テストによって品質が改善した」ことの証明ではありません。効果を測るには、同じ課題・同じ評価基準での比較と複数回の検証が必要です（`metrics --runs` はその材料を並べるだけで、改善を判定しません）。

## License

MIT
