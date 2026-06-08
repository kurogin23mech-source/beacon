"""Tests for `beacon summary` deprecation completion (ms-57 / e-1040).

SPEC v2 splits summary's three roles:
  - cross-session hand-off → session log (e-1037)
  - human narrative        → project-vision doc

After the soft-deprecation period, `beacon summary "text"` writes are
now a NO-OP: the project's `summary` field is no longer mutated by
this command, by `beacon log --summary`, or by the /beacon-log Skill.
Read paths still work so existing session-start displays don't go
blank on legacy projects that already have a value stored.
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
        try:
            yield Path(tmp)
        finally:
            # Windows can't remove a directory that is the process CWD, so the
            # TemporaryDirectory cleanup would raise WinError 32. Step out of the
            # temp project before cleanup (no-op on POSIX). ms-57.
            os.chdir(tempfile.gettempdir())


def test_summary_write_is_ignored(project_dir, monkeypatch):
    """e-1040 deprecation completed: `beacon summary "text"` is no-op.
    The previous text is preserved (empty string in this fixture)."""
    monkeypatch.setenv("BEACON_SUMMARY_TEXT", "should be ignored")
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        commands.cmd_summary()
    data = json.loads((project_dir / ".beacon" / "project.json").read_text())
    assert data["summary"] == ""  # untouched


def test_summary_write_existing_value_preserved(project_dir):
    """Reads through a legacy stored value continue to work even though
    writes are no-op."""
    data_path = project_dir / ".beacon" / "project.json"
    data = json.loads(data_path.read_text())
    data["summary"] = "legacy stored value"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        commands.cmd_summary()  # read mode (no env var)
    assert "legacy stored value" in out.getvalue()


def test_summary_json_write_reports_ignored_flag(project_dir, monkeypatch):
    """JSON callers (the legacy /beacon-log Skill, scripts) need a
    machine-readable signal that the write was dropped."""
    monkeypatch.setenv("BEACON_SUMMARY_TEXT", "attempted")
    monkeypatch.setenv("BEACON_JSON", "1")
    out = io.StringIO()
    with redirect_stderr(io.StringIO()), redirect_stdout(out):
        commands.cmd_summary()
    body = json.loads(out.getvalue())
    assert body.get("write_ignored") is True
    assert body.get("deprecated_since") == "e-1040"


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
