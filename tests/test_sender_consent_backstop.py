"""Server-side sender consent backstop for cross-user DMs (ms-110 / e-3443).

The one choke point every send path passes: ``POST /api/projects/{p}/bus``.
This module pins the endpoint behaviour that closes the sender-side hole —
the symmetric mirror of the ms-70 receiver gate (``test_dm_gate.py``):

  (AC3-a) cross-user DM with no ``recipient_confirmed`` claim → 403.
  (AC3-b) cross-user DM via ``--no-envelope`` (no envelope at all) → 403.
  (AC3-c) cross-user DM with a valid matching claim → 200 (passes).
  (AC3-d) same-user DM with no claim → 200 (a user's own sessions).
  (AC3-e) reply (envelope.in_reply_to) cross-user → 200 (recipient derived).
  (AC3-f) shared-Trek cross-user → 200 (pre-approved scope).
  (AC3-g) non-dm broadcast cross-user → 200 (not person-directed).
  (replay) claim confirming Bob does not authorise a send to Carol → 403.

The envelope *verify* pipeline (signature / tier / nonce …) is covered by
``test_envelope_integration.py``; here it is stubbed to pass so the
assertions isolate the consent gate. What we exercise is exactly the code
path app.py runs between "verify passed" and "append_bus_event".
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import dm_consent  # noqa: E402
import firestore_client  # noqa: E402

PROJECT_ID = "test-sender-consent"

_PROJECT_DOC = {
    "name": "test",
    "milestones": [],
    "owner": "uid-alice",
    "owner_email": "alice@example.com",
    "members": [
        {"user_id": "uid-alice", "email": "alice@example.com", "role": "owner"},
        {"user_id": "uid-bob", "email": "bob@example.com", "role": "editor"},
        {"user_id": "uid-carol", "email": "carol@example.com", "role": "editor"},
    ],
}

# sender + recipient sessions with resolvable user_ids.
_SESSIONS = [
    {"session_id": "sv-alice", "user_id": "uid-alice",
     "actor": {"email": "alice@example.com"}},
    {"session_id": "sv-bob", "user_id": "uid-bob",
     "actor": {"email": "bob@example.com"}},
    {"session_id": "sv-alice2", "user_id": "uid-alice",
     "actor": {"email": "alice@example.com"}},
    {"session_id": "sv-carol", "user_id": "uid-carol",
     "actor": {"email": "carol@example.com"}},
]

_bus_store: dict[str, list[dict]] = {}
_audit_store: list[dict] = []
_event_seq = [0]
# Per-test override of what db.list_treks returns for the shared-Trek case.
_treks: list[dict] = []


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"ev-{_event_seq[0]:06d}"
    _bus_store.setdefault(project_id, []).append({"event_id": eid, **copy.deepcopy(data)})
    return eid


def _mock_list_sessions(project_id: str) -> list[dict]:
    return copy.deepcopy(_SESSIONS)


class _FakeVerify:
    """Minimal stand-in for envelope_mod.VerifyResult (always passes)."""

    rejection_reason = None
    effective_tier = "T1"
    steps: dict = {}

    def to_audit_dict(self) -> dict:
        return {}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    import app as app_module  # imported inside fixture so path is set
    import store_router as db_module

    _bus_store.clear()
    _audit_store.clear()
    _event_seq[0] = 0
    _treks.clear()

    for name, ref in (
        ("append_bus_event", _mock_append_bus_event),
        ("list_bus_events", lambda pid, **k: copy.deepcopy(_bus_store.get(pid, []))),
        ("list_sessions", _mock_list_sessions),
        ("get_project", lambda pid: copy.deepcopy(_PROJECT_DOC)),
        ("save_project", lambda pid, data: None),
        ("list_projects", lambda: []),
        ("append_bus_audit", lambda pid, rec: (_audit_store.append(rec), "audit-1")[1]),
        ("put_bus_event_approval", lambda *a, **k: {"approval_status": "pending"}),
        ("append_decision_event", lambda *a, **k: "dec-1"),
        ("list_treks", lambda *a, **k: copy.deepcopy(_treks)),
        ("find_bus_event", lambda pid, eid: None),
        ("get_bus_cursor", lambda pid, rid: {}),
        ("check_and_record_bus_nonce", lambda *a, **k: True),
        ("set_bus_event_receipt", lambda *a, **k: None),
        ("get_or_create_user", lambda *a, **k: None),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)

    # Isolate the consent gate: stub the envelope verify pipeline to pass.
    monkeypatch.setattr(app_module.envelope_mod, "verify", lambda *a, **k: _FakeVerify())
    monkeypatch.setattr(app_module.envelope_mod, "validate_t5_payload", lambda p: None)
    monkeypatch.setattr(
        app_module.envelope_mod, "decide_delivery", lambda **k: "propose-to-ai")
    monkeypatch.setattr(app_module, "_start_watcher", lambda project_id: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda project_id: None)
    monkeypatch.setattr(app_module, "_auth_enabled", False)

    async def _noop_fanout(project_id, event):
        return None

    monkeypatch.setattr(app_module, "_fanout_bus_event", _noop_fanout)
    return app_module


def _post(app_module, *, sender_sid, recipient_sid, channel="dm",
          envelope="__default__", text="hi"):
    from fastapi.testclient import TestClient

    payload = {"text": text}
    if recipient_sid:
        payload["recipient_session_id"] = recipient_sid
    body = {"channel": channel, "sender_session_id": sender_sid, "payload": payload}
    if envelope != "__no_envelope__":
        if envelope == "__default__":
            envelope = {"tier": "T1", "actions_authorized": []}
        body["envelope"] = envelope
    client = TestClient(app_module.app)
    return client.post(f"/api/projects/{PROJECT_ID}/bus", json=body)


def _claim_for(session_id, user_id):
    return dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_session_id=session_id,
        recipient_user_id=user_id,
    )


# ---------------------------------------------------------------------------
# AC3-a / AC3-b: cross-user without a valid claim is rejected
# ---------------------------------------------------------------------------

def test_cross_user_without_claim_rejected(wired):
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-bob")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "sender_consent_required"
    assert detail["reason"] == dm_consent.SEND_DENY_MISSING_CONFIRMATION


def test_cross_user_no_envelope_rejected(wired):
    """--no-envelope carries no consent claim → cross-user must be rejected."""
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-bob",
                 envelope="__no_envelope__")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "sender_consent_required"


# ---------------------------------------------------------------------------
# AC3-c: cross-user WITH a valid matching claim passes
# ---------------------------------------------------------------------------

def test_cross_user_with_matching_claim_passes(wired):
    env = {"tier": "T1", "actions_authorized": [],
           "recipient_confirmed": _claim_for("sv-bob", "uid-bob")}
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-bob", envelope=env)
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_id"]


def test_cross_user_with_mismatched_claim_rejected(wired):
    """Replay guard: a claim confirming Bob must not authorise a send to Carol."""
    env = {"tier": "T1", "actions_authorized": [],
           "recipient_confirmed": _claim_for("sv-bob", "uid-bob")}
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-carol", envelope=env)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["reason"] == dm_consent.SEND_DENY_RECIPIENT_MISMATCH


# ---------------------------------------------------------------------------
# AC3-d / e / f / g: carve-outs pass without a claim (no regression)
# ---------------------------------------------------------------------------

def test_same_user_passes_without_claim(wired):
    """A user's own two sessions talking → no confirmation needed."""
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-alice2")
    assert resp.status_code == 200, resp.text


