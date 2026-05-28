"""Unit tests for `_infer_pr_ms_id` — automatic milestone inference for PR create.

The inference logic is the e-607 "stop forcing the user to type -m" rule.
We test the priority order is respected:
    branch > recent commits (ms-N) > recent commits (e-N) > sole active MS > none

All git invocations are mocked so the tests do not depend on the real
working-tree state.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def project(milestones):
    """Build a minimal project shape with the given milestones list."""
    return {"name": "t", "milestones": milestones, "summary": ""}


def ms(id_: str, status: str = "todo", entries=None):
    return {
        "id": id_,
        "title": f"Milestone {id_}",
        "status": status,
        "entries": entries or [],
        "progress": 0,
    }


def patch_git(monkeypatch, branch: str = "", log_lines: list | None = None):
    """Install a fake subprocess.run that only answers git calls."""
    log_text = "\n".join(log_lines or [])

    def fake_run(args, **kwargs):
        if args[:3] == ["git", "branch", "--show-current"]:
            return _FakeRun(stdout=branch + "\n")
        if args[:3] == ["git", "log", "--pretty=%s"]:
            return _FakeRun(stdout=log_text)
        return _FakeRun(returncode=1)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# Branch-name priority (highest)
# ---------------------------------------------------------------------------

def test_branch_name_wins_over_active_ms(monkeypatch):
    """Branch `ms-42/work` must beat the sole active MS (ms-99)."""
    data = project([ms("ms-42"), ms("ms-99", status="in_progress")])
    patch_git(monkeypatch, branch="ms-42/work", log_lines=[])
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == "ms-42"
    assert "ms-42" in reason and "branch" in reason


def test_branch_ms_not_in_project_is_ignored(monkeypatch):
    """Branch `ms-77/work` where ms-77 doesn't exist must fall through."""
    data = project([ms("ms-99", status="in_progress")])
    patch_git(monkeypatch, branch="ms-77/work", log_lines=[])
    inferred, reason = commands._infer_pr_ms_id(data)
    # Should fall through to "sole active MS"
    assert inferred == "ms-99"


# ---------------------------------------------------------------------------
# Commit-message ms-N priority (second)
# ---------------------------------------------------------------------------

def test_commit_ms_pattern_used_when_branch_missing(monkeypatch):
    data = project([ms("ms-15")])
    patch_git(monkeypatch, branch="feature/foo",
              log_lines=["feat(thing): refactor xyz for ms-15"])
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == "ms-15"
    assert "commit" in reason


# ---------------------------------------------------------------------------
# Commit-message e-N reverse lookup (third)
# ---------------------------------------------------------------------------

def test_e_id_in_commit_reverse_lookups_owning_ms(monkeypatch):
    """Commit message mentions `(e-606)` → that task lives under ms-42."""
    data = project([
        ms("ms-42", status="in_progress", entries=[
            {"id": "e-606", "type": "task", "description": "..."},
        ]),
    ])
    patch_git(monkeypatch, branch="feature/foo",
              log_lines=["feat(skill): new pr-create skill (e-606)"])
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == "ms-42"
    assert "e-606" in reason


# ---------------------------------------------------------------------------
# Sole-active-MS priority (fourth)
# ---------------------------------------------------------------------------

def test_sole_active_ms_used_when_no_other_signal(monkeypatch):
    data = project([
        ms("ms-1", status="done"),
        ms("ms-2", status="in_progress"),
        ms("ms-3", status="todo"),
    ])
    patch_git(monkeypatch, branch="hotfix/x", log_lines=["fix typo"])
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == "ms-2"
    assert "active" in reason


def test_multiple_active_ms_returns_empty(monkeypatch):
    """Ambiguous active state → don't guess."""
    data = project([
        ms("ms-2", status="in_progress"),
        ms("ms-3", status="in_progress"),
    ])
    patch_git(monkeypatch, branch="hotfix/x", log_lines=["fix typo"])
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == ""
    assert reason == ""


# ---------------------------------------------------------------------------
# No signal at all
# ---------------------------------------------------------------------------

def test_no_signal_returns_empty(monkeypatch):
    data = project([ms("ms-1", status="done")])
    patch_git(monkeypatch, branch="main", log_lines=[])
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == ""


def test_git_missing_does_not_crash(monkeypatch):
    """If git is not on PATH, inference must not raise."""
    def boom(*a, **kw):
        raise FileNotFoundError("git missing")
    monkeypatch.setattr(commands.subprocess, "run", boom)
    data = project([ms("ms-2", status="in_progress")])
    # Falls through to "sole active MS" — which doesn't need git.
    inferred, reason = commands._infer_pr_ms_id(data)
    assert inferred == "ms-2"
