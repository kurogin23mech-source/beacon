"""DynamoDB backend tests for the trek READ surface (ms-69 / e-1652).

Uses moto's mock_aws so real boto3 paths are exercised against an
in-memory DynamoDB — confirms PK=trek_id table shape, visibility
filter, status filter, archived hiding, and ordering.

Pattern mirrors tests/test_dynamodb_documents_retros.py.

**ms-185 e-7306 — this file used to test CRUD; now it tests the reads only.**
The write functions (``save_trek`` / ``delete_trek`` / ``append_trek_log``) were
removed from all three backends because e-7070 deleted every write path
(create / join / approve / halt / fire) and left them at zero callers. Three
tests went with them (``test_save_trek_forces_pk`` — PK authority is a property
of the removed writer; ``test_delete_trek_existing`` / ``test_delete_trek_missing``
— idempotent delete, same). ``test_the_write_functions_are_gone`` below replaces
them: it asserts the ABSENCE on all three backends, so restoring a writer from
history during later work goes red instead of silently passing.

Seeding now goes through ``put_item`` on the table rather than through
``save_trek``. Keeping a production function alive only so tests can seed is
the pattern this milestone is removing (see the ``shared_trek_member`` note in
tests/test_dm_gate.py — "規則を消すときに helper だけ残すと、誰も呼ばない判定が
1 つ生き続ける").
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


def _seed(db, trek_id, doc):
    """Put a trek row straight onto the table (ms-185 e-7306).

    The former seeding path was ``_seed(db, trek_id, doc)``. That function is
    gone, so the tests write the row with the same shape it produced
    (``{**data, "trek_id": trek_id}`` — the PK arg wins over any trek_id in the
    payload) using the table primitive.
    """
    db._table("treks").put_item(Item={**doc, "trek_id": trek_id})


# ---------------------------------------------------------------------------
# get_trek
# ---------------------------------------------------------------------------

def test_get_trek_reads_a_seeded_record(db):
    t = _make_trek(trek_id="tk-aaaa1111", title="Daily Ops",
                   creator_uid="u-1", creator_email="a@b.com")
    _seed(db, "tk-aaaa1111", t)

    loaded = db.get_trek("tk-aaaa1111")
    assert loaded is not None
    assert loaded["trek_id"] == "tk-aaaa1111"
    assert loaded["title"] == "Daily Ops"
    assert loaded["status"] == "planning"
    assert loaded["creator_actor"]["email"] == "a@b.com"
    assert len(loaded["members"]) == 1


def test_get_trek_missing_returns_none(db):
    assert db.get_trek("tk-not-here") is None


# ---------------------------------------------------------------------------
# list_treks — visibility, status, archived hiding, ordering
# ---------------------------------------------------------------------------

def test_list_treks_admin_view_returns_all(db):
    _seed(db, "tk-1", _make_trek(
        trek_id="tk-1", title="t1", creator_uid="u-1", creator_email="a@b",
        created_at="2026-06-10T00:00:00.000000Z",
    ))
    _seed(db, "tk-2", _make_trek(
        trek_id="tk-2", title="t2", creator_uid="u-2", creator_email="c@d",
        created_at="2026-06-12T00:00:00.000000Z",
    ))
    out = db.list_treks()
    assert {t["trek_id"] for t in out} == {"tk-1", "tk-2"}


def test_list_treks_filters_by_actor_creator(db):
    _seed(db, "tk-1", _make_trek(
        trek_id="tk-1", title="t1", creator_uid="u-1", creator_email="a@b",
    ))
    _seed(db, "tk-2", _make_trek(
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
    _seed(db, "tk-1", _make_trek(
        trek_id="tk-1", title="t1", creator_uid="u-2", creator_email="c@d",
        members=members,
    ))
    _seed(db, "tk-2", _make_trek(
        trek_id="tk-2", title="t2", creator_uid="u-3", creator_email="e@f",
    ))
    out = db.list_treks(actor_id="u-1")
    assert [t["trek_id"] for t in out] == ["tk-1"]


def test_list_treks_hides_archived_by_default(db):
    _seed(db, "tk-1", _make_trek(
        trek_id="tk-1", title="alive", creator_uid="u-1",
        creator_email="a@b", status="active",
    ))
    _seed(db, "tk-2", _make_trek(
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
        _seed(db, sid, _make_trek(
            trek_id=sid, title=sid, creator_uid="u-1", creator_email="a@b",
            status=status,
        ))
    out = db.list_treks(status="active")
    assert [t["trek_id"] for t in out] == ["tk-2"]


def test_list_treks_orders_newest_first(db):
    # Insert in creation order, expect reverse in result.
    for ts in ["2026-06-10", "2026-06-12", "2026-06-08"]:
        _seed(db, f"tk-{ts}", _make_trek(
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

def test_the_write_functions_are_gone(db):
    """書き込み 3 関数が 3 backend すべてから消えている (ms-185 e-7306)。

    **置き換えたもの**: ``test_delete_trek_existing`` / ``test_delete_trek_missing``
    は冪等な削除を固定していたが、削除する関数ごと無くなったので固定対象が消えた。
    対象を消して古い pin をそのまま緑で通すのは e-7320 で踏んだ型なので、裏返して
    「戻ってきたら赤くなる」側に置き換える。

    3 backend すべてを見るのは、1 つだけ戻ると振り分け (store_router) 経由で
    backend によって書けたり書けなかったりする状態になり、それが最も気付きにくい形
    だから。読み出し 3 関数は意図して残す面なので、同時に在ることも測る
    (= 読み書きをまとめて落としていないことの証拠)。
    """
    import importlib

    gone = ("save_trek", "delete_trek", "append_trek_log")
    kept = ("get_trek", "list_treks", "list_trek_logs")
    for mod_name in ("dynamodb_client", "firestore_client", "mysql_client"):
        mod = importlib.import_module(mod_name)
        for fn in gone:
            assert not hasattr(mod, fn), (
                f"{mod_name}.{fn} が戻っている — e-7070 が落とした書き込み経路の"
                f"保存層側が復活している (呼び出し元が無いまま書ける口が開く)")
        for fn in kept:
            assert hasattr(mod, fn), (
                f"{mod_name}.{fn} が無い — 読み出し面を一緒に落としている "
                f"(e-7081 / SPEC 方針5 に反する)")

    import store_router
    for fn in gone:
        assert not hasattr(store_router, fn), (
            f"store_router が {fn} を再公開している")
    for fn in kept:
        assert hasattr(store_router, fn), (
            f"store_router が {fn} を公開していない")
