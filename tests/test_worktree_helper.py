"""Unit tests for lib/worktree.create_workspace (ms-65 e-1476).

Pins down the behaviour previously inlined in cmd_milestone_workspace:
    - idempotent: existing path → no git call, no failure
    - branch-may-exist retry: -b fails, retry without -b succeeds
    - hard failure: both attempts fail → WorktreeCreateError
    - git missing: FileNotFoundError → GitNotInstalledError
    - happy path: -b succeeds → branch_created=True

Why test this contract:
    Once cmd_milestone_start (e-1477) starts calling create_workspace from
    main project root, two CLI entry points share the same helper. A silent
    regression in the helper (= changing which exit code triggers the retry,
    forgetting to capture stderr, dropping the FileNotFoundError mapping)
    would break BOTH paths at once. Locking the contract here keeps the
    refactor in e-1477 honest.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import worktree  # noqa: E402


def _result(returncode: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stderr=stderr, stdout="")


def test_existing_path_is_idempotent_returns_created_false(tmp_path, monkeypatch):
    """Path already exists → return without calling git, created=False."""
    existing = tmp_path / "already-there"
    existing.mkdir()
    runner = MagicMock()

    out = worktree.create_workspace(str(existing), "ms-65-foo", runner=runner)

    assert out["created"] is False
    assert out["abs_workspace_path"] == str(existing.resolve())
    assert out["branch_name"] == "ms-65-foo"
    runner.assert_not_called()  # idempotent: never touched git


def test_happy_path_with_new_branch(tmp_path):
    """git worktree add ... -b <branch> succeeds → created+branch_created both True."""
    target = tmp_path / "wt"  # does not exist yet
    runner = MagicMock(return_value=_result(0))

    out = worktree.create_workspace(str(target), "ms-65-dispatch", runner=runner)

    assert out["created"] is True
    assert out["branch_created"] is True
    runner.assert_called_once()
    cmd = runner.call_args[0][0]
    assert cmd[:3] == ["git", "worktree", "add"]
    assert "-b" in cmd
    assert "ms-65-dispatch" in cmd


def test_branch_already_exists_retry_without_dash_b(tmp_path):
    """First call (-b) fails because branch exists → retry without -b succeeds."""
    target = tmp_path / "wt"
    runner = MagicMock(side_effect=[
        _result(128, "fatal: A branch named 'ms-65-x' already exists."),
        _result(0),
    ])

    out = worktree.create_workspace(str(target), "ms-65-x", runner=runner)

    assert out["created"] is True
    assert out["branch_created"] is False
    assert runner.call_count == 2
    # 2nd call must NOT include -b
    second_cmd = runner.call_args_list[1][0][0]
    assert "-b" not in second_cmd
    assert "ms-65-x" in second_cmd


def test_both_attempts_fail_raises_with_both_stderrs(tmp_path):
    """Both -b and without-b fail → WorktreeCreateError carrying both stderrs."""
    target = tmp_path / "wt"
    runner = MagicMock(side_effect=[
        _result(128, "first failure detail"),
        _result(128, "second failure detail"),
    ])

    with pytest.raises(worktree.WorktreeCreateError) as exc_info:
        worktree.create_workspace(str(target), "ms-65-y", runner=runner)

    msg = str(exc_info.value)
    assert "first failure detail" in msg
    assert "second failure detail" in msg


def test_git_not_installed_raises_specific_error(tmp_path):
    """subprocess.run raises FileNotFoundError → GitNotInstalledError (specific subclass)."""
    target = tmp_path / "wt"
    runner = MagicMock(side_effect=FileNotFoundError("no such file: git"))

    with pytest.raises(worktree.GitNotInstalledError):
        worktree.create_workspace(str(target), "ms-65-z", runner=runner)


def test_relative_path_resolves_to_abs(tmp_path, monkeypatch):
    """workspace_path is preserved; abs_workspace_path is resolved against cwd."""
    monkeypatch.chdir(tmp_path)
    runner = MagicMock(return_value=_result(0))

    out = worktree.create_workspace(".worktrees/ms-65-abc", "ms-65-abc", runner=runner)

    # Relative form preserved for the caller's logging.
    assert out["workspace_path"] == ".worktrees/ms-65-abc"
    # Absolute form is what git was invoked with.
    assert os.path.isabs(out["abs_workspace_path"])
    assert out["abs_workspace_path"].endswith(".worktrees/ms-65-abc")
    cmd = runner.call_args[0][0]
    assert out["abs_workspace_path"] in cmd
