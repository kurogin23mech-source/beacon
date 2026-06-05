"""Tests for `beacon summary` soft-deprecation (ms-57 / e-1040).

SPEC v2 splits summary's three roles:
  - cross-session hand-off → session log (e-1037)
  - human narrative        → project-vision doc
The legacy `beacon summary "text"` write path still works (so the
PostToolUse hook + /beacon-log Skill don't break) but interactive
calls get a stderr deprecation note, and the Web UI no longer
renders the summary banner (handled separately in index.html).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "project.json").write_text(
            json.dumps({
                "name": "t",
                "summary": "",
                "milestones": [],
            }), encoding="utf-8"
        )
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_JSON", raising=False)
        monkeypatch.delenv("BEACON_SUPPRESS_DEPRECATION", raising=False)
        yield Path(tmp)


def test_summary_write_still_persists(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_SUMMARY_TEXT", "still works")
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        commands.cmd_summary()
    data = json.loads((project_dir / ".beacon" / "project.json").read_text())
    assert data["summary"] == "still works"


def test_summary_write_prints_deprecation_to_stderr(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_SUMMARY_TEXT", "text")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        commands.cmd_summary()
    assert "deprecated" in err.getvalue().lower()
    assert "session log" in err.getvalue()


def test_summary_json_mode_does_not_print_deprecation(project_dir, monkeypatch):
    """The /beacon-log Skill calls this programmatically every commit;
    spamming a deprecation message there would be noise."""
    monkeypatch.setenv("BEACON_SUMMARY_TEXT", "text")
    monkeypatch.setenv("BEACON_JSON", "1")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        commands.cmd_summary()
    assert err.getvalue() == ""


def test_summary_suppress_env_silences_deprecation(project_dir, monkeypatch):
    monkeypatch.setenv("BEACON_SUMMARY_TEXT", "text")
    monkeypatch.setenv("BEACON_SUPPRESS_DEPRECATION", "1")
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        commands.cmd_summary()
    assert err.getvalue() == ""


def test_summary_read_unchanged(project_dir, monkeypatch):
    """Reading current summary (no BEACON_SUMMARY_TEXT) is not deprecated —
    we only nudge writers, since reads are needed by session-start
    consumers while the value still exists for legacy projects."""
    data_path = project_dir / ".beacon" / "project.json"
    data = json.loads(data_path.read_text())
    data["summary"] = "existing value"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        commands.cmd_summary()
    assert "existing value" in out.getvalue()
    assert err.getvalue() == ""
