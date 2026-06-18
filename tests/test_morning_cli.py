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
        "BEACON_MORNING_NO_DOC",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(k, raising=False)
    # ms-55 e-1733: the doc-save side effect of cmd_morning needs a
    # project.json + writable .beacon/documents dir. The mechanics
    # tests below don't care about that surface, so opt out by default;
    # the dedicated doc-save tests override BEACON_MORNING_NO_DOC.
    monkeypatch.setenv("BEACON_MORNING_NO_DOC", "1")


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


def _setup_local_project(monkeypatch, tmp_path):
    """Bring up a minimal local project.json + .beacon/documents path.

    Returns the docs_dir Path. Used by the doc-save tests below."""
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(exist_ok=True)
    project_file = beacon_dir / "project.json"
    project_file.write_text(json.dumps({
        "name": "test-morning-doc",
        "schema_version": "1",
        "summary": "",
        "milestones": [{
            "id": "ms-1", "title": "Active",
            "status": "in_progress", "progress": 0,
            "target_date": "", "entries": [],
        }],
    }))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    return beacon_dir / "documents"


def test_doc_save_default_writes_report(monkeypatch, capsys, tmp_path):
    """ms-55 e-1733: by default, cmd_morning saves the rendered briefing
    as a scope=report doc so it's discoverable via the Web UI."""
    _clear_env(monkeypatch)
    # Override the default opt-out from _clear_env.
    monkeypatch.delenv("BEACON_MORNING_NO_DOC", raising=False)
    docs_dir = _setup_local_project(monkeypatch, tmp_path)

    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        _make_release_event(_iso(NOW_REF - timedelta(hours=1))),
    ]))
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    commands.cmd_morning()

    out = capsys.readouterr().out
    assert "Saved briefing as doc" in out
    # Verify the doc landed on disk under .beacon/documents/.
    assert docs_dir.exists()
    docs = sorted(docs_dir.glob("morning-briefing-*.md"))
    assert len(docs) == 1, f"expected one doc, got {[d.name for d in docs]}"
    body = docs[0].read_text()
    assert "scope: report" in body
    assert "Beacon morning briefing" in body
    # The MS timeline got a doc-add entry linked to the saved doc_id.
    saved = json.loads((tmp_path / ".beacon/project.json").read_text())
    entries = saved["milestones"][0]["entries"]
    matches = [
        e for e in entries
        if e.get("type") == "save"
        and "morning briefing" in e.get("description", "")
    ]
    assert len(matches) == 1, entries


def test_doc_save_no_doc_flag_skips(monkeypatch, capsys, tmp_path):
    """--no-doc / BEACON_MORNING_NO_DOC=1 keeps the surface terminal-only."""
    _clear_env(monkeypatch)
    # _clear_env already sets the opt-out — confirm by ensuring no doc
    # appears even when the local project is wired up.
    docs_dir = _setup_local_project(monkeypatch, tmp_path)

    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        _make_release_event(_iso(NOW_REF - timedelta(hours=1))),
    ]))
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    commands.cmd_morning()

    out = capsys.readouterr().out
    assert "Beacon morning briefing" in out
    assert "Saved briefing as doc" not in out
    assert not docs_dir.exists() or list(docs_dir.glob("morning-briefing-*.md")) == []


def test_doc_save_in_json_mode(monkeypatch, capsys, tmp_path):
    """JSON mode also returns the doc save info as a `doc` sub-object."""
    _clear_env(monkeypatch)
    monkeypatch.delenv("BEACON_MORNING_NO_DOC", raising=False)
    docs_dir = _setup_local_project(monkeypatch, tmp_path)

    p = tmp_path / "ev.json"
    p.write_text(json.dumps([
        _make_release_event(_iso(NOW_REF - timedelta(hours=1))),
    ]))
    monkeypatch.setenv("BEACON_MORNING_EVENTS_FILE", str(p))
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_morning()
    parsed = json.loads(capsys.readouterr().out.strip())
    assert parsed["counts"]["completed"] == 1
    assert "doc" in parsed
    assert parsed["doc"]["status"] == "saved"
    assert parsed["doc"]["doc_id"]
    # File should exist on disk too.
    assert (docs_dir / f"{parsed['doc']['doc_id']}.md").exists()


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
