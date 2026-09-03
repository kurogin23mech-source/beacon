"""ms-164 e-5945: push record attributes to the worked Target(s) generically.

The old auto-pick walked ``data['milestones']`` for an ``in_progress`` one — blind
to non-milestone target classes and single-valued. Now it routes through the shared
``resolve_worked_target_ids`` rule (fork Target in a fork worktree, else the active
Target(s)) and stamps ``target_ids`` on the push record, keeping ``ms_id`` as the
first for back-compat. An explicit ``--ms`` still wins.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_push  # noqa: E402
import git_read_port  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "project.json").write_text(
            json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
        monkeypatch.chdir(tmp)
        # Feed branch / HEAD via env so no real git is invoked; stub the rest.
        monkeypatch.setenv("BEACON_BRANCH", "feature-x")
        monkeypatch.setenv("BEACON_TO", "deadbee")
        monkeypatch.setenv("BEACON_DESCRIPTION", "pushed")
        for k in ("BEACON_MODE", "BEACON_MS", "BEACON_FROM", "BEACON_VERSION",
                  "BEACON_JSON"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(git_read_port, "log_commits",
                            lambda *a, **k: [{"hash": "deadbee", "message": "m"}])
        monkeypatch.setattr(git_read_port, "config_user_name", lambda: "tester")
        try:
            yield Path(tmp)
        finally:
            os.chdir(tempfile.gettempdir())


def _set_project(project_dir: Path, milestones: list) -> None:
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": milestones}), encoding="utf-8")


def _last_push(project_dir: Path) -> dict:
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8"))
    return data["pushes"][-1]


def test_push_attributes_to_active_target(project_dir):
    _set_project(project_dir, [
        {"id": "ms-1", "status": "done"},
        {"id": "ms-2", "status": "in_progress"},
    ])
    commands.cmd_push_record()
    entry = _last_push(project_dir)
    assert entry["target_ids"] == ["ms-2"]
    assert entry["ms_id"] == "ms-2"  # back-compat first-of-set


def test_push_explicit_ms_wins(project_dir, monkeypatch):
    _set_project(project_dir, [{"id": "ms-2", "status": "in_progress"}])
    monkeypatch.setenv("BEACON_MS", "ms-7")
    commands.cmd_push_record()
    entry = _last_push(project_dir)
    assert entry["target_ids"] == ["ms-7"]
    assert entry["ms_id"] == "ms-7"


def test_push_attributes_to_fork_target(project_dir):
    _set_project(project_dir, [{"id": "ms-9", "status": "todo"}])
    (project_dir / ".beacon" / "fork.json").write_text(
        json.dumps({"target_ms_id": "ms-9"}), encoding="utf-8")
    commands.cmd_push_record()
    entry = _last_push(project_dir)
    assert entry["target_ids"] == ["ms-9"]


def test_push_no_target_when_none_active(project_dir):
    _set_project(project_dir, [{"id": "ms-1", "status": "done"}])
    commands.cmd_push_record()
    entry = _last_push(project_dir)
    assert "target_ids" not in entry
    assert entry["ms_id"] is None


def test_push_generic_over_opportunity_target(project_dir):
    """The fix removes the milestone hardcode: a sales project's active
    Opportunity is now a visible worked-target (was invisible before)."""
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "opportunities": [
            {"id": "opp-1", "status": "in_progress"}]}), encoding="utf-8")
    commands.cmd_push_record()
    entry = _last_push(project_dir)
    assert entry["target_ids"] == ["opp-1"]
