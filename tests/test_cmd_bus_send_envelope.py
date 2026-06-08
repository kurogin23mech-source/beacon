"""Tests for cmd_bus_send envelope adoption (ms-54 / e-1290).

PR #104 added a server-side envelope verify pipeline; until clients call
``/bus/envelope/issue`` before posting, real traffic falls through the legacy
T5-equivalent lane (delivery soft-degrades to ``notify-user-only``).

This test file pins the client-side contract introduced in e-1290:

  * Default behavior: ``cmd_bus_send`` issues an envelope before posting.
    Tier = T1 (human signature, since the CLI is invoked by a human typing
    a command). ``actions_authorized`` = the comma-joined ``--action`` flags.
  * ``--action <name>`` populates ``actions_authorized`` and (when exactly
    one is given) ``requested_action`` on the outgoing post.
  * ``--no-envelope`` (``BEACON_BUS_NO_ENVELOPE=1``) opts out — useful for
    debugging the server's legacy lane. No issuance call, post has no
    envelope.
  * Legacy fallback: when ``/bus/envelope/issue`` returns 404 (older server)
    or 400 (server-side rejection), the CLI falls back to posting without
    an envelope. The user's message still lands; only the verify upgrade
    is skipped. A note is written to stderr so it's visible without being
    a hard failure.
  * Transport / 5xx errors on issuance also fall back rather than break the
    send — the bus must not become more fragile by adopting envelopes.

The tests stub ``ApiClient`` directly so the assertions are about the
outgoing request shape; the server-side verify pipeline is covered by
``test_envelope_integration.py``.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# Stub plumbing
# ---------------------------------------------------------------------------


class _StubApiClient:
    """Records every issue / post call so tests can assert call shape + order."""

    def __init__(
        self,
        *,
        issue_error: Exception | None = None,
        issue_payload: dict | None = None,
    ):
        self.issue_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.issue_error = issue_error
        self.issue_payload = issue_payload

    def issue_bus_envelope(
        self,
        project_id,
        *,
        tier,
        actions_authorized=None,
        scope=None,
        data_class="free",
        conversation_id=None,
        in_reply_to=None,
        chain_depth=0,
        ttl_seconds=3600,
    ):
        self.issue_calls.append({
            "project_id": project_id,
            "tier": tier,
            "actions_authorized": list(actions_authorized or []),
            "data_class": data_class,
        })
        if self.issue_error is not None:
            raise self.issue_error
        return self.issue_payload or {
            "tier": tier,
            "actions_authorized": list(actions_authorized or []),
            "data_class": data_class,
            "signature": "stub-sig",
        }

    def post_bus_event(
        self,
        project_id,
        channel,
        *,
        sender_session_id="",
        payload=None,
        delivery="propose-to-ai",
        envelope=None,
        requested_action=None,
    ):
        self.post_calls.append({
            "project_id": project_id,
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
            "envelope": envelope,
            "requested_action": requested_action,
        })
        return {
            "event_id": f"e-{len(self.post_calls)}",
            "channel": channel,
            "delivery": delivery,
            "created_at": "2026-06-07T00:00:00.000000Z",
        }


@pytest.fixture
def stub(monkeypatch):
    """Wire commands._get_api_client to our stub + a fixed project."""
    stub = _StubApiClient()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    return stub


def _clear_bus_env(monkeypatch):
    for key in (
        "BEACON_BUS_CHANNEL", "BEACON_BUS_PAYLOAD", "BEACON_BUS_SENDER",
        "BEACON_BUS_DELIVERY", "BEACON_BUS_IN_REPLY_TO", "BEACON_JSON",
        "BEACON_BUS_RECIPIENT_SESSION", "BEACON_BUS_ACTION",
        "BEACON_BUS_NO_ENVELOPE", "BEACON_BUS_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Default behavior: envelope-by-default, T1, empty actions
# ---------------------------------------------------------------------------


def test_send_issues_envelope_before_post(monkeypatch, stub):
    """Default send: issue → post, in that order. Tier = T1 (human-CLI)."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")
    commands.cmd_bus_send()

    assert len(stub.issue_calls) == 1
    assert len(stub.post_calls) == 1
    issue = stub.issue_calls[0]
    post = stub.post_calls[0]

    # Tier rule: cmd_bus_send is human-invoked → T1.
    assert issue["tier"] == "T1"
    assert issue["actions_authorized"] == []
    assert issue["project_id"] == "proj-1"

    # The minted envelope is stamped on the post.
    assert post["envelope"] is not None
    assert post["envelope"]["tier"] == "T1"
    # No --action passed → no requested_action on the post.
    assert post["requested_action"] is None


