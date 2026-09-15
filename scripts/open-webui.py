#!/usr/bin/env python3
"""Open the Beacon board for the current project (ms-170 e-6347).

One way to see the board, regardless of how the project is backed.

History:
  * ms-85 e-3178 extracted this from the session-start Skill (Step 2.7) as a
    script, preserving the then-current behavior: cloud projects opened the
    hosted Web UI (``beacon-ai.dev``) in a browser, local projects launched the
    Tauri desktop app. The launch destination branched on project form.
  * ms-170 e-6347 folds that branch away. ``beacon view`` is now the single
    unified viewer: its conversion layer absorbs the data source (local
    ``.beacon`` vs cloud API) behind a fixed view schema, so the same board
    renders for both — there is no per-form branch left to make here. The
    Tauri desktop launch and the cloud/local switch are retired; ms-149
    (repointing the desktop app's local read to SQLite) is thereby moot and
    recorded as absorbed into ms-170.

Behavior:
  * Launch ``beacon view`` detached and return immediately. ``beacon view``
    stands up the viewer (delegating to the bundled Go 運用室 when present,
    else the Python fallback board) and opens the browser itself, so this
    launcher neither blocks on the server nor opens the browser. It prints
    ``VIEWER_LAUNCHED=beacon view`` so session-start can note it in the header.
  * The auto-opened board is now the unified viewer for cloud projects too
    (a transitional downgrade from the richer hosted Web UI until the Go
    viewer is bundled — e-6476 — and the server serves the same viewer). The
    hosted Web UI at ``beacon-ai.dev`` is still reachable by hand.

Always exits 0 — never blocks session-start. If ``beacon view`` cannot be
launched (binary missing, spawn error) it prints nothing (best-effort).

Call sites:
  * /beacon-session-start Skill (Step 2.7)
"""
from __future__ import annotations

import os
import subprocess
import sys


def _beacon_bin() -> str:
    """Resolve the beacon CLI that backs this install.

    This script is invoked as ``<install-root>/scripts/open-webui.py``, so the
    sibling ``<install-root>/bin/beacon`` is the CLI belonging to the very
    install being run — preferring it avoids a shadowing ``beacon`` earlier on
    PATH launching a different viewer than the one that started this session.
    Falls back to a bare ``beacon`` (PATH) when the sibling is absent (e.g. a
    packaged install with a different layout).
    """
    sibling = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "beacon",
    )
    if os.path.isfile(sibling):
        return sibling
    return "beacon"


def _launch_viewer(beacon_bin: str) -> bool:
    """Best-effort detached launch of ``beacon view``.

    ``beacon view`` runs a foreground server (it serves until Ctrl+C), so we
    must not wait on it — session-start would hang. Detach into its own session
    (``start_new_session``) so the viewer outlives this launcher and the shell
    that ran it, and silence its streams (the browser it opens is the UI; its
    stdout URL line is not surfaced here because we do not block to read it).
    Returns True if the child was spawned, False if it could not be.
    """
    try:
        subprocess.Popen(
            [beacon_bin, "view"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def main() -> int:
    if _launch_viewer(_beacon_bin()):
        print("VIEWER_LAUNCHED=beacon view")
    return 0


if __name__ == "__main__":
    sys.exit(main())
