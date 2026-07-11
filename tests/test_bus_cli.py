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
                       payload=None, delivery="propose-to-ai",
                       envelope=None, requested_action=None,
                       context="", rationale=""):
        # e-1290: envelope / requested_action are accepted to match the real
        # api_client signature. The pre-envelope CLI tests don't care about
        # these fields; they assert payload/channel/delivery only.
        # ms-90 / e-3246: context / rationale も real signature に合わせて受ける。
        ev = {
            "event_id": f"e-{len(self.events) + 1}",
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
            "envelope": envelope,
            "requested_action": requested_action,
            "context": context,
            "rationale": rationale,
            "created_at": f"2026-06-07T00:00:0{len(self.events)}.000000Z",
        }
        self.events.append(ev)
        return ev

    def issue_bus_envelope(self, project_id, *, tier, actions_authorized=None,
                            scope=None, data_class="free",
                            conversation_id=None, in_reply_to=None,
                            chain_depth=0, ttl_seconds=3600):
        # e-1290: minimal in-memory mint. Tests that need to verify envelope
        # contents inspect the recorded event's `envelope` field instead of
        # checking signatures. Returning a stable dict keeps the post path
        # exercisable without standing up the real server.
        return {
            "tier": tier,
            "actions_authorized": list(actions_authorized or []),
            "scope": scope,
            "data_class": data_class,
            "conversation_id": conversation_id,
            "in_reply_to": in_reply_to,
            "chain_depth": chain_depth,
            "signature": "stub-sig",
        }

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

    # ms-54 / e-1348: per-event receipt state. The stub stores receipts as
    # extra fields on the event dict (mirrors the Firestore document layout).
    def get_bus_event(self, project_id, event_id):
        for ev in self.events:
            if ev["event_id"] == event_id:
                return ev
        # Mirror the api_client behavior: raise with the 404 substring so the
        # CLI's error branch can detect it.
        raise Exception(f"GET .../bus/{event_id} -> 404: not found")

    def stamp_receipt(self, event_id, *, stage, by, ts):
        """Test helper: pretend the bridge ack'd `stage` for an event."""
        for ev in self.events:
            if ev["event_id"] == event_id:
                ev[f"{stage}_at"] = ts
                ev[f"{stage}_by"] = by
                return


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
                "BEACON_BUS_RECIPIENT_SESSION", "BEACON_BUS_IN_REPLY_TO",
                "BEACON_BUS_CONTEXT", "BEACON_BUS_RATIONALE",
                "BEACON_BUS_EVENT_ID", "BEACON_BUS_ACK_EVENT_ID"):
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
# ms-90 / e-3246: --context / --rationale forward to post_bus_event so the
# server can record a DM-send decision-event, and initiating a DM without
# context nudges the sender (promote, not block).
# ---------------------------------------------------------------------------

