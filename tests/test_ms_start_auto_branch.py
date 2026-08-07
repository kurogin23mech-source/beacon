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
import cmd_milestone  # noqa: E402  (ms-127 e-4849: milestone family moved here)


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
        # ms-95 e-2441: monkeypatch.delitem auto-restores. Raw pop leaks the
        # deletion → adjacent test_api mocks vanish (8 fails on CI).
        monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
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
              exists_branches=(), in_worktree: bool = True,
              worktree_add_succeeds: bool = True):
    """Install a fake subprocess.run that emulates the git surface used by
    ``_ensure_on_branch`` AND the cwd-detection in ``_is_in_main_project_root``.

    Parameters:
        in_repo: ``rev-parse --is-inside-work-tree`` reports True/False.
        current: what ``git branch --show-current`` returns.
        exists_branches: which refs are reported as already existing.
        in_worktree: if True, ``--git-dir`` and ``--git-common-dir`` return
            **different** paths (= we are inside a worktree, in-place
            checkout is safe). If False, they return the **same** path
            (= main project root, cwd-aware path engages the worktree
            helper).
        worktree_add_succeeds: when ``in_worktree=False`` the worktree
            helper will invoke ``git worktree add``. Set False to simulate
            git rejecting the add (used by negative tests).

    The fake records each call into ``calls`` so tests can assert on it.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return _R(stdout="true\n" if in_repo else "", returncode=0 if in_repo else 128)
        if args[:3] == ["git", "rev-parse", "--git-dir"]:
            # In a worktree, --git-dir points to a per-worktree location;
            # in the main repo it matches --git-common-dir.
            gd = "/repo/.git/worktrees/x" if in_worktree else "/repo/.git"
            return _R(stdout=gd + "\n", returncode=0)
        if args[:3] == ["git", "rev-parse", "--git-common-dir"]:
            return _R(stdout="/repo/.git\n", returncode=0)
        if args[:3] == ["git", "branch", "--show-current"]:
            return _R(stdout=current + "\n")
        if args[:3] == ["git", "show-ref", "--verify"]:
            wanted_ref = args[-1]
            wanted_branch = wanted_ref.replace("refs/heads/", "")
            return _R(returncode=0 if wanted_branch in exists_branches else 1)
        if args[:2] == ["git", "checkout"]:
            return _R(returncode=0)
        if args[:3] == ["git", "worktree", "add"]:
            return _R(returncode=0 if worktree_add_succeeds else 128,
                      stderr="" if worktree_add_succeeds else "simulated worktree add failure")
        return _R(returncode=1)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    # The worktree helper has its own ``subprocess`` import; mirror the
    # patch there so create_workspace shells out through the same fake.
    import worktree as _worktree
    monkeypatch.setattr(_worktree.subprocess, "run", fake_run)
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


# ---------------------------------------------------------------------------
# ms-65 e-1477: cwd-aware behaviour — main project root creates a worktree
# instead of switching the main cwd's HEAD. This is the root fix for the
# 2026-06-10 incident where two sessions in the same cwd silently shared a
# git HEAD.
# ---------------------------------------------------------------------------

def test_start_from_main_root_creates_worktree(project, monkeypatch, capsys):
    """From main repo (not in a worktree), MS start invokes git worktree add."""
    calls = _mock_git(monkeypatch, in_repo=True, in_worktree=False)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()

    # The cwd-aware path must call ``git worktree add`` ...
    wt_calls = [c for c in calls if c[:3] == ["git", "worktree", "add"]]
    assert wt_calls, f"expected 'git worktree add' call, got: {calls}"
    # ... at the canonical .worktrees/<branch>/ path.
    target = wt_calls[0][3]
    assert ".worktrees/ms-51-" in target.replace(os.sep, "/")
    # And must NOT switch the main cwd's HEAD via plain checkout.
    checkout_calls = [c for c in calls if c[:2] == ["git", "checkout"]]
    assert not checkout_calls, (
        "main-root MS start must not run 'git checkout' in the current cwd "
        "(= the bug ms-65 e-1477 was created to fix); calls were: "
        + repr(calls)
    )

    out = capsys.readouterr().out
    assert "Activated" in out
    # User-facing guidance: cd into the new worktree before starting bclaude.
    assert ".worktrees/ms-51-" in out
    assert "bclaude" in out


def test_start_from_main_root_idempotent_when_worktree_exists(project, monkeypatch, capsys):
    """If .worktrees/<branch>/ already exists, do not crash; do not re-add."""
    import branch as _branch
    branch_name = _branch.ms_branch_name("ms-51", "MS への挙手 → 自動 branch + join")
    # Pre-create the worktree directory on disk so create_workspace returns
    # the idempotent ``created=False`` path.
    target_dir = project / ".worktrees" / branch_name
    target_dir.mkdir(parents=True)

    calls = _mock_git(monkeypatch, in_repo=True, in_worktree=False)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    commands.cmd_milestone_start()

    # No worktree add call should fire when the path already exists.
    wt_calls = [c for c in calls if c[:3] == ["git", "worktree", "add"]]
    assert not wt_calls
    out = capsys.readouterr().out
    assert "Activated" in out
    # Status still flipped + assignee still added.
    ms = _ms(project)
    assert ms["status"] == "in_progress"
    assert ms["assignee"] == "test-claude"


def test_start_from_main_root_falls_back_gracefully_on_worktree_failure(
    project, monkeypatch, capsys
):
    """When git worktree add fails (both -b and reuse attempts), warn and continue."""
    calls = _mock_git(
        monkeypatch, in_repo=True, in_worktree=False,
        worktree_add_succeeds=False,
    )
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")

    # Should NOT raise; the failure surfaces as a warning + the MS status
    # still flips. We must not block activation on a worktree problem.
    commands.cmd_milestone_start()

    ms = _ms(project)
    assert ms["status"] == "in_progress"
    captured = capsys.readouterr()
    assert "could not create worktree" in captured.err or \
           "could not create worktree" in captured.out
    # No "next: cd ..." guidance when the worktree failed to materialise.
    assert "next: cd" not in captured.out


def test_is_in_main_project_root_helper_detects_worktree(monkeypatch):
    """Direct test of the detection helper — git-dir != git-common-dir → worktree."""
    def fake_run(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "--git-dir"]:
            return _R(stdout="/repo/.git/worktrees/feat-x\n", returncode=0)
        if args[:3] == ["git", "rev-parse", "--git-common-dir"]:
            return _R(stdout="/repo/.git\n", returncode=0)
        return _R(returncode=1)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    assert cmd_milestone._is_in_main_project_root() is False


def test_is_in_main_project_root_helper_detects_main(monkeypatch):
    """Direct test: matching paths → main project root."""
    def fake_run(args, **kwargs):
        if args[:3] == ["git", "rev-parse", "--git-dir"]:
            return _R(stdout="/repo/.git\n", returncode=0)
        if args[:3] == ["git", "rev-parse", "--git-common-dir"]:
            return _R(stdout="/repo/.git\n", returncode=0)
        return _R(returncode=1)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    assert cmd_milestone._is_in_main_project_root() is True


def test_is_in_main_project_root_helper_conservative_on_git_missing(monkeypatch):
    """No git binary → conservative True (caller's worktree helper will error gracefully)."""
    def fake_run(args, **kwargs):
        raise FileNotFoundError("no git")
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    assert cmd_milestone._is_in_main_project_root() is True
