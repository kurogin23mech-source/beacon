#!/usr/bin/env python3
"""MySQL 用テーブル作成スクリプト (ms-96)。

``mysql_client.py`` の真値源 (ENTITIES / TABLES) を読み、JSON-blob generic
schema (pk, sk, data JSON) のテーブルを未作成の分だけ作る。冪等 (=
``CREATE TABLE IF NOT EXISTS``) なので起動のたびに走っても安全。

接続先は環境変数 (mysql_client._connect と同じ):
  - BEACON_MYSQL_HOST      : 例 127.0.0.1 (既定)
  - BEACON_MYSQL_PORT      : 例 3306 (既定)
  - BEACON_MYSQL_USER      : 接続ユーザー
  - BEACON_MYSQL_PASSWORD  : パスワード
  - BEACON_MYSQL_DB        : データベース名 (既定 beacon)
  - BEACON_ENV             : テーブル prefix (既定 dev → beacon_dev_*)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mysql_client import TABLES, create_mysql_tables  # noqa: E402


def main() -> int:
    created = create_mysql_tables()
    print(f"[create_mysql_tables] done. {created} created, "
          f"{len(TABLES) - created} already present "
          f"({len(TABLES)} tables total).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
