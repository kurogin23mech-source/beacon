#!/usr/bin/env python3
"""DynamoDB Local 用テーブル作成スクリプト (e-1987)。

amazon/dynamodb-local は -sharedDb -dbPath でデータをディスクに永続化するが、
テーブル自体は新規ボリュームに対して一度作成する必要がある。本スクリプトは
``dynamodb_client.py`` の真値源 (TABLES + TABLE_KEY_SCHEMA) を読み、未作成の
テーブルだけを作る。

冪等 (= 既存テーブルは触らない) なので ``docker compose up`` のたびに走っても
安全。docker-compose の dynamodb-init サービスが起動時に 1 回実行する。

接続先は環境変数:
  - BEACON_DYNAMODB_ENDPOINT : 例 http://dynamodb-local:8000
  - AWS_REGION               : 例 ap-northeast-1 (既定)
  - AWS_ACCESS_KEY_ID /      : DynamoDB Local は任意値で可 (compose がダミーを渡す)
    AWS_SECRET_ACCESS_KEY
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from dynamodb_client import TABLES, TABLE_KEY_SCHEMA  # noqa: E402


def _client():
    endpoint = os.environ.get("BEACON_DYNAMODB_ENDPOINT")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    return boto3.client("dynamodb", region_name=region, endpoint_url=endpoint or None)


def _wait_for_dynamodb(client, *, attempts: int = 30, delay: float = 2.0) -> None:
    """DynamoDB Local が listen し始めるまで待つ (起動順の取りこぼし防止)。"""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            client.list_tables()
            return
        except (EndpointConnectionError, ClientError, Exception) as exc:  # noqa: BLE001
            last_err = exc
            print(f"[create_local_tables] waiting for DynamoDB Local "
                  f"({i + 1}/{attempts})...", flush=True)
            time.sleep(delay)
    raise SystemExit(
        f"[create_local_tables] DynamoDB Local に接続できませんでした: {last_err}"
    )


def _list_existing(client) -> set[str]:
    existing: set[str] = set()
    kwargs: dict = {}
    while True:
        resp = client.list_tables(**kwargs)
        existing.update(resp.get("TableNames", []))
        last = resp.get("LastEvaluatedTableName")
        if not last:
            return existing
        kwargs["ExclusiveStartTableName"] = last


def main() -> int:
    # 不変条件: 全 TABLES エントリに key schema が定義されていること。
    missing = set(TABLES) - set(TABLE_KEY_SCHEMA)
    if missing:
        raise SystemExit(
            f"[create_local_tables] TABLE_KEY_SCHEMA に schema 未定義の "
            f"entity があります: {sorted(missing)}"
        )

    client = _client()
    _wait_for_dynamodb(client)
    existing = _list_existing(client)

    created = 0
    for entity, table_name in TABLES.items():
        if table_name in existing:
            print(f"[create_local_tables] skip (exists): {table_name}", flush=True)
            continue
        pk, sk = TABLE_KEY_SCHEMA[entity]
        key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
        attr_defs = [{"AttributeName": pk, "AttributeType": "S"}]
        if sk:
            key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
            attr_defs.append({"AttributeName": sk, "AttributeType": "S"})
        client.create_table(
            TableName=table_name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode="PAY_PER_REQUEST",
        )
        created += 1
        print(f"[create_local_tables] created: {table_name} "
              f"(PK={pk}{', SK=' + sk if sk else ''})", flush=True)

    print(f"[create_local_tables] done. {created} created, "
          f"{len(TABLES) - created} already present "
          f"({len(TABLES)} tables total).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
