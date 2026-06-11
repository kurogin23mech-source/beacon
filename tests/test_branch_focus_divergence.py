"""Unit tests for scripts/check-branch-focus-divergence.py (ms-65 e-1481).

Pins down the conditions that trigger the warning so we don't accidentally
make it too noisy (= warn on the safe single-session case) or too quiet
(= miss the actual unsafe combination). The warning is detection-side
forcing function for the same-cwd branch-share incident from 2026-06-10.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

# Load the script as a module so we can call its internals directly.
_HERE = os.path.dirname(__file__)
_SCRIPT_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "scripts", "check-branch-focus-divergence.py")
)
_spec = importlib.util.spec_from_file_location("check_branch_focus", _SCRIPT_PATH)
check_branch_focus = importlib.util.module_from_spec(_spec)
sys.modules["check_branch_focus"] = check_branch_focus
_spec.loader.exec_module(check_branch_focus)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _runner(table: dict):
    """Return a fake _run that looks up its args in ``table`` (joined by space).

    Unknown args return "" (= matches the real fail-soft behaviour).
    """
    def fake(args, timeout=5):
        key = " ".join(args)
        return table.get(key, "")
    return fake


# ---------------------------------------------------------------------------
# Tests — main project root detection
# ---------------------------------------------------------------------------

def test_in_worktree_no_warning(monkeypatch):
    """git-dir != git-common-dir → in worktree → no warning."""
    table = {
        "git rev-parse --git-dir": "/repo/.git/worktrees/feat-x",
        "git rev-parse --git-common-dir": "/repo/.git",
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))
    assert check_branch_focus.check_divergence() is False


def test_no_git_repo_no_warning(monkeypatch):
    """No git → _run returns "" for both → no warning."""
    monkeypatch.setattr(check_branch_focus, "_run", _runner({}))
    assert check_branch_focus.check_divergence() is False


# ---------------------------------------------------------------------------
# Tests — branch comparison
# ---------------------------------------------------------------------------

def test_on_default_branch_no_warning(monkeypatch):
    """main project root + on default branch → no warning."""
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "main",
        "git symbolic-ref refs/remotes/origin/HEAD": "refs/remotes/origin/main",
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))
    assert check_branch_focus.check_divergence() is False


def test_default_branch_falls_back_to_main(monkeypatch):
    """When symbolic-ref returns "" we still use "main" as the safe default."""
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "main",
        # symbolic-ref intentionally absent
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))
    assert check_branch_focus.check_divergence() is False  # main matches fallback


def test_detached_head_no_warning(monkeypatch):
    """git branch --show-current returns "" for detached HEAD → no warning."""
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "",
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))
    assert check_branch_focus.check_divergence() is False


# ---------------------------------------------------------------------------
# Tests — bridge counting
# ---------------------------------------------------------------------------

def test_single_session_no_warning(monkeypatch, tmp_path):
    """Main root + feature branch + only 1 bridge (= solo) → no warning."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "feat/ms-99",
        "git symbolic-ref refs/remotes/origin/HEAD": "refs/remotes/origin/main",
        "beacon bus directory --live --json":
            f'[{{"cwd": "{cwd}", "session_id": "sid-A"}}]',
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))
    assert check_branch_focus.check_divergence() is False


def test_two_sessions_different_cwd_no_warning(monkeypatch, tmp_path):
    """Two bridges live but other one is in a different cwd → no warning."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "feat/ms-99",
        "git symbolic-ref refs/remotes/origin/HEAD": "refs/remotes/origin/main",
        "beacon bus directory --live --json":
            f'[{{"cwd": "{cwd}", "session_id": "sid-A"}},'
            f' {{"cwd": "/some/other/place", "session_id": "sid-B"}}]',
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))
    assert check_branch_focus.check_divergence() is False


def test_two_sessions_same_cwd_emits_warning(monkeypatch, capsys, tmp_path):
    """Main root + feature branch + 2 bridges sharing cwd → WARNING."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "ms-65-ms-dispatch",
        "git symbolic-ref refs/remotes/origin/HEAD": "refs/remotes/origin/main",
        "beacon bus directory --live --json":
            f'[{{"cwd": "{cwd}", "session_id": "sid-A"}},'
            f' {{"cwd": "{cwd}", "session_id": "sid-B"}}]',
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))

    triggered = check_branch_focus.check_divergence()

    assert triggered is True
    err = capsys.readouterr().err
    assert "ms-65 e-1481" in err
    assert "ms-65-ms-dispatch" in err
    assert "main project root" in err
    # The bridge count text must mention "2 セッション".
    assert "2 セッション" in err
    # Must mention the recommended remedy.
    assert "beacon milestone start" in err
    # And explicitly state non-blocking nature.
    assert "block しません" in err


def test_main_function_exits_zero(monkeypatch, tmp_path):
    """The main() entry point must always return 0 — warn-only contract."""
    monkeypatch.chdir(tmp_path)
    cwd = os.getcwd()
    # Set up the "warn" condition so main() actually runs through the print path.
    table = {
        "git rev-parse --git-dir": "/repo/.git",
        "git rev-parse --git-common-dir": "/repo/.git",
        "git branch --show-current": "feat/x",
        "git symbolic-ref refs/remotes/origin/HEAD": "refs/remotes/origin/main",
        "beacon bus directory --live --json":
            f'[{{"cwd": "{cwd}"}}, {{"cwd": "{cwd}"}}]',
    }
    monkeypatch.setattr(check_branch_focus, "_run", _runner(table))

    rc = check_branch_focus.main()
    assert rc == 0
