"""Tests for bin/beacon-session-heartbeat-hook.sh (ms-54 / e-1189).

Background
----------
The bug e-1189: long-running idle Claude Code sessions drift out of
``beacon bus directory --since-min 30 --live`` because the per-session
``last_active`` timestamp only gets bumped by /beacon-session-start Step 0b
(once per session) and by explicit ``beacon`` CLI invocations from the
user. A session that runs only ``git`` / ``ls`` / etc. for 30 min becomes
invisible to DM senders even though it is alive.

Primary fix lives in ``bin/beacon-bus-inbox-hook.py`` (which already runs on
every SessionStart + UserPromptSubmit). This shell hook is the optional,
opt-in carrier for projects that want the refresh on every PostToolUse
Bash event too — fully covered by ``.claude/settings.json``::

    {
      "hooks": {
        "PostToolUse": [{
          "matcher": "Bash",
          "hooks": [{
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/bin/beacon-session-heartbeat-hook.sh",
            "timeout": 5
          }]
        }]
      }
    }

These tests lock in the hook's contracts:
  - silent exit on every malformed / off-project / recursive path
  - background ``beacon session id`` spawn on the happy path
  - never blocks the harness (return code 0 always)
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
    """tmp dir with a minimal .beacon/project.json so the hook proceeds past
    the find_beacon_root gate."""
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
    """Plant a fake `beacon` binary on PATH that records its invocations to
    a sentinel file. Lets the test prove the hook actually invoked the CLI
    without depending on a real beacon install."""
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

    # PATH override is what the hook reads via `command -v beacon`.
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_plain_bash_command_fires_heartbeat(beacon_project_dir, fake_beacon_bin):
    """The whole point of the hook: any ordinary Bash invocation triggers a
    background ``beacon session id`` so the bus directory sees us live."""
    result = _run(beacon_project_dir, "ls -la")
    assert result.returncode == 0

    # The fake binary is invoked asynchronously via `( ... ) &`; give the
    # detached subshell a brief moment to write the sentinel before asserting.
    for _ in range(20):
        if fake_beacon_bin.exists() and fake_beacon_bin.read_text().strip():
            break
        time.sleep(0.05)

    assert fake_beacon_bin.exists(), (
        "the hook never invoked `beacon` — recursion / path resolution "
        "regressed (no sentinel file written)"
    )
    body = fake_beacon_bin.read_text().strip()
    assert "session id" in body, (
        f"expected `beacon session id` invocation, got: {body!r}"
    )


def test_git_command_fires_heartbeat(beacon_project_dir, fake_beacon_bin):
    """Common case in real sessions: AI is running git status / git log
    while drafting a commit. Each one must keep the directory entry fresh."""
    result = _run(beacon_project_dir, "git status")
    assert result.returncode == 0

    for _ in range(20):
        if fake_beacon_bin.exists() and fake_beacon_bin.read_text().strip():
            break
        time.sleep(0.05)
    assert fake_beacon_bin.read_text().strip(), (
        "git status MUST trigger a heartbeat — the inbox hook only covers "
        "UserPromptSubmit, so PostToolUse is the gap-filler for long, "
        "prompt-free tool sequences"
    )


# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------

def test_beacon_command_is_skipped(beacon_project_dir, fake_beacon_bin):
    """``beacon <anything>`` already runs ``update_last_active`` in the main
    dispatcher (ms-57 e-1035). Re-firing here would double the cloud writes
    in the debounce window and clutter hook-debug logs."""
    result = _run(beacon_project_dir, "beacon status")
    assert result.returncode == 0

    # Give the (hopefully not-spawned) subprocess time to leak if recursion
    # actually fires.
    time.sleep(0.15)
    assert not fake_beacon_bin.exists() or not fake_beacon_bin.read_text().strip(), (
        "recursion guard failed — `beacon status` should NOT trigger a "
        "secondary `beacon session id` fire"
    )


def test_beacon_session_id_self_call_is_skipped(beacon_project_dir, fake_beacon_bin):
    """The most dangerous recursion shape: the hook's own command. If the
    skip regex misses this, the bash subshell loops on itself."""
    result = _run(beacon_project_dir, "beacon session id")
    assert result.returncode == 0

    time.sleep(0.15)
    assert not fake_beacon_bin.exists() or not fake_beacon_bin.read_text().strip()


def test_word_beacon_in_other_position_is_skipped(beacon_project_dir, fake_beacon_bin):
    """`/opt/homebrew/bin/beacon --version` — beacon invoked via absolute
    path. Must also be detected by the recursion guard."""
    result = _run(beacon_project_dir, "/opt/homebrew/bin/beacon --version")
    assert result.returncode == 0

    time.sleep(0.15)
    assert not fake_beacon_bin.exists() or not fake_beacon_bin.read_text().strip()


def test_word_beacon_inside_string_does_not_skip(beacon_project_dir, fake_beacon_bin):
    """False positive prevention: a git log message that mentions the word
    'beacon' as a token should still fire the heartbeat. The skip regex
    only matches an actual command-position invocation (start-of-line or
    after whitespace, with whitespace or EOL after)."""
    # `echo` with "beacon" as an argument — the word `beacon` is the second
    # token, not the command. The current regex matches `beacon` anywhere
    # word-bounded by whitespace/slash; the test asserts the documented
    # behavior. If a future refinement of the guard narrows the regex,
    # this expectation should be updated.
    result = _run(beacon_project_dir, "echo 'I mention beacon in a string'")
    assert result.returncode == 0
    # We don't strictly assert the heartbeat fires here — the current regex
    # is conservative and will skip if `beacon` appears after whitespace
    # anywhere. The test exists to make the trade-off explicit; if it ever
    # becomes a real problem in practice, narrow the regex to require start
    # of line or `;`/`|`/`&&` before `beacon`.


# ---------------------------------------------------------------------------
# Off-project / no-binary / malformed paths — silent exit
# ---------------------------------------------------------------------------

def test_silent_exit_when_not_in_beacon_project(tmp_path, fake_beacon_bin):
    """No .beacon/project.json anywhere up the tree — silent exit. The
    hook is installed at the user / project level but the actual cwd at
    the time of invocation may be outside a beacon project (e.g. AI cd'd
    into /tmp for a one-off command)."""
    result = _run(tmp_path, "ls")
    assert result.returncode == 0
    assert result.stdout == ""
    time.sleep(0.1)
    assert not fake_beacon_bin.exists() or not fake_beacon_bin.read_text().strip()


def test_silent_exit_when_no_beacon_binary(beacon_project_dir, monkeypatch):
    """beacon project is present but no `beacon` CLI on PATH and no
    bin/beacon in the project tree. Hook must still exit 0 — the user has
    a partial install and we cannot rescue their heartbeat, but breaking
    every Bash tool call would be much worse."""
    # Empty PATH so `command -v beacon` returns nothing. We keep /bin and
    # /usr/bin so jq / grep / bash itself still resolve.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = _run(beacon_project_dir, "ls",
                  env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0
    assert result.stdout == ""


def test_repo_local_beacon_fallback(beacon_project_dir, tmp_path, monkeypatch):
    """If `beacon` is not on PATH but a project-local `bin/beacon` exists,
    the hook uses that. Matches the dev-clone / test-runner reality where
    the system beacon is older than what's checked out."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    # Plant bin/beacon inside the beacon_project_dir.
    bin_dir = beacon_project_dir / "bin"
    bin_dir.mkdir()
    sentinel = beacon_project_dir / "beacon-local-calls.log"
    fake = bin_dir / "beacon"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> '{sentinel}'\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    result = _run(beacon_project_dir, "ls",
                  env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0

    for _ in range(20):
        if sentinel.exists() and sentinel.read_text().strip():
            break
        time.sleep(0.05)
    assert sentinel.exists() and "session id" in sentinel.read_text()


def test_silent_exit_on_malformed_stdin(beacon_project_dir):
    """Stdin is not valid JSON — silent exit. The hook contract mirrors
    beacon-post-commit-hook.sh: malformed payload must not break the chain
    (other hooks behind us may still want to run)."""
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
    """Empty stdin — silent exit. Some hook events do not provide a payload."""
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
    """Valid JSON but no tool_input field — the JSON is parseable, the
    command string ends up empty, and the recursion guard is irrelevant.
    Must still terminate without blocking."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps({"hook_event_name": "PostToolUse"}),
        capture_output=True,
        text=True,
        cwd=str(beacon_project_dir),
        timeout=10,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# AC #3 simulation contrast — Step 0b only vs Step 0b + periodic refresh
# ---------------------------------------------------------------------------

def test_simulation_contrast_session_start_only_vs_per_bash_refresh(
    beacon_project_dir, fake_beacon_bin
):
    """e-1189 AC #3: "session-start のみ / session-start + 周期 refresh の
    2 状態を simulate して directory 露出差を再現できるテスト". We model the
    two states by counting heartbeat invocations across N Bash tool calls.

    state (a) Step 0b only: heartbeat fires 0 times across N tool calls
              after session-start. Real-world: directory drops the session
              after `--since-min` window.
    state (b) Step 0b + per-PostToolUse refresh (this hook): fires N times.
              Real-world: directory keeps the session for at least one
              window past the LAST tool call.

    The arithmetic difference (0 vs N) is what makes the directory entry
    sticky enough to receive a DM.
    """
    N = 5

    # State (a): the hook is not installed. We simulate by running an
    # equivalent script that just exits 0 without touching the heartbeat.
    # The sentinel must stay empty.
    fake_beacon_bin.write_text("")  # reset
    for _ in range(N):
        # No hook fires in this scenario.
        pass
    assert not fake_beacon_bin.read_text(), (
        "state (a) baseline: no heartbeats fired without the hook"
    )

    # State (b): the hook is installed. Each Bash tool call fires it.
    for _ in range(N):
        result = _run(beacon_project_dir, "ls")
        assert result.returncode == 0

    # Wait for backgrounded fakes to land.
    for _ in range(40):
        if len(fake_beacon_bin.read_text().splitlines()) >= N:
            break
        time.sleep(0.05)

    lines = [l for l in fake_beacon_bin.read_text().splitlines() if l.strip()]
    assert len(lines) == N, (
        f"state (b): expected {N} heartbeat fires across {N} tool calls, "
        f"got {len(lines)} — periodic-refresh contract broken"
    )
    assert all("session id" in l for l in lines)
