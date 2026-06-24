"""ms-95 / e-1490 — periodic refresh of .beacon/bridges/<sid>.json.

Incident pinned by these tests: 2026-06-XX e-1482 dogfood. A 3rd bclaude
launched in a separate worktree was healthy on the live process side, but
`bus directory --live --healthy` silently dropped it from the listing. Root
cause: `writeBridgeClaim()` ran exactly once at cold-start
(`stampColdStartMetadata()`); if bus.mjs ever died and was respawned, the
existing `.beacon/bridges/<sid>.json` kept the original pid / parent_pid /
cwd values, so directory readers saw a dead pid and pruned the entry even
though the bridge was alive.

The fix wires `refreshBridgeClaim()` into the poll loop. It is self-gated
on two triggers (drift OR periodic timer), so a steady-state bridge
rewrites the JSON at most once per `BRIDGE_CLAIM_REFRESH_MS` (default 60s).
That's cheap (~250 bytes per minute), but enough to recover from any
sudden drift within a single retro/ops loop instead of waiting on a manual
restart.

These tests are structural pins on the bus.mjs source so the regression
cannot land via a partial revert. They do NOT spin up a Node bridge — that
level of integration is the 3-bclaude dogfood (AC #2) which a subagent
cannot run; it's a manual smoke before release.

Four defenses are pinned:

  (a) **BRIDGE_CLAIM_REFRESH_MS constant exists and is env-overridable** —
      named knob so ops can tighten or loosen the refresh cadence without
      editing source.
  (b) **refreshBridgeClaim() helper exists** and is gated on drift OR
      timer expiry (= "don't rewrite the same bytes every 5s").
  (c) **writeBridgeClaim() updates the last-written tracker** so the gate
      in (b) has a real signal to compare against.
  (d) **loop() calls refreshBridgeClaim()** inside the while body — the
      load-bearing wiring step.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUS_MJS = ROOT / "channel" / "bus.mjs"


def _read() -> str:
    return BUS_MJS.read_text(encoding="utf-8")


class TestBridgeClaimRefreshConstant:
    def test_constant_is_defined(self):
        src = _read()
        assert "BRIDGE_CLAIM_REFRESH_MS" in src, (
            "Refresh cadence must be a named constant so the disk-cost / "
            "recovery-latency tradeoff is tunable without editing source "
            "(e-1490)."
        )

    def test_constant_reads_env_override(self):
        src = _read()
        assert "BEACON_BUS_BRIDGE_CLAIM_REFRESH_MS" in src, (
            "Env override is required so a dogfood box can drop the "
            "refresh interval to e.g. 5000ms for fast verification without "
            "redeploying (e-1490)."
        )

    def test_constant_default_is_60_seconds(self):
        src = _read()
        # 60000ms is the disk-friendly default — frequent enough to recover
        # from drift within a single retro loop, sparse enough to be a
        # rounding error on IO load.
        m = re.search(
            r"BEACON_BUS_BRIDGE_CLAIM_REFRESH_MS\s*\|\|\s*['\"](\d+)['\"]",
            src,
        )
        assert m, "Could not locate BRIDGE_CLAIM_REFRESH_MS default"
        assert int(m.group(1)) == 60000, (
            "Default must stay at 60000ms — 5s would thrash the disk "
            "(every POLL_INTERVAL tick), >>60s lets stale claims linger "
            "long enough that directory --healthy false-prunes (e-1490)."
        )

    def test_constant_logged_at_startup(self):
        src = _read()
        # Visibility: the startup log line includes the active value so
        # support can confirm refresh cadence from bridge log alone.
        assert "bridge_claim_refresh=" in src


class TestRefreshHelper:
    def test_refresh_helper_exists(self):
        src = _read()
        assert "function refreshBridgeClaim(" in src, (
            "refreshBridgeClaim() helper must exist as the named entry "
            "point from the poll loop (e-1490). loop() calls this, not "
            "writeBridgeClaim() directly, so the drift/timer gate is "
            "enforced in one place."
        )

    def test_refresh_gates_on_drift_and_timer(self):
        src = _read()
        m = re.search(
            r"function refreshBridgeClaim\([^)]*\)\s*{(.*?)\n  }\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate refreshBridgeClaim function body"
        body = m.group(1)
        # Drift gate: compare current process pid/ppid/cwd against the
        # last-written tracker so a steady-state bridge skips disk IO.
        assert "process.pid" in body and "lastClaimPid" in body, (
            "Drift gate must compare process.pid vs lastClaimPid so a "
            "no-op iteration doesn't rewrite the same bytes (e-1490)."
        )
        assert "process.ppid" in body and "lastClaimPpid" in body
        assert "CWD" in body and "lastClaimCwd" in body
        # Timer gate: rewrite at least once per BRIDGE_CLAIM_REFRESH_MS
        # regardless of drift — this is the load-bearing fallback so a
        # bridge that survives a crash gets a fresh mtime + authoritative
        # snapshot reasserted periodically.
        assert "BRIDGE_CLAIM_REFRESH_MS" in body, (
            "Timer gate must consult BRIDGE_CLAIM_REFRESH_MS (e-1490)."
        )
        assert "lastBridgeClaimWriteAt" in body, (
            "Timer gate needs a write-time tracker to compare against "
            "Date.now() (e-1490)."
        )
        # Must actually invoke the writer when gated through.
        assert "writeBridgeClaim()" in body
        # Must be wrapped in try/catch so a transient fs error doesn't
        # kill the poll loop — refresh is best-effort observability,
        # never load-bearing for event delivery.
        assert "try {" in body and "catch" in body, (
            "refreshBridgeClaim must wrap its logic in try/catch — a "
            "drift-check throw must not kill the poll loop (e-1490)."
        )

    def test_writer_updates_last_claim_tracker(self):
        src = _read()
        m = re.search(
            r"function writeBridgeClaim\([^)]*\)\s*{(.*?)\n  }\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate writeBridgeClaim function body"
        body = m.group(1)
        # Without these assignments, the drift gate in refreshBridgeClaim
        # always sees null vs real-value drift and rewrites every iteration.
        assert "lastClaimPid = process.pid" in body, (
            "writeBridgeClaim must record what pid it wrote so the drift "
            "gate has a real signal (e-1490)."
        )
        assert "lastClaimPpid = process.ppid" in body
        assert "lastClaimCwd = CWD" in body
        assert "lastBridgeClaimWriteAt = Date.now()" in body, (
            "writeBridgeClaim must record when it wrote so the timer "
            "gate can compute elapsed time (e-1490)."
        )


class TestLoopIntegration:
    def test_loop_calls_refresh(self):
        src = _read()
        m = re.search(
            r"async function loop\(\)\s*{(.*?)\n  }\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate loop() function body"
        body = m.group(1)
        assert "refreshBridgeClaim()" in body, (
            "loop() must call refreshBridgeClaim() once per iteration — "
            "this is the load-bearing wiring step. Without it, "
            "writeBridgeClaim still runs only at cold-start and the "
            "e-1482 stale-pid regression returns (e-1490)."
        )

    def test_refresh_call_is_inside_while(self):
        src = _read()
        # Defense against "refreshBridgeClaim sits after the while loop
        # exit" mistake — it must be inside the iteration body so it runs
        # repeatedly, not once at shutdown.
        m = re.search(
            r"while \(!stopping\) {(.*?)\n    }\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate while(!stopping) body"
        body = m.group(1)
        assert "refreshBridgeClaim()" in body, (
            "refreshBridgeClaim() must be inside the while(!stopping) body "
            "so it runs every iteration. Placing it after the loop exits "
            "would defeat the periodic refresh (e-1490)."
        )

    def test_refresh_not_awaited(self):
        """refreshBridgeClaim is sync fs — no await needed.

        Pin this so future hands don't accidentally turn it into an async
        helper that needs withWatchdog. The whole point of doing this off
        the network path is that disk-only ops don't need the e-1667
        watchdog scaffolding.
        """
        src = _read()
        # Find the loop iteration body specifically (inside while).
        m = re.search(
            r"while \(!stopping\) {(.*?)\n    }\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate while(!stopping) body"
        body = m.group(1)
        # The exact call line should be "refreshBridgeClaim()" not
        # "await refreshBridgeClaim(...)" or "withWatchdog(refresh...)".
        assert re.search(r"\n      refreshBridgeClaim\(\)\n", body) \
            or re.search(r"\n      refreshBridgeClaim\(\) *\n", body), (
            "refreshBridgeClaim() should be called bare (sync). Wrapping "
            "in await / withWatchdog would imply network IO and is wrong "
            "for a local fs.writeFileSync call (e-1490)."
        )
