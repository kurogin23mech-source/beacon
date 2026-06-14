"""CLI tests for `beacon claim` (ms-55 e-1648).

Stubs out the api_client and isolates BEACON_DIR to a tmp_path so the
local active_claims.json doesn't leak between tests.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import claims as _claims  # noqa: E402


class _StubBus:
    def __init__(self):
        self.events: list[dict] = []
        self._counter = 0

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai",
                       envelope=None, requested_action=None):
        self._counter += 1
        ev = {
            "event_id": f"e-{self._counter}",
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
        }
        self.events.append(ev)
        return ev


@pytest.fixture
def stub(monkeypatch, tmp_path):
    s = _StubBus()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (s, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(
        commands, "_resolve_session_id",
        lambda: "sv-test",
    )
    monkeypatch.setenv("BEACON_DIR", str(tmp_path))
    return s


def _clear_env(monkeypatch):
    for k in (
        "BEACON_CLAIM_TARGET_KIND",
        "BEACON_CLAIM_TARGET_ID",
        "BEACON_CLAIM_INTENT",
        "BEACON_CLAIM_TO",
        "BEACON_CLAIM_EXPIRES_AT",
        "BEACON_CLAIM_ID",
        "BEACON_CLAIM_DECISION",
        "BEACON_CLAIM_REASON",
        "BEACON_CLAIM_OUTCOME",
        "BEACON_CLAIM_MINE",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# claim post (broadcast)
# ---------------------------------------------------------------------------

def test_claim_post_requires_target(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_claim_post()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "target" in err


def test_claim_post_broadcasts(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    monkeypatch.setenv("BEACON_CLAIM_INTENT", "implementing primitives")
    commands.cmd_claim_post()
    assert len(stub.events) == 1
    ev = stub.events[0]
    assert ev["channel"] == "claim-signal"
    p = ev["payload"]
    assert p["claim_kind"] == "claim"
    assert "to_session_id" not in p
    out = capsys.readouterr().out
    assert "CLAIM" in out
    assert "task:e-1648" in out


def test_claim_post_local_persistence(monkeypatch, capsys, stub, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    commands.cmd_claim_post()
    # The claim should be visible to a fresh list call (= restart simulation).
    out = _claims.list_local_claims(beacon_dir=str(tmp_path))
    assert len(out) == 1
    assert out[0]["claim_kind"] == "claim"


# ---------------------------------------------------------------------------
# claim request (targeted)
# ---------------------------------------------------------------------------

def test_claim_request_requires_recipient(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    # No --to
    with pytest.raises(SystemExit):
        commands.cmd_claim_request()
    err = capsys.readouterr().err
    assert "to_session_id" in err


def test_claim_request_posts(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    monkeypatch.setenv("BEACON_CLAIM_TO", "sv-peer")
    monkeypatch.setenv("BEACON_CLAIM_INTENT", "please pick this up")
    commands.cmd_claim_request()
    p = stub.events[0]["payload"]
    assert p["claim_kind"] == "request"
    assert p["to_session_id"] == "sv-peer"
    out = capsys.readouterr().out
    assert "REQUEST" in out
    assert "sv-peer" in out


# ---------------------------------------------------------------------------
# claim handoff
# ---------------------------------------------------------------------------

def test_claim_handoff_requires_recipient(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "ms-55")
    with pytest.raises(SystemExit):
        commands.cmd_claim_handoff()


def test_claim_handoff_posts(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "ms-55")
    monkeypatch.setenv("BEACON_CLAIM_TO", "sv-peer")
    commands.cmd_claim_handoff()
    p = stub.events[0]["payload"]
    assert p["claim_kind"] == "handoff"
    assert p["to_session_id"] == "sv-peer"


# ---------------------------------------------------------------------------
# claim respond
# ---------------------------------------------------------------------------

def test_claim_respond_requires_claim_id(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_DECISION", "accept")
    with pytest.raises(SystemExit):
        commands.cmd_claim_respond()
    err = capsys.readouterr().err
    assert "claim_id" in err


def test_claim_respond_requires_decision(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_ID", "cl-1")
    with pytest.raises(SystemExit):
        commands.cmd_claim_respond()
    err = capsys.readouterr().err
    assert "accept" in err or "decline" in err


def test_claim_respond_accept(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_ID", "cl-1")
    monkeypatch.setenv("BEACON_CLAIM_DECISION", "accept")
    monkeypatch.setenv("BEACON_CLAIM_REASON", "have capacity")
    commands.cmd_claim_respond()
    p = stub.events[0]["payload"]
    assert p["kind"] == "claim_response"
    assert p["decision"] == "accept"


def test_claim_respond_decline(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_ID", "cl-1")
    monkeypatch.setenv("BEACON_CLAIM_DECISION", "decline")
    commands.cmd_claim_respond()
    p = stub.events[0]["payload"]
    assert p["decision"] == "decline"


# ---------------------------------------------------------------------------
# claim release
# ---------------------------------------------------------------------------

def test_claim_release_drops_local_cache(monkeypatch, capsys, stub, tmp_path):
    _clear_env(monkeypatch)
    # First post a claim so there's a local entry.
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    commands.cmd_claim_post()
    cid = stub.events[0]["payload"]["claim_id"]
    capsys.readouterr()

    # Now release.
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_ID", cid)
    monkeypatch.setenv("BEACON_CLAIM_OUTCOME", "completed")
    commands.cmd_claim_release()
    out = _claims.list_local_claims(beacon_dir=str(tmp_path))
    assert out == []  # local cache cleaned


def test_claim_release_invalid_outcome(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_ID", "cl-1")
    monkeypatch.setenv("BEACON_CLAIM_OUTCOME", "success")
    with pytest.raises(SystemExit):
        commands.cmd_claim_release()
    err = capsys.readouterr().err
    assert "outcome" in err


# ---------------------------------------------------------------------------
# claim list
# ---------------------------------------------------------------------------

def test_claim_list_empty(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    commands.cmd_claim_list()
    out = capsys.readouterr().out
    assert "No active claims" in out


def test_claim_list_after_post(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    monkeypatch.setenv("BEACON_CLAIM_INTENT", "doing it")
    commands.cmd_claim_post()
    capsys.readouterr()

    _clear_env(monkeypatch)
    commands.cmd_claim_list()
    out = capsys.readouterr().out
    assert "task:e-1648" in out
    assert "claim" in out


def test_claim_list_mine_filters_by_session(monkeypatch, capsys, stub, tmp_path):
    """--mine filters to claims issued by our session_id."""
    _clear_env(monkeypatch)
    # Pre-seed the local store with a claim from another session.
    other = _claims.build_claim_payload(
        claim_kind="claim",
        from_session_id="sv-other",
        target_kind="ms",
        target_id="ms-99",
    )
    _claims.record_local_claim(other, beacon_dir=str(tmp_path))

    # And one from us (via the CLI).
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "ms-55")
    commands.cmd_claim_post()
    capsys.readouterr()

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_MINE", "1")
    commands.cmd_claim_list()
    out = capsys.readouterr().out
    assert "ms:ms-55" in out
    assert "ms:ms-99" not in out  # other session filtered out


def test_claim_list_json(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    commands.cmd_claim_post()
    capsys.readouterr()

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_claim_list()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1


# ---------------------------------------------------------------------------
# Roundtrip: post → list → release reflects in latest_claims_by_target.
# ---------------------------------------------------------------------------

def test_full_lifecycle_via_event_stream(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_CLAIM_TARGET_ID", "e-1648")
    commands.cmd_claim_post()
    cid = stub.events[-1]["payload"]["claim_id"]

    # Stamp created_at on the events so the reducer can sort.
    for i, ev in enumerate(stub.events):
        ev["created_at"] = f"2026-06-15T00:00:{i:02d}Z"

    state = _claims.latest_claims_by_target(stub.events)
    assert ("task", "e-1648") in state

    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_CLAIM_ID", cid)
    monkeypatch.setenv("BEACON_CLAIM_OUTCOME", "completed")
    commands.cmd_claim_release()
    for i, ev in enumerate(stub.events):
        ev["created_at"] = f"2026-06-15T00:00:{i:02d}Z"

    state_after = _claims.latest_claims_by_target(stub.events)
    assert ("task", "e-1648") not in state_after
