#!/bin/bash
# context-usage-monitor.sh — thin delegator to the canonical Python monitor.
#
# Hook type: Stop.
# Payload (stdin JSON): { "session_id": "...", "transcript_path": "...", ... }
#
# ── Why this is now a shim (ms-159 / e-6534) ─────────────────────────────────
# This file historically carried a full bash + jq reimplementation of the
# context-usage threshold monitor, kept parallel to the Python port in
# beacon_cli/hooks/context_monitor.py. Maintaining two copies in lock-step
# failed silently: e-6499 added context_pct persistence (the value the ops-room
# roster shows as each session's context% badge) to the PYTHON side only. This
# bash file — the one actually wired as the Stop hook — kept writing just
# {session_id, notified_thresholds} and never emitted context_pct, so the
# monitor→state-file→bridge→heartbeat→server→roster chain died at stage 1 and
# the badge stayed permanently empty.
#
# The fix is structural, not another patch: collapse to ONE canonical
# implementation. The Python module is the canonical one — it is cross-platform
# (this bash+jq script cannot run on Windows pipx), already carries the full
# context_pct logic, and is what `beacon-hook-context-monitor` / manifest ship
# on every platform. With no second copy left to fall behind, "only one impl got
# fixed" can no longer happen. tests/test_context_monitor_drift_e6534.py pins
# that this file delegates rather than reimplementing.
#
# stdin is inherited across `exec`, so the Python entry point reads the same Stop
# payload. bin/context-usage-monitor.py resolves beacon_cli.hooks.context_monitor
# whether run from a source checkout or a pipx install; both files are installed
# side by side (manifest.json), so resolving this script's own directory locates
# it in either layout. The Python main() is itself fail-safe (silent exit when
# there is no .beacon/project.json, malformed payload, etc.), so the Stop event
# is never blocked.
#
# Resolve the real directory of THIS script before exec (#755 review M-F5): a
# bare `dirname "$0"` returns "." when the hook is invoked by bare name, which
# would look for the sibling .py under the caller's cwd instead of the install
# dir. `cd -- "$(dirname -- "$0")" && pwd -P` yields the absolute, symlink-
# resolved directory so the delegation is location-independent.
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
exec python3 "$SCRIPT_DIR/context-usage-monitor.py"
