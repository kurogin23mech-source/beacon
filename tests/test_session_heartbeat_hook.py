"""Tests for bin/beacon-session-heartbeat-hook.sh — post ms-54 e-1319 contract.

Background
----------
Originally (ms-54 e-1189) this PostToolUse hook fire-and-forget'd
``beacon session id`` after every Bash tool call to keep
``.beacon/session.json.last_active`` fresh, so a long-running idle Claude
Code session would stay visible in
``beacon bus directory --since-min 30 --live`` even when the user wasn't
typing prompts.

Post Option C (PR #111 / commit 78048b6) the bridge's poll loop stamps
``last_active`` + ``last_poll_at`` every poll iteration, which is the
structural truth source. A duplicate CLI-side write let a session appear
"alive" while the bridge poll loop was dead, so DMs piled up server-side
unread (the dogfood incident that motivated this cleanup).

The hook is intentionally kept on disk as a silent no-op tombstone:
existing users have it wired into their PostToolUse hook list in
``~/.claude/settings.json`` and removing the script would spam
"command not found" warnings. Replacing it with a no-op gives a smooth
migration path — the next ``beacon channel install`` (or a manual edit)
can drop the hook entry entirely.

These tests pin the new contract:
  - every input shape (happy path, recursion, malformed) → exit 0, no beacon call
  - stdin is still consumed (so any pipe writer sees a normal reader)
  - never blocks the harness
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "beacon-session-heartbeat-hook.sh"


@pytest.fixture
def beacon_project_dir(tmp_path):
    """tmp dir with a minimal .beacon/project.json — kept for the
    legacy "find_beacon_root succeeds" case, even though the no-op hook
    no longer cares about it."""
    beacon = tmp_path / ".beacon"
    beacon.mkdir()
    (beacon / "project.json").write_text(json.dumps({
        "name": "SessionHeartbeatHookTest",
        "summary": "",
        "milestones": [],
    }))
    return tmp_path


@pytest.fixture
def fake_beacon_bin(tmp_path, monkeypatch):
    """Plant a fake `beacon` binary on PATH that records invocations.

    Post e-1319 the hook MUST NOT invoke it — this fixture is the
    regression net for "the hook accidentally got un-no-op'd".
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    sentinel = tmp_path / "beacon-calls.log"
    fake = bin_dir / "beacon"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> '{sentinel}'\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return sentinel


def _run(cwd: Path, command: str, env: dict | None = None,
         tool_input: dict | None = None) -> subprocess.CompletedProcess:
    """Drive the hook with a tool_input.command payload."""
    payload_inner = {"command": command, "cwd": str(cwd)}
    if tool_input:
        payload_inner.update(tool_input)
    payload = {"tool_input": payload_inner}
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=10,
        env={**os.environ, **(env or {})},
    )


def _assert_no_beacon_call(sentinel: Path) -> None:
    """Wait a beat and assert the fake beacon was never invoked."""
    time.sleep(0.2)
    assert not sentinel.exists() or not sentinel.read_text().strip(), (
        "post-e-1319 contract: hook is a no-op and MUST NOT spawn beacon. "
        "If this fires, the hook regressed to the e-1189 active-heartbeat shape."
    )


# ---------------------------------------------------------------------------
# Core invariant: every shape -> exit 0, never spawns beacon (e-1319)
# ---------------------------------------------------------------------------

def test_plain_bash_command_does_not_fire_heartbeat(beacon_project_dir, fake_beacon_bin):
    """Post e-1319: the hook never spawns beacon, regardless of input.

    The bridge poll loop owns liveness; a CLI-side write would only
    re-introduce the dual-truth ambiguity that this cleanup removed.
    """
    result = _run(beacon_project_dir, "ls -la")
    assert result.returncode == 0
    _assert_no_beacon_call(fake_beacon_bin)


def test_git_command_does_not_fire_heartbeat(beacon_project_dir, fake_beacon_bin):
    """Same contract for git commands — the original e-1189 happy path
    that DID spawn beacon. Post e-1319 it must not."""
    result = _run(beacon_project_dir, "git status")
    assert result.returncode == 0
    _assert_no_beacon_call(fake_beacon_bin)


def test_beacon_command_does_not_fire_heartbeat(beacon_project_dir, fake_beacon_bin):
    """`beacon <anything>` invocations — historically the recursion guard
    handled this. Post e-1319 the hook is no-op regardless, so the
    recursion guard is moot but the result is still "no beacon spawn"."""
    result = _run(beacon_project_dir, "beacon status")
    assert result.returncode == 0
    _assert_no_beacon_call(fake_beacon_bin)


def test_beacon_session_id_does_not_fire_heartbeat(beacon_project_dir, fake_beacon_bin):
    """Historically the dangerous self-recursion case; now trivially safe."""
    result = _run(beacon_project_dir, "beacon session id")
    assert result.returncode == 0
    _assert_no_beacon_call(fake_beacon_bin)


def test_silent_exit_when_not_in_beacon_project(tmp_path, fake_beacon_bin):
    """Hook fires outside a beacon project (no .beacon/project.json up
    the tree). Must exit 0 silently and not spawn beacon."""
    result = _run(tmp_path, "ls")
    assert result.returncode == 0
    assert result.stdout == ""
    _assert_no_beacon_call(fake_beacon_bin)


# ---------------------------------------------------------------------------
# Malformed / empty input — silent exit invariant (preserved from e-1189)
# ---------------------------------------------------------------------------

def test_silent_exit_on_malformed_stdin(beacon_project_dir):
    """Stdin is not valid JSON — exit 0. Other hooks behind us may still
    want to run; we must not break the chain."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input="not valid json at all",
        capture_output=True,
        text=True,
        cwd=str(beacon_project_dir),
        timeout=10,
    )
    assert result.returncode == 0


def test_silent_exit_on_empty_stdin(beacon_project_dir):
    """Empty stdin — exit 0. Some hook events don't provide a payload."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input="",
        capture_output=True,
        text=True,
        cwd=str(beacon_project_dir),
        timeout=10,
    )
    assert result.returncode == 0


def test_silent_exit_when_tool_input_missing(beacon_project_dir):
    """Valid JSON but no tool_input field — exit 0."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps({"hook_event_name": "PostToolUse"}),
        capture_output=True,
        text=True,
        cwd=str(beacon_project_dir),
        timeout=10,
    )
    assert result.returncode == 0
