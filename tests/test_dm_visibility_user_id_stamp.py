"""DM payload visibility relies on session.user_id (ms-95 fix).

Regression captured on 2026-07-06: Iruka's Windows bridge (agent v0.54.0)
registered its session without a ``user_id`` field, so
``_apply_dm_payload_visibility`` resolved ``sid_to_uid["8d9b818f-..."] = ""``,
which failed to match her authenticated caller uid — her own inbound DM's
``payload.text`` was redacted from her own receiver-side AI.

Two structural fixes:

  * **Root cause**: PUT ``/api/projects/{p}/sessions/{sid}`` now stamps the
    JWT ``sub`` (= user_id) on the session row unconditionally. The bridge
    was never supposed to supply this itself — it lives on the auth token
    the server already trusts.

  * **Defense-in-depth**: ``_apply_dm_payload_visibility`` falls back to
    ``session.actor.email → project.members[].user_id`` when the primary
    ``sid_to_uid`` lookup returns empty. actor.email is server-stamped from
    the same JWT and cannot be spoofed by the bridge, so legacy rows written
    before the root-cause fix landed still resolve correctly on read.

These tests pin both fixes independently: the endpoint test proves fresh
mints get user_id, and the visibility test proves legacy rows (without
user_id but with actor.email) still let the intended recipient see the
body.
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

_bus_store: dict[str, list[dict]] = {}
_sessions_store: dict[str, list[dict]] = {}
_event_seq = [0]


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"vis-{_event_seq[0]:06d}"
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


def _mock_list_sessions(project_id: str) -> list[dict]:
    return copy.deepcopy(_sessions_store.get(project_id, []))


def _mock_upsert_session(project_id: str, session_id: str,
                         data: dict) -> None:
    bucket = _sessions_store.setdefault(project_id, [])
    for i, s in enumerate(bucket):
        if s.get("session_id") == session_id:
            bucket[i] = {**s, **data, "session_id": session_id}
            return
    bucket.append({"session_id": session_id, **copy.deepcopy(data)})


def _mock_stamp_session_actor_email(project_id: str, session_id: str,
                                    email: str) -> None:
    if not email:
        return
    bucket = _sessions_store.setdefault(project_id, [])
    for i, s in enumerate(bucket):
        if s.get("session_id") == session_id:
            actor = dict(s.get("actor") or {})
            actor["email"] = email
            bucket[i] = {**s, "actor": actor}
            return
    bucket.append({
        "session_id": session_id,
        "actor": {"email": email},
    })


PROJECT_ID = "test-vis-user-id"

_PROJECT_DOC = {
    "name": "test",
    "milestones": [],
    "owner": "uid-alice",
    "owner_email": "alice@example.com",
    "members": [
        {"user_id": "uid-alice", "email": "alice@example.com", "role": "owner"},
        {"user_id": "uid-bob",   "email": "bob@example.com",   "role": "editor"},
        {"user_id": "uid-carol", "email": "carol@example.com", "role": "editor"},
    ],
}


firestore_client.append_bus_event = _mock_append_bus_event
firestore_client.list_bus_events = _mock_list_bus_events
firestore_client.list_sessions = _mock_list_sessions
firestore_client.upsert_session = _mock_upsert_session
firestore_client.stamp_session_actor_email = _mock_stamp_session_actor_email
firestore_client.get_project = lambda pid: copy.deepcopy(_PROJECT_DOC)
firestore_client.save_project = lambda pid, data: None
firestore_client.list_projects = lambda: []
firestore_client.find_bus_event = lambda pid, eid: next(
    (e for e in _bus_store.get(pid, []) if e.get("event_id") == eid),
    None,
)
firestore_client.get_bus_cursor = lambda pid, rid: {}
firestore_client.check_and_record_bus_nonce = lambda *a, **kw: True
firestore_client.append_bus_audit = lambda pid, rec: "audit-1"
firestore_client.set_bus_event_receipt = lambda *a, **kw: None
firestore_client.get_or_create_user = lambda *a, **kw: None


from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import store_router as db_module  # noqa: E402


app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None


@pytest.fixture(autouse=True)
def reset_store(monkeypatch):
    for name, ref in (
        ("append_bus_event", _mock_append_bus_event),
        ("list_bus_events", _mock_list_bus_events),
        ("list_sessions", _mock_list_sessions),
        ("upsert_session", _mock_upsert_session),
        ("stamp_session_actor_email", _mock_stamp_session_actor_email),
        ("get_project", lambda pid: copy.deepcopy(_PROJECT_DOC)),
        ("save_project", lambda pid, data: None),
        ("find_bus_event", lambda pid, eid: next(
            (e for e in _bus_store.get(pid, []) if e.get("event_id") == eid),
            None,
        )),
        ("get_bus_cursor", lambda pid, rid: {}),
        ("check_and_record_bus_nonce", lambda *a, **kw: True),
        ("append_bus_audit", lambda pid, rec: "audit-1"),
        ("set_bus_event_receipt", lambda *a, **kw: None),
        ("get_or_create_user", lambda *a, **kw: None),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)
    _bus_store.clear()
    _sessions_store.clear()
    _event_seq[0] = 0
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    # ms-110 / e-3443: these tests post cross-user plain DMs directly as a
    # fixture to exercise payload *visibility* (redaction), a separate
    # subsystem. The ms-110 sender-consent backstop would (correctly) 403
    # those unconfirmed cross-user posts; bypass it here so this file keeps
    # testing visibility. The backstop itself is covered in
    # test_sender_consent_backstop.py.
    monkeypatch.setattr(
        app_module.dm_consent_mod, "classify_send_consent",
        lambda **k: (False, "visibility-test-bypass"),
    )
    yield
    _bus_store.clear()
    _sessions_store.clear()


def _stub_token(monkeypatch, user_id: str, email: str = "") -> None:
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": user_id, "email": email or f"{user_id}@example.com"},
    )


def _post_dm(sender_sid: str, recipient_sid: str, text: str) -> str:
    saved = app_module._auth_enabled
    app_module._auth_enabled = False
    try:
        client = TestClient(app_module.app)
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


# ---------------------------------------------------------------------------
# Fix 1: upsert_session endpoint stamps user_id from JWT sub
# ---------------------------------------------------------------------------

def test_upsert_session_stamps_user_id_from_jwt_sub(monkeypatch):
    """A fresh mint call (bridge sends actor + last_active, no user_id)
    lands with user_id = the caller's JWT sub. Without this, the DM
    visibility gate cannot resolve sid → uid on subsequent reads."""
    _stub_token(monkeypatch, "uid-bob", email="bob@example.com")
    client = TestClient(app_module.app)
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/sess-B",
        json={
            "actor": {"machine": "WORKMACHINE", "agent": "bridge-v54"},
            "last_active": "2026-07-06T09:00:00Z",
        },
        headers={"Authorization": "Bearer t-bob"},
    )
    assert resp.status_code == 200, resp.text

    stored = [s for s in _sessions_store[PROJECT_ID] if s["session_id"] == "sess-B"]
    assert stored, "sess-B not written to registry"
    assert stored[0]["user_id"] == "uid-bob", (
        f"user_id not stamped: got {stored[0].get('user_id')!r}, "
        "expected 'uid-bob' (JWT sub)"
    )
    # Email stamp still lands via the existing mint path.
    assert stored[0]["actor"]["email"] == "bob@example.com"


def test_upsert_session_stamps_user_id_on_heartbeat(monkeypatch):
    """A heartbeat-only body (just last_active) also stamps user_id.
    Idempotent: repeat heartbeats overwrite with the same value."""
    _sessions_store[PROJECT_ID] = [
        {"session_id": "sess-B", "actor": {"machine": "WORKMACHINE"}},
    ]
    _stub_token(monkeypatch, "uid-bob", email="bob@example.com")
    client = TestClient(app_module.app)
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/sess-B",
        json={"last_active": "2026-07-06T09:05:00Z"},
        headers={"Authorization": "Bearer t-bob"},
    )
    assert resp.status_code == 200, resp.text

    stored = [s for s in _sessions_store[PROJECT_ID] if s["session_id"] == "sess-B"]
    assert stored[0]["user_id"] == "uid-bob"


# ---------------------------------------------------------------------------
# Fix 2: visibility gate falls back to actor.email → member.user_id
# ---------------------------------------------------------------------------

def test_recipient_sees_dm_payload_when_registry_missing_user_id(monkeypatch):
    """Legacy row: sess-B was minted before the root-cause fix landed, so
    its ``user_id`` field is empty. But ``actor.email`` was still server-
    stamped, and matches Bob's member email. The visibility gate must
    resolve via email → member fallback and NOT redact Bob's inbound DM."""
    _sessions_store[PROJECT_ID] = [
        {"session_id": "sess-A", "user_id": "uid-alice",
         "actor": {"email": "alice@example.com"}},
        # Legacy — no user_id, only email.
        {"session_id": "sess-B", "user_id": "",
         "actor": {"email": "bob@example.com"}},
    ]
    _post_dm("sess-A", "sess-B", "before the mint fix landed")

    _stub_token(monkeypatch, "uid-bob", email="bob@example.com")
    client = TestClient(app_module.app)
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=dm",
        headers={"Authorization": "Bearer t-bob"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload.get("text") == "before the mint fix landed", (
        f"payload was redacted despite email-fallback match: {payload!r}"
    )


def test_third_party_still_redacted_when_registry_missing_user_id(monkeypatch):
    """The fallback must be recipient-scoped, not a blanket bypass. Carol
    (project member but not a party to Alice→Bob's DM) must still see the
    redaction marker even when sess-B has no user_id."""
    _sessions_store[PROJECT_ID] = [
        {"session_id": "sess-A", "user_id": "uid-alice",
         "actor": {"email": "alice@example.com"}},
        {"session_id": "sess-B", "user_id": "",
         "actor": {"email": "bob@example.com"}},
        {"session_id": "sess-C", "user_id": "uid-carol",
         "actor": {"email": "carol@example.com"}},
    ]
    _post_dm("sess-A", "sess-B", "not for carol")

    _stub_token(monkeypatch, "uid-carol", email="carol@example.com")
    client = TestClient(app_module.app)
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=dm",
        headers={"Authorization": "Bearer t-carol"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload.get("redacted") is True, (
        f"third-party payload leaked through fallback: {payload!r}"
    )
    assert payload.get("reason") == "dm_payload_visibility"


def test_recipient_without_email_stamp_still_redacted(monkeypatch):
    """Boundary: if actor.email is also missing (= session never touched
    an authenticated endpoint), the fallback has nothing to work with and
    the visibility gate must remain closed. This preserves the
    fail-closed guarantee for genuinely un-attributable sessions."""
    _sessions_store[PROJECT_ID] = [
        {"session_id": "sess-A", "user_id": "uid-alice"},
        # No user_id AND no actor.email.
        {"session_id": "sess-B"},
    ]
    _post_dm("sess-A", "sess-B", "cannot verify recipient")

    _stub_token(monkeypatch, "uid-bob", email="bob@example.com")
    client = TestClient(app_module.app)
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/bus?channel=dm",
        headers={"Authorization": "Bearer t-bob"},
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["payload"].get("redacted") is True
