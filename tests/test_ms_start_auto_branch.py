"""Tests for ``cmd_milestone_start`` auto-branch + auto-assignee (ms-51 / e-932).

The fixture isolates BEACON_PROJECT_FILE to a temp file and mocks
subprocess.run so we can drive the git-state branches without touching
the real repo.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        # Write a stub agent.json so we know exactly what the actor name is.
        (beacon_dir / "agent.json").write_text(
            json.dumps({"name": "test-claude"}), encoding="utf-8"
        )
        # Project with one MS for activation.
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({
                "name": "test",
                "milestones": [
                    {
                        "id": "ms-51",
                        "title": "MS への挙手 → 自動 branch + join",
                        "status": "todo",
                        "entries": [],
                        "progress": 0,
                    }
                ],
                "summary": "",
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.chdir(tmp)
        # Clean inherited sub-agent env so the actor name is deterministic.
        monkeypatch.delenv("BEACON_AGENT_PARENT", raising=False)
        monkeypatch.delenv("BEACON_AGENT_CHILD_ID", raising=False)
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _load(project_dir: Path) -> dict:
    return json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8")
    )


def _ms(project_dir: Path, ms_id: str = "ms-51") -> dict:
    for ms in _load(project_dir).get("milestones", []):
        if ms["id"] == ms_id:
            return ms
    raise AssertionError(f"{ms_id} not found")


# ---------------------------------------------------------------------------
# Mocking helpers
# ---------------------------------------------------------------------------

class _R:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _mock_git(monkeypatch, *, in_repo: bool = True, current: str = "main",
              exists_branches=()):
    """Install a fake subprocess.run that emulates the git surface used by
    ``_ensure_on_branch``. ``exists_branches`` lists local branches that
    should be reported as already existing.

    The fake records each call into ``calls`` so tests can assert on it.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return _R(stdout="true\n" if in_repo else "", returncode=0 if in_repo else 128)
        if args[:3] == ["git", "branch", "--show-current"]:
            return _R(stdout=current + "\n")
        if args[:3] == ["git", "show-ref", "--verify"]:
            wanted_ref = args[-1]
            wanted_branch = wanted_ref.replace("refs/heads/", "")
            return _R(returncode=0 if wanted_branch in exists_branches else 1)
        if args[:2] == ["git", "checkout"]:
            return _R(returncode=0)
        return _R(returncode=1)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_start_creates_branch_and_adds_assignee(project, monkeypatch, capsys):
    """AC-1 + AC-2: branch checked out + actor self-added as assignee."""
    calls = _mock_git(monkeypatch, in_repo=True, current="main", exists_branches=())
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()

    ms = _ms(project)
    assert ms["status"] == "in_progress"
    assert ms["assignee"] == "test-claude"  # from agent.json fixture

    # The expected branch name combines the MS id and the title slug.
    # Title is "MS への挙手 → 自動 branch + join" → slug includes ascii tokens.
    checkout_calls = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert len(checkout_calls) == 1
    branch = checkout_calls[0][3]
    assert branch.startswith("ms-51-")
    out = capsys.readouterr().out
    assert "Activated" in out
    assert "test-claude" in out
    assert branch in out


def test_start_switches_to_existing_branch(project, monkeypatch, capsys):
    """If the MS branch already exists, switch instead of recreate."""
    # Pre-derive the expected branch name (so we can flag it as existing).
    import branch as _branch
    expected = _branch.ms_branch_name(
        "ms-51", "MS への挙手 → 自動 branch + join"
    )
    calls = _mock_git(
        monkeypatch, in_repo=True, current="main",
        exists_branches=(expected,),
    )
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()

    # Should have called `git checkout <branch>` (no -b) — not -b create.
    create_calls = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    switch_calls = [c for c in calls if c == ["git", "checkout", expected]]
    assert not create_calls
    assert switch_calls
    out = capsys.readouterr().out
    assert "switched" in out


def test_start_no_op_when_already_on_branch(project, monkeypatch, capsys):
    """If we're already on the MS branch, don't checkout again."""
    import branch as _branch
    expected = _branch.ms_branch_name(
        "ms-51", "MS への挙手 → 自動 branch + join"
    )
    calls = _mock_git(
        monkeypatch, in_repo=True, current=expected,
        exists_branches=(expected,),
    )
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()

    # No checkout subprocess call should have fired.
    co = [c for c in calls if c[:2] == ["git", "checkout"]]
    assert not co
    out = capsys.readouterr().out
    assert "already on it" in out


def test_start_skips_branch_when_not_a_git_repo(project, monkeypatch, capsys):
    """Outside git, transparently fall back to status flip + assignee only."""
    calls = _mock_git(monkeypatch, in_repo=False)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()

    ms = _ms(project)
    assert ms["status"] == "in_progress"
    assert ms["assignee"] == "test-claude"
    # No checkout attempted past the is-inside-work-tree probe.
    co = [c for c in calls if c[:2] == ["git", "checkout"]]
    assert not co
    out = capsys.readouterr().out
    # Branch line absent when skipped.
    assert "branch:" not in out


def test_start_no_branch_flag_skips_git(project, monkeypatch, capsys):
    calls = _mock_git(monkeypatch, in_repo=True)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_NO_BRANCH", "1")

    commands.cmd_milestone_start()

    # No git call should have fired at all.
    assert not calls


def test_start_no_assignee_flag_skips_assignee(project, monkeypatch, capsys):
    _mock_git(monkeypatch, in_repo=True)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_NO_ASSIGNEE", "1")

    commands.cmd_milestone_start()

    ms = _ms(project)
    assert ms["status"] == "in_progress"
    assert "assignee" not in ms or not ms["assignee"]


def test_start_duplicate_actor_is_idempotent(project, monkeypatch):
    """Starting an MS twice with the same actor should keep assignee as one entry."""
    _mock_git(monkeypatch, in_repo=True)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()
    # Second invocation: simulate a re-start (e.g. dispatcher re-runs).
    commands.cmd_milestone_start()

    ms = _ms(project)
    assert ms["assignee"] == "test-claude"
