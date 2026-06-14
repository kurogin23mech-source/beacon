"""DynamoDB backend tests for trek CRUD (ms-69 / e-1652).

Uses moto's mock_aws so real boto3 paths are exercised against an
in-memory DynamoDB — confirms PK=trek_id table shape, visibility
filter, status filter, archived hiding, and idempotent delete.

Pattern mirrors tests/test_dynamodb_documents_retros.py.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def db(monkeypatch):
    """Boot a moto-backed DynamoDB and return the freshly-imported client."""
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
            TableName="beacon-dev-treks",
            KeySchema=[{"AttributeName": "trek_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "trek_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Reimport client so the lazy _RESOURCE picks up the mock.
        if "dynamodb_client" in sys.modules:
            del sys.modules["dynamodb_client"]
        import dynamodb_client
        # Reset the cached resource so a clean boto3 binding is used.
        dynamodb_client._RESOURCE = None
        yield dynamodb_client
    finally:
        mock_ctx.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trek(*, trek_id, title, creator_uid, creator_email,
               status="planning", type_="persistent", members=None,
               scope=None, created_at="2026-06-14T00:00:00.000000Z"):
    return {
        "trek_id": trek_id,
        "title": title,
        "description": "",
        "type": type_,
        "status": status,
        "creator_actor": {"user_id": creator_uid, "email": creator_email},
        "leader_actor": {"user_id": creator_uid, "email": creator_email},
        "members": members if members is not None else [
            {"user_id": creator_uid, "email": creator_email,
             "role": "leader", "invited_at": created_at,
             "joined_at": created_at, "invited_by": creator_uid},
        ],
        "scope": scope or [],
        "created_at": created_at,
        "updated_at": created_at,
        "archived_at": None,
    }


# ---------------------------------------------------------------------------
# save_trek / get_trek
# ---------------------------------------------------------------------------

def test_save_then_get_roundtrip(db):
    t = _make_trek(trek_id="tk-aaaa1111", title="Daily Ops",
                   creator_uid="u-1", creator_email="a@b.com")
    db.save_trek("tk-aaaa1111", t)

    loaded = db.get_trek("tk-aaaa1111")
    assert loaded is not None
    assert loaded["trek_id"] == "tk-aaaa1111"
    assert loaded["title"] == "Daily Ops"
    assert loaded["status"] == "planning"
    assert loaded["creator_actor"]["email"] == "a@b.com"
    assert len(loaded["members"]) == 1


def test_get_trek_missing_returns_none(db):
    assert db.get_trek("tk-not-here") is None


def test_save_trek_forces_pk(db):
    """trek_id in payload is ignored — the PK arg is authoritative."""
    t = _make_trek(trek_id="tk-WRONG", title="x",
                   creator_uid="u-1", creator_email="a@b.com")
    db.save_trek("tk-actual", t)
    assert db.get_trek("tk-actual")["trek_id"] == "tk-actual"
    assert db.get_trek("tk-WRONG") is None


# ---------------------------------------------------------------------------
# list_treks — visibility, status, archived hiding, ordering
# ---------------------------------------------------------------------------

def test_list_treks_admin_view_returns_all(db):
    db.save_trek("tk-1", _make_trek(
        trek_id="tk-1", title="t1", creator_uid="u-1", creator_email="a@b",
        created_at="2026-06-10T00:00:00.000000Z",
    ))
    db.save_trek("tk-2", _make_trek(
        trek_id="tk-2", title="t2", creator_uid="u-2", creator_email="c@d",
        created_at="2026-06-12T00:00:00.000000Z",
    ))
    out = db.list_treks()
    assert {t["trek_id"] for t in out} == {"tk-1", "tk-2"}


def test_list_treks_filters_by_actor_creator(db):
    db.save_trek("tk-1", _make_trek(
        trek_id="tk-1", title="t1", creator_uid="u-1", creator_email="a@b",
    ))
    db.save_trek("tk-2", _make_trek(
        trek_id="tk-2", title="t2", creator_uid="u-2", creator_email="c@d",
    ))
    out = db.list_treks(actor_id="u-1")
    assert [t["trek_id"] for t in out] == ["tk-1"]


def test_list_treks_filters_by_actor_member(db):
    # u-2 is creator; u-1 is a non-creator member of tk-1
    members = [
        {"user_id": "u-2", "email": "c@d", "role": "leader",
         "invited_at": "x", "joined_at": "x", "invited_by": "u-2"},
        {"user_id": "u-1", "email": "a@b", "role": "member",
         "invited_at": "x", "joined_at": "x", "invited_by": "u-2"},
    ]
    db.save_trek("tk-1", _make_trek(
        trek_id="tk-1", title="t1", creator_uid="u-2", creator_email="c@d",
        members=members,
    ))
    db.save_trek("tk-2", _make_trek(
        trek_id="tk-2", title="t2", creator_uid="u-3", creator_email="e@f",
    ))
    out = db.list_treks(actor_id="u-1")
    assert [t["trek_id"] for t in out] == ["tk-1"]


def test_list_treks_hides_archived_by_default(db):
    db.save_trek("tk-1", _make_trek(
        trek_id="tk-1", title="alive", creator_uid="u-1",
        creator_email="a@b", status="active",
    ))
    db.save_trek("tk-2", _make_trek(
        trek_id="tk-2", title="gone", creator_uid="u-1",
        creator_email="a@b", status="archived",
    ))
    out = db.list_treks()
    assert {t["trek_id"] for t in out} == {"tk-1"}

    out_all = db.list_treks(include_archived=True)
    assert {t["trek_id"] for t in out_all} == {"tk-1", "tk-2"}


def test_list_treks_status_filter(db):
    for sid, status in [("tk-1", "planning"), ("tk-2", "active"),
                        ("tk-3", "paused")]:
        db.save_trek(sid, _make_trek(
            trek_id=sid, title=sid, creator_uid="u-1", creator_email="a@b",
            status=status,
        ))
    out = db.list_treks(status="active")
    assert [t["trek_id"] for t in out] == ["tk-2"]


def test_list_treks_orders_newest_first(db):
    # Insert in creation order, expect reverse in result.
    for ts in ["2026-06-10", "2026-06-12", "2026-06-08"]:
        db.save_trek(f"tk-{ts}", _make_trek(
            trek_id=f"tk-{ts}", title=ts, creator_uid="u-1",
            creator_email="a@b",
            created_at=f"{ts}T00:00:00.000000Z",
        ))
    out = db.list_treks()
    assert [t["trek_id"] for t in out] == [
        "tk-2026-06-12", "tk-2026-06-10", "tk-2026-06-08",
    ]


# ---------------------------------------------------------------------------
# delete_trek
# ---------------------------------------------------------------------------

def test_delete_trek_existing(db):
    db.save_trek("tk-x", _make_trek(
        trek_id="tk-x", title="x", creator_uid="u-1", creator_email="a@b",
    ))
    assert db.delete_trek("tk-x") is True
    assert db.get_trek("tk-x") is None


def test_delete_trek_missing(db):
    assert db.delete_trek("tk-not-here") is False
