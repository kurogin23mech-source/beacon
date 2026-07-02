"""Tests for ms-98 / e-2773 — wall-clock TTL on beacon CLI subcommands.

Rationale: the 2026-07-02 incident produced 104 hung ``python
commands.py trigger_check`` processes. Individual ``urllib`` timeouts
(30-60 s) did not compose across the four ``_auto_fire_*`` + cleanup
chain; a hang anywhere in the sequence let the process outlive its
budget. ``_install_wall_clock_timeout`` caps every dispatch at a hard
budget via ``signal.SIGALRM`` regardless of how many API round-trips
the subcommand internally makes.

These tests pin:
  * timeout fires at the configured budget
  * ``BEACON_CLI_MAX_SECONDS=0`` disables the timer
  * malformed values fall back to the default (60 s)
  * Windows / non-POSIX (no SIGALRM) skip silently
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMANDS_PY = REPO_ROOT / "lib" / "commands.py"


def _run_cli_hang_script(timeout_env: str = "1", sleep_seconds: int = 5) -> tuple[int, str]:
    """Spawn a subprocess that installs the timeout then sleeps.

    Returns (exit_code, stderr). Never blocks longer than the harness
    timeout below; if the child doesn't die on its own we kill it and
    the test fails.
    """
    script = f"""
import os, sys, time
sys.path.insert(0, {str(REPO_ROOT / 'lib')!r})
os.environ['BEACON_CLI_MAX_SECONDS'] = {timeout_env!r}
import importlib.util
spec = importlib.util.spec_from_file_location('commands', {str(COMMANDS_PY)!r})
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except SystemExit:
    pass
mod._install_wall_clock_timeout('hang-test')
time.sleep({sleep_seconds})
print('reached-end')
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=max(sleep_seconds + 5, 10),
    )
    return proc.returncode, (proc.stderr or "")


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"), reason="POSIX only")
def test_timeout_fires_at_budget():
    """Budget 1 s + sleep 5 s ⇒ process exits with 124 within ~1 s."""
    start = time.time()
    exit_code, stderr = _run_cli_hang_script(timeout_env="1", sleep_seconds=5)
    elapsed = time.time() - start
    assert exit_code == 124, f"expected exit 124, got {exit_code} (stderr={stderr!r})"
    assert elapsed < 3, f"timeout should fire within ~1 s, took {elapsed:.1f} s"
    assert "wall-clock timeout" in stderr
    assert "hang-test" in stderr


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"), reason="POSIX only")
def test_env_zero_disables_timeout():
    """``BEACON_CLI_MAX_SECONDS=0`` ⇒ timer NOT installed, sleep runs to end."""
    exit_code, stderr = _run_cli_hang_script(timeout_env="0", sleep_seconds=2)
    assert exit_code == 0, f"expected exit 0, got {exit_code} (stderr={stderr!r})"
    assert "wall-clock timeout" not in stderr


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGALRM"), reason="POSIX only")
def test_malformed_env_falls_back_to_default():
    """A non-integer value must not crash; timer runs at default 60 s.

    We can't wait a full minute, so the assertion here is just "the
    script did not raise" and no timeout message is emitted at the 2-s
    sleep point.
    """
    exit_code, stderr = _run_cli_hang_script(timeout_env="not-a-number", sleep_seconds=2)
    assert exit_code == 0
    assert "wall-clock timeout" not in stderr


def test_windows_shim_silent_skip():
    """If ``signal.SIGALRM`` isn't defined, the install function is a no-op.

    We simulate the Windows path by monkeypatching ``hasattr`` before
    the call. On real POSIX this test only checks the delete branch — a
    successful monkeypatch means no exception + no signal side effect.
    """
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    try:
        import commands as _commands
    finally:
        # Keep the import cached so subsequent tests don't pay the cost.
        pass

    import signal as _signal

    saved = getattr(_signal, "SIGALRM", None)
    try:
        if hasattr(_signal, "SIGALRM"):
            del _signal.SIGALRM
        _commands._install_wall_clock_timeout("simulated-windows")
        # No exception means the shim engaged silently.
    finally:
        if saved is not None:
            _signal.SIGALRM = saved
