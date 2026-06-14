"""CLI tests for `beacon stuck check` (ms-55 e-1649)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _StubBus:
    def __init__(self):
        self.events = []

    def post_bus_event(self, project_id, channel, *, sender_session_id="",
                       payload=None, delivery="propose-to-ai",
                       envelope=None, requested_action=None):
        ev = {
            "event_id": f"e-{len(self.events) + 1}",
            "channel": channel,
            "payload": payload or {},
        }
        self.events.append(ev)
        return ev


@pytest.fixture
def stub(monkeypatch):
    s = _StubBus()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (s, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(
        commands, "_resolve_session_id", lambda: "sv-detector",
    )
    return s


def _clear_env(monkeypatch):
    for k in (
        "BEACON_STUCK_TELEMETRY_INLINE",
        "BEACON_STUCK_TELEMETRY_FILE",
        "BEACON_STUCK_TIMEOUT_MINUTES",
        "BEACON_STUCK_DRY_RUN",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(k, raising=False)


def test_requires_telemetry_input(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit):
        commands.cmd_stuck_check()
    err = capsys.readouterr().err
    assert "telemetry" in err


def test_no_stuck_clean_output(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    fresh = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.setenv(
        "BEACON_STUCK_TELEMETRY_INLINE",
        json.dumps([{"session_id": "sv-A", "last_active": fresh}]),
    )
    monkeypatch.setenv("BEACON_STUCK_TIMEOUT_MINUTES", "30")
    commands.cmd_stuck_check()
    out = capsys.readouterr().out
    assert "No stuck sessions" in out
    assert len(stub.events) == 0


def test_stuck_session_emits_signal(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=45))
    monkeypatch.setenv(
        "BEACON_STUCK_TELEMETRY_INLINE",
        json.dumps([
            {"session_id": "sv-stuck", "last_active": stale,
             "ms_id": "ms-55"},
        ]),
    )
    monkeypatch.setenv("BEACON_STUCK_TIMEOUT_MINUTES", "30")
    commands.cmd_stuck_check()
    out = capsys.readouterr().out
    assert "sv-stuck" in out
    assert "STUCK signal posted" in out
    assert len(stub.events) == 1
    ev = stub.events[0]
    assert ev["channel"] == "stop-signal"
    assert ev["payload"]["reason_kind"] == "stuck"
    assert ev["payload"]["target"] == {"kind": "session", "id": "sv-stuck"}


def test_dry_run_does_not_emit(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=45))
    monkeypatch.setenv(
        "BEACON_STUCK_TELEMETRY_INLINE",
        json.dumps([{"session_id": "sv-stuck", "last_active": stale}]),
    )
    monkeypatch.setenv("BEACON_STUCK_DRY_RUN", "1")
    commands.cmd_stuck_check()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert len(stub.events) == 0


def test_invalid_timeout(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STUCK_TELEMETRY_INLINE", "[]")
    monkeypatch.setenv("BEACON_STUCK_TIMEOUT_MINUTES", "not-a-number")
    with pytest.raises(SystemExit):
        commands.cmd_stuck_check()
    err = capsys.readouterr().err
    assert "integer" in err


def test_invalid_telemetry_json(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STUCK_TELEMETRY_INLINE", "{not json")
    with pytest.raises(SystemExit):
        commands.cmd_stuck_check()
    err = capsys.readouterr().err
    assert "JSON" in err


def test_telemetry_must_be_array(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_STUCK_TELEMETRY_INLINE",
                       json.dumps({"session_id": "sv-A"}))
    with pytest.raises(SystemExit):
        commands.cmd_stuck_check()
    err = capsys.readouterr().err
    assert "array" in err


def test_json_mode(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=45))
    monkeypatch.setenv(
        "BEACON_STUCK_TELEMETRY_INLINE",
        json.dumps([
            {"session_id": "sv-A", "last_active": stale},
            {"session_id": "sv-B",
             "last_active": _iso(datetime.now(timezone.utc) -
                                  timedelta(minutes=5))},
        ]),
    )
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_stuck_check()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["timeout_minutes"] == 30
    assert len(parsed["stuck"]) == 1
    assert parsed["stuck"][0]["session_id"] == "sv-A"


def test_telemetry_file_mode(monkeypatch, capsys, stub, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "tel.json"
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=45))
    p.write_text(json.dumps([
        {"session_id": "sv-A", "last_active": stale, "task_id": "e-1649"},
    ]))
    monkeypatch.setenv("BEACON_STUCK_TELEMETRY_FILE", str(p))
    commands.cmd_stuck_check()
    assert len(stub.events) == 1
    assert stub.events[0]["payload"]["machine_reason"]["task_id"] == "e-1649"


def test_both_inline_and_file_rejected(monkeypatch, capsys, stub, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "tel.json"
    p.write_text("[]")
    monkeypatch.setenv("BEACON_STUCK_TELEMETRY_INLINE", "[]")
    monkeypatch.setenv("BEACON_STUCK_TELEMETRY_FILE", str(p))
    with pytest.raises(SystemExit):
        commands.cmd_stuck_check()


def test_ignore_flag_in_telemetry(monkeypatch, capsys, stub):
    _clear_env(monkeypatch)
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=45))
    monkeypatch.setenv(
        "BEACON_STUCK_TELEMETRY_INLINE",
        json.dumps([
            {"session_id": "sv-A", "last_active": stale, "ignore": True},
            {"session_id": "sv-B", "last_active": stale},
        ]),
    )
    commands.cmd_stuck_check()
    out = capsys.readouterr().out
    assert "sv-B" in out
    assert "sv-A" not in out
    assert len(stub.events) == 1
