"""Unit tests for `beacon update` (cmd_update / _detect_install_method).

These tests focus on the *decision logic* of self-update:
  - install method detection (brew vs git vs unknown)
  - dry-run (--check) prints intent without invoking brew/git
  - --skill-only short-circuits past brew detection

Subprocess calls (`brew`, `git`, `beacon skill install`) are mocked so the
suite never mutates the user's Homebrew or filesystem.
"""

from __future__ import annotations

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# _detect_install_method
# ---------------------------------------------------------------------------

class _FakeRun:
    """Minimal stand-in for subprocess.CompletedProcess."""
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_detect_brew_when_root_under_brew_prefix(tmp_path, monkeypatch):
    """If `brew --prefix beacon` resolves to a parent of the beacon root,
    method must be reported as 'brew'."""
    # Simulate beacon root == brew prefix
    beacon_root = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.abspath(commands.__file__)))
    )

    def fake_run(args, **kwargs):
        if args[:2] == ["brew", "--prefix"]:
            return _FakeRun(returncode=0, stdout=beacon_root + "\n")
        return _FakeRun(returncode=1)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    info = commands._detect_install_method()
    assert info["method"] == "brew"
    assert info["prefix"] == beacon_root


def test_detect_git_when_dotgit_present(monkeypatch):
    """If brew detection fails but beacon root has .git/, fall back to 'git'."""
    def fake_run(args, **kwargs):
        if args[:2] == ["brew", "--prefix"]:
            return _FakeRun(returncode=1, stderr="No such keg")
        return _FakeRun(returncode=1)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    beacon_root = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.abspath(commands.__file__)))
    )
    # The worktree directory does have a `.git` file (worktree marker) — that's
    # enough for os.path.isdir? Actually `.git` in a worktree is a *file*, not
    # a dir. To make this test robust regardless of whether the test runs in a
    # real git checkout or not, we patch os.path.isdir.
    real_isdir = os.path.isdir

    def fake_isdir(p):
        if p == os.path.join(beacon_root, ".git"):
            return True
        return real_isdir(p)

    monkeypatch.setattr(commands.os.path, "isdir", fake_isdir)
    info = commands._detect_install_method()
    assert info["method"] == "git"
    assert info["prefix"] == beacon_root


def test_detect_unknown_when_neither(monkeypatch):
    """Brew absent and no .git/: must report unknown so we don't guess."""
    def fake_run(args, **kwargs):
        raise FileNotFoundError("brew not on PATH")
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    # Force os.path.isdir(.git) to be False
    real_isdir = os.path.isdir

    beacon_root = os.path.realpath(
        os.path.dirname(os.path.dirname(os.path.abspath(commands.__file__)))
    )

    def fake_isdir(p):
        if p == os.path.join(beacon_root, ".git"):
            return False
        return real_isdir(p)

    monkeypatch.setattr(commands.os.path, "isdir", fake_isdir)
    info = commands._detect_install_method()
    assert info["method"] == "unknown"
    assert info["prefix"] is None


# ---------------------------------------------------------------------------
# cmd_update
# ---------------------------------------------------------------------------

def test_update_skill_only_bypasses_brew(monkeypatch, capsys):
    """--skill-only must not call brew or git at all; it just refreshes Skills.

    With --check on top, it should report intent and exit 0 *without* invoking
    skill_install."""
    monkeypatch.setenv("BEACON_UPDATE_CHECK", "1")
    monkeypatch.setenv("BEACON_UPDATE_SKILL_ONLY", "1")

    # If anything tries to shell out, fail the test loudly.
    def boom(*a, **kw):
        pytest.fail(f"subprocess.run unexpectedly called with {a}")

    monkeypatch.setattr(commands.subprocess, "run", boom)

    # _detect_install_method *does* invoke subprocess.run for `brew --prefix`,
    # so we install a quieter detector for this test.
    monkeypatch.setattr(
        commands, "_detect_install_method",
        lambda: {"method": "git", "prefix": "/x", "current_version": "0.0.0"},
    )

    with pytest.raises(SystemExit) as exc:
        commands.cmd_update()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Skipping CLI upgrade" in out
    assert "would run: beacon skill install" in out


def test_update_check_dry_run_brew(monkeypatch, capsys):
    """--check on a brew install must print the planned commands and exit 0
    without running them."""
    monkeypatch.setenv("BEACON_UPDATE_CHECK", "1")
    monkeypatch.delenv("BEACON_UPDATE_SKILL_ONLY", raising=False)
    monkeypatch.delenv("BEACON_UPDATE_YES", raising=False)

    monkeypatch.setattr(
        commands, "_detect_install_method",
        lambda: {"method": "brew", "prefix": "/opt/homebrew", "current_version": "0.4.0"},
    )

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        # Pretend `brew info` returns a different latest version.
        if args[:2] == ["brew", "info"]:
            payload = {"formulae": [{"versions": {"stable": "0.5.0"}}]}
            return _FakeRun(returncode=0, stdout=json.dumps(payload))
        pytest.fail(f"unexpected subprocess call in --check mode: {args}")

    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        commands.cmd_update()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Latest:  beacon 0.5.0" in out
    assert "brew update" in out
    assert "brew upgrade beacon" in out
    assert "beacon skill install" in out
    # Only `brew info` should have been invoked.
    assert all(c[:2] == ["brew", "info"] for c in calls), calls


def test_update_unknown_method_exits_1(monkeypatch, capsys):
    """If we cannot determine the install method, we must not guess —
    exit 1 with a clear hint to use --skill-only."""
    monkeypatch.delenv("BEACON_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("BEACON_UPDATE_SKILL_ONLY", raising=False)
    monkeypatch.delenv("BEACON_UPDATE_YES", raising=False)
    monkeypatch.setattr(
        commands, "_detect_install_method",
        lambda: {"method": "unknown", "prefix": None, "current_version": "0.4.0"},
    )

    with pytest.raises(SystemExit) as exc:
        commands.cmd_update()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Could not determine" in out
    assert "--skill-only" in out


def test_update_brew_already_up_to_date_reinstalls_skills(monkeypatch, capsys):
    """When brew reports the same version we have, we skip `brew upgrade` but
    still run `beacon skill install` (the Skills directory may have been
    modified manually or got out of sync)."""
    monkeypatch.delenv("BEACON_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("BEACON_UPDATE_SKILL_ONLY", raising=False)
    monkeypatch.setenv("BEACON_UPDATE_YES", "1")

    current = commands.__version__
    monkeypatch.setattr(
        commands, "_detect_install_method",
        lambda: {"method": "brew", "prefix": "/opt/homebrew", "current_version": current},
    )

    skill_install_called = {"flag": False}

    def fake_skill_install():
        skill_install_called["flag"] = True

    monkeypatch.setattr(commands, "cmd_skill_install", fake_skill_install)

    def fake_run(args, **kwargs):
        if args[:2] == ["brew", "info"]:
            payload = {"formulae": [{"versions": {"stable": current}}]}
            return _FakeRun(returncode=0, stdout=json.dumps(payload))
        pytest.fail(f"unexpected subprocess call when already up to date: {args}")

    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        commands.cmd_update()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Already up to date" in out
    assert skill_install_called["flag"], "skill install must still run to stay in sync"
