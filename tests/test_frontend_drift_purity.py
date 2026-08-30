"""SHARED region purity + frontend-drift strict gate (ms-93 e-3203, ms-160 e-5808).

2026-07-10 Codex launch review flagged Web/Tauri frontend drift. This pins:
  1. The SHARED region must not call platform-specific fetch primitives
     (api()/invoke()) — trek scope approve/reject now route through dataSource.
  2. copy-setup-prompt (empty-state install button, wired via addEventListener)
     must be allowlisted so it is not treated as an orphan action.

ms-160 e-5808: the drift guard is now a hard CI stop (--strict, wired in
test.yml). The trek-*-noop Resume/STOP placeholder buttons — previously left
flagged on purpose — are now allowlisted (INTENTIONAL_NOOP_ACTIONS) as
known-deferred Trek debt, so the strict gate can be green on the current tree
while still catching any NEW unhandled action. That makes overall drift-green an
enforceable invariant, which we assert directly below.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-frontend-drift.py"


def _drift_run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO), capture_output=True, text=True,
    )


def _drift_output() -> str:
    proc = _drift_run()
    return proc.stdout + proc.stderr


def test_shared_region_has_no_api_or_invoke_purity_violation():
    out = _drift_output()
    assert "shared/purity" not in out, (
        f"SHARED region leaked a platform fetch primitive (api()/invoke()):\n{out}"
    )


def test_copy_setup_prompt_is_not_flagged_as_orphan():
    out = _drift_output()
    assert "copy-setup-prompt" not in out, (
        f"copy-setup-prompt should be allowlisted (WEB_ONLY + event-listener):\n{out}"
    )


def test_strict_gate_is_clean_on_current_tree():
    """ms-160 e-5808: the same gate CI runs (--strict, exit 1 on drift) must be
    green on the committed tree — otherwise the CI job would red-line every PR."""
    proc = _drift_run("--strict")
    assert proc.returncode == 0, (
        "frontend-drift --strict is not clean; CI would block all merges:\n"
        f"{proc.stdout}{proc.stderr}"
    )


def test_trek_noop_buttons_are_allowlisted_not_flagged():
    """The Trek Resume/STOP placeholders are known-deferred (SPEC 方針5) and must
    be allowlisted, not surface as unhandled-action drift."""
    out = _drift_output()
    assert "trek-resume-noop" not in out and "trek-stop-noop" not in out, (
        f"trek-*-noop should be in INTENTIONAL_NOOP_ACTIONS, not flagged:\n{out}"
    )
