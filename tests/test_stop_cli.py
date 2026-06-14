"""CLI tests for `beacon stop` / `beacon resume` (ms-55 e-1646).

Mirrors the structure of test_bus_cli.py — stubs out _get_api_client so
the Python entry points can be exercised end-to-end without standing up
a server.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import stop_signal  # noqa: E402


# ---------------------------------------------------------------------------
# Stub harness — same shape as test_bus_cli._StubApiClient but smaller.
# ---------------------------------------------------------------------------


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
            "envelope": envelope,
            "requested_action": requested_action,
            "created_at": f"2026-06-15T00:00:{self._counter:02d}.000000Z",
        }
        self.events.append(ev)
        return ev

    def list_unread_bus_events(self, project_id, recipient_id, *,
                                channel="", limit=100):
        out = []
        for ev in self.events:
            if channel and ev["channel"] != channel:
                continue
            out.append(ev)
        return out


@pytest.fixture
def stub(monkeypatch):
    s = _StubBus()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (s, {"project_id": "proj-1"}),
    )
    # Stable session id so tests are deterministic.
    monkeypatch.setattr(
        commands, "_resolve_session_id",
        lambda: "sv-test",
    )
    return s


def _clear_env(monkeypatch):
    for k in (
        "BEACON_STOP_TARGET_KIND",
        "BEACON_STOP_TARGET_ID",
        "BEACON_STOP_REASON",
        "BEACON_STOP_REASON_KIND",
        "BEACON_STOP_MACHINE_REASON",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# beacon stop scoped
# ---------------------------------------------------------------------------

def test_stop_scoped_requires_target(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        commands.cmd_stop_scoped()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "target" in err
    assert len(stub.events) == 0


def test_stop_scoped_posts_event(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    monkeypatch.setenv("BEACON_STOP_REASON", "build fail 3 retries")
    monkeypatch.setenv("BEACON_STOP_REASON_KIND", "build_fail")
    commands.cmd_stop_scoped()
    assert len(stub.events) == 1
    ev = stub.events[0]
    assert ev["channel"] == "stop-signal"
    assert ev["sender_session_id"] == "sv-test"
    p = ev["payload"]
    assert p["kind"] == "stop"
    assert p["scope"] == "scoped"
    assert p["target"] == {"kind": "ms", "id": "ms-55"}
    assert p["reason"] == "build fail 3 retries"
    assert p["reason_kind"] == "build_fail"
    out = capsys.readouterr().out
    assert "STOP signal (scoped)" in out
    assert "ms:ms-55" in out


def test_stop_scoped_json_mode(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "task")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "e-1646")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_stop_scoped()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["channel"] == "stop-signal"
    assert parsed["payload"]["target"] == {"kind": "task", "id": "e-1646"}


def test_stop_scoped_invalid_target_kind(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "release")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "v0.1")
    with pytest.raises(SystemExit):
        commands.cmd_stop_scoped()
    err = capsys.readouterr().err
    assert "target_kind" in err
    assert len(stub.events) == 0  # no event posted on validation error


def test_stop_scoped_invalid_reason_kind(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    monkeypatch.setenv("BEACON_STOP_REASON_KIND", "catastrophic")
    with pytest.raises(SystemExit):
        commands.cmd_stop_scoped()
    err = capsys.readouterr().err
    assert "reason_kind" in err
    assert len(stub.events) == 0


def test_stop_scoped_machine_reason_must_be_object(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    monkeypatch.setenv("BEACON_STOP_MACHINE_REASON", "[1, 2]")  # array, not object
    with pytest.raises(SystemExit):
        commands.cmd_stop_scoped()
    err = capsys.readouterr().err
    assert "JSON object" in err


def test_stop_scoped_machine_reason_invalid_json(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    monkeypatch.setenv("BEACON_STOP_MACHINE_REASON", "{not json")
    with pytest.raises(SystemExit):
        commands.cmd_stop_scoped()
    err = capsys.readouterr().err
    assert "valid JSON" in err


def test_stop_scoped_machine_reason_attached(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    mr = {"retry_count": 3, "last_error": "exit 137"}
    monkeypatch.setenv("BEACON_STOP_MACHINE_REASON", json.dumps(mr))
    commands.cmd_stop_scoped()
    assert stub.events[0]["payload"]["machine_reason"] == mr


def test_stop_scoped_no_session_id_errors(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    monkeypatch.setattr(commands, "_resolve_session_id", lambda: "")
    with pytest.raises(SystemExit):
        commands.cmd_stop_scoped()
    err = capsys.readouterr().err
    assert "session_id" in err


# ---------------------------------------------------------------------------
# beacon stop global
# ---------------------------------------------------------------------------

def test_stop_global_minimum(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    commands.cmd_stop_global()
    assert len(stub.events) == 1
    p = stub.events[0]["payload"]
    assert p["scope"] == "global"
    assert "target" not in p
    out = capsys.readouterr().out
    assert "GLOBAL" in out


def test_stop_global_with_reason(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_REASON", "hostile prompt detected")
    monkeypatch.setenv("BEACON_STOP_REASON_KIND", "approved_actions_violation")
    commands.cmd_stop_global()
    p = stub.events[0]["payload"]
    assert p["reason"] == "hostile prompt detected"
    assert p["reason_kind"] == "approved_actions_violation"


def test_stop_global_json_mode(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_stop_global()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["payload"]["scope"] == "global"


# ---------------------------------------------------------------------------
# beacon stop status
# ---------------------------------------------------------------------------

def test_stop_status_empty(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    commands.cmd_stop_status()
    out = capsys.readouterr().out
    assert "No active stop signals" in out


def test_stop_status_lists_active(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    # 2 stops, 1 resume — the resume clears one of them.
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    commands.cmd_stop_scoped()
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-66")
    commands.cmd_stop_scoped()
    # Now resume ms-66.
    monkeypatch.delenv("BEACON_STOP_TARGET_KIND", raising=False)
    monkeypatch.delenv("BEACON_STOP_TARGET_ID", raising=False)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-66")
    commands.cmd_resume_scoped()

    # Clear capsys before the status call so we only assert on status output.
    capsys.readouterr()
    monkeypatch.delenv("BEACON_STOP_TARGET_KIND", raising=False)
    monkeypatch.delenv("BEACON_STOP_TARGET_ID", raising=False)
    commands.cmd_stop_status()
    out = capsys.readouterr().out
    assert "ms:ms-55" in out
    assert "ms:ms-66" not in out  # resumed
    assert "Active stop signals (1)" in out


def test_stop_status_json_mode(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    commands.cmd_stop_global()
    capsys.readouterr()
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_stop_status()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["scope"] == "global"
    # raw_event is stripped for compactness
    assert "raw_event" not in parsed[0]


# ---------------------------------------------------------------------------
# beacon resume
# ---------------------------------------------------------------------------

def test_resume_global(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    commands.cmd_resume_global()
    assert len(stub.events) == 1
    p = stub.events[0]["payload"]
    assert p["kind"] == "resume"
    assert p["scope"] == "global"


def test_resume_scoped(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "ms")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "ms-55")
    commands.cmd_resume_scoped()
    p = stub.events[0]["payload"]
    assert p["kind"] == "resume"
    assert p["target"] == {"kind": "ms", "id": "ms-55"}


def test_resume_scoped_requires_target(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit):
        commands.cmd_resume_scoped()
    err = capsys.readouterr().err
    assert "target" in err


# ---------------------------------------------------------------------------
# Sanity: roundtrip — what stop_scoped posts is what parse_stop_event reads.
# ---------------------------------------------------------------------------

def test_roundtrip_post_then_parse(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STOP_TARGET_KIND", "session")
    monkeypatch.setenv("BEACON_STOP_TARGET_ID", "sv-target")
    monkeypatch.setenv("BEACON_STOP_REASON", "stale heartbeat")
    monkeypatch.setenv("BEACON_STOP_REASON_KIND", "stuck")
    commands.cmd_stop_scoped()
    ev = stub.events[0]
    rec = stop_signal.parse_stop_event(ev)
    assert rec is not None
    assert rec["scope"] == "scoped"
    assert rec["target"] == {"kind": "session", "id": "sv-target"}
    assert rec["reason_kind"] == "stuck"
    assert stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-target",
    )
    assert not stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-other",
    )
