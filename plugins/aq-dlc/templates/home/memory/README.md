# E-AQDL 共通 Memory（旧 AQ-DLC Global Memory）

プロジェクトをまたいで使う学び。人が読めるMarkdownで管理する。

- failure/ — 失敗の条件・根本原因・再発防止（F-）
- success/ — Code Success（S-）と Workflow Success（W-）
- rules/ — 検証済みルール（R-）。active化は人間の確認が必要

状態：candidate → validated → active → deprecated（candidate は読み込まれない）
初期ファイルは設計段階の例で、未検証のため candidate。

このフォルダはプラグインとは別に、各自のPCに置かれる。個人の学びを公開したくない場合は、ここをgit管理する場合でも private リポジトリにする。

E-AQDL では、エージェントはこのフォルダへ直接書き込めない。プロジェクトの `.aq-dlc/memory` に保存した学びのうち、
`memory-share` で内容・根拠・機密性のリスクを確認し、人間が候補ごとに「承認 SHARE-…」と返答したものだけが、
エンジンによって candidate として追加される（記録は audit.jsonl）。run でここを読むのは、人間が開始時に
「共通 Memory も使う」を承認したとき、または設定 `memory_policy.scope_default` を `all` にしたときだけ。
