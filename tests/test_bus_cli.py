"""CLI tests for `beacon bus send / listen / receive / ack` (ms-54 / e-999).

The bash dispatch in bin/beacon turns flags into env vars and then invokes
`python3 lib/commands.py bus_send` (etc). The CLI functions in commands.py
read those env vars, call api_client, and either print events or advance
cursors. This test file exercises the python side directly with a stubbed
ApiClient — bypassing the bash wrapper — so:

  * arg-name regressions (env var typos in bin/beacon vs commands.py) are caught
  * exit codes for "got events" (0), "error" (1), "receive timeout" (2) are
    locked in
  * the long-poll loop terminates on --once + auto-ack advances the cursor

The bash wrapper itself is exercised by `beacon bus` → usage smoke (executed
manually). The two layers are kept separate so a flaky network condition or
a malformed cloud.json never appears in this test's failure surface.
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


class _StubApiClient:
    """Minimal in-memory ApiClient that satisfies the bus CLI surface."""

    def __init__(self):
        self.events: list[dict] = []
        self.cursors: dict[str, str] = {}
        # When > 0, list_unread_bus_events returns events on the Nth call.
        self.delay_calls = 0
        self._call_counter = 0

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai"):
        ev = {
            "event_id": f"e-{len(self.events) + 1}",
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
            "created_at": f"2026-06-07T00:00:0{len(self.events)}.000000Z",
        }
        self.events.append(ev)
        return ev

    def list_unread_bus_events(self, project_id, recipient_id, *,
                                channel="", limit=100):
        self._call_counter += 1
        if self._call_counter <= self.delay_calls:
            return []
        cursor = self.cursors.get(recipient_id, "")
        out = []
        for ev in self.events:
            if cursor and ev["created_at"] <= cursor:
                continue
            if channel and ev["channel"] != channel:
                continue
            out.append(ev)
        return out

    def advance_bus_cursor(self, project_id, recipient_id, last_seen_at):
        existing = self.cursors.get(recipient_id, "")
        if not existing or last_seen_at > existing:
            self.cursors[recipient_id] = last_seen_at
        return {"last_seen_at": self.cursors[recipient_id]}


@pytest.fixture
def stub(monkeypatch, capsys):
    """Pre-wire commands._get_api_client to return our stub + a fixed project."""
    stub = _StubApiClient()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    # Speed up the polling loop so tests don't sit on time.sleep(2).
    monkeypatch.setenv("BEACON_BUS_INTERVAL", "0.25")
    # Default recipient to avoid the "no recipient_id" exit path everywhere.
    monkeypatch.setenv("BEACON_BUS_RECIPIENT", "R1")
    return stub


def _clear_bus_env(monkeypatch):
    for key in ("BEACON_BUS_CHANNEL", "BEACON_BUS_PAYLOAD", "BEACON_BUS_SENDER",
                "BEACON_BUS_DELIVERY", "BEACON_BUS_ONCE", "BEACON_BUS_AUTO_ACK",
                "BEACON_BUS_TIMEOUT", "BEACON_BUS_LAST_SEEN_AT", "BEACON_JSON",
                "BEACON_BUS_RECIPIENT_SESSION", "BEACON_BUS_IN_REPLY_TO"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def test_bus_send_requires_channel(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_send()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "channel" in err


def test_bus_send_minimum_args(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "session-dm")
    commands.cmd_bus_send()
    assert len(stub.events) == 1
    assert stub.events[0]["channel"] == "session-dm"
    assert stub.events[0]["delivery"] == "propose-to-ai"  # default
    out = capsys.readouterr().out
    assert "Sent:" in out


def test_bus_send_payload_json_object_only(monkeypatch, capsys, stub):
    """--payload must be a JSON object; a top-level array would crash the
    receiver's payload.* access."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "x")
    monkeypatch.setenv("BEACON_BUS_PAYLOAD", "[1, 2]")
    with pytest.raises(SystemExit):
        commands.cmd_bus_send()


def test_bus_send_payload_invalid_json_exits(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "x")
    monkeypatch.setenv("BEACON_BUS_PAYLOAD", "{not json")
    with pytest.raises(SystemExit):
        commands.cmd_bus_send()


def test_bus_send_delivery_forwarded(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "ops")
    monkeypatch.setenv("BEACON_BUS_DELIVERY", "notify-user-only")
    commands.cmd_bus_send()
    assert stub.events[-1]["delivery"] == "notify-user-only"


def test_bus_send_json_mode(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "x")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_send()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["channel"] == "x"


# ---------------------------------------------------------------------------
# --to (e-1209): the sender CLI must stamp payload.recipient_session_id so
# the server-side filter can route the event to a single recipient. Before
# this, dm events fanned out to every dm-subscribed session because nothing
# in the pipeline stamped the recipient.
# ---------------------------------------------------------------------------

