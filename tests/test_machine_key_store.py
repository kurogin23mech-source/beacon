"""Store CRUD + cross-backend registration for machine keys (ms-151 / e-5474).

Two layers:

1. **登録 parity** (cheap, no live backend): machine_keys が mysql / dynamodb の
   entity 登録 (ENTITIES / TABLES / _SUBCOLLECTION_SK_NAMES / TABLE_KEY_SCHEMA) に
   揃っていること、firestore の delete_project cascade に載っていること。過去
   (ms-96) に entity 登録の非対称で「移行行が一覧から silent に消える」事故が
   あったため、登録の左右対称を test で固定する (test_master_backend_e3620 と同趣旨)。

2. **round-trip** (moto-backed DynamoDB): save → get → list → revoke を実 boto3
   経路で通し、machine_key.verify_token と結線して「発行→保存→検証OK / 失効→検証NG」
   を end-to-end で示す。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import machine_key as mk  # noqa: E402

NOW = "2026-08-24T12:00:00Z"
PID = "beacon-b95643"


# ---------------------------------------------------------------------------
# 1. 登録 parity
# ---------------------------------------------------------------------------
def test_registered_in_mysql():
    import mysql_client as mc
    assert "machine_keys" in mc.ENTITIES
    assert mc._SUBCOLLECTION_SK_NAMES["machine_keys"] == "key_id"
    assert mc.TABLES["machine_keys"].endswith("_machine_keys")


def test_registered_in_dynamodb():
    import dynamodb_client as dc
    assert "machine_keys" in dc.TABLES
    assert dc._SUBCOLLECTION_SK_NAMES["machine_keys"] == "key_id"
    # TABLE_KEY_SCHEMA は _SUBCOLLECTION_SK_NAMES から導出される。
    assert dc.TABLE_KEY_SCHEMA["machine_keys"] == ("project_id", "key_id")


def test_mysql_dynamodb_sk_parity():
    # 2 backend の subcollection SK 名が一致していること (= 片側だけ足す非対称を防ぐ)。
    import mysql_client as mc
    import dynamodb_client as dc
    assert (mc._SUBCOLLECTION_SK_NAMES["machine_keys"]
            == dc._SUBCOLLECTION_SK_NAMES["machine_keys"])


def test_firestore_delete_cascade_includes_machine_keys():
    # delete_project が machine_keys subcollection を消すこと (orphan な鍵を残さない)。
    import inspect
    import firestore_client as fc
    src = inspect.getsource(fc.delete_project)
    assert "machine_keys" in src


@pytest.mark.parametrize("backend", ["firestore", "mysql", "dynamodb"])
def test_store_router_reexports_machine_key_crud(backend, monkeypatch):
    # e-5502 maint review B: 3 backend 全てで 4 関数が re-export されること。
    # 従来は firestore のみ検証で、mysql/dynamodb の re-export 抜けを検出できなかった。
    import importlib
    monkeypatch.setenv("BEACON_STORE_BACKEND", backend)
    import store_router
    importlib.reload(store_router)
    for fn in ("save_machine_key", "get_machine_key",
               "list_machine_keys", "revoke_machine_key"):
        assert hasattr(store_router, fn), f"{backend} store_router missing {fn}"


# ---------------------------------------------------------------------------
# 2. round-trip (moto-backed DynamoDB)
# ---------------------------------------------------------------------------
@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("BEACON_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("BEACON_ENV", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    moto = pytest.importorskip("moto")
    import boto3

    mock_ctx = moto.mock_aws()
    mock_ctx.start()
    try:
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="beacon-dev-machine_keys",
            KeySchema=[
                {"AttributeName": "project_id", "KeyType": "HASH"},
                {"AttributeName": "key_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "project_id", "AttributeType": "S"},
                {"AttributeName": "key_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        import importlib
        import dynamodb_client
        importlib.reload(dynamodb_client)
        yield dynamodb_client
    finally:
        mock_ctx.stop()


def test_save_get_roundtrip(db):
    raw, record = mk.issue(PID, label="PE Lambda", created_by="u-1", now=NOW)
    db.save_machine_key(PID, record)
    got = db.get_machine_key(PID, record["key_id"])
    assert got is not None
    assert got["secret_hash"] == record["secret_hash"]
    assert got["label"] == "PE Lambda"
    assert got["revoked_at"] is None
    # project_id は正規のデータ (verify_token が別 project すり替え検知に読む) なので
    # surface に残す。bus_event_approvals (project_id を落とす) とは異なる契約。
    assert got["project_id"] == PID


def test_get_missing_returns_none(db):
    assert db.get_machine_key(PID, "nope") is None


def test_list_newest_first(db):
    _, r1 = mk.issue(PID, now="2026-08-24T10:00:00Z", key_id="k1", secret="s1")
    _, r2 = mk.issue(PID, now="2026-08-24T12:00:00Z", key_id="k2", secret="s2")
    db.save_machine_key(PID, r1)
    db.save_machine_key(PID, r2)
    rows = db.list_machine_keys(PID)
    assert [r["key_id"] for r in rows] == ["k2", "k1"]


def test_revoke_marks_and_missing_returns_none(db):
    _, record = mk.issue(PID, now=NOW, key_id="k1", secret="s1")
    db.save_machine_key(PID, record)
    updated = db.revoke_machine_key(PID, "k1", revoked_at="2026-08-24T13:00:00Z")
    assert updated["revoked_at"] == "2026-08-24T13:00:00Z"
    assert db.get_machine_key(PID, "k1")["revoked_at"] == "2026-08-24T13:00:00Z"
    # 存在しない key の失効は None。
    assert db.revoke_machine_key(PID, "nope", revoked_at=NOW) is None


def test_revoke_idempotent_preserves_first_time(db):
    # e-5502 AX review A: 再 revoke は最初の revoked_at を保持し上書きしない。
    _, record = mk.issue(PID, now=NOW, key_id="k1", secret="s1")
    db.save_machine_key(PID, record)
    first = db.revoke_machine_key(PID, "k1", revoked_at="2026-08-24T13:00:00Z")
    assert first["revoked_at"] == "2026-08-24T13:00:00Z"
    second = db.revoke_machine_key(PID, "k1", revoked_at="2026-08-25T20:00:00Z")
    assert second["revoked_at"] == "2026-08-24T13:00:00Z"  # 最初の時刻を保持


def test_end_to_end_issue_store_verify_revoke(db):
    # 発行 → 保存 → (token 由来で引いて) 検証OK → 失効 → 検証NG。
    raw, record = mk.issue(PID, now=NOW)
    db.save_machine_key(PID, record)

    # verify 経路の再現: token を parse して (project_id, key_id) で store を引く。
    project_id, key_id, _ = mk.parse_token(raw)
    fetched = db.get_machine_key(project_id, key_id)
    assert mk.verify_token(raw, fetched) is not None

    db.revoke_machine_key(PID, key_id, revoked_at="2026-08-24T13:00:00Z")
    fetched_after = db.get_machine_key(project_id, key_id)
    assert mk.verify_token(raw, fetched_after) is None


def test_cross_project_isolation(db):
    # 別 project へ書いた key は、対象 project の一覧・get に現れない。
    _, r = mk.issue("beacon-OTHER", now=NOW, key_id="k1", secret="s1")
    db.save_machine_key("beacon-OTHER", r)
    assert db.get_machine_key(PID, "k1") is None
    assert db.list_machine_keys(PID) == []
