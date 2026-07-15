"""DM payload visibility boundary on bus read endpoints (ms-93 / e-2275).

Without server-side enforcement, every project member who can call
``GET /api/projects/{id}/bus`` (or its single-event / unread siblings)
sees the full ``payload`` of every DM in the project — including DMs
they were neither sender nor recipient of. This is a visibility bug
distinct from the routing fan-out fix in e-1209: routing decides who
gets a push, visibility decides what shows up when a member explicitly
reads the bus.

The fix in ``server/app.py`` resolves sender / recipient user_ids from
the project session registry and, for DM-channel events, strips
``payload`` from the response when the caller is neither party.
Sidecar metadata (event_id, channel, sender_session_id, created_at,
receipt timestamps, envelope view) stays visible so audit tooling
keeps working.

These tests pin three invariants:

  1. Sender sees the payload (= they wrote it).
  2. Recipient sees the payload (= it's addressed to them).
  3. Third-party project member sees the sidecar but NOT the payload.

Plus boundary checks:

  * Non-DM channels (broadcast) are untouched.
  * Single-event endpoint ``GET /bus/{event_id}`` applies the same rule.
  * ``GET /bus/unread`` applies the same rule for cross-recipient reads.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import firestore_client  # noqa: E402

# In-memory stores mirror the Firestore subcollections.
_bus_store: dict[str, list[dict]] = {}
_sessions_store: dict[str, list[dict]] = {}
_cursor_store: dict[tuple[str, str], dict] = {}
_audit_store: dict[str, list[dict]] = {}
_nonce_store: dict[tuple[str, str], bool] = {}
_event_seq = [0]


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"e2e-{_event_seq[0]:06d}"
    bucket = _bus_store.setdefault(project_id, [])
    bucket.append({"event_id": eid, **copy.deepcopy(data)})
    return eid


def _mock_list_bus_events(project_id: str, since: str = "", channel: str = "",
                          limit: int = 100) -> list[dict]:
    items = list(_bus_store.get(project_id, []))
    items.sort(key=lambda e: e.get("created_at", ""))
    if since:
        items = [e for e in items if e.get("created_at", "") > since]
    if channel:
        items = [e for e in items if e.get("channel") == channel]
    if limit:
        items = items[:limit]
    return copy.deepcopy(items)


def _mock_find_bus_event(project_id: str, event_id: str) -> dict | None:
    for ev in _bus_store.get(project_id, []):
        if ev.get("event_id") == event_id:
            return copy.deepcopy(ev)
    return None


def _mock_list_sessions(project_id: str) -> list[dict]:
    return copy.deepcopy(_sessions_store.get(project_id, []))


def _mock_get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    return copy.deepcopy(_cursor_store.get((project_id, recipient_id), {}))


def _mock_check_and_record_bus_nonce(*a, **kw) -> bool:
    return True


def _mock_append_bus_audit(project_id: str, record: dict) -> str:
    bucket = _audit_store.setdefault(project_id, [])
    audit_id = f"audit-{len(bucket) + 1:06d}"
    bucket.append({"audit_id": audit_id, **copy.deepcopy(record)})
    return audit_id


def _mock_set_bus_event_receipt(*a, **kw):
    return None


def _mock_get_or_create_user(*a, **kw) -> None:
    return None


# Patch firestore_client BEFORE importing app: store_router does
# `from firestore_client import list_sessions, ...` at app-import time,
# and that grabs whichever object firestore_client.<name> points at right
# then. If we patched only after `import app`, store_router would snapshot
# the real Firestore stub and our fixture-level monkeypatch on
# firestore_client.* would never reach the code path inside app.py.
#
# We mirror the same pattern test_bus_transport.py uses (= patch the
# functions the bus paths reach), so that store_router's `from X import Y`
# resolves to our mocks for *every* sibling test module that imports app
# after us. Per-test refinement (auth toggling, seeded sessions, etc.)
# still happens via the autouse fixture below.
firestore_client.append_bus_event = _mock_append_bus_event
firestore_client.list_bus_events = _mock_list_bus_events
firestore_client.find_bus_event = _mock_find_bus_event
firestore_client.list_sessions = _mock_list_sessions
firestore_client.get_bus_cursor = _mock_get_bus_cursor
firestore_client.check_and_record_bus_nonce = _mock_check_and_record_bus_nonce
firestore_client.append_bus_audit = _mock_append_bus_audit
firestore_client.set_bus_event_receipt = _mock_set_bus_event_receipt
firestore_client.get_or_create_user = _mock_get_or_create_user
# These keep app's _load() path hitting our in-memory project rather than
# real Firestore — store_router snapshots them too at app-import time.
firestore_client.get_project = lambda pid: {
    "name": "test",
    "milestones": [],
    "owner": "uid-alice",
    "members": [
        {"user_id": "uid-alice", "role": "owner"},
        {"user_id": "uid-bob", "role": "editor"},
        {"user_id": "uid-carol", "role": "editor"},
    ],
}
firestore_client.save_project = lambda pid, data: None
firestore_client.list_projects = lambda: []


from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import store_router as db_module  # noqa: E402


# Auth is enabled for these tests — the visibility boundary only matters
# when there is a distinguishable caller identity to gate on.
app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)

PROJECT_ID = "test-payload-visibility"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_store(monkeypatch):
    _get_project = lambda pid: {
        "name": "test",
        "milestones": [],
        "owner": "uid-alice",
        "members": [
            {"user_id": "uid-alice", "role": "owner"},
            {"user_id": "uid-bob", "role": "editor"},
            {"user_id": "uid-carol", "role": "editor"},
        ],
    }
    # Patch firestore_client (= surface most modules touch) AND db_module
    # (= store_router's snapshot, which is what app.py's `db.*` calls
    # resolve to). monkeypatch.setattr auto-restores on teardown so we
    # never leak into test_bus_transport.py or other sibling modules.
    for name, ref in (
        ("append_bus_event", _mock_append_bus_event),
        ("list_bus_events", _mock_list_bus_events),
        ("find_bus_event", _mock_find_bus_event),
        ("list_sessions", _mock_list_sessions),
        ("get_bus_cursor", _mock_get_bus_cursor),
        ("check_and_record_bus_nonce", _mock_check_and_record_bus_nonce),
        ("append_bus_audit", _mock_append_bus_audit),
        ("set_bus_event_receipt", _mock_set_bus_event_receipt),
        ("get_or_create_user", _mock_get_or_create_user),
        ("get_project", _get_project),
        ("save_project", lambda pid, data: None),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)
    _bus_store.clear()
    _sessions_store.clear()
    _cursor_store.clear()
    _audit_store.clear()
    _nonce_store.clear()
    _event_seq[0] = 0
    # Seed the session registry: Alice owns sess-A, Bob owns sess-B,
    # Carol owns sess-C.
    _sessions_store[PROJECT_ID] = [
        {"session_id": "sess-A", "user_id": "uid-alice"},
        {"session_id": "sess-B", "user_id": "uid-bob"},
        {"session_id": "sess-C", "user_id": "uid-carol"},
    ]
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    # ms-110 / e-3443: these tests post cross-user DMs directly to exercise
    # payload visibility (redaction). The sender-consent backstop would 403
    # those unconfirmed cross-user posts; bypass it here. The backstop is
    # covered in test_sender_consent_backstop.py.
    monkeypatch.setattr(
        app_module.dm_consent_mod, "classify_send_consent",
        lambda **k: (False, "visibility-test-bypass"),
    )
    yield
    _bus_store.clear()
    _sessions_store.clear()
    _cursor_store.clear()
    _audit_store.clear()
    _nonce_store.clear()


def _stub_token(monkeypatch, user_id: str, email: str = "") -> None:
    """Stub the bearer-token verifier so the next request runs as ``user_id``."""
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": user_id, "email": email or f"{user_id}@example.com"},
    )


def _post_dm(sender_sid: str, recipient_sid: str, text: str) -> str:
    """Post a DM via the bus endpoint as a sender (auth disabled for this
    helper). Returns the event_id.
    """
    # Temporarily disable auth so the seed POST doesn't require a token.
    saved = app_module._auth_enabled
    app_module._auth_enabled = False
    try:
        resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
            "channel": "dm",
            "sender_session_id": sender_sid,
            "payload": {
                "recipient_session_id": recipient_sid,
                "text": text,
            },
        })
        assert resp.status_code == 200, resp.text
        return resp.json()["event_id"]
    finally:
        app_module._auth_enabled = saved


def _post_broadcast(sender_sid: str, text: str) -> str:
    saved = app_module._auth_enabled
    app_module._auth_enabled = False
    try:
        resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json={
            "channel": "notify",
            "sender_session_id": sender_sid,
            "payload": {"text": text},
        })
        assert resp.status_code == 200, resp.text
        return resp.json()["event_id"]
    finally:
        app_module._auth_enabled = saved


# ---------------------------------------------------------------------------
# AC1: sender sees payload, recipient sees payload, third party does not
# ---------------------------------------------------------------------------

def test_sender_sees_dm_payload_via_list(monkeypatch):
    """Alice (= sender, sess-A) calls GET /bus and sees the body of the DM
    she sent to Bob. This is the baseline: never hide a sender's own outbox
    text from them."""
    _post_dm("sess-A", "sess-B", "lunch at 12?")
    _stub_token(monkeypatch, "uid-alice")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=dm",
        headers={"Authorization": "Bearer t-alice"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "lunch at 12?"
    assert events[0]["payload"]["recipient_session_id"] == "sess-B"


def test_recipient_sees_dm_payload_via_list(monkeypatch):
    """Bob (= recipient, sess-B) calls GET /bus and sees the body of the DM
    Alice sent to him."""
    _post_dm("sess-A", "sess-B", "lunch at 12?")
    _stub_token(monkeypatch, "uid-bob")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=dm",
        headers={"Authorization": "Bearer t-bob"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "lunch at 12?"


def test_third_party_payload_redacted_but_sidecar_visible(monkeypatch):
    """Carol (= project member, not party to the DM) calls GET /bus.

    AC1: she MUST NOT see the body of Alice→Bob's DM.
    AC2: she MUST still see the sidecar (event_id, channel,
    sender_session_id, created_at, delivery) — that's the boundary.
    """
    event_id = _post_dm("sess-A", "sess-B", "lunch at 12?")
    _stub_token(monkeypatch, "uid-carol")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=dm",
        headers={"Authorization": "Bearer t-carol"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    ev = events[0]
    # AC1: payload body must be gone — only the redaction marker remains.
    assert ev["payload"] == {
        "redacted": True,
        "reason": "dm_payload_visibility",
    }
    # AC2: sidecar metadata is fully visible.
    assert ev["event_id"] == event_id
    assert ev["channel"] == "dm"
    assert ev["sender_session_id"] == "sess-A"
    assert ev.get("created_at")  # server-stamped
    assert "delivery" in ev


def test_third_party_payload_redacted_on_single_event_endpoint(monkeypatch):
    """GET /bus/{event_id} must apply the same redaction. The 3-stage
    receipt display (sent / delivered / opened) is sidecar — non-parties
    can still see it, but never the body."""
    event_id = _post_dm("sess-A", "sess-B", "secret plan")
    _stub_token(monkeypatch, "uid-carol")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/{event_id}",
        headers={"Authorization": "Bearer t-carol"},
    )
    assert resp.status_code == 200, resp.text
    ev = resp.json()
    assert ev["payload"] == {
        "redacted": True,
        "reason": "dm_payload_visibility",
    }
    # Sidecar still present.
    assert ev["event_id"] == event_id
    assert ev["channel"] == "dm"


def test_sender_sees_dm_payload_on_single_event_endpoint(monkeypatch):
    """Sender can read their own DM body via GET /bus/{event_id}."""
    event_id = _post_dm("sess-A", "sess-B", "secret plan")
    _stub_token(monkeypatch, "uid-alice")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/{event_id}",
        headers={"Authorization": "Bearer t-alice"},
    )
    assert resp.status_code == 200, resp.text
    ev = resp.json()
    assert ev["payload"]["text"] == "secret plan"


# ---------------------------------------------------------------------------
# AC2 boundary: non-DM channels are untouched
# ---------------------------------------------------------------------------

def test_broadcast_channel_payload_remains_visible_to_all_members(monkeypatch):
    """Non-DM channels (notify / ops / etc.) have no privacy contract —
    the redaction MUST NOT bleed into broadcast traffic. If it did, every
    broadcast announcement would look empty to everyone but the sender."""
    _post_broadcast("sess-A", "deploy starting")
    _stub_token(monkeypatch, "uid-carol")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=notify",
        headers={"Authorization": "Bearer t-carol"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    # Body still visible to Carol because notify is not a DM channel.
    assert events[0]["payload"]["text"] == "deploy starting"


def test_mixed_channel_list_only_redacts_dm_for_non_party(monkeypatch):
    """A bus listing that mixes DM + broadcast events should redact only
    the DM bodies the caller isn't a party to, never the broadcast bodies."""
    dm_eid = _post_dm("sess-A", "sess-B", "private note")
    bc_eid = _post_broadcast("sess-A", "team-wide announcement")
    _stub_token(monkeypatch, "uid-carol")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus",
        headers={"Authorization": "Bearer t-carol"},
    )
    assert resp.status_code == 200, resp.text
    by_id = {e["event_id"]: e for e in resp.json()}
    assert by_id[dm_eid]["payload"] == {
        "redacted": True,
        "reason": "dm_payload_visibility",
    }
    assert by_id[bc_eid]["payload"]["text"] == "team-wide announcement"


# ---------------------------------------------------------------------------
# Cross-recipient /bus/unread read: payload still redacted for non-parties
# ---------------------------------------------------------------------------

def test_unread_redacts_when_caller_not_party(monkeypatch):
    """Carol queries /bus/unread for sess-A (= not her session). Routing
    delivers nothing to sess-A (the DM is addressed to sess-B), but if she
    queried sess-B as recipient_id the routing filter would let her pull
    the body. The visibility gate catches that: even when a non-party
    fishes for someone else's inbox, the body stays redacted."""
    _post_dm("sess-A", "sess-B", "private")
    _stub_token(monkeypatch, "uid-carol")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=sess-B&channel=dm",
        headers={"Authorization": "Bearer t-carol"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["payload"] == {
        "redacted": True,
        "reason": "dm_payload_visibility",
    }


def test_unread_returns_payload_when_caller_is_recipient(monkeypatch):
    """The standard inbox-poll path: Bob asks for sess-B's unread, and the
    DM addressed to him comes back with the body intact."""
    _post_dm("sess-A", "sess-B", "for your eyes only")
    _stub_token(monkeypatch, "uid-bob")
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus/unread?recipient_id=sess-B&channel=dm",
        headers={"Authorization": "Bearer t-bob"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "for your eyes only"