def test_bus_send_to_stamps_recipient_session_id(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", "target-session")
    commands.cmd_bus_send()
    assert stub.events[-1]["payload"]["recipient_session_id"] == "target-session"


def test_bus_send_to_overrides_payload_recipient(monkeypatch, capsys, stub):
    """If both --to and --payload supply a recipient_session_id, --to wins.
    The flag is the unambiguous source of truth for routing — letting
    --payload override would mean a typo in JSON silently changes the
    delivery destination."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", "flag-target")
    monkeypatch.setenv(
        "BEACON_BUS_PAYLOAD",
        json.dumps({"recipient_session_id": "payload-target", "text": "hi"}),
    )
    commands.cmd_bus_send()
    p = stub.events[-1]["payload"]
    assert p["recipient_session_id"] == "flag-target"
    assert p["text"] == "hi"  # other payload fields preserved


def test_bus_send_dm_without_to_warns(monkeypatch, capsys, stub):
    """Sending to dm without --to triggers a stderr warning because the
    server now drops unaddressed dm events (e-1209). The send still goes
    through — we don't hard-error so existing scripts surface the issue
    via the warning rather than disappearing into silent failures."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    commands.cmd_bus_send()
    err = capsys.readouterr().err
    assert "dm" in err and "--to" in err
    # The event was still posted (the warning is advisory, not a refuse).
    assert len(stub.events) == 1


def test_bus_send_dm_with_payload_recipient_does_not_warn(monkeypatch, capsys, stub):
    """If the user pre-stamped payload.recipient_session_id via --payload,
    --to is redundant and we must NOT print the warning — that would be
    noise the user explicitly avoided."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv(
        "BEACON_BUS_PAYLOAD",
        json.dumps({"recipient_session_id": "pre-stamped", "text": "hi"}),
    )
    commands.cmd_bus_send()
    err = capsys.readouterr().err
    assert "--to" not in err


def test_bus_send_non_dm_without_to_does_not_warn(monkeypatch, capsys, stub):
    """Non-DM channels keep broadcast semantics; missing --to is normal."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "notify")
    commands.cmd_bus_send()
    err = capsys.readouterr().err
    assert "--to" not in err
    # The send did happen and did NOT stamp recipient (broadcast intent).
    assert "recipient_session_id" not in stub.events[-1]["payload"]


# ---------------------------------------------------------------------------
# listen --once + auto-ack
# ---------------------------------------------------------------------------

def test_bus_listen_once_streams_events_as_jsonl(monkeypatch, capsys, stub):
    """Each event must appear on its own line as JSON — that's how Claude
    Code's Monitor tool turns events into individual notifications."""
    _clear_bus_env(monkeypatch)
    stub.events.append({
        "event_id": "e-A", "channel": "c", "sender_session_id": "S",
        "payload": {"n": 1}, "delivery": "propose-to-ai",
        "created_at": "2026-06-07T00:00:01.000000Z",
    })
    stub.events.append({
        "event_id": "e-B", "channel": "c", "sender_session_id": "S",
        "payload": {"n": 2}, "delivery": "propose-to-ai",
        "created_at": "2026-06-07T00:00:02.000000Z",
    })
    monkeypatch.setenv("BEACON_BUS_ONCE", "1")
    commands.cmd_bus_listen()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    parsed = [json.loads(line) for line in out]
    assert [e["event_id"] for e in parsed] == ["e-A", "e-B"]


def test_bus_listen_once_auto_ack_advances_cursor(monkeypatch, capsys, stub):
    """--auto-ack must advance the cursor to the last delivered event's
    timestamp so a subsequent listen does not redeliver."""
    _clear_bus_env(monkeypatch)
    stub.events.append({
        "event_id": "e-A", "channel": "c", "sender_session_id": "",
        "payload": {}, "delivery": "propose-to-ai",
        "created_at": "2026-06-07T00:00:01.000000Z",
    })
    monkeypatch.setenv("BEACON_BUS_ONCE", "1")
    monkeypatch.setenv("BEACON_BUS_AUTO_ACK", "1")
    commands.cmd_bus_listen()
    assert stub.cursors.get("R1") == "2026-06-07T00:00:01.000000Z"


def test_bus_listen_once_no_events_keeps_polling_until_some_arrive(monkeypatch,
                                                                    capsys, stub):
    """--once means 'return after the first non-empty batch', not 'poll once'.
    Otherwise a receiver subscribed before the sender posts would falsely
    return empty and skip the message."""
    _clear_bus_env(monkeypatch)
    stub.delay_calls = 2  # first 2 polls return empty
    stub.events.append({
        "event_id": "e-A", "channel": "c", "sender_session_id": "",
        "payload": {}, "delivery": "propose-to-ai",
        "created_at": "2026-06-07T00:00:01.000000Z",
    })
    monkeypatch.setenv("BEACON_BUS_ONCE", "1")
    commands.cmd_bus_listen()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0])["event_id"] == "e-A"


# ---------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------

