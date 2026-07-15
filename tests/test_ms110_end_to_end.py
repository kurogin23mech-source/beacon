"""ms-110 end-to-end: real CLI send → real server backstop (the seam).

The unit tests cover the two halves separately: ``test_cmd_bus_send_consent``
proves the CLI stamps a ``recipient_confirmed`` claim, and
``test_sender_consent_backstop`` proves the server gate reads one. This test
joins them — it drives the **actual** ``commands.cmd_bus_send`` (which builds
the envelope + claim exactly as the shipped CLI does) and routes its POST into
the **actual** ``app.post_bus_event`` backstop via a bridge client. That
verifies the claim the CLI produces is the claim the server accepts — the seam
a field-name / key mismatch would silently break.

The scenario replays the original accident and its fix:

  1. AI fires a raw cross-user DM (no /beacon-dm-send, no claim) → server 403.
  2. A user's own two sessions DM each other (same-user)             → 200.
  3. The /beacon-dm-send path (--recipient-confirmed) cross-user     → 200.
  4. A claim confirming Bob reused to send to Carol (replay)         → 403.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import firestore_client  # noqa: E402

PROJECT_ID = "proj-e2e"

_PROJECT_DOC = {
    "name": "e2e", "milestones": [], "owner": "uid-alice",
    "owner_email": "alice@example.com",
    "members": [
        {"user_id": "uid-alice", "email": "alice@example.com", "role": "owner"},
        {"user_id": "uid-bob", "email": "bob@example.com", "role": "editor"},
        {"user_id": "uid-carol", "email": "carol@example.com", "role": "editor"},
    ],
}
_SESSIONS = [
    {"session_id": "sv-alice", "user_id": "uid-alice", "actor": {"email": "alice@example.com"}},
    {"session_id": "sv-alice2", "user_id": "uid-alice", "actor": {"email": "alice@example.com"}},
    {"session_id": "sv-bob", "user_id": "uid-bob", "actor": {"email": "bob@example.com"}},
    {"session_id": "sv-carol", "user_id": "uid-carol", "actor": {"email": "carol@example.com"}},
]

_bus: dict[str, list[dict]] = {}
_seq = [0]


def _append(project_id, data):
    _seq[0] += 1
    eid = f"e2e-{_seq[0]:04d}"
    _bus.setdefault(project_id, []).append({"event_id": eid, **copy.deepcopy(data)})
    return eid


class _FakeVerify:
    rejection_reason = None
    effective_tier = "T1"
    steps: dict = {}

    def to_audit_dict(self):
        return {}


class _BridgeClient:
    """CLI-side ApiClient stand-in that forwards post to the in-process server.

    ``issue_bus_envelope`` returns a stub envelope (server verify is stubbed);
    ``post_bus_event`` POSTs to the real FastAPI handler and mimics
    api_client's ``RuntimeError('API error <code>: ...')`` on non-200 so the
    CLI's own error path runs unchanged.
    """

    def __init__(self, testclient):
        self.tc = testclient

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized=None,
                           scope=None, data_class="free", conversation_id=None,
                           in_reply_to=None, chain_depth=0, ttl_seconds=3600):
        return {"tier": tier, "actions_authorized": list(actions_authorized or []),
                "data_class": data_class, "signature": "stub-sig"}

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai", envelope=None,
                       requested_action=None, context="", rationale=""):
        resp = self.tc.post(f"/api/projects/{project_id}/bus", json={
            "channel": channel, "sender_session_id": sender_session_id,
            "payload": payload or {}, "delivery": delivery, "envelope": envelope,
            "requested_action": requested_action, "context": context,
            "rationale": rationale,
        })
        if resp.status_code != 200:
            raise RuntimeError(f"API error {resp.status_code}: {resp.text}")
        return resp.json()


@pytest.fixture
def send(monkeypatch):
    """Return a helper that runs the real cmd_bus_send against the real server."""
    import app as app_module
    import store_router as db_module
    from fastapi.testclient import TestClient

    _bus.clear()
    _seq[0] = 0

    for name, ref in (
        ("append_bus_event", _append),
        ("list_sessions", lambda pid: copy.deepcopy(_SESSIONS)),
        ("get_project", lambda pid: copy.deepcopy(_PROJECT_DOC)),
        ("save_project", lambda pid, d: None),
        ("append_bus_audit", lambda pid, rec: "audit"),
        ("put_bus_event_approval", lambda *a, **k: {"approval_status": "pending"}),
        ("append_decision_event", lambda *a, **k: "dec"),
        ("list_treks", lambda *a, **k: []),
        ("find_bus_event", lambda pid, eid: None),
        ("check_and_record_bus_nonce", lambda *a, **k: True),
        ("set_bus_event_receipt", lambda *a, **k: None),
        ("get_or_create_user", lambda *a, **k: None),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)

    monkeypatch.setattr(app_module.envelope_mod, "verify", lambda *a, **k: _FakeVerify())
    monkeypatch.setattr(app_module.envelope_mod, "validate_t5_payload", lambda p: None)
    monkeypatch.setattr(app_module.envelope_mod, "decide_delivery", lambda **k: "propose-to-ai")
    monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_auth_enabled", False)
    # e-3492: gate is behind a kill-switch (default OFF); enable for these tests.
    monkeypatch.setenv("BEACON_SENDER_CONSENT_ENABLED", "1")

    async def _noop(pid, ev):
        return None

    monkeypatch.setattr(app_module, "_fanout_bus_event", _noop)

    bridge = _BridgeClient(TestClient(app_module.app))
    monkeypatch.setattr(commands, "_get_api_client",
                        lambda: (bridge, {"project_id": PROJECT_ID}))

    def _do(*, to, sender="sv-alice", sender_uid="uid-alice", confirmed=False):
        for k in ("BEACON_BUS_CHANNEL", "BEACON_BUS_PAYLOAD", "BEACON_BUS_SENDER",
                  "BEACON_BUS_RECIPIENT_SESSION", "BEACON_BUS_RECIPIENT_USER",
                  "BEACON_BUS_IN_REPLY_TO", "BEACON_BUS_ACTION",
                  "BEACON_BUS_NO_ENVELOPE", "BEACON_BUS_RECIPIENT_CONFIRMED",
                  "BEACON_BUS_PROJECT_ID", "BEACON_JSON"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("BEACON_USER_ID", sender_uid)
        monkeypatch.setenv("BEACON_USER_EMAIL", f"{sender_uid}@example.com")
        monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
        monkeypatch.setenv("BEACON_BUS_SENDER", sender)
        monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", to)
        monkeypatch.setenv("BEACON_BUS_PAYLOAD", '{"text":"hello"}')
        if confirmed:
            monkeypatch.setenv("BEACON_BUS_RECIPIENT_CONFIRMED", "1")
        commands.cmd_bus_send()

    return _do


# ---------------------------------------------------------------------------
# The four scenarios
# ---------------------------------------------------------------------------

def test_raw_cross_user_send_is_blocked(send):
    """The original accident: AI fires a cross-user DM directly, no claim."""
    with pytest.raises(RuntimeError) as ei:
        send(to="sv-bob")
    assert "API error 403" in str(ei.value)
    assert "sender_consent_required" in str(ei.value)


def test_same_user_send_passes(send):
    """A user's own two sessions need no confirmation."""
    send(to="sv-alice2")  # no exception → accepted
    assert any(e["payload"].get("recipient_session_id") == "sv-alice2"
               for e in _bus[PROJECT_ID])


