#!/usr/bin/env python3
"""beacon-state-hook — declare this session's execution state (ms-159 e-6244).

Wired as a Claude Code lifecycle hook on UserPromptSubmit / PreToolUse /
PostToolUse / Notification / Stop / SessionEnd. Each fire maps the event to a
canonical work-unit state (running / awaiting_human / idle / terminated) via
``lib/session_state_hook`` and writes it to a per-cwd marker file
``<root>/.beacon/session-state.json``.

The beacon-bus bridge (``channel/bus.mjs``) reads that marker on its next poll
and piggybacks ``declared_state`` / ``declared_at`` onto the EXISTING heartbeat
PUT (slice SPEC ``Icb8zFtbnZZ1yXzMsLO6`` 方針4 — no new send path). The server
then projects the final ``state`` via ``lib/bus_liveness.derive_state`` (e-6245).
This hook only records the declaration locally; it does not talk to the server.

Hook protocol
-------------
Stdin: the harness JSON ({cwd, hook_event_name, session_id, ...}).
Stdout: silent — this hook injects no context. It only writes a file.
It NEVER raises to the harness: any error is swallowed to stderr so a marker
write can't block the turn or tear down the session (a best-effort observability
signal must never be load-bearing for the session itself).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path


def _find_beacon_dir(start: Path):
    """Walk up from ``start`` for a ``.beacon`` directory; return it or None.

    Uses the ``.beacon`` dir itself as the marker (not ``project.json``) so this
    works in cloud-mode worktrees where the local project.json may be absent but
    ``.beacon/`` (cloud.json / session.json) is always present — the same dir the
    bridge reads.
    """
    try:
        cur = start.resolve()
    except Exception:
        return None
    while True:
        candidate = cur / ".beacon"
        if candidate.is_dir():
            return candidate
        if cur == cur.parent:
            return None
        cur = cur.parent


def _import_session_state_hook():
    """Import ``lib/session_state_hook`` from the beacon source tree, or None.

    Mirrors the fail-safe lazy import posture of ``beacon-bus-inbox-hook.py``:
    the state hook is a thin observability layer, so a missing lib must degrade
    to "write nothing" rather than error into the harness.
    """
    here = Path(__file__).resolve().parent
    for lib_dir in (here.parent / "lib", here.parent.parent / "lib"):
        if (lib_dir / "session_state_hook.py").exists():
            sys.path.insert(0, str(lib_dir))
            try:
                import session_state_hook as _mod  # type: ignore[import-not-found]
            except Exception:
                return None
            return _mod
    return None


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Write JSON to ``path`` atomically (temp + os.replace) so the bridge never
    reads a half-written marker."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    try:
        hook_input = json.loads(raw) if raw.strip() else {}
    except Exception:
        hook_input = {}

    event_name = hook_input.get("hook_event_name") or ""
    mod = _import_session_state_hook()
    if mod is None:
        return 0
    if mod.event_to_declared_state(event_name) is None:
        # Unrecognized / empty event declares nothing — leave the last marker
        # untouched (never clobber a real state with a guess). Cheap check before
        # touching the filesystem.
        return 0
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")

    start = Path(hook_input.get("cwd") or os.getcwd())
    beacon_dir = _find_beacon_dir(start)
    if beacon_dir is None:
        return 0
    marker_path = beacon_dir / "session-state.json"
    # Read the prior marker so build_state_marker can preserve state_since across
    # a re-declaration of the same state (only reset the "since" clock on a real
    # transition). A missing / unreadable prior marker is fine — treated as None.
    prev_marker = None
    try:
        with open(marker_path, encoding="utf-8") as f:
            prev_marker = json.load(f)
    except Exception:
        prev_marker = None
    marker = mod.build_state_marker(event_name, now_iso, prev_marker=prev_marker)
    if marker is None:  # defensive: mapping already checked above, but re-guard
        return 0
    try:
        _atomic_write_json(marker_path, marker)
    except Exception as e:  # never block the turn on a marker write
        print(f"beacon-state-hook: marker write failed (non-fatal): {e}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # absolute backstop — never raise to the harness
        print(f"beacon-state-hook: unexpected error (non-fatal): {e}",
              file=sys.stderr)
        sys.exit(0)
