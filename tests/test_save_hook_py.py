"""Tests for the Python MCP-save hook (``beacon_cli.hooks.save_hook``).

Bash counterpart: ``bin/beacon-save-hook.sh``. The bash version has no
existing pytest coverage (it's a 100-line filter) so this file is the
first canonical executable spec for its behaviour.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from beacon_cli.hooks import save_hook  # noqa: E402


# ---------------------------------------------------------------------------
# _source_for + _is_write_operation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("mcp__google-drive__createFolder", "google_drive"),
        ("mcp__claude_ai_Google_Drive__upload", "google_drive"),
        ("mcp__github__create_issue", None),
        ("Bash", None),
        ("", None),
    ],
)
def test_source_for_matches_only_monitored_prefixes(tool, expected):
    assert save_hook._source_for(tool) == expected


@pytest.mark.parametrize(
    "op,is_write",
    [
        ("createFolder", True),
        ("uploadFile", True),
        ("addPermission", True),
        ("readGoogleDoc", False),
        ("get_file_metadata", False),
        ("list_recent_files", False),
        ("search_files", False),
        ("downloadFile", False),
        ("exportSlideThumbnail", False),
        ("authenticate", False),
    ],
)
def test_is_write_operation(op, is_write):
    assert save_hook._is_write_operation(op) is is_write


# ---------------------------------------------------------------------------
# Main flow — uses subprocess.run mock to capture the would-be CLI call
# ---------------------------------------------------------------------------


@pytest.fixture
def beacon_project(tmp_path, monkeypatch):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "SaveHookTest",
        "milestones": [
            {"id": "ms-1", "title": "Active", "status": "in_progress",
             "entries": []},
        ],
    }))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _patched_main(monkeypatch, payload: dict, *, has_beacon: bool = True):
    """Run ``save_hook.main`` with stdin mocked and subprocess captured."""
    captured = {}

    def fake_which(name):
        return "/usr/local/bin/beacon" if has_beacon and name == "beacon" else None

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return mock.MagicMock(returncode=0)

    monkeypatch.setattr(save_hook.shutil, "which", fake_which)
    monkeypatch.setattr(save_hook.subprocess, "run", fake_run)
    monkeypatch.setattr(
        save_hook.sys, "stdin",
        type("S", (), {"read": staticmethod(lambda: json.dumps(payload))})(),
    )

    rc = save_hook.main()
    return rc, captured


def test_main_skips_when_no_beacon_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, captured = _patched_main(monkeypatch, {
        "tool_name": "mcp__google-drive__createFolder",
        "tool_input": {"name": "Foo"},
    })
    assert rc == 0
    assert "cmd" not in captured


def test_main_skips_for_unmonitored_tool(beacon_project, monkeypatch):
    rc, captured = _patched_main(monkeypatch, {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    })
    assert rc == 0
    assert "cmd" not in captured


def test_main_skips_read_operations(beacon_project, monkeypatch):
    rc, captured = _patched_main(monkeypatch, {
        "tool_name": "mcp__google-drive__readGoogleDoc",
        "tool_input": {"fileId": "abc"},
    })
    assert rc == 0
    assert "cmd" not in captured


def test_main_emits_save_for_monitored_write(beacon_project, monkeypatch):
    rc, captured = _patched_main(monkeypatch, {
        "tool_name": "mcp__google-drive__createGoogleDoc",
        "tool_input": {"title": "My Doc", "url": "https://docs.google.com/x"},
    })
    assert rc == 0
    assert "cmd" in captured
    cmd = captured["cmd"]
    # save args structure
    assert cmd[0].endswith("beacon")
    assert cmd[1] == "save"
    assert cmd[2] == "[createGoogleDoc] My Doc"
    assert "-m" in cmd and "ms-1" in cmd
    assert "--source" in cmd and "google_drive" in cmd
    assert "--url" in cmd and "https://docs.google.com/x" in cmd


def test_main_skips_when_zero_active_milestones(tmp_path, monkeypatch):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "NoActive", "milestones": [
            {"id": "ms-1", "title": "T", "status": "todo", "entries": []},
        ],
    }))
    monkeypatch.chdir(tmp_path)
    rc, captured = _patched_main(monkeypatch, {
        "tool_name": "mcp__google-drive__createGoogleDoc",
        "tool_input": {"title": "X"},
    })
    assert rc == 0
    assert "cmd" not in captured


def test_main_skips_when_two_active_milestones(tmp_path, monkeypatch):
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "TwoActive", "milestones": [
            {"id": "ms-1", "status": "in_progress", "entries": []},
            {"id": "ms-2", "status": "in_progress", "entries": []},
        ],
    }))
    monkeypatch.chdir(tmp_path)
    rc, captured = _patched_main(monkeypatch, {
        "tool_name": "mcp__google-drive__createGoogleDoc",
        "tool_input": {"title": "X"},
    })
    assert rc == 0
    assert "cmd" not in captured


def test_main_skips_when_beacon_cli_missing(beacon_project, monkeypatch):
    rc, captured = _patched_main(
        monkeypatch,
        {
            "tool_name": "mcp__google-drive__createGoogleDoc",
            "tool_input": {"title": "X"},
        },
        has_beacon=False,
    )
    assert rc == 0
    assert "cmd" not in captured