def test_bus_receive_returns_when_events_available(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    stub.events.append({
        "event_id": "e-A", "channel": "c", "sender_session_id": "",
        "payload": {}, "delivery": "propose-to-ai",
        "created_at": "2026-06-07T00:00:01.000000Z",
    })
    commands.cmd_bus_receive()  # exits 0 implicitly
    out = capsys.readouterr().out.strip().splitlines()
    assert json.loads(out[0])["event_id"] == "e-A"


def test_bus_receive_timeout_exits_2(monkeypatch, capsys, stub):
    """exit 2 distinguishes 'timed out, no events' from 'error' (1) and
    'got events' (0) so shell scripts can branch on the outcome."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_TIMEOUT", "0.3")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_receive()
    assert exc.value.code == 2


def test_bus_receive_auto_ack_advances_cursor(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    stub.events.append({
        "event_id": "e-A", "channel": "c", "sender_session_id": "",
        "payload": {}, "delivery": "propose-to-ai",
        "created_at": "2026-06-07T00:00:01.000000Z",
    })
    monkeypatch.setenv("BEACON_BUS_AUTO_ACK", "1")
    commands.cmd_bus_receive()
    assert stub.cursors["R1"] == "2026-06-07T00:00:01.000000Z"


# ---------------------------------------------------------------------------
# ack
# ---------------------------------------------------------------------------

def test_bus_ack_requires_last_seen_at(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_ack()
    assert exc.value.code == 1


def test_bus_ack_advances_cursor(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_LAST_SEEN_AT", "2026-06-07T00:00:01.000000Z")
    commands.cmd_bus_ack()
    assert stub.cursors["R1"] == "2026-06-07T00:00:01.000000Z"
    out = capsys.readouterr().out
    assert "2026-06-07T00:00:01.000000Z" in out


# ---------------------------------------------------------------------------
# directory (e-1134)
# ---------------------------------------------------------------------------

class _StubDirectoryClient(_StubApiClient):
    """Add list_sessions to the stub so the directory CLI can be exercised
    independently of the bus event stub above."""

    def __init__(self):
        super().__init__()
        self.sessions: list[dict] = []
        self.last_query: dict = {}

    def list_sessions(self, project_id, *, user_id="", machine="",
                      agent="", live_only=False, since_minutes=5):
        self.last_query = {
            "user_id": user_id, "machine": machine, "agent": agent,
            "live_only": live_only, "since_minutes": since_minutes,
        }
        out = []
        for s in self.sessions:
            actor = s.get("actor") or {}
            if user_id and actor.get("email") != user_id:
                continue
            if machine and actor.get("machine") != machine:
                continue
            if agent and actor.get("agent") != agent:
                continue
            out.append(s)
        return out


@pytest.fixture
def dir_stub(monkeypatch):
    stub = _StubDirectoryClient()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    return stub


def _clear_dir_env(monkeypatch):
    for k in ("BEACON_DIR_USER", "BEACON_DIR_MACHINE", "BEACON_DIR_AGENT",
              "BEACON_DIR_LIVE", "BEACON_DIR_SINCE_MIN", "BEACON_JSON"):
        monkeypatch.delenv(k, raising=False)


def test_bus_directory_no_filter_prints_all_sessions(monkeypatch, capsys, dir_stub):
    """The unfiltered call must show every registered session so a sender
    can scan the whole list when they don't know who to pick yet."""
    _clear_dir_env(monkeypatch)
    dir_stub.sessions = [
        {"session_id": "s-1",
         "actor": {"email": "a@x", "machine": "mac", "agent": "claude"},
         "last_active": "2026-06-07T00:00:01.000000Z"},
        {"session_id": "s-2",
         "actor": {"email": "b@x", "machine": "win", "agent": "claude"},
         "last_active": "2026-06-07T00:00:02.000000Z"},
    ]
    commands.cmd_bus_directory()
    out = capsys.readouterr().out
    assert "s-1" in out
    assert "s-2" in out
    assert "a@x" in out  # identity surfaces so the human can pick


def test_bus_directory_forwards_filters_to_api(monkeypatch, capsys, dir_stub):
    """Every CLI filter must reach api_client unchanged — otherwise the user
    types --live but the server still returns stale sessions, breaking the
    picker."""
    _clear_dir_env(monkeypatch)
    monkeypatch.setenv("BEACON_DIR_USER", "alice@x")
    monkeypatch.setenv("BEACON_DIR_MACHINE", "mac")
    monkeypatch.setenv("BEACON_DIR_AGENT", "claude")
    monkeypatch.setenv("BEACON_DIR_LIVE", "1")
    monkeypatch.setenv("BEACON_DIR_SINCE_MIN", "10")
    commands.cmd_bus_directory()
    assert dir_stub.last_query == {
        "user_id": "alice@x", "machine": "mac", "agent": "claude",
        "live_only": True, "since_minutes": 10,
    }


def test_bus_directory_empty_result_message(monkeypatch, capsys, dir_stub):
    _clear_dir_env(monkeypatch)
    commands.cmd_bus_directory()
    out = capsys.readouterr().out
    assert "no matching sessions" in out.lower()


def test_bus_directory_json_mode(monkeypatch, capsys, dir_stub):
    """JSON mode is the contract scripts depend on for auto-routing
    (e.g. send-to-every-live-agent-of-user-X). Format must round-trip."""
    _clear_dir_env(monkeypatch)
    dir_stub.sessions = [{
        "session_id": "s-1", "actor": {"email": "a@x"},
        "last_active": "2026-06-07T00:00:01.000000Z",
    }]
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_directory()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["session_id"] == "s-1"