def test_bus_send_context_and_rationale_forwarded(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", "target")
    monkeypatch.setenv("BEACON_BUS_CONTEXT", "listener が 60s で死ぬ")
    monkeypatch.setenv("BEACON_BUS_RATIONALE", "相談が速い")
    commands.cmd_bus_send()
    assert stub.events[-1]["context"] == "listener が 60s で死ぬ"
    assert stub.events[-1]["rationale"] == "相談が速い"


def test_bus_send_dm_initiate_without_context_warns(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", "target")
    commands.cmd_bus_send()
    err = capsys.readouterr().err
    assert "--context" in err  # promote-context nudge on initiating DM


def test_bus_send_dm_reply_without_context_does_not_warn(monkeypatch, capsys,
                                                         stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    monkeypatch.setenv("BEACON_BUS_RECIPIENT_SESSION", "target")
    monkeypatch.setenv("BEACON_BUS_IN_REPLY_TO", "evt-parent")
    # --in-reply-to triggers the budget gate; grant it so the send proceeds
    # to the (non-)warning path rather than refusing on budget state.
    # Stub BOTH the pre-check read and the consume; _read_bus_budget() reads
    # the cwd-scoped .beacon/bus-budget.json, so without this stub the test
    # only passes when an ambient budget file happens to exist in cwd (= the
    # cwd-scoped budget footgun). Stubbing keeps the test hermetic.
    monkeypatch.setattr(commands, "_read_bus_budget",
                        lambda: {"armed": True, "total": 10, "used": 1})
    monkeypatch.setattr(commands, "_bus_budget_consume_one",
                        lambda: (True, {"armed": True, "total": 10, "used": 1}))
    commands.cmd_bus_send()
    err = capsys.readouterr().err
    # 返信は継続なので背景 nudge は出さない (= ノイズ回避)。
    assert "この相談で" not in err and "--context \"" not in err


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


def test_bus_send_stamps_source_project_by_default(monkeypatch, capsys, stub):
    """ms-94 / e-2811 (2026-07-06): payload.source_project は sender の cwd
    project_id で stamp される (= bridge fallback で receiver 側 cwd に
    上書きされる病理を CLI 側で塞ぐ)。 stub fixture の config は
    {"project_id": "proj-1"} なので payload に "proj-1" が入るはず。"""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "session-x")
    commands.cmd_bus_send()
    assert len(stub.events) == 1
    payload = stub.events[0]["payload"]
    assert payload.get("source_project") == "proj-1"


def test_bus_send_source_project_pre_stamped_wins(monkeypatch, capsys, stub):
    """caller が明示的に payload.source_project を stamp した場合、 CLI 側の
    自動 stamp は override しない (= caller の意図優先)。"""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "x")
    monkeypatch.setenv(
        "BEACON_BUS_PAYLOAD",
        json.dumps({"source_project": "custom-proj", "text": "hi"}),
    )
    commands.cmd_bus_send()
    payload = stub.events[0]["payload"]
    assert payload.get("source_project") == "custom-proj"


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
    err = capsys.readouterr().err
    assert "--last-seen-at" in err and "--event" in err


def test_bus_ack_advances_cursor(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_LAST_SEEN_AT", "2026-06-07T00:00:01.000000Z")
    commands.cmd_bus_ack()
    assert stub.cursors["R1"] == "2026-06-07T00:00:01.000000Z"
    out = capsys.readouterr().out
    assert "2026-06-07T00:00:01.000000Z" in out


# ---------------------------------------------------------------------------
# e-1423 (ms-54 Bug 4 第 2 層): bus ack --event <event_id>
# ---------------------------------------------------------------------------
#
# Forcing function for /beacon-operation-execute: after a Skill run lands a
# run_record, ack the triggering operation-trigger event so the next
# UserPromptSubmit hook doesn't re-inject it. Bug 5 (e-1424) prevents
# session_id rotation as the structural cause; this ack handles the rare
# residual case (= cursor advance failed mid-poll, or the event arrived
# after the bridge's last cursor write).

def test_bus_ack_via_event_id_resolves_created_at(monkeypatch, capsys, stub):
    """--event <id> looks up the event and advances cursor to its
    created_at. The Skill never has to format ISO8601 itself."""
    _clear_bus_env(monkeypatch)
    stub.events.append({
        "event_id": "ev-trigger-1",
        "channel": "operation-trigger",
        "delivery": "auto-execute",
        "created_at": "2026-06-10T11:00:00.000000Z",
        "payload": {"op_id": "op-2"},
    })
    monkeypatch.setenv("BEACON_BUS_ACK_EVENT_ID", "ev-trigger-1")
    commands.cmd_bus_ack()
    assert stub.cursors["R1"] == "2026-06-10T11:00:00.000000Z"
    out = capsys.readouterr().out
    assert "2026-06-10T11:00:00.000000Z" in out


def test_bus_ack_via_unknown_event_id_errors(monkeypatch, capsys, stub):
    """A missing event_id surfaces a clean 1-line error rather than
    a Python traceback (mirrors `bus status <unknown>`)."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_ACK_EVENT_ID", "ev-does-not-exist")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_ack()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ev-does-not-exist" in err and "not found" in err
    # Cursor must remain unset — failed lookup must NOT advance silently.
    assert "R1" not in stub.cursors


def test_bus_ack_rejects_both_event_and_last_seen_at(monkeypatch, capsys, stub):
    """Passing both inputs is ambiguous — refuse rather than pick one
    silently (which one the user meant is impossible to guess)."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_LAST_SEEN_AT", "2026-06-07T00:00:01.000000Z")
    monkeypatch.setenv("BEACON_BUS_ACK_EVENT_ID", "ev-trigger-1")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_ack()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "not both" in err or "both" in err


# ---------------------------------------------------------------------------
# directory (e-1134)
# ---------------------------------------------------------------------------

class _StubDirectoryClient(_StubApiClient):
    """Add list_sessions + list_user_sessions to the stub so the directory
    CLI can be exercised independently of the bus event stub above.

    ms-94 / e-2291 (2026-07-06 default reversal): cmd_bus_directory は
    default で list_user_sessions (cross-project) を呼ぶ、 --cwd-only 時のみ
    list_sessions (cwd-scoped) を呼ぶ。 両方 stub して両 code path を pin。
    """

    def __init__(self):
        super().__init__()
        self.sessions: list[dict] = []
        self.user_sessions: list[dict] = []
        self.last_query: dict = {}
        self.last_user_query: dict = {}

    def list_sessions(self, project_id, *, user_id="", machine="",
                      agent="", live_only=False, since_minutes=5,
                      healthy_only=False):
        self.last_query = {
            "user_id": user_id, "machine": machine, "agent": agent,
            "live_only": live_only, "since_minutes": since_minutes,
            "healthy_only": healthy_only,
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
            # e-1318: when healthy_only is set, the server drops anything
            # whose poll_health.healthy is not exactly True. Mirror the
            # contract so this stub stays an honest stand-in.
            if healthy_only:
                ph = s.get("poll_health") or {}
                if ph.get("healthy") is not True:
                    continue
            out.append(s)
        return out

    def list_user_sessions(self, *, live_only=False, since_minutes=5,
                           healthy_only=False, machine="", agent=""):
        """ms-94 / e-2291: cross-project session listing (= /me/sessions)。
        row に project_id / project_name が付いて返るのが仕様。
        """
        self.last_user_query = {
            "live_only": live_only, "since_minutes": since_minutes,
            "healthy_only": healthy_only, "machine": machine, "agent": agent,
        }
        out = []
        for s in self.user_sessions:
            actor = s.get("actor") or {}
            if machine and actor.get("machine") != machine:
                continue
            if agent and actor.get("agent") != agent:
                continue
            if healthy_only:
                ph = s.get("poll_health") or {}
                if ph.get("healthy") is not True:
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
    # ms-94 / e-2291 (2026-07-06 default reversal): 既存 test は cwd-scoped
    # 挙動を pin していたので、 clear と同時に --cwd-only 相当の env を stamp
    # して従来 code path (= list_sessions) をテストできる状態にする。 新
    # cross-project default を突く test は明示的に BEACON_DIR_CWD_ONLY を
    # delenv してから走らせる (= 下方 test_bus_directory_cross_project_default 系)。
    for k in ("BEACON_DIR_USER", "BEACON_DIR_MACHINE", "BEACON_DIR_AGENT",
              "BEACON_DIR_LIVE", "BEACON_DIR_HEALTHY",
              "BEACON_DIR_SINCE_MIN", "BEACON_JSON", "BEACON_BUS_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_DIR_CWD_ONLY", "1")


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
        # e-1318: --healthy defaults off, but the kwarg must still be wired
        # through the dispatcher → commands.py → api_client chain.
        "healthy_only": False,
    }


def test_bus_directory_forwards_healthy_filter_to_api(monkeypatch, capsys, dir_stub):
    """The e-1318 --healthy filter must reach api_client. Catches the
    regression where the dispatcher reads the flag but commands.py never
    forwards it — the picker would silently still include stale rows."""
    _clear_dir_env(monkeypatch)
    monkeypatch.setenv("BEACON_DIR_HEALTHY", "1")
    commands.cmd_bus_directory()
    assert dir_stub.last_query["healthy_only"] is True


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


# ---------------------------------------------------------------------------
# --healthy filter (e-1318 Option C true-heartbeat)
# ---------------------------------------------------------------------------

def test_bus_directory_healthy_filter_drops_stale_and_shutdown(
    monkeypatch, capsys, dir_stub,
):
    """Hands the stub three sessions — healthy, stale, shutdown — and
    pins that --healthy only keeps the healthy one. The stub mirrors the
    server's poll_health contract, so this test catches CLI-level filter
    forwarding *and* the consumer-side handling in one shot."""
    _clear_dir_env(monkeypatch)
    dir_stub.sessions = [
        {"session_id": "s-ok", "actor": {},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": True, "age_seconds": 1, "shutdown": False}},
        {"session_id": "s-stale", "actor": {},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": False, "age_seconds": 300, "shutdown": False}},
        {"session_id": "s-down", "actor": {},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": False, "age_seconds": 1, "shutdown": True}},
    ]
    monkeypatch.setenv("BEACON_DIR_HEALTHY", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_directory()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    sids = [s["session_id"] for s in parsed]
    assert sids == ["s-ok"], parsed


def test_bus_directory_json_includes_poll_health_for_every_row(
    monkeypatch, capsys, dir_stub,
):
    """Even without --healthy, every JSON row carries poll_health so the
    /beacon-dm-send Skill (separate task) can display the age + filter
    client-side."""
    _clear_dir_env(monkeypatch)
    dir_stub.sessions = [
        {"session_id": "s-1", "actor": {"email": "a@x"},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": True, "age_seconds": 2,
                         "shutdown": False, "last_poll_at": "2026-06-07T00:00:01Z",
                         "poll_interval_ms": 2000}},
    ]
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_directory()
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed[0]["poll_health"]["healthy"] is True
    assert parsed[0]["poll_health"]["age_seconds"] == 2


def test_bus_directory_human_output_shows_health_tag(
    monkeypatch, capsys, dir_stub,
):
    """The picker line must show a health tag so a human reviewer can
    tell at a glance which sessions are receive-capable RIGHT NOW vs.
    just last-heartbeat-recent. Without this tag, the picker is back
    to the pre-e-1318 ambiguity."""
    _clear_dir_env(monkeypatch)
    dir_stub.sessions = [
        {"session_id": "s-ok", "actor": {"email": "a@x"},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": True, "age_seconds": 2,
                         "shutdown": False}},
        {"session_id": "s-stale", "actor": {"email": "b@x"},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": False, "age_seconds": 300,
                         "shutdown": False}},
        {"session_id": "s-down", "actor": {"email": "c@x"},
         "last_active": "2026-06-07T00:00:01.000000Z",
         "poll_health": {"healthy": False, "age_seconds": 1,
                         "shutdown": True}},
        {"session_id": "s-old", "actor": {"email": "d@x"},
         "last_active": "2026-06-07T00:00:01.000000Z"},  # legacy, no poll_health
    ]
    commands.cmd_bus_directory()
    out = capsys.readouterr().out
    # Healthy line gets ok tag.
    assert "s-ok" in out and "health=ok" in out
    # Stale line gets stale tag with age.
    assert "s-stale" in out and "health=stale" in out
    # Shutdown line gets shutdown tag (regardless of age).
    assert "s-down" in out and "health=shutdown" in out
    # Legacy row (no poll_health) → unknown, so the user knows liveness
    # is undecided rather than falsely "ok".
    assert "s-old" in out and "health=unknown" in out


# ---------------------------------------------------------------------------
# ms-94 / e-2291 (2026-07-06): cross-project default reversal のテスト。
# 新 default (= --cwd-only 未指定 / --project 未指定 時) は list_user_sessions
# 経由で全 project 横断 lookup、 各行は [project_name] prefix 付きで表示される。
# ---------------------------------------------------------------------------

def test_bus_directory_default_uses_list_user_sessions(monkeypatch, capsys, dir_stub):
    """新 default: BEACON_DIR_CWD_ONLY / BEACON_BUS_PROJECT_ID 未指定 なら
    list_user_sessions が呼ばれる (list_sessions は呼ばれない)。 各行に
    project_name が prefix 付きで見えることを pin。"""
    for k in ("BEACON_DIR_USER", "BEACON_DIR_MACHINE", "BEACON_DIR_AGENT",
              "BEACON_DIR_LIVE", "BEACON_DIR_HEALTHY",
              "BEACON_DIR_SINCE_MIN", "BEACON_JSON",
              "BEACON_BUS_PROJECT_ID", "BEACON_DIR_CWD_ONLY"):
        monkeypatch.delenv(k, raising=False)
    dir_stub.user_sessions = [
        {"session_id": "cross-1",
         "actor": {"email": "u1@x", "machine": "mac", "agent": "claude"},
         "last_active": "2026-07-06T00:00:01.000000Z",
         "project_id": "proj-x", "project_name": "ProjectX"},
        {"session_id": "cross-2",
         "actor": {"email": "u2@x", "machine": "linux", "agent": "codex"},
         "last_active": "2026-07-06T00:00:02.000000Z",
         "project_id": "proj-y", "project_name": "ProjectY"},
    ]
    commands.cmd_bus_directory()
    out = capsys.readouterr().out
    # list_user_sessions 経路 - list_sessions は空 (cwd 経路呼ばれず)。
    assert dir_stub.last_query == {}
    assert dir_stub.last_user_query != {}
    # 各行に project_name が prefix 付きで表示される (= 誤送信防止の識別情報)。
    assert "[ProjectX]" in out and "cross-1" in out
    assert "[ProjectY]" in out and "cross-2" in out


def test_bus_directory_cwd_only_uses_list_sessions(monkeypatch, capsys, dir_stub):
    """--cwd-only 明示指定 (= BEACON_DIR_CWD_ONLY=1) では従来通り
    list_sessions が呼ばれる、 cross-project は使わない。"""
    _clear_dir_env(monkeypatch)  # ← auto set BEACON_DIR_CWD_ONLY=1
    dir_stub.sessions = [
        {"session_id": "cwd-1",
         "actor": {"email": "u1@x"},
         "last_active": "2026-07-06T00:00:01.000000Z"},
    ]
    dir_stub.user_sessions = [
        {"session_id": "should-not-appear",
         "project_id": "proj-x", "project_name": "ProjectX"},
    ]
    commands.cmd_bus_directory()
    out = capsys.readouterr().out
    # cwd-only path: list_sessions が呼ばれる、 list_user_sessions は呼ばれない。
    assert dir_stub.last_query != {}
    assert dir_stub.last_user_query == {}
    assert "cwd-1" in out
    # cross-project 側の row は絶対に出ない。
    assert "should-not-appear" not in out
    # cwd-only 時は project prefix なし (= 暗黙で cwd 内なので冗長)。
    assert "[ProjectX]" not in out


def test_bus_directory_explicit_project_uses_list_sessions(monkeypatch, capsys, dir_stub):
    """--project X 明示指定は cwd-only 相当の限定 lookup で list_sessions 経路。
    cross-project スキャンは行わない (= caller が明示的に target 決めている)。"""
    for k in ("BEACON_DIR_USER", "BEACON_DIR_MACHINE", "BEACON_DIR_AGENT",
              "BEACON_DIR_LIVE", "BEACON_DIR_HEALTHY",
              "BEACON_DIR_SINCE_MIN", "BEACON_JSON", "BEACON_DIR_CWD_ONLY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEACON_BUS_PROJECT_ID", "explicit-proj")
    dir_stub.sessions = [
        {"session_id": "explicit-1", "actor": {"email": "u@x"},
         "last_active": "2026-07-06T00:00:01.000000Z"},
    ]
    commands.cmd_bus_directory()
    assert dir_stub.last_query != {}
    assert dir_stub.last_user_query == {}
    out = capsys.readouterr().out
    assert "explicit-1" in out


# ---------------------------------------------------------------------------
# status (ms-54 / e-1348)
#
# The 3-stage view must:
#   1. Always show sent ✓ (the row exists ⇒ server stamped created_at).
#   2. Render delivered/opened ✗ as "(not yet)" when missing, so the sender
#      can localize the stall point at a glance.
#   3. Render delivered/opened ✓ with the by-attribution when stamped.
#   4. Emit a clean 1-line error (exit 1) on unknown event_id rather than
#      a Python traceback.
#   5. JSON mode returns the raw event dict for scripts.
# ---------------------------------------------------------------------------


def test_bus_status_requires_event_id(monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_status()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Usage:" in err and "bus status" in err


def test_bus_status_unknown_event_exits_with_error_line(
        monkeypatch, capsys, stub):
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_EVENT_ID", "ghost-evt")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_bus_status()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    err = captured.err
    assert "not found" in err
    assert "ghost-evt" in err
    # No traceback ever leaks to the user.
    assert "Traceback" not in err


def test_bus_status_renders_3_stages_with_missing_marked_not_yet(
        monkeypatch, capsys, stub):
    """The structurally important case: a DM that was sent but never
    delivered/opened. The renderer must mark the gap rather than print
    blank cells — otherwise the sender can't tell 'no stamp yet' from
    'the field doesn't exist on this build'."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    stub.post_bus_event(
        "proj-1", "dm", sender_session_id="sender-X",
        payload={"recipient_session_id": "recv-Y", "text": "hi"},
    )
    eid = stub.events[-1]["event_id"]
    monkeypatch.setenv("BEACON_BUS_EVENT_ID", eid)
    commands.cmd_bus_status()
    out = capsys.readouterr().out
    assert f"event: {eid}" in out
    assert "channel: dm" in out
    assert "sender:" in out and "sender-X" in out
    assert "✓ sent" in out
    # Stages not yet stamped must show ✗ AND "(not yet)".
    assert "✗ delivered" in out and "(not yet)" in out
    assert "✗ opened" in out


def test_bus_status_renders_full_3_stages_when_all_stamped(
        monkeypatch, capsys, stub):
    """The happy path: every stage has a timestamp and a `by` attribution.
    The renderer must surface the attribution so an auditor can answer
    'who opened it'."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    stub.post_bus_event(
        "proj-1", "dm", sender_session_id="sender-X",
        payload={"recipient_session_id": "recv-Y", "text": "hi"},
    )
    eid = stub.events[-1]["event_id"]
    stub.stamp_receipt(
        eid, stage="delivered", by="bridge-A",
        ts="2026-06-09T05:00:01.000000Z",
    )
    stub.stamp_receipt(
        eid, stage="opened", by="bridge-A",
        ts="2026-06-09T05:00:01.234567Z",
    )
    monkeypatch.setenv("BEACON_BUS_EVENT_ID", eid)
    commands.cmd_bus_status()
    out = capsys.readouterr().out
    assert "✓ sent" in out
    assert "✓ delivered" in out and "by bridge-A" in out
    assert "✓ opened" in out
    # The bridge identity for `opened` is printed on the opened row, not
    # collapsed into a single 'by' line — the renderer keeps the rows
    # independent so a future split-attribution case (delivered=X, opened=Y)
    # surfaces correctly.
    delivered_idx = out.index("✓ delivered")
    opened_idx = out.index("✓ opened")
    assert delivered_idx < opened_idx
    # "(not yet)" must NOT appear when every stage is stamped.
    assert "(not yet)" not in out


def test_bus_status_json_emits_raw_event_dict(monkeypatch, capsys, stub):
    """Scripts should be able to pivot on the receipt fields directly
    rather than parse the human view. JSON mode returns whatever the
    server returned, including the stage timestamps when present."""
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_CHANNEL", "dm")
    stub.post_bus_event(
        "proj-1", "dm", sender_session_id="sender-X",
        payload={"recipient_session_id": "recv-Y"},
    )
    eid = stub.events[-1]["event_id"]
    stub.stamp_receipt(
        eid, stage="delivered", by="bridge-A",
        ts="2026-06-09T05:00:01.000000Z",
    )
    monkeypatch.setenv("BEACON_BUS_EVENT_ID", eid)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_bus_status()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["event_id"] == eid
    assert parsed["channel"] == "dm"
    assert parsed["delivered_at"] == "2026-06-09T05:00:01.000000Z"
    assert parsed["delivered_by"] == "bridge-A"
    # opened was never stamped; field should be absent from the dict.
    assert "opened_at" not in parsed