def test_reply_cross_user_passes_without_claim(wired):
    """Reply (envelope.in_reply_to) recipient is derived from the parent."""
    env = {"tier": "T3", "actions_authorized": [], "in_reply_to": "ev-parent"}
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-bob", envelope=env)
    assert resp.status_code == 200, resp.text


def test_shared_trek_cross_user_passes_without_claim(wired):
    """Cross-user but both in the same active Trek → pre-approved scope."""
    _treks.append({
        "trek_id": "tk-shared",
        "status": "active",
        "creator_actor": {"user_id": "uid-alice"},
        "members": [
            {"user_id": "uid-alice"},
            {"user_id": "uid-bob"},
        ],
    })
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-bob")
    assert resp.status_code == 200, resp.text


def test_non_dm_broadcast_cross_user_passes(wired):
    """A cross-user broadcast on a non-dm channel is out of scope (ms-55)."""
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="",
                 channel="claim-signal")
    assert resp.status_code == 200, resp.text


def test_operation_envelope_cross_user_passes(wired):
    """T1-system (Trek scheduler) / T2 (Operation) envelopes are pre-approved."""
    env = {"tier": "T1-system", "issuer": "beacon-system", "actions_authorized": []}
    resp = _post(wired, sender_sid="sv-alice", recipient_sid="sv-bob", envelope=env)
    assert resp.status_code == 200, resp.text
