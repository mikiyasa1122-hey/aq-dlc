#!/usr/bin/env python3
"""E-AQDL エンジンの入口。本体は aqdlc_core パッケージ（Python 3.8+ 標準ライブラリのみ）。

  python3 aqdlc.py help        コマンド一覧
  python3 aqdlc.py hook <名前>  Claude Code の hook から呼ばれる
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aqdlc_core.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
