"""Windows hook resolution / executability (ms-44 e-853).

On Windows, a bare ``.sh`` PostToolUse hook command cannot be executed by
Claude Code's hook runner, so commit -> /beacon-log silently never fires.
``_resolve_hook_command`` must therefore never resolve to a ``.sh`` on Windows
(falling through to the entry-point / ``python -m`` form), and ``_install_claude_hook``
(the ``beacon init`` path) must use that resolver rather than the raw bash
``CLAUDE_*_HOOK_SCRIPT`` constants.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # type: ignore


def test_hook_unusable_on_windows_true_for_sh(monkeypatch):
    monkeypatch.setattr(commands.os, "name", "nt")
    assert commands._hook_unusable_on_windows("C:\\x\\beacon-post-commit-hook.sh")
    assert commands._hook_unusable_on_windows("foo.SH")  # case-insensitive


def test_hook_unusable_on_windows_false_for_exe_and_module(monkeypatch):
    monkeypatch.setattr(commands.os, "name", "nt")
    assert not commands._hook_unusable_on_windows("C:\\x\\beacon-hook-post-commit.exe")
    assert not commands._hook_unusable_on_windows("python -m beacon_cli.hooks.post_commit")


def test_hook_sh_allowed_off_windows(monkeypatch):
    monkeypatch.setattr(commands.os, "name", "posix")
    assert not commands._hook_unusable_on_windows("/x/beacon-post-commit-hook.sh")


def test_resolve_hook_command_skips_sh_on_windows(monkeypatch, tmp_path):
    """On Windows, an existing .sh (const/source) must be skipped in favor of
    the python -m fallback when the entry-point exe is not on PATH."""
    monkeypatch.setattr(commands.os, "name", "nt")
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    sh = tmp_path / "beacon-post-commit-hook.sh"
    sh.write_text("#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(commands, "CLAUDE_HOOK_SCRIPT", str(sh))
    monkeypatch.setattr(commands, "_find_hook", lambda b: str(sh))

    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    assert not cmd.lower().endswith(".sh"), cmd
    assert "beacon_cli.hooks.post_commit" in cmd  # python -m fallback


def test_resolve_hook_command_allows_sh_off_windows(monkeypatch, tmp_path):
    """Off Windows, the .sh remains a valid resolution (unchanged behavior)."""
    monkeypatch.setattr(commands.os, "name", "posix")
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    sh = tmp_path / "beacon-post-commit-hook.sh"
    sh.write_text("#!/bin/bash\nexit 0\n")
    monkeypatch.setattr(commands, "CLAUDE_HOOK_SCRIPT", str(sh))
    monkeypatch.setattr(commands, "_find_hook", lambda b: str(sh))

    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    assert cmd == str(sh)


def test_resolve_hook_command_prefers_entrypoint(monkeypatch):
    """The installed entry-point exe (shutil.which) always wins."""
    monkeypatch.setattr(commands.shutil, "which", lambda name: "C:\\bin\\" + name + ".EXE")
    cmd = commands._resolve_hook_command("beacon-post-commit-hook.sh")
    assert cmd == "C:\\bin\\beacon-hook-post-commit.EXE"


def test_init_hook_install_writes_no_sh_on_windows(monkeypatch, tmp_path):
    """`beacon init`'s _install_claude_hook must not write a .sh commit hook on
    Windows (regression for the bug where it hardcoded CLAUDE_HOOK_SCRIPT)."""
    import json

    monkeypatch.setattr(commands.os, "name", "nt")
    monkeypatch.setattr(commands.shutil, "which", lambda name: None)
    # Make every disk-resolved candidate a .sh so the only safe answer is python -m.
    sh = tmp_path / "hook.sh"
    sh.write_text("#!/bin/bash\n")
    monkeypatch.setattr(commands, "CLAUDE_HOOK_SCRIPT", str(sh))
    monkeypatch.setattr(commands, "CLAUDE_SAVE_HOOK_SCRIPT", str(sh))
    monkeypatch.setattr(commands, "CLAUDE_POSTCOMPACT_HOOK_SCRIPT", str(sh))
    monkeypatch.setattr(commands, "_find_hook", lambda b: str(sh))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(commands, "_user_home", lambda: str(home))

    commands._install_claude_hook()

    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = [
        h.get("command", "")
        for entry in settings["hooks"].get("PostToolUse", [])
        for h in entry.get("hooks", [])
    ]
    assert cmds, settings
    assert not any(c.lower().endswith(".sh") for c in cmds), cmds
