"""Tests for _install_claude_hooks (ms-43 e-672).

The `beacon skill install` code path previously registered only the
PostToolUse commit hook, while `beacon init` registered PostToolUse +
PostCompact. The two paths must produce the same end state so users don't
silently miss the post-compaction orientation hook.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def fake_hooks(tmp_path, monkeypatch):
    """Create stub hook scripts and a tmp settings.json path."""
    posttool = tmp_path / "beacon-post-commit-hook.sh"
    posttool.write_text("#!/bin/bash\nexit 0\n")
    postcompact = tmp_path / "beacon-postcompact.sh"
    postcompact.write_text("#!/bin/bash\nexit 0\n")
    settings = tmp_path / "settings.json"

    # ms-95 e-2438: use monkeypatch.delitem so sys.modules entry is auto-restored.
    monkeypatch.delitem(sys.modules, "commands", raising=False)
    import commands  # type: ignore
    monkeypatch.setattr(commands, "CLAUDE_POSTCOMPACT_HOOK_SCRIPT", str(postcompact))

    # ms-95 e-2440: hide the beacon-* console-script entry-points from
    # `_resolve_hook_command`'s `shutil.which()` probe. On CI (and any dev box
    # where `pip install -e .[dev]` placed the entry-points on PATH) the
    # resolver would pick the bare entry-point name (e.g. `beacon-hook-
    # postcompact`) BEFORE consulting the module-level constant we just
    # monkeypatched, so the test's stub path never lands in settings.json.
    # Forcing `which()` to return None for the beacon entry-points makes the
    # resolver fall through to the constant (which IS our stub).
    import shutil
    _real_which = shutil.which
    _entry_block = {
        "beacon-hook-post-commit",
        "beacon-hook-postcompact",
        "beacon-hook-save",
        "beacon-hook-context-monitor",
    }
    def _which_hiding_beacon_entrypoints(cmd, *args, **kwargs):
        if cmd in _entry_block:
            return None
        return _real_which(cmd, *args, **kwargs)
    monkeypatch.setattr(shutil, "which", _which_hiding_beacon_entrypoints)

    return {
        "posttool": str(posttool),
        "postcompact": str(postcompact),
        "settings": str(settings),
        "commands": commands,
    }


def _load_settings(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_install_registers_both_posttooluse_and_postcompact(fake_hooks):
    """Fresh install must end with both hooks present."""
    fake_hooks["commands"]._install_claude_hooks(
        fake_hooks["posttool"], fake_hooks["settings"]
    )
    s = _load_settings(fake_hooks["settings"])
    hooks = s.get("hooks", {})
    # PostToolUse Bash hook
    bash_cmds = [
        h.get("command")
        for entry in hooks.get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    assert fake_hooks["posttool"] in bash_cmds, hooks
    # PostCompact hook
    pc_cmds = [
        h.get("command")
        for entry in hooks.get("PostCompact", [])
        for h in entry.get("hooks", [])
    ]
    assert fake_hooks["postcompact"] in pc_cmds, hooks


def test_install_is_idempotent(fake_hooks):
    """Re-running install must not duplicate the entries."""
    fake_hooks["commands"]._install_claude_hooks(
        fake_hooks["posttool"], fake_hooks["settings"]
    )
    fake_hooks["commands"]._install_claude_hooks(
        fake_hooks["posttool"], fake_hooks["settings"]
    )
    s = _load_settings(fake_hooks["settings"])
    bash_hooks = [
        h
        for entry in s["hooks"].get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    assert len(bash_hooks) == 1, bash_hooks
    pc_hooks = [
        h for entry in s["hooks"].get("PostCompact", [])
        for h in entry.get("hooks", [])
    ]
    assert len(pc_hooks) == 1, pc_hooks


def test_install_preserves_existing_user_hooks(fake_hooks):
    """If the user already had unrelated PostToolUse / PostCompact hooks,
    install must preserve them and add beacon's alongside."""
    pre = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/usr/local/bin/myhook.sh"}],
                }
            ],
            "PostCompact": [
                {"hooks": [{"type": "command", "command": "/usr/local/bin/my-pc.sh"}]}
            ],
        }
    }
    with open(fake_hooks["settings"], "w", encoding="utf-8") as f:
        json.dump(pre, f)
    fake_hooks["commands"]._install_claude_hooks(
        fake_hooks["posttool"], fake_hooks["settings"]
    )
    s = _load_settings(fake_hooks["settings"])
    bash_cmds = [
        h.get("command")
        for entry in s["hooks"].get("PostToolUse", [])
        if entry.get("matcher") == "Bash"
        for h in entry.get("hooks", [])
    ]
    assert "/usr/local/bin/myhook.sh" in bash_cmds
    assert fake_hooks["posttool"] in bash_cmds
    pc_cmds = [
        h.get("command")
        for entry in s["hooks"].get("PostCompact", [])
        for h in entry.get("hooks", [])
    ]
    assert "/usr/local/bin/my-pc.sh" in pc_cmds
    assert fake_hooks["postcompact"] in pc_cmds


def test_install_skips_postcompact_if_script_missing(fake_hooks, monkeypatch):
    """When the postcompact hook script is missing from disk, the function
    must still install PostToolUse cleanly without erroring."""
    monkeypatch.setattr(
        fake_hooks["commands"], "CLAUDE_POSTCOMPACT_HOOK_SCRIPT", "/nonexistent"
    )
    fake_hooks["commands"]._install_claude_hooks(
        fake_hooks["posttool"], fake_hooks["settings"]
    )
    s = _load_settings(fake_hooks["settings"])
    assert "PostToolUse" in s["hooks"]
    # PostCompact may or may not be present (depending on prior state) but
    # shouldn't reference /nonexistent.
    pc_cmds = [
        h.get("command")
        for entry in s["hooks"].get("PostCompact", [])
        for h in entry.get("hooks", [])
    ]
    assert "/nonexistent" not in pc_cmds
