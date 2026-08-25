"""Store-side operation-fires claim across backends (ms-95 / e-5477).

The claim function lived ONLY in firestore_client and was never re-exported via
store_router, so `db.claim_operation_fire_if_new` (called by the endpoint) raised
AttributeError on every backend — the dedup gate was silently dark. e-5477 fixes
the re-export and adds the missing MySQL / DynamoDB implementations.

- 登録 parity: operation_fires が mysql / dynamodb の entity 登録に現れる。
- 全 backend で store_router が claim_operation_fire_if_new を re-export する。
- DynamoDB (moto) round-trip: first-write-wins (同 period は 2 発目 claimed=False /
  別 period は claimed=True) を実 ConditionExpression 経路で確認。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


# ---------------------------------------------------------------------------
# 登録 parity + re-export
# ---------------------------------------------------------------------------
def test_registered_in_mysql_and_dynamo():
    import mysql_client as mc
    import dynamodb_client as dc
    assert "operation_fires" in mc.ENTITIES
    assert mc._SUBCOLLECTION_SK_NAMES["operation_fires"] == "fire_key"
    assert "operation_fires" in dc.TABLES
    assert dc._SUBCOLLECTION_SK_NAMES["operation_fires"] == "fire_key"
    assert dc.TABLE_KEY_SCHEMA["operation_fires"] == ("project_id", "fire_key")


@pytest.mark.parametrize("backend", ["firestore", "mysql", "dynamodb"])
def test_store_router_reexports_claim(backend, monkeypatch):
    import importlib
    monkeypatch.setenv("BEACON_STORE_BACKEND", backend)
    import store_router
    importlib.reload(store_router)
    assert hasattr(store_router, "claim_operation_fire_if_new")


# ---------------------------------------------------------------------------
# DynamoDB round-trip (moto) — real ConditionExpression first-write-wins
# ---------------------------------------------------------------------------
@pytest.fixture
def ddb(monkeypatch):
    monkeypatch.setenv("BEACON_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("BEACON_ENV", "dev")
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.setenv(k, "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    moto = pytest.importorskip("moto")
    import boto3

    ctx = moto.mock_aws()
    ctx.start()
    try:
        res = boto3.resource("dynamodb", region_name="us-east-1")
        res.create_table(
            TableName="beacon-dev-operation_fires",
            KeySchema=[
                {"AttributeName": "project_id", "KeyType": "HASH"},
                {"AttributeName": "fire_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "project_id", "AttributeType": "S"},
                {"AttributeName": "fire_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        import importlib
        import dynamodb_client
        importlib.reload(dynamodb_client)
        yield dynamodb_client
    finally:
        ctx.stop()


PID = "beacon-b95643"


def test_first_write_wins_same_period(ddb):
    first = ddb.claim_operation_fire_if_new(PID, "op-1", "2026-08-24T09", "s1")
    assert first["claimed"] is True
    assert first["claimed_by"] == "s1"
    # 同じ period の 2 発目は既存 claim を見て claimed=False + 最初の勝者を返す。
    second = ddb.claim_operation_fire_if_new(PID, "op-1", "2026-08-24T09", "s2")
    assert second["claimed"] is False
    assert second["claimed_by"] == "s1"


def test_different_period_both_claim(ddb):
    a = ddb.claim_operation_fire_if_new(PID, "op-1", "2026-08-24T09", "s1")
    b = ddb.claim_operation_fire_if_new(PID, "op-1", "2026-08-24T10", "s1")
    assert a["claimed"] is True and b["claimed"] is True


def test_different_op_isolated(ddb):
    a = ddb.claim_operation_fire_if_new(PID, "op-1", "2026-08-24", "s1")
    b = ddb.claim_operation_fire_if_new(PID, "op-2", "2026-08-24", "s1")
    assert a["claimed"] is True and b["claimed"] is True
