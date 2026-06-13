"""Logic tests for server/dynamodb_client.py Phase 3 + Phase 4 (e-1544).

Phase 3 covers the 9 bus-related functions:
- append_bus_event / list_bus_events (chronological + channel filter)
- get_bus_cursor / advance_bus_cursor (forward-only)
- check_and_record_bus_nonce (atomic single-use)
- set_bus_event_receipt (first-write-wins per stage)
- find_bus_event
- append_bus_audit / list_bus_audit

Phase 4 covers the 14 identity / session / envelope functions:
- upsert_session / stamp_session_actor_email / list_sessions
- get_or_mint_machine / get_or_mint_session_by_tuple / list_user_machines
- upsert_session_log / list_session_logs / get_session_log
- get_active_operation_envelope / issue_operation_envelope /
  revoke_operation_envelope / list_operation_envelopes / get_operation_envelope

moto's mock_aws drives real boto3 code paths against an in-memory DynamoDB
so schema (PK / SK / conditional writes / nested map updates) is exercised
end-to-end.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


# All Phase 3 + 4 tables required by the implementation.
_PHASE_3_4_TABLES = [
    ("beacon-dev-bus_events", "project_id", "event_id"),
    ("beacon-dev-bus_cursors", "project_id", "cursor_id"),
    ("beacon-dev-bus_nonces", "project_id", "nonce"),
    ("beacon-dev-bus_audit", "project_id", "audit_id"),
    ("beacon-dev-sessions", "project_id", "session_id"),
    ("beacon-dev-session_lookup", "project_id", "lookup_key"),
    ("beacon-dev-session_logs", "project_id", "session_id"),
    ("beacon-dev-operation_envelopes", "project_id", "envelope_id"),
    ("beacon-dev-machines", "user_id", "fingerprint_id"),
]


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
        for name, pk, sk in _PHASE_3_4_TABLES:
            ddb.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": pk, "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": pk, "AttributeType": "S"},
                    {"AttributeName": sk, "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        sys.modules.pop("dynamodb_client", None)
        import dynamodb_client as _db
        yield _db
    finally:
        mock_ctx.stop()
        sys.modules.pop("dynamodb_client", None)


PID = "proj-x"
UID = "user-a"


# ---------------------------------------------------------------------------
# Phase 3: bus_events / cursors / nonces / audit
# ---------------------------------------------------------------------------

def test_append_and_list_bus_events_chronological(db):
    eid_1 = db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:00Z",
                                       "channel": "dm", "payload": {"x": 1}})
    eid_2 = db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:01Z",
                                       "channel": "operation-trigger", "payload": {"x": 2}})
    eid_3 = db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:02Z",
                                       "channel": "dm", "payload": {"x": 3}})
    assert eid_1 != eid_2 != eid_3

    all_events = db.list_bus_events(PID)
    assert [e["payload"]["x"] for e in all_events] == [1, 2, 3]


def test_list_bus_events_since_and_channel_filter(db):
    db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:00Z",
                               "channel": "dm", "payload": {"x": 1}})
    db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:01Z",
                               "channel": "operation-trigger", "payload": {"x": 2}})
    db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:02Z",
                               "channel": "dm", "payload": {"x": 3}})

    since_filtered = db.list_bus_events(PID, since="2026-06-13T10:00:00Z")
    assert [e["payload"]["x"] for e in since_filtered] == [2, 3]

    channel_filtered = db.list_bus_events(PID, channel="dm")
    assert [e["payload"]["x"] for e in channel_filtered] == [1, 3]

    limited = db.list_bus_events(PID, limit=2)
    assert len(limited) == 2


def test_find_bus_event_roundtrip(db):
    eid = db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:00Z",
                                    "channel": "dm", "payload": {"x": 1}})
    found = db.find_bus_event(PID, eid)
    assert found and found["payload"]["x"] == 1
    assert db.find_bus_event(PID, "no-such-event") is None


def test_bus_cursor_forward_only(db):
    assert db.get_bus_cursor(PID, "session-a") == {}

    r1 = db.advance_bus_cursor(PID, "session-a", "2026-06-13T10:00:00Z")
    assert r1["last_seen_at"] == "2026-06-13T10:00:00Z"

    # Rewind attempt: older or equal value is rejected
    r2 = db.advance_bus_cursor(PID, "session-a", "2026-06-13T09:00:00Z")
    assert r2["last_seen_at"] == "2026-06-13T10:00:00Z"

    r3 = db.advance_bus_cursor(PID, "session-a", "2026-06-13T10:00:00Z")
    assert r3["last_seen_at"] == "2026-06-13T10:00:00Z"

    # Forward advance succeeds
    r4 = db.advance_bus_cursor(PID, "session-a", "2026-06-13T11:00:00Z")
    assert r4["last_seen_at"] == "2026-06-13T11:00:00Z"

    cur = db.get_bus_cursor(PID, "session-a")
    assert cur["last_seen_at"] == "2026-06-13T11:00:00Z"

    # Different recipient gets independent cursor
    assert db.get_bus_cursor(PID, "session-b") == {}


def test_check_and_record_bus_nonce_single_use(db):
    expires = "2026-06-14T00:00:00Z"
    assert db.check_and_record_bus_nonce(PID, "nonce-1", expires) is True
    # Second consumption of the same nonce is rejected
    assert db.check_and_record_bus_nonce(PID, "nonce-1", expires) is False
    # Different nonce still accepted
    assert db.check_and_record_bus_nonce(PID, "nonce-2", expires) is True


def test_set_bus_event_receipt_first_write_wins(db):
    eid = db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:00Z",
                                    "channel": "dm", "payload": {}})

    r1 = db.set_bus_event_receipt(PID, eid, "delivered", "session-recv-1")
    assert r1["already_set"] is False
    assert r1["by"] == "session-recv-1"

    # Second ack of same stage returns existing
    r2 = db.set_bus_event_receipt(PID, eid, "delivered", "session-recv-2")
    assert r2["already_set"] is True
    assert r2["by"] == "session-recv-1"
    assert r2["timestamp"] == r1["timestamp"]

    # Different stage still works independently
    r3 = db.set_bus_event_receipt(PID, eid, "opened", "session-recv-1")
    assert r3["already_set"] is False

    # Missing event
    assert db.set_bus_event_receipt(PID, "no-such", "delivered", "x") is None


def test_set_bus_event_receipt_rejects_invalid_stage(db):
    eid = db.append_bus_event(PID, {"created_at": "2026-06-13T10:00:00Z",
                                    "channel": "dm", "payload": {}})
    with pytest.raises(ValueError):
        db.set_bus_event_receipt(PID, eid, "weird-stage", "session-r")


def test_append_and_list_bus_audit_with_since(db):
    db.append_bus_audit(PID, {"received_at": "2026-06-13T10:00:00Z",
                              "verify": {"passed": True}})
    db.append_bus_audit(PID, {"received_at": "2026-06-13T10:00:01Z",
                              "verify": {"passed": False}})
    db.append_bus_audit(PID, {"received_at": "2026-06-13T10:00:02Z",
                              "verify": {"passed": True}})

    full = db.list_bus_audit(PID)
    assert len(full) == 3
    assert [r["received_at"] for r in full] == [
        "2026-06-13T10:00:00Z",
        "2026-06-13T10:00:01Z",
        "2026-06-13T10:00:02Z",
    ]

    since_filtered = db.list_bus_audit(PID, since="2026-06-13T10:00:00Z")
    assert len(since_filtered) == 2

    limited = db.list_bus_audit(PID, limit=2)
    assert len(limited) == 2


# ---------------------------------------------------------------------------
# Phase 4: sessions
# ---------------------------------------------------------------------------

def test_upsert_session_merge_semantics(db):
    db.upsert_session(PID, "sv-1", {"machine_id": "mc-aaa", "agent": "claude"})
    # Heartbeat-only update must not wipe the prior fields
    db.upsert_session(PID, "sv-1", {"last_active": "2026-06-13T10:00:00Z"})

    sessions = db.list_sessions(PID)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["session_id"] == "sv-1"
    assert s["machine_id"] == "mc-aaa"
    assert s["agent"] == "claude"
    assert s["last_active"] == "2026-06-13T10:00:00Z"


def test_list_sessions_orders_by_last_active_desc(db):
    db.upsert_session(PID, "sv-old", {"last_active": "2026-06-13T09:00:00Z"})
    db.upsert_session(PID, "sv-new", {"last_active": "2026-06-13T11:00:00Z"})
    db.upsert_session(PID, "sv-mid", {"last_active": "2026-06-13T10:00:00Z"})

    sessions = db.list_sessions(PID)
    assert [s["session_id"] for s in sessions] == ["sv-new", "sv-mid", "sv-old"]


def test_stamp_session_actor_email_preserves_siblings(db):
    db.upsert_session(PID, "sv-1", {"actor": {"machine": "mc-1", "agent": "cc"}})
    db.stamp_session_actor_email(PID, "sv-1", "alice@example.com")

    s = db.list_sessions(PID)[0]
    actor = s.get("actor", {})
    assert actor.get("email") == "alice@example.com"
    assert actor.get("machine") == "mc-1"
    assert actor.get("agent") == "cc"


def test_stamp_session_actor_email_when_actor_missing(db):
    # No prior upsert_session — DynamoDB update on missing key would normally
    # fail; the fallback path creates actor map fresh.
    db.upsert_session(PID, "sv-1", {"other_field": "x"})
    db.stamp_session_actor_email(PID, "sv-1", "bob@example.com")

    s = db.list_sessions(PID)[0]
    assert s["actor"]["email"] == "bob@example.com"


# ---------------------------------------------------------------------------
# Phase 4: machines + session minting
# ---------------------------------------------------------------------------

def test_get_or_mint_machine_idempotent_per_fingerprint(db):
    mid_1, minted_1 = db.get_or_mint_machine(UID, "fp-host-1", hostname="mac")
    assert minted_1 is True
    assert mid_1.startswith("mc-")

    mid_2, minted_2 = db.get_or_mint_machine(UID, "fp-host-1")
    assert minted_2 is False
    assert mid_2 == mid_1

    # Different fingerprint -> different machine
    mid_other, minted_other = db.get_or_mint_machine(UID, "fp-host-2")
    assert minted_other is True
    assert mid_other != mid_1


def test_list_user_machines(db):
    db.get_or_mint_machine(UID, "fp-host-1")
    db.get_or_mint_machine(UID, "fp-host-2")
    db.get_or_mint_machine("other-user", "fp-host-1")

    machines = db.list_user_machines(UID)
    assert len(machines) == 2
    assert all(m["user_id"] == UID for m in machines)


def test_get_or_mint_session_by_tuple_lookup_hit_and_miss(db):
    result_1 = db.get_or_mint_session_by_tuple(
        PID, "mc-abcd1234", 12345, user_id=UID, cwd="/some/dir"
    )
    assert result_1["minted"] is True
    sid = result_1["session_id"]
    assert sid.startswith("sv-")

    # Same tuple — lookup hit
    result_2 = db.get_or_mint_session_by_tuple(
        PID, "mc-abcd1234", 12345, user_id=UID, cwd="/other/dir"
    )
    assert result_2["minted"] is False
    assert result_2["session_id"] == sid

    # Different parent_pid — new mint
    result_3 = db.get_or_mint_session_by_tuple(
        PID, "mc-abcd1234", 67890, user_id=UID, cwd="/yet/other"
    )
    assert result_3["minted"] is True
    assert result_3["session_id"] != sid


# ---------------------------------------------------------------------------
# Phase 4: session_logs
# ---------------------------------------------------------------------------

def test_session_log_upsert_and_list(db):
    db.upsert_session_log(PID, "sv-1", {"summary": "session 1",
                                         "last_aggregated_at": "2026-06-13T09:00:00Z"})
    db.upsert_session_log(PID, "sv-2", {"summary": "session 2",
                                         "last_aggregated_at": "2026-06-13T11:00:00Z"})
    db.upsert_session_log(PID, "sv-3", {"summary": "session 3",
                                         "last_aggregated_at": "2026-06-13T10:00:00Z"})

    logs = db.list_session_logs(PID)
    assert [l["session_id"] for l in logs] == ["sv-2", "sv-3", "sv-1"]

    top1 = db.list_session_logs(PID, limit=1)
    assert top1 == logs[:1]

    one = db.get_session_log(PID, "sv-2")
    assert one and one["summary"] == "session 2"
    assert db.get_session_log(PID, "no-such") is None


def test_session_log_upsert_merges_partial_fields(db):
    db.upsert_session_log(PID, "sv-1", {"summary": "v1",
                                         "note_ids": ["n1", "n2"],
                                         "last_aggregated_at": "2026-06-13T09:00:00Z"})
    # Rescue path that only updates summary should not wipe note_ids
    db.upsert_session_log(PID, "sv-1", {"summary": "v2 (rescued)",
                                         "recovered": True})

    log = db.get_session_log(PID, "sv-1")
    assert log["summary"] == "v2 (rescued)"
    assert log["recovered"] is True
    assert log["note_ids"] == ["n1", "n2"]


# ---------------------------------------------------------------------------
# Phase 4: operation envelopes
# ---------------------------------------------------------------------------

def _make_envelope(nonce: str, **extra) -> dict:
    return {"nonce": nonce, "issuer": "user", **extra}


def test_issue_and_get_active_operation_envelope(db):
    issued = db.issue_operation_envelope(
        PID, "op-1", "spec-doc-a", "rev-1",
        _make_envelope("nonce-1"), ["beacon log"], created_by="alice"
    )
    assert issued["status"] == "active"
    assert issued["envelope_id"] == "nonce-1"

    active = db.get_active_operation_envelope(PID, "op-1")
    assert active and active["envelope_id"] == "nonce-1"

    # No active for unknown op
    assert db.get_active_operation_envelope(PID, "op-other") is None


def test_issue_auto_revokes_prior_active(db):
    db.issue_operation_envelope(
        PID, "op-1", "spec-a", "rev-1",
        _make_envelope("nonce-1"), ["a"], created_by="alice"
    )
    db.issue_operation_envelope(
        PID, "op-1", "spec-a", "rev-2",
        _make_envelope("nonce-2"), ["a"], created_by="alice"
    )

    active = db.get_active_operation_envelope(PID, "op-1")
    assert active["envelope_id"] == "nonce-2"

    prior = db.get_operation_envelope(PID, "nonce-1")
    assert prior["status"] == "revoked"
    assert prior["revoke_reason"] == "superseded by new approve"


def test_revoke_operation_envelope_idempotent(db):
    db.issue_operation_envelope(
        PID, "op-1", "spec-a", "rev-1",
        _make_envelope("nonce-1"), ["a"], created_by="alice"
    )
    r1 = db.revoke_operation_envelope(PID, "nonce-1", "alice", "test")
    assert r1["status"] == "revoked"
    assert r1["revoke_reason"] == "test"

    r2 = db.revoke_operation_envelope(PID, "nonce-1", "bob", "later")
    assert r2["status"] == "revoked"
    assert r2["revoke_reason"] == "test"  # unchanged

    assert db.revoke_operation_envelope(PID, "no-such-nonce", "x", "x") is None


def test_list_operation_envelopes_filters_and_order(db):
    db.issue_operation_envelope(
        PID, "op-1", "spec-a", "rev-1",
        _make_envelope("nonce-1"), [], created_by="alice"
    )
    # Issue a second for op-1; the first will be auto-revoked
    db.issue_operation_envelope(
        PID, "op-1", "spec-a", "rev-2",
        _make_envelope("nonce-2"), [], created_by="alice"
    )
    db.issue_operation_envelope(
        PID, "op-2", "spec-b", "rev-1",
        _make_envelope("nonce-3"), [], created_by="alice"
    )

    all_envs = db.list_operation_envelopes(PID)
    assert len(all_envs) == 3

    op1_envs = db.list_operation_envelopes(PID, op_id="op-1")
    assert len(op1_envs) == 2
    assert {e["envelope_id"] for e in op1_envs} == {"nonce-1", "nonce-2"}

    active_envs = db.list_operation_envelopes(PID, status="active")
    assert {e["envelope_id"] for e in active_envs} == {"nonce-2", "nonce-3"}

    op1_active = db.list_operation_envelopes(PID, op_id="op-1", status="active")
    assert len(op1_active) == 1
    assert op1_active[0]["envelope_id"] == "nonce-2"


# ---------------------------------------------------------------------------
# Smoke: full skeleton replacement (no NotImplementedError on any Phase 3/4 fn)
# ---------------------------------------------------------------------------

def test_no_remaining_skeleton_in_phase_3_4(db):
    import inspect
    phase_3_4_fns = [
        "append_bus_event", "get_bus_cursor", "advance_bus_cursor",
        "check_and_record_bus_nonce", "set_bus_event_receipt",
        "find_bus_event", "append_bus_audit", "list_bus_audit",
        "list_bus_events",
        "upsert_session", "stamp_session_actor_email", "list_sessions",
        "get_or_mint_machine", "get_or_mint_session_by_tuple",
        "list_user_machines",
        "upsert_session_log", "list_session_logs", "get_session_log",
        "get_active_operation_envelope", "issue_operation_envelope",
        "revoke_operation_envelope", "list_operation_envelopes",
        "get_operation_envelope",
    ]
    skeletons = []
    for fn in phase_3_4_fns:
        src = inspect.getsource(getattr(db, fn))
        if "_not_implemented" in src:
            skeletons.append(fn)
    assert not skeletons, f"Still skeletons: {skeletons}"
