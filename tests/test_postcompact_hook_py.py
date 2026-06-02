"""Tests for the Python PostCompact hook (``beacon_cli.hooks.postcompact``).

The bash version is exercised by ``test_postcompact_hook.py`` — this file
covers the cross-platform translation and the unit-level helpers so that
breakage shows up even on a runner where bash / the beacon CLI are missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def beacon_project_dir(tmp_path):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "PostCompactPyTest",
        "summary": "previous summary",
        "milestones": [
            {
                "id": "ms-1", "title": "Active MS", "status": "in_progress",
                "progress": 50, "target_date": "", "entries": [],
                "commits": [],
            },
        ],
    }))
    return tmp_path


# ---------------------------------------------------------------------------
# Unit: _format_context — render matches bash labels
# ---------------------------------------------------------------------------


def test_format_context_includes_active_ms():
    sys.path.insert(0, str(_REPO_ROOT))
    from beacon_cli.hooks import postcompact

    status = json.dumps({
        "name": "Demo",
        "milestones": [
            {"id": "ms-1", "title": "Active", "status": "in_progress",
             "progress": 42, "done_tasks": 3, "total_tasks": 7},
            {"id": "ms-2", "title": "Idle", "status": "todo"},
        ],
        "operations": [
            {"id": "op-1", "title": "RunIt", "status": "open"},
            {"id": "op-2", "title": "Done", "status": "closed"},
        ],
    })
    notes = json.dumps([
        {"text": "# Heading A\nbody"},
        {"text": "# Heading B"},
    ])

    text = postcompact._format_context(status, notes)
    assert "プロジェクト: Demo" in text
    assert "Active MS:" in text
    assert "ms-1" in text
    assert "42%" in text
    assert "3/7" in text
    assert "ms-2" not in text  # idle MS not surfaced
    assert "Active Operation:" in text
    assert "op-1" in text
    assert "op-2" not in text
    assert "直近 note 見出し:" in text
    assert "Heading A" in text
    assert "Heading B" in text


def test_format_context_handles_no_active_ms():
    sys.path.insert(0, str(_REPO_ROOT))
    from beacon_cli.hooks import postcompact

    status = json.dumps({"name": "Demo", "milestones": []})
    text = postcompact._format_context(status, "[]")
    assert "Active MS: なし" in text


def test_format_context_resilient_to_garbage_input():
    sys.path.insert(0, str(_REPO_ROOT))
    from beacon_cli.hooks import postcompact

    # Malformed JSON must not crash; fall through to "Active MS: なし"
    text = postcompact._format_context("not json", "also not")
    assert "Active MS: なし" in text


def test_build_message_uses_fallback_when_context_empty():
    sys.path.insert(0, str(_REPO_ROOT))
    from beacon_cli.hooks import postcompact

    msg = postcompact._build_message("")
    assert "post-compaction orientation" in msg
    assert "/beacon-session-start" in msg
    assert "beacon status の取得に失敗" in msg


# ---------------------------------------------------------------------------
# Integration: subprocess invocation
# ---------------------------------------------------------------------------


def _env_with_path() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
        if env.get("PYTHONPATH") else str(_REPO_ROOT)
    )
    return env


def test_silent_exit_when_no_beacon(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "beacon_cli.hooks.postcompact"],
        input="",
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=_env_with_path(),
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_emits_system_message_with_active_ms(beacon_project_dir):
    """When the beacon CLI is on PATH the hook must produce a JSON
    systemMessage that includes the in-progress MS."""
    import shutil
    if not shutil.which("beacon"):
        pytest.skip("beacon CLI not on PATH")

    result = subprocess.run(
        [sys.executable, "-m", "beacon_cli.hooks.postcompact"],
        input="",
        capture_output=True,
        text=True,
        cwd=str(beacon_project_dir),
        env=_env_with_path(),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert out, "Hook must emit a systemMessage when .beacon exists"
    payload = json.loads(out)
    msg = payload.get("systemMessage", "")
    assert "post-compaction orientation" in msg
    assert "/beacon-session-start" in msg
