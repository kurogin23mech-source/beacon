"""SHARED region purity + copy-setup-prompt allowlisting (ms-93 / e-3203).

2026-07-10 Codex launch review flagged Web/Tauri frontend drift. This pins the
two issues fixed here:
  1. The SHARED region must not call platform-specific fetch primitives
     (api()/invoke()) — trek scope approve/reject now route through dataSource.
  2. copy-setup-prompt (empty-state install button, wired via addEventListener)
     must be allowlisted so it is not treated as an orphan action.

We assert on these SPECIFIC issues rather than overall drift-green: the
trek-*-noop STOP buttons remain flagged on purpose (wiring them to the halt
endpoint is a separate, higher-stakes change).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _drift_output() -> str:
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "check-frontend-drift.py")],
        cwd=str(REPO), capture_output=True, text=True,
    )
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