def test_confirmed_cross_user_send_passes(send):
    """The /beacon-dm-send path: CLI stamps the claim → server accepts."""
    send(to="sv-bob", confirmed=True)  # no exception → accepted
    ev = _bus[PROJECT_ID][-1]
    # The claim the CLI built actually rode through onto the stored envelope.
    assert ev["envelope"]["recipient_confirmed"]["recipient"]["session_id"] == "sv-bob"


def test_confirmed_claim_cannot_be_replayed_to_another_user(send, monkeypatch):
    """A claim is pinned to its recipient: confirming Bob then aiming the same
    send at Carol is rejected by the server's replay guard.

    We build the envelope+claim for Bob (via the real CLI), then post it with
    the payload retargeted to Carol — simulating a replayed confirmation.
    """
    captured = {}
    real_post = _BridgeClient.post_bus_event

    def _capture(self, project_id, channel, *, sender_session_id="", payload=None,
                 delivery="propose-to-ai", envelope=None, requested_action=None,
                 context="", rationale=""):
        # Retarget the send to Carol while keeping the Bob-pinned claim.
        payload = {**(payload or {}), "recipient_session_id": "sv-carol"}
        captured["envelope"] = envelope
        return real_post(self, project_id, channel, sender_session_id=sender_session_id,
                         payload=payload, delivery=delivery, envelope=envelope,
                         requested_action=requested_action, context=context,
                         rationale=rationale)

    monkeypatch.setattr(_BridgeClient, "post_bus_event", _capture)
    with pytest.raises(RuntimeError) as ei:
        send(to="sv-bob", confirmed=True)
    assert "API error 403" in str(ei.value)
    # Confirm the claim really was Bob's (so the rejection is the replay guard).
    assert captured["envelope"]["recipient_confirmed"]["recipient"]["session_id"] == "sv-bob"
