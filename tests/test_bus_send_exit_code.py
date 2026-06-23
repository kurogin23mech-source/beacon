"""Tests for `beacon bus send` exit code contract (ms-93 / e-2274).

Background
==========
2026-06-23 Codex CLI dogfood report: `beacon bus send --channel dm --to <sid>`
created an event AND stamped delivery / opened, yet the bash wrapper exited
with status 1. Codex (and any other CLI agent that gates its next action on
`$?`) treats this as a hard failure even though the message succeeded —
breaking automation built on top of beacon's bus surface.

Root cause
----------
``bin/beacon`` under ``set -euo pipefail`` ran the `bus send` flow through
``while ... done`` over `_bus_to_list`, ending with the idiom::

    [[ "$_rc" -ne 0 ]] && exit "$_rc"

When every recipient succeeded, `_rc=0`, so the ``[[ -ne 0 ]]`` test failed
(= shell exit status 1), the ``&&`` short-circuited, and the failed test was
the LAST executed simple command in the case block. Bash propagates the
last command's exit status, so the whole `beacon bus send` invocation
returned 1 despite every Python subprocess returning 0.

Direct invocation (``python3 lib/commands.py bus_send`` with env vars) was
unaffected because it never traversed the broken bash idiom. The mismatch
(direct = 0, wrapper = 1) is the exact symptom that surfaced in dogfood.

Fix
---
Replace ``[[ "$_rc" -ne 0 ]] && exit "$_rc"`` with an unconditional
``exit "$_rc"`` so the success path lands on exit 0 explicitly and the
failure path still exits with the failing recipient's status.

These tests pin the contract end-to-end via ``subprocess.run(["beacon",
"bus", "send", ...])`` so any future regression that re-introduces the
broken idiom (or any other path that drops a stray non-zero on a
successful send) is caught at test time, not at the next dogfood report.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BEACON_BIN = REPO_ROOT / "bin" / "beacon"


# ---------------------------------------------------------------------------
# Pure bash idiom tests (no API call required — pins the case-statement
# exit code contract regardless of network / cloud config).
# ---------------------------------------------------------------------------


def _run_bash_snippet(snippet: str) -> int:
    """Run a bash snippet under `set -euo pipefail` and return its exit code.

    The wrapper deliberately uses ``set -euo pipefail`` like ``bin/beacon``
    does, so the idiom test mirrors production shell semantics.
    """
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + snippet],
        capture_output=True,
        text=True,
    )
    return proc.returncode


def test_legacy_broken_idiom_returns_1_on_success_path():
    """Regression demo: the OLD idiom drops exit 1 on the success path.

    This test pins the *known-bad* behavior so anyone resurrecting the
    pattern in another command immediately sees what they broke. The
    only purpose of this test is to make the bug visible.
    """
    rc = _run_bash_snippet(
        "case outer in\n"
        "  outer)\n"
        "    case send in\n"
        "      send)\n"
        "        _rc=0\n"
        "        [[ \"$_rc\" -ne 0 ]] && exit \"$_rc\"\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n"
    )
    # The bug: every "recipient" succeeded but bash still returned 1.
    assert rc == 1, (
        "The legacy idiom should fail this regression demo. "
        "If it stops failing, bash semantics changed and this whole "
        "test file's premise needs revisiting."
    )


def test_fixed_idiom_returns_0_on_success_path():
    """The fix: unconditional `exit "$_rc"` lands on 0 when all succeeded."""
    rc = _run_bash_snippet(
        "case outer in\n"
        "  outer)\n"
        "    case send in\n"
        "      send)\n"
        "        _rc=0\n"
        "        exit \"$_rc\"\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n"
    )
    assert rc == 0


def test_fixed_idiom_preserves_failure_exit_code():
    """The fix must not flatten genuine failures into exit 0.

    A real recipient subprocess returning non-zero (here we simulate with
    `_rc=2`) must propagate through unchanged so callers can still tell
    success from failure.
    """
    rc = _run_bash_snippet(
        "case outer in\n"
        "  outer)\n"
        "    case send in\n"
        "      send)\n"
        "        _rc=2\n"
        "        exit \"$_rc\"\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "esac\n"
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# bin/beacon source inspection — guard against the broken idiom returning.
# ---------------------------------------------------------------------------


def test_bin_beacon_bus_send_uses_explicit_exit_rc():
    """Structural guard: bin/beacon must not reintroduce the broken idiom
    on the `bus send` fan-out path.

    We don't try to enforce this everywhere in the file (other branches may
    legitimately use ``[[ ]] && exit``), only on the post-loop line of the
    bus send fan-out, which is the documented regression site.
    """
    src = BEACON_BIN.read_text(encoding="utf-8")
    # The fan-out lives right after `done <<< "$_bus_to_list"`. Find that
    # marker and look at the next few lines for the broken pattern.
    marker = 'done <<< "$_bus_to_list"'
    assert marker in src, (
        f"Could not find the fan-out marker `{marker}` in bin/beacon — "
        "the file layout changed; update this test."
    )
    after = src.split(marker, 1)[1]
    # Look at the first ~20 lines after the marker (the fix lives here).
    head = "\n".join(after.splitlines()[:20])
    assert '[[ "$_rc" -ne 0 ]] && exit "$_rc"' not in head, (
        "Regression: the broken `[[ -ne 0 ]] && exit` idiom is back on the "
        "bus send fan-out line. Use `exit \"$_rc\"` unconditionally "
        "(see ms-93 / e-2274)."
    )
    assert 'exit "$_rc"' in head, (
        "The fan-out path must exit with `$_rc` explicitly so success path "
        "= 0 / failure path = the failing recipient's status."
    )


# ---------------------------------------------------------------------------
# End-to-end: `beacon bus send` against an unreachable API still distinguishes
# success / failure exit codes correctly via the wrapper.
#
# We don't have a stub server here, so we exercise the codepath that fails
# *before* the network call — `--channel` missing — and assert the wrapper
# faithfully propagates the Python exit code.
# ---------------------------------------------------------------------------


def test_beacon_bus_send_missing_channel_exits_nonzero():
    """`beacon bus send` without --channel must exit non-zero.

    This is the failure path (Python `sys.exit(1)` on validation error).
    The wrapper must propagate that, not flatten it.
    """
    env = os.environ.copy()
    # Make sure we don't accidentally hit the network during validation.
    env["BEACON_BUS_SKIP_TO_CHECK"] = "1"
    env["BEACON_BUS_NO_LIVE_CHECK"] = "1"
    proc = subprocess.run(
        [str(BEACON_BIN), "bus", "send"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    # Either Python validation fails (exit 1) or bash arg-parsing surfaces an
    # error before the python call (also non-zero). Both are acceptable here;
    # the contract this test pins is "broken send = non-zero".
    assert proc.returncode != 0, (
        f"Expected non-zero exit on missing --channel, got 0.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
