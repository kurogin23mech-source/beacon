"""`beacon doctor` HOOK_MANIFEST completeness check (ms-160 e-5807).

The pre-e-5807 doctor only confirmed that SOME runnable beacon PostToolUse hook
existed, so a partial install (e.g. `beacon skill install` before e-5806 left the
MCP save hook and the bus-inbox receive hook unwired) passed doctor green while
DM receive / auto-save / STOP silently no-op'd. These tests pin that doctor now
lists every missing manifest hook — the "installer manifest × doctor 照合" half
of the e-5806 forcing function.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def doctor_env(monkeypatch):
    """A minimal beacon project cwd + an isolated ~/.claude home so cmd_doctor
    reads OUR settings.json. Yields (write_settings, home)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".beacon").mkdir()
    (root / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(root / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")

    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    import commands  # type: ignore
    monkeypatch.setattr(commands, "_user_home", lambda: str(home))

    original_cwd = os.getcwd()
    os.chdir(root)

    def _write_settings(data):
        (home / ".claude" / "settings.json").write_text(
            json.dumps(data), encoding="utf-8")

    try:
        yield _write_settings, home, commands
    finally:
        os.chdir(original_cwd)
        tmp.cleanup()


def _run_doctor(commands, capsys) -> str:
    try:
        commands.cmd_doctor()
    except SystemExit:
        pass
    return capsys.readouterr().out


def test_doctor_lists_missing_manifest_hooks(doctor_env, capsys):
    """A settings.json with ONLY the commit hook must make doctor name the
    hooks that were not wired (save / halt / bus-inbox / ...)."""
    write_settings, home, commands = doctor_env
    # Partial install: only the PostToolUse commit hook.
    write_settings({"hooks": {"PostToolUse": [
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "beacon-hook-post-commit"}]}]}})

    out = _run_doctor(commands, capsys)
    assert "Beacon hooks missing" in out, out
    # The wired one is not listed; the unwired ones are.
    assert "post-commit" not in out.split("Beacon hooks missing")[1].split("\n")[0]
    for key in ("save", "halt-check", "bus-inbox"):
        assert key in out, f"{key} should be reported missing:\n{out}"


def test_doctor_silent_when_full_manifest_installed(doctor_env, capsys):
    """After a real install wires the full manifest, doctor must not report any
    missing hooks."""
    write_settings, home, commands = doctor_env
    settings_path = str(home / ".claude" / "settings.json")
    commands._install_claude_hooks(
        commands._resolve_hook_command("beacon-post-commit-hook.sh"), settings_path)

    out = _run_doctor(commands, capsys)
    assert "Beacon hooks missing" not in out, out
