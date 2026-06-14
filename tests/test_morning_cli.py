"""CLI tests for `beacon morning` (ms-55 e-1650)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402
import stop_signal  # noqa: E402
import claims as _claims  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW_REF = datetime.now(timezone.utc)


def _make_stop_event(ts, *, scope="global", reason_kind="manual"):
    payload = stop_signal.build_stop_payload(
        scope=scope,
        issued_by_session_id="sv-A",
        reason_kind=reason_kind,
        issued_at=ts,
    )
    return {
        "event_id": f"ev-{ts}",
        "channel": "stop-signal",
        "payload": payload,
        "created_at": ts,
    }


def _make_release_event(ts, *, outcome="completed"):
    payload = _claims.build_release_payload(
        claim_id=f"cl-{ts}",
        outcome=outcome,
        from_session_id="sv-A",
        issued_at=ts,
    )
    return {
        "event_id": f"ev-{ts}",
        "channel": "claim-signal",
        "payload": payload,
        "created_at": ts,
    }


def _clear_env(monkeypatch):
    for k in (
        "BEACON_MORNING_SINCE_HOURS",
        "BEACON_MORNING_EVENTS_FILE",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(k, raising=False)


def test_invalid_since_hours(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MORNING_SINCE_HOURS", "not-a-number")
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", "/nonexistent")
    with pytest.raises(SystemExit):
        commands.cmd_morning()
    err = capsys.readouterr().err
    assert "number" in err


def test_non_positive_since_hours(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MORNING_SINCE_HOURS", "0")
    with pytest.raises(SystemExit):
        commands.cmd_morning()
    err = capsys.readouterr().err
    assert "> 0" in err


def test_events_file_missing_errors(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", "/does/not/exist.json")
    with pytest.raises(SystemExit):
        commands.cmd_morning()
    err = capsys.readouterr().err
    assert "events file" in err.lower() or "no such" in err.lower()


def test_events_file_not_array_errors(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "ev.json"
    p.write_text(json.dumps({"not": "an array"}))
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    with pytest.raises(SystemExit):
        commands.cmd_morning()


def test_events_file_empty_renders_empty_briefing(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "ev.json"
    p.write_text("[]")
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    commands.cmd_morning()
    out = capsys.readouterr().out
    assert "Beacon morning briefing" in out
    assert "(none)" in out


def _make_stuck_event(ts, *, target_id="sv-stuck"):
    payload = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-detector",
        target_kind="session",
        target_id=target_id,
        reason_kind="stuck",
        issued_at=ts,
    )
    return {
        "event_id": f"ev-stuck-{ts}",
        "channel": "stop-signal",
        "payload": payload,
        "created_at": ts,
    }


def test_events_file_categorizes(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        _make_release_event(_iso(NOW_REF - timedelta(hours=1))),
        _make_stop_event(_iso(NOW_REF - timedelta(minutes=30)),
                          reason_kind="build_fail"),
        _make_stuck_event(_iso(NOW_REF - timedelta(minutes=15))),
        _make_release_event(_iso(NOW_REF - timedelta(minutes=5)),
                             outcome="abandoned"),
    ]))
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    commands.cmd_morning()
    out = capsys.readouterr().out
    # All 4 buckets should have hits.
    assert "✓ 1" in out and "⚠ 1" in out and "✗ 1" in out and "⏱ 1" in out


def test_json_mode(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        _make_release_event(_iso(NOW_REF - timedelta(hours=1))),
    ]))
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_morning()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["counts"]["completed"] == 1
    assert len(parsed["entries"]) == 1
    assert parsed["entries"][0]["bucket"] == "completed"


def test_window_filters_old_events(monkeypatch, capsys, tmp_path):
    _clear_env(monkeypatch)
    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        _make_release_event(_iso(NOW_REF - timedelta(days=2))),  # too old
        _make_release_event(_iso(NOW_REF - timedelta(minutes=30))),
    ]))
    monkeypatch.setenv("BEACON_MORNING_SINCE_HOURS", "12")
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_morning()
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["counts"]["completed"] == 1


def test_bus_query_path(monkeypatch, capsys):
    """When no --events-file is given, the CLI queries the bus client."""
    _clear_env(monkeypatch)

    class _StubBus:
        def __init__(self):
            self.events = {
                "stop-signal": [_make_stop_event(
                    _iso(NOW_REF - timedelta(minutes=30)),
                    reason_kind="manual",
                )],
                "claim-signal": [_make_release_event(
                    _iso(NOW_REF - timedelta(minutes=10)),
                )],
            }

        def list_unread_bus_events(self, project_id, recipient_id, *,
                                    channel="", limit=100):
            return self.events.get(channel, [])

    stub = _StubBus()
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_morning()
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["counts"]["completed"] == 1
    assert parsed["counts"]["halted"] == 1
