"""DM receipt attribution — resolve opener/deliverer identity (ms-93 / Phase 3).

``opened_by`` / ``delivered_by`` / ``sender_session_id`` on a bus event are raw
ephemeral session_ids (route tokens). A human reading ``beacon bus status``
cannot tell WHO opened a DM from ``by sv-77e81553-…4649aa42``, and a green
``opened`` ✓ stamped by a mis-addressed session is indistinguishable from a
correct delivery — the "緑の opened が誤配を隠す" failure that motivated Phase 3.

Phase 3 resolves those sids to a stable identity (email / machine / agent_kind
/ cwd) on the read side, riding the SAME participant gate as the DM payload:

  (a) attribution 全面 — sender, recipient, delivered_by, opened_by all resolve.
  (b) 認可拒否は identity 送信時のみ — a third-party member sees receipt
      timestamps (sidecar) but NOT who opened it; attribution is attached only
      inside the ``can_see`` branch, never on a redacted event.

These tests pin the invariants directly against ``_apply_dm_payload_visibility``
(the single funnel every bus read endpoint passes through) so we don't couple
to the HTTP scaffolding.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app as app_module  # noqa: E402

UID_ALICE = "uid-alice"
UID_BOB = "uid-bob"
UID_CAROL = "uid-carol"

PROJECT_ID = "test-attribution"

# Directory rows with the identity blocks Phase 3 resolves from. actor.* is the
# spoof-resistant source (server stamps actor.email from the JWT); agent.* is
# the self-reported fallback for machine / agent_kind.
_SESSIONS = [
    {
        "session_id": "sess-A",
        "user_id": UID_ALICE,
        "actor": {"email": "alice@example.com", "machine": "MAC-A",
                  "agent_kind": "claude-code"},
        "cwd": "/Users/alice/beacon",
    },
    {
        "session_id": "sess-B",
        "user_id": UID_BOB,
        "actor": {"email": "bob@example.com", "machine": "WIN-B"},
        "agent": {"kind": "codex", "machine_id": "mc-bob"},
        "cwd": "D:\\Projects\\beacon",
    },
    {
        "session_id": "sess-C",
        "user_id": UID_CAROL,
        "actor": {"email": "carol@example.com"},
    },
]


@pytest.fixture(autouse=True)
def _enable_auth(monkeypatch):
    # Attribution + redaction only mean something with a distinguishable
    # caller identity, so run these with auth enabled and a seeded directory.
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(app_module.db, "list_sessions",
                        lambda pid: [dict(s) for s in _SESSIONS], raising=False)
    yield


def _dm_event(**over) -> dict:
    ev = {
        "event_id": "e-1",
        "channel": "dm",
        "sender_session_id": "sess-A",
        "delivery": "propose-to-ai",
        "created_at": "2026-07-08T00:00:00Z",
        "delivered_at": "2026-07-08T00:00:01Z",
        "delivered_by": "sess-B",
        "opened_at": "2026-07-08T00:00:02Z",
        "opened_by": "sess-B",
        "payload": {"recipient_session_id": "sess-B", "text": "lunch at 12?"},
    }
    ev.update(over)
    return ev


def _visible(caller_uid: str, event: dict) -> dict:
    out = app_module._apply_dm_payload_visibility(PROJECT_ID, [event], caller_uid)
    assert len(out) == 1
    return out[0]


# ---------------------------------------------------------------------------
# (a) attribution 全面 — participants see resolved identity on every receipt sid
# ---------------------------------------------------------------------------

def test_recipient_sees_opener_identity():
    """Bob (recipient) reads the DM he opened. opened_by resolves to his own
    identity — email + machine + agent_kind + cwd — not the raw sid."""
    ev = _visible(UID_BOB, _dm_event())
    ident = ev["opened_by_identity"]
    assert ident["email"] == "bob@example.com"
    assert ident["machine"] == "WIN-B"
    assert ident["agent_kind"] == "codex"       # agent.kind fallback
    assert ident["cwd"] == "D:\\Projects\\beacon"
    # delivered_by resolves the same way.
    assert ev["delivered_by_identity"]["email"] == "bob@example.com"


def test_sender_sees_recipient_and_opener_identity():
    """Alice (sender) sees her own sender_identity, plus the recipient/opener
    identity so she can confirm the DM landed on the right person."""
    ev = _visible(UID_ALICE, _dm_event())
    assert ev["sender_identity"]["email"] == "alice@example.com"
    assert ev["sender_identity"]["agent_kind"] == "claude-code"
    assert ev["recipient_identity"]["email"] == "bob@example.com"
    assert ev["opened_by_identity"]["email"] == "bob@example.com"
    # Payload is intact for the sender.
    assert ev["payload"]["text"] == "lunch at 12?"


# ---------------------------------------------------------------------------
# (b) 認可拒否は identity 送信時のみ — third party: sidecar yes, identity no
# ---------------------------------------------------------------------------

def test_third_party_gets_no_attribution():
    """Carol (member, not a party) sees receipt timestamps (sidecar) but the
    payload is redacted AND no *_identity field leaks who opened it."""
    ev = _visible(UID_CAROL, _dm_event())
    # Payload redacted (existing e-2275 boundary).
    assert ev["payload"] == {"redacted": True, "reason": "dm_payload_visibility"}
    # Sidecar timestamps stay visible.
    assert ev["opened_at"] == "2026-07-08T00:00:02Z"
    assert ev["opened_by"] == "sess-B"          # raw sid still present
    # But NO resolved identity — attribution is gated to participants.
    assert "opened_by_identity" not in ev
    assert "delivered_by_identity" not in ev
    assert "sender_identity" not in ev
    assert "recipient_identity" not in ev


# ---------------------------------------------------------------------------
# Backward compatibility + boundary
# ---------------------------------------------------------------------------

def test_unknown_sid_leaves_raw_sid_untouched():
    """A legacy / GC'd opener sid that no longer exists in the directory
    resolves to nothing — the field is simply not attached, and the client
    falls back to the raw sid."""
    ev = _visible(UID_BOB, _dm_event(opened_by="sess-GONE"))
    assert ev["opened_by"] == "sess-GONE"
    assert "opened_by_identity" not in ev
    # The delivered_by (still sess-B) resolves fine — partial resolution is ok.
    assert ev["delivered_by_identity"]["email"] == "bob@example.com"


def test_non_dm_event_untouched():
    """Broadcast (non-DM) events carry no privacy contract and get no
    attribution pass — they flow through verbatim."""
    bcast = {
        "event_id": "e-2",
        "channel": "notify",
        "sender_session_id": "sess-A",
        "opened_by": "sess-B",
        "payload": {"text": "deploy done"},
    }
    ev = _visible(UID_CAROL, bcast)
    assert ev["payload"]["text"] == "deploy done"
    assert "opened_by_identity" not in ev
    assert "sender_identity" not in ev


def test_session_identity_prefers_actor_over_agent():
    """_session_identity favours the spoof-resistant actor block; agent.* only
    fills gaps (machine_id, kind)."""
    ident = app_module._session_identity({
        "session_id": "s",
        "actor": {"email": "x@e.com", "agent_kind": "claude-code"},
        "agent": {"kind": "codex", "machine_id": "mc-x"},
        "cwd": "/tmp/w",
    })
    assert ident == {
        "email": "x@e.com",
        "machine": "mc-x",           # actor.machine absent → agent.machine_id
        "agent_kind": "claude-code",  # actor.agent_kind wins over agent.kind
        "cwd": "/tmp/w",
    }
