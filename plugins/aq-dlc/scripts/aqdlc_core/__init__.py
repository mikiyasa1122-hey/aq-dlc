"""E-AQDL engine package.

Modules:
  util      入出力・文字コード・ハッシュなどの共通処理
  project   全体設定・プロジェクトの初期化・アクティブなrunの登録
  state     run の状態、Evidence Graph、証拠の鮮度判定、次の工程の決定
  gate      Quality Gate、コードの指紋、CI の確認
  agents    サブエージェントの役割・引き継ぎ・成果物の検証・権限
  gitops    GitHub への公開と危険な操作のガード
  memory    Success / Failure Memory の検索
  cli       コマンドと hook の入口
"""
