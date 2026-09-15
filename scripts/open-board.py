#!/usr/bin/env python3
"""Open the Beacon board for the current project (ms-170 e-6347).

One way to see the board, regardless of how the project is backed.

History:
  * ms-85 e-3178 extracted this from the session-start Skill (Step 2.7) as a
    script (then named ``open-webui.py``), preserving the then-current
    behavior: cloud projects opened the hosted Web UI (``beacon-ai.dev``) in a
    browser, local projects launched the Tauri desktop app.
  * ms-170 e-6347 folded that branch away and renamed the script to
    ``open-board.py`` (the name ``open-webui.py`` had become the negation of
    the behavior — AX / maintainability review, 2026-09-15). ``beacon view`` is
    now the single unified viewer; its conversion layer absorbs the data source
    (local ``.beacon`` vs cloud API) behind a fixed view schema, so the same
    board renders for both. The Tauri desktop launch and the cloud/local switch
    are retired; ms-149 (repointing the desktop app's local read to SQLite) is
    thereby moot (recorded as absorbed into ms-170).

Contract of THIS launcher (only):
  * Resolve the beacon CLI of *this* install (the ``bin/beacon`` sibling;
    PATH fallback) so a shadowing ``beacon`` can't launch a different viewer.
  * Launch ``beacon view`` detached — it serves in the foreground (until
    Ctrl+C) and would hang session-start otherwise — capturing its startup
    output to a log file.
  * **Observe before announcing** (AX review, 2026-09-15): poll the log briefly
    for the board URL and emit ``VIEWER_URL=<url>`` only once we actually see
    it. If the child dies, emit ``VIEWER_LAUNCH_FAILED=<phase> (log: <path>)``;
    if it is alive but hasn't announced a URL within the wait, emit
    ``VIEWER_LAUNCHED_UNCONFIRMED (log: <path>)``. Never print a success marker
    we didn't observe, and keep stderr in the log so a non-opening board can be
    diagnosed.

``beacon view`` itself decides delegation (bundled Go viewer vs Python
fallback) and opens the browser — see ``beacon view --help``. This launcher
does not re-describe that (single source of truth).

Known limitation (AX-4, deferred): re-running session-start spawns a fresh
``beacon view`` each time — there is no reuse of an already-running viewer yet.
Idempotent reuse belongs viewer-side (a portfile / liveness check) and is
folded into the Go bundling work (e-6476). Tracked as a follow-up task.

Always exits 0 — never blocks session-start beyond a short bounded wait.

Call sites:
  * /beacon-session-start Skill (Step 2.7)
  * /beacon-init Skill (post-creation board open)
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time

# The board URL appears in `beacon view`'s startup line ("盤を開きました:
# http://127.0.0.1:PORT/", or the Go viewer's own address line). We only need
# the first http(s) URL it prints.
_URL_RE = re.compile(r"https?://[^\s]+")

# Bounded wait so session-start is never held for long. beacon view flushes its
# URL line at startup, so this is normally satisfied in well under a second.
_WAIT_SECONDS = 2.5
_POLL_INTERVAL = 0.15


def _beacon_bin() -> str:
    """Resolve the beacon CLI that backs this install.

    This script is invoked as ``<install-root>/scripts/open-board.py``, so the
    sibling ``<install-root>/bin/beacon`` is the CLI belonging to the very
    install being run — preferring it avoids a shadowing ``beacon`` earlier on
    PATH launching a different viewer than the one that started this session.
    Falls back to a bare ``beacon`` (PATH) when the sibling is absent.
    """
    sibling = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bin",
        "beacon",
    )
    if os.path.isfile(sibling):
        return sibling
    return "beacon"


def _log_path() -> str:
    """A stable-per-project temp log so diagnostics are findable across runs."""
    key = hashlib.sha1(os.path.abspath(".").encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"beacon-view-{key}.log")


def _spawn(beacon_bin: str, log_fd) -> subprocess.Popen:
    """Detached ``beacon view``; stdout+stderr → log_fd (boundary, patchable).

    ``start_new_session`` puts the viewer in its own session so it outlives this
    launcher and the shell that ran it. Streams go to a file (not a pipe) so the
    child never blocks on a full pipe buffer once we stop reading.
    """
    return subprocess.Popen(
        [beacon_bin, "view"],
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _scan_url(log_path: str) -> str:
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            m = _URL_RE.search(f.read())
        return m.group(0) if m else ""
    except OSError:
        return ""


def launch_and_observe(
    beacon_bin: str,
    log_path: str,
    *,
    wait_seconds: float = _WAIT_SECONDS,
    poll_interval: float = _POLL_INTERVAL,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> str:
    """Spawn the viewer, observe its startup, return the marker line to print.

    Returns one of:
      * ``VIEWER_URL=<url>``                              — board is serving
      * ``VIEWER_LAUNCH_FAILED=exited-<rc> (log: <path>)``— child died
      * ``VIEWER_LAUNCHED_UNCONFIRMED (log: <path>)``     — alive, URL not seen
      * ``""``                                            — could not spawn
    """
    try:
        log_fd = open(log_path, "w", encoding="utf-8")
    except OSError:
        # No place to log — fall back to a silent detached spawn (best-effort);
        # we cannot observe, so we do not claim success.
        try:
            _spawn(beacon_bin, subprocess.DEVNULL)
        except Exception:
            return ""
        return "VIEWER_LAUNCHED_UNCONFIRMED (log: unavailable)"

    with log_fd:
        try:
            proc = _spawn(beacon_bin, log_fd)
        except Exception:
            return ""

    deadline = monotonic() + wait_seconds
    while monotonic() < deadline:
        rc = proc.poll()
        url = _scan_url(log_path)
        if url:
            return f"VIEWER_URL={url}"
        if rc is not None:
            return f"VIEWER_LAUNCH_FAILED=exited-{rc} (log: {log_path})"
        sleep(poll_interval)
    # Alive but hasn't announced a URL yet — don't assert success we didn't see.
    return f"VIEWER_LAUNCHED_UNCONFIRMED (log: {log_path})"


def main() -> int:
    marker = launch_and_observe(_beacon_bin(), _log_path())
    if marker:
        print(marker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
