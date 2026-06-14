"""PostToolUse halt-check hook round-trip tests (ms-55 e-1721).

Verifies that ``beacon_cli.hooks.halt_check`` correctly:
  * detects the project root + session_id from the hook input
  * reads the persisted halt request via lib/stop_signal
  * emits a proper ``hookSpecificOutput`` JSON line when a halt is pending
  * stamps ``acknowledged_at`` so a subsequent fire doesn't re-surface
  * stays silent when no halt is pending / when not in a Beacon project
  * never propagates exceptions to Claude Code
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

import stop_signal  # noqa: E402
from beacon_cli.hooks import halt_check  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Bring up a fake beacon project with a session and halt request."""
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text('{"name": "test"}')
    (tmp_path / ".beacon" / "session.json").write_text(
        json.dumps({"session_id": "sv-A", "ms_id": "ms-55"})
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_hook(stdin_payload: dict) -> tuple[str, int]:
    """Invoke halt_check.main with the given stdin payload, capture stdout."""
    raw = json.dumps(stdin_payload)
    stdout_capture = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(raw)), \
         mock.patch.object(sys, "stdout", stdout_capture):
        rc = halt_check.main()
    return stdout_capture.getvalue(), rc


# ---------------------------------------------------------------------------
# Surface assertions — hook stays silent when no halt is pending
# ---------------------------------------------------------------------------

def test_silent_when_not_in_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out, rc = _run_hook({"cwd": str(tmp_path), "session_id": "sv-A"})
    assert out == ""
    assert rc == 0


def test_silent_when_no_session_id(project):
    # Drop the session.json so neither source has an id.
    (project / ".beacon" / "session.json").unlink()
    out, rc = _run_hook({"cwd": str(project)})
    assert out == ""
    assert rc == 0


def test_silent_when_no_halt_request(project):
    out, rc = _run_hook({"cwd": str(project), "session_id": "sv-A"})
    assert out == ""
    assert rc == 0


# ---------------------------------------------------------------------------
# Surface assertions — hook injects when a halt is pending
# ---------------------------------------------------------------------------

def test_injects_when_halt_pending(project):
    record = {
        "scope": "global",
        "reason": "build failed 3 retries",
        "reason_kind": "build_fail",
        "issued_by_session_id": "sv-sender",
        "issued_at": "2026-06-15T01:00:00Z",
    }
    stop_signal.write_halt_request(
        record, session_id="sv-A",
        beacon_dir=str(project / ".beacon"),
    )
    out, rc = _run_hook({"cwd": str(project), "session_id": "sv-A"})
    assert rc == 0
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "STOP SIGNAL" in ctx
    assert "GLOBAL" in ctx
    assert "build failed 3 retries" in ctx


def test_ack_suppresses_next_fire(project):
    record = {
        "scope": "global",
        "reason": "x",
        "reason_kind": "manual",
        "issued_by_session_id": "sv-sender",
        "issued_at": "2026-06-15T01:00:00Z",
    }
    stop_signal.write_halt_request(
        record, session_id="sv-A",
        beacon_dir=str(project / ".beacon"),
    )
    # First fire surfaces + acknowledges.
    out1, _ = _run_hook({"cwd": str(project), "session_id": "sv-A"})
    assert "STOP SIGNAL" in out1
    # Second fire stays silent (= acknowledged_at on disk).
    out2, _ = _run_hook({"cwd": str(project), "session_id": "sv-A"})
    assert out2 == ""
    # The file still exists (= audit trail).
    saved = stop_signal.read_halt_request(
        "sv-A", beacon_dir=str(project / ".beacon"),
    )
    assert saved["acknowledged_at"]


def test_scoped_halt_injects_target_line(project):
    record = {
        "scope": "scoped",
        "target": {"kind": "ms", "id": "ms-55"},
        "reason": "test fail",
        "reason_kind": "test_fail",
        "issued_by_session_id": "sv-sender",
        "issued_at": "2026-06-15T01:00:00Z",
    }
    stop_signal.write_halt_request(
        record, session_id="sv-A",
        beacon_dir=str(project / ".beacon"),
    )
    out, _ = _run_hook({"cwd": str(project), "session_id": "sv-A"})
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "ms:ms-55" in ctx
    assert "test_fail" in ctx


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------

def test_malformed_stdin_is_silent(project):
    """A non-JSON stdin payload should still exit 0 with no output."""
    stdout_capture = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO("not json")), \
         mock.patch.object(sys, "stdout", stdout_capture):
        rc = halt_check.main()
    assert rc == 0
    assert stdout_capture.getvalue() == ""


def test_empty_stdin_is_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stdout_capture = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO("")), \
         mock.patch.object(sys, "stdout", stdout_capture):
        rc = halt_check.main()
    assert rc == 0
    assert stdout_capture.getvalue() == ""


def test_session_id_from_hook_input_when_session_json_missing(project):
    """If session.json doesn't have session_id, fall back to harness."""
    (project / ".beacon" / "session.json").write_text("{}")
    record = {
        "scope": "global",
        "reason": "x",
        "reason_kind": "manual",
        "issued_by_session_id": "sv-sender",
        "issued_at": "2026-06-15T01:00:00Z",
    }
    stop_signal.write_halt_request(
        record, session_id="sv-fallback",
        beacon_dir=str(project / ".beacon"),
    )
    out, _ = _run_hook({"cwd": str(project), "session_id": "sv-fallback"})
    assert "STOP SIGNAL" in out