def test_send_with_single_action_propagates_to_envelope_and_request(
    monkeypatch, stub,
):
    """`--action status_check` populates both actions_authorized (capability
    grant on the envelope) and requested_action (what this message asks for).
    """
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")
    monkeypatch.setenv("BEACON_BUS_ACTION", "status_check")
    commands.cmd_bus_send()

    assert stub.issue_calls[0]["actions_authorized"] == ["status_check"]
    # Single action: also stamped as requested_action so the receiver verify
    # pipeline can check tier permission against this one explicit action.
    assert stub.post_calls[0]["requested_action"] == "status_check"


def test_send_with_multiple_actions_grants_all_but_no_single_request(
    monkeypatch, stub,
):
    """Two or more `--action` flags grant a multi-action capability without
    setting requested_action — the message isn't asking for a specific one,
    it's just delegating a set."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")
    # The bash wrapper joins repeated --action flags with commas.
    monkeypatch.setenv("BEACON_BUS_ACTION", "status_check,refresh_cache")
    commands.cmd_bus_send()

    assert stub.issue_calls[0]["actions_authorized"] == [
        "status_check", "refresh_cache",
    ]
    assert stub.post_calls[0]["requested_action"] is None


# ---------------------------------------------------------------------------
# Opt-out: --no-envelope skips issuance entirely
# ---------------------------------------------------------------------------


def test_no_envelope_flag_skips_issuance(monkeypatch, stub):
    """`--no-envelope` → no issue call, post has envelope=None,
    requested_action=None. Useful for debugging the server's legacy path."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")
    monkeypatch.setenv("BEACON_BUS_NO_ENVELOPE", "1")
    monkeypatch.setenv("BEACON_BUS_ACTION", "status_check")  # ignored
    commands.cmd_bus_send()

    assert stub.issue_calls == []
    assert len(stub.post_calls) == 1
    assert stub.post_calls[0]["envelope"] is None
    assert stub.post_calls[0]["requested_action"] is None


# ---------------------------------------------------------------------------
# Legacy fallback: 404 (older server) and 400 (rejected) both fall through
# ---------------------------------------------------------------------------


def test_send_falls_back_on_issue_404(monkeypatch, capsys):
    """Older Beacon servers don't expose /bus/envelope/issue → 404. Client
    must fall back to posting without envelope so the message still lands."""
    _clear_bus_env(monkeypatch)
    stub = _StubApiClient(
        issue_error=RuntimeError("API error 404: not found"),
    )
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")

    commands.cmd_bus_send()

    # Issuance was attempted...
    assert len(stub.issue_calls) == 1
    # ...but the post went out without envelope (legacy lane).
    assert len(stub.post_calls) == 1
    assert stub.post_calls[0]["envelope"] is None
    assert stub.post_calls[0]["requested_action"] is None

    # And the user got a stderr note explaining the fallback, without it
    # becoming a hard failure.
    err = capsys.readouterr().err
    assert "404" in err
    assert "legacy" in err


def test_send_falls_back_on_issue_400(monkeypatch, capsys):
    """Server rejection (e.g. payload validation, wildcard action) → 400.
    Same fallback path: legacy post, stderr note, no hard failure."""
    _clear_bus_env(monkeypatch)
    stub = _StubApiClient(
        issue_error=RuntimeError(
            "API error 400: action wildcards forbidden"
        ),
    )
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")

    commands.cmd_bus_send()

    assert len(stub.post_calls) == 1
    assert stub.post_calls[0]["envelope"] is None
    err = capsys.readouterr().err
    assert "400" in err
    assert "legacy" in err


def test_send_falls_back_on_issue_transport_error(monkeypatch, capsys):
    """Transport / 5xx on issuance must not break the send. The bus must
    stay reliable for the user even when the envelope path misbehaves."""
    _clear_bus_env(monkeypatch)
    stub = _StubApiClient(
        issue_error=ConnectionError("Cannot connect to API"),
    )
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")

    commands.cmd_bus_send()

    # Legacy post still went out.
    assert len(stub.post_calls) == 1
    assert stub.post_calls[0]["envelope"] is None
    err = capsys.readouterr().err
    assert "legacy" in err


# ---------------------------------------------------------------------------
# Payload + tier-selection interaction: --action is the only knob; payload
# shape doesn't change the tier for cmd_bus_send (always T1).
# ---------------------------------------------------------------------------


def test_send_payload_does_not_change_tier(monkeypatch, stub):
    """Even a payload that would qualify as T5 short-ping on the MCP bridge
    stays T1 from the CLI — the human signature is the differentiator."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")
    monkeypatch.setenv("BEACON_BUS_PAYLOAD", json.dumps({"ping": "ok"}))
    commands.cmd_bus_send()

    assert stub.issue_calls[0]["tier"] == "T1"
