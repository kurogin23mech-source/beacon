"""Tests for bin/beacon-postcompact.sh (e-565).

The hook should:
  - emit a systemMessage with the post-compaction orientation
  - include active MS info pulled from `beacon status --json`
  - degrade gracefully when no .beacon dir is present (early exit, no output)
  - include the soft "rehydrate from source" guidance

These tests run the script as a subprocess against a tmp dir that has a
.beacon/project.json, similar to test_context_monitor_limit.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "beacon-postcompact.sh"


@pytest.fixture
def beacon_project_dir(tmp_path):
    """tmp dir with a minimal .beacon/project.json."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "PostCompactTest",
        "summary": "previous summary",
        "milestones": [
            {
                "id": "ms-1", "title": "Active MS", "status": "in_progress",
                "progress": 50, "target_date": "", "entries": [
                    {"id": "e-1", "type": "task", "status": "todo",
                     "description": "todo task", "date": "", "created_at": "",
                     "done_at": None, "meta": {}},
                    {"id": "e-2", "type": "task", "status": "done",
                     "description": "done task", "date": "", "created_at": "",
                     "done_at": "", "meta": {}},
                ], "commits": [],
            },
            {
                "id": "ms-2", "title": "Idle MS", "status": "todo",
                "progress": 0, "target_date": "", "entries": [], "commits": [],
            },
        ],
    }))
    return tmp_path


def _run(script_path: Path, cwd: Path, payload: dict | None = None) -> subprocess.CompletedProcess:
    payload = payload or {}
    return subprocess.run(
        ["bash", str(script_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    )


def test_emits_system_message_with_active_ms(beacon_project_dir):
    """When beacon is set up, the hook returns a JSON systemMessage that
    includes the in-progress milestone."""
    # Skip if beacon CLI is not available in PATH (CI may not have it).
    if not shutil.which("beacon"):
        pytest.skip("beacon CLI not on PATH")

    result = _run(SCRIPT, beacon_project_dir)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert out, "Hook must emit a systemMessage when .beacon exists"

    payload = json.loads(out)
    msg = payload.get("systemMessage", "")
    assert "post-compaction orientation" in msg
    assert "ms-1" in msg or "Active MS" in msg  # active MS leaked through
    assert "/beacon-session-start" in msg  # soft escalation hint


def test_silent_exit_when_no_beacon(tmp_path):
    """No .beacon/project.json → silent exit, no output, no error."""
    result = _run(SCRIPT, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_walks_up_to_find_beacon(beacon_project_dir):
    """If invoked from a subdirectory, the hook should walk up to find .beacon."""
    if not shutil.which("beacon"):
        pytest.skip("beacon CLI not on PATH")
    subdir = beacon_project_dir / "src" / "deep"
    subdir.mkdir(parents=True)

    result = _run(SCRIPT, subdir)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert out, "Should still emit message from subdirectory"
    payload = json.loads(out)
    assert "systemMessage" in payload
