"""Tests for fork session log / Trek summary inclusion in retrospect
search (ms-79 / e-1835 / UC10-F4).

Verifies:

* ``_load_session_logs`` discovers local-mode .beacon/session_logs/*.json
* ``_load_bus_archive`` discovers local-mode .beacon/bus_archive/*.json
* ``_load_trek_summaries`` discovers .beacon/treks/*/summaries/*.json
* End-to-end: cmd_search with --include-session-logs --include-trek
  surfaces fork session log + Trek summary rows in the result set,
  with the from_fork / from_trek markers set.
* End-to-end: cmd_search with --include-bus-dm surfaces DM archive
  rows with the from_dm marker.

These tests guard against the regression UC10-F4 worried about:
"fork で何を決めたか" / "Trek 内でいつ合意したか" が retrospect から
見えず cross-session 知見が遭難する。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


def _make_project(tmp: Path) -> Path:
    beacon_dir = tmp / ".beacon"
    beacon_dir.mkdir()
    project_file = beacon_dir / "project.json"
    project_file.write_text(
        json.dumps({
            "name": "test",
            "milestones": [
                {"id": "ms-1", "title": "T", "status": "in_progress",
                 "entries": [], "progress": 0},
            ],
            "summary": "",
        }),
        encoding="utf-8",
    )
    return project_file


@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        project_file = _make_project(tmp_p)
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.chdir(tmp)
        yield tmp_p


def test_load_session_logs_empty_when_no_dir(project):
    # No .beacon/session_logs/ yet → empty list, no exception.
    assert commands._load_session_logs() == []


def test_load_session_logs_finds_local_files(project):
    sl_dir = project / ".beacon" / "session_logs"
    sl_dir.mkdir()
    (sl_dir / "sv-fork-1.json").write_text(
        json.dumps({
            "session_id": "sv-fork-1",
            "summary": "Fork session decided X",
            "fork_parent_session_id": "sv-parent",
            "created_at": "2026-06-15T12:00:00Z",
        }),
        encoding="utf-8",
    )
    rows = commands._load_session_logs()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sv-fork-1"
    assert rows[0]["fork_parent_session_id"] == "sv-parent"


def test_load_session_logs_ignores_malformed_json(project):
    sl_dir = project / ".beacon" / "session_logs"
    sl_dir.mkdir()
    (sl_dir / "good.json").write_text(
        json.dumps({"session_id": "s-1", "summary": "ok"}), encoding="utf-8"
    )
    (sl_dir / "bad.json").write_text("not json {{", encoding="utf-8")
    rows = commands._load_session_logs()
    # Bad file silently skipped; good file kept.
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s-1"


def test_load_bus_archive_finds_local_files(project):
    ba_dir = project / ".beacon" / "bus_archive"
    ba_dir.mkdir()
    (ba_dir / "ev-1.json").write_text(
        json.dumps({
            "event_id": "ev-1",
            "channel": "dm",
            "payload": "cross-project DM",
            "created_at": "2026-06-15T13:00:00Z",
        }),
        encoding="utf-8",
    )
    rows = commands._load_bus_archive()
    assert len(rows) == 1
    assert rows[0]["channel"] == "dm"


def test_load_trek_summaries_finds_local_files(project):
    sumdir = project / ".beacon" / "treks" / "trek-99" / "summaries"
    sumdir.mkdir(parents=True)
    (sumdir / "ts-1.json").write_text(
        json.dumps({
            "id": "ts-1",
            "text": "Reached consensus on Y",
            "created_at": "2026-06-15T14:00:00Z",
        }),
        encoding="utf-8",
    )
    rows = commands._load_trek_summaries()
    assert len(rows) == 1
    assert rows[0]["trek_id"] == "trek-99"
    assert rows[0]["id"] == "ts-1"


def test_cmd_search_with_session_logs_surfaces_fork_marker(project, monkeypatch):
    # Plant a fork session_log
    sl_dir = project / ".beacon" / "session_logs"
    sl_dir.mkdir()
    (sl_dir / "sv-fork-1.json").write_text(
        json.dumps({
            "session_id": "sv-fork-1",
            "summary": "Fork session decided X via consensus",
            "fork_parent_session_id": "sv-parent",
            "created_at": "2026-06-15T12:00:00Z",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BEACON_QUERY", "consensus")
    monkeypatch.setenv("BEACON_INCLUDE_SESSION_LOGS", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_search()
    out = json.loads(buf.getvalue())
    fork_rows = [r for r in out["results"]
                 if r.get("entity_type") == "session_log"]
    assert len(fork_rows) == 1
    assert fork_rows[0]["from_fork"] is True


def test_cmd_search_with_trek_surfaces_trek_marker(project, monkeypatch):
    sumdir = project / ".beacon" / "treks" / "trek-99" / "summaries"
    sumdir.mkdir(parents=True)
    (sumdir / "ts-1.json").write_text(
        json.dumps({
            "id": "ts-1",
            "text": "Reached agreement on Y via Trek consensus",
            "created_at": "2026-06-15T14:00:00Z",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BEACON_QUERY", "agreement")
    monkeypatch.setenv("BEACON_INCLUDE_TREK", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_search()
    out = json.loads(buf.getvalue())
    trek_rows = [r for r in out["results"]
                 if r.get("entity_type") == "trek_summary"]
    assert len(trek_rows) == 1
    assert trek_rows[0]["from_trek"] is True


def test_cmd_search_with_bus_dm_surfaces_dm_marker(project, monkeypatch):
    ba_dir = project / ".beacon" / "bus_archive"
    ba_dir.mkdir()
    (ba_dir / "ev-1.json").write_text(
        json.dumps({
            "event_id": "ev-1",
            "channel": "dm",
            "payload": "Cross-project review feedback approval",
            "created_at": "2026-06-15T13:00:00Z",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("BEACON_QUERY", "approval")
    monkeypatch.setenv("BEACON_INCLUDE_BUS_DM", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_search()
    out = json.loads(buf.getvalue())
    dm_rows = [r for r in out["results"]
               if r.get("entity_type") == "bus_event"]
    assert len(dm_rows) == 1
    assert dm_rows[0]["from_dm"] is True


def test_cmd_search_default_path_does_not_include_extension_types(project,
                                                                    monkeypatch):
    """Without any --include-* flags, extension entity types are NOT merged.
    Back-compat: existing retrospect Skill behavior is unchanged."""
    sl_dir = project / ".beacon" / "session_logs"
    sl_dir.mkdir()
    (sl_dir / "sv-1.json").write_text(
        json.dumps({"session_id": "sv-1", "summary": "data present",
                    "created_at": "2026-06-15"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BEACON_QUERY", "data")
    monkeypatch.setenv("BEACON_JSON", "1")
    # No --include-* flags
    monkeypatch.delenv("BEACON_INCLUDE_SESSION_LOGS", raising=False)
    monkeypatch.delenv("BEACON_INCLUDE_BUS_DM", raising=False)
    monkeypatch.delenv("BEACON_INCLUDE_TREK", raising=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_search()
    out = json.loads(buf.getvalue())
    # No session_log row should be present in default path.
    assert all(r.get("entity_type") != "session_log" for r in out["results"])


def test_cmd_search_source_filter_excludes_human_default_when_only_auto(project,
                                                                          monkeypatch):
    """e-1833: --source auto-op narrows result to auto-op tagged entries only."""
    import core
    data = commands.load_project()
    core.log_commit(data, ms_id="ms-1", commit_hash="h-human",
                    message="human dialog work item", date="2026-06-15")
    core.log_commit(data, ms_id="ms-1", commit_hash="h-autop",
                    message="auto-op work item", date="2026-06-15",
                    source="auto-op")
    # Persist (cmd_search calls load_project again, so we must write back).
    commands.save_project(data)

    monkeypatch.setenv("BEACON_QUERY", "work")
    monkeypatch.setenv("BEACON_SOURCE", "auto-op")
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_search()
    out = json.loads(buf.getvalue())
    commit_rows = [r for r in out["results"] if r.get("entity_type") == "commit"]
    # Only the auto-op commit should survive the filter.
    assert len(commit_rows) == 1
    assert "auto-op" in commit_rows[0].get("title", "").lower() or \
           "auto-op" in commit_rows[0].get("snippet", "").lower()
