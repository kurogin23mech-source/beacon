"""cmd_bus_send attaches the recipient_confirmed claim (ms-110 / e-3445).

When /beacon-dm-send confirms the recipient with a human it re-invokes the send
with ``--recipient-confirmed`` (BEACON_BUS_RECIPIENT_CONFIRMED=1). The CLI then
assembles the ``recipient_confirmed`` consent claim from the send's own target
flags + caller identity and stamps it onto the outgoing envelope — the claim
the server backstop (e-3443) requires for a cross-user DM. A raw send without
the flag carries no claim (and the server rejects it if cross-user).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import dm_consent  # noqa: E402


class _StubApiClient:
    def __init__(self):
        self.post_calls: list[dict] = []

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized=None,
                           scope=None, data_class="free", conversation_id=None,
                           in_reply_to=None, chain_depth=0, ttl_seconds=3600,
                           consent_claim=None):
        env = {"tier": tier, "actions_authorized": list(actions_authorized or []),
               "data_class": data_class, "signature": "stub-sig"}
        # ms-110 fix: the consent claim is now minted INTO the envelope (signed
        # in server-side), not grafted after signing. Mirror that here so the
        # stub reflects the real mint endpoint's behavior.
        if consent_claim is not None:
            env["recipient_confirmed"] = consent_claim
        return env

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai", envelope=None,
                       requested_action=None, context="", rationale="", client_event_id="", is_retry=False):
        self.post_calls.append({"project_id": project_id, "channel": channel,
                                "payload": payload or {}, "envelope": envelope})
        return {"event_id": "ev-1", "channel": channel, "delivery": delivery,
                "created_at": "2026-07-15T00:00:00.000000Z"}


_BUS_ENV = (
    "BEACON_BUS_CHANNEL", "BEACON_BUS_PAYLOAD", "BEACON_BUS_SENDER",
    "BEACON_BUS_DELIVERY", "BEACON_BUS_IN_REPLY_TO", "BEACON_JSON",
    "BEACON_BUS_RECIPIENT_SESSION", "BEACON_BUS_RECIPIENT_USER",
    "BEACON_BUS_ACTION", "BEACON_BUS_NO_ENVELOPE", "BEACON_BUS_PROJECT_ID",
    "BEACON_BUS_RECIPIENT_CONFIRMED", "BEACON_BUS_CONTEXT",
    "BEACON_BUS_RATIONALE", "BEACON_USER_ID", "BEACON_USER_EMAIL",
)


@pytest.fixture
def stub(monkeypatch):
    s = _StubApiClient()
    monkeypatch.setattr(commands, "_get_api_client",
                        lambda: (s, {"project_id": "proj-recipient"}))
    # Deterministic identity so _resolve_creator_identity resolves.
    for k in _BUS_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_USER_ID", "uid-me")
    monkeypatch.setenv("BEACON_USER_EMAIL", "me@example.com")
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", "sv-bob")
    return s


def test_confirmed_flag_attaches_claim(monkeypatch, stub):
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_CONFIRMED", "1")
    commands.cmd_bus_send()

    env = stub.post_calls[0]["envelope"]
    assert env is not None
    claim = env.get(dm_consent.CONSENT_CLAIM_KEY)
    assert claim is not None
    # Claim pins the send target + confirmer, independent of actions_authorized.
    assert claim["kind"] == dm_consent.RECIPIENT_CONFIRMED_KIND
    assert claim["recipient"]["session_id"] == "sv-bob"
    assert claim["recipient"]["project_id"] == "proj-recipient"
    assert claim["confirmed_by"]["user_id"] == "uid-me"
    assert "actions_authorized" not in claim


def test_no_flag_attaches_no_claim(monkeypatch, stub):
    # Without the flag, the envelope carries no consent claim.
    commands.cmd_bus_send()
    env = stub.post_calls[0]["envelope"]
    assert env is not None
    assert dm_consent.CONSENT_CLAIM_KEY not in env


def test_claim_matches_the_actual_recipient(monkeypatch, stub):
    """The stamped claim confirms exactly the session being sent to (so the
    server's replay guard accepts it)."""
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_CONFIRMED", "1")
    commands.cmd_bus_send()
    claim = stub.post_calls[0]["envelope"][dm_consent.CONSENT_CLAIM_KEY]
    assert dm_consent.claim_matches_recipient(claim, recipient_session_id="sv-bob")


def test_confirmed_with_no_envelope_errors(monkeypatch, stub):
    """--recipient-confirmed + --no-envelope has no envelope to carry the claim
    → hard error rather than sending something the server will 403."""
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_CONFIRMED", "1")
    monkeypatch.setenv("BEACON_BUS_NO_ENVELOPE", "1")
    with pytest.raises(SystemExit):
        commands.cmd_bus_send()
    assert stub.post_calls == []


def test_user_scoped_recipient_claim(monkeypatch, stub):
    """--to-user path pins recipient_user_id in the claim."""
    monkeypatch.delenv("BEACON_BUS_RECIPIENT_SESSION", raising=False)
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_USER", "uid-bob")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_CONFIRMED", "1")
    commands.cmd_bus_send()
    claim = stub.post_calls[0]["envelope"][dm_consent.CONSENT_CLAIM_KEY]
    assert claim["recipient"]["user_id"] == "uid-bob"
