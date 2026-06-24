"""ms-95 / e-1667 — bridge poll loop hang resilience (channel/bus.mjs).

Incident pinned by these tests: 2026-06-12 04:42 JST. A bclaude bridge's
`apiGet`/`apiPost`/`apiPut` used Node's bare `fetch()` with no timeout. The
cloud server stalled (Cloud Run cold start + 429 backpressure) and the
`await pollOnce()` inside the `while (!stopping)` loop hung indefinitely.
`last_poll_at` stopped advancing; the bus directory query marked the bridge
`healthy=false` for 43 minutes (= incident report doc 5Hg9QWvmhfn5MXa1q2uj).

Full root cause analysis lives in the PR body for e-1667. These tests are
structural pins on the bus.mjs source so the regression cannot land via a
partial revert. They do NOT spin up a Node bridge — that level of
integration is the 1h smoke (AC #3) and is run manually before release.

Three defenses are pinned:

  (a) **HTTP_TIMEOUT_MS constant exists and is env-overridable** — the
      per-request budget that bounds any single fetch.
  (b) **apiFetch wraps fetch with AbortController** — the helper through
      which apiGet/apiPost/apiPut now flow. No bare `fetch()` calls remain
      in the HTTP helpers.
  (c) **Loop body wraps pollOnce / scheduler / heartbeat with withWatchdog**
      — defense-in-depth so any non-fetch await (mcp.notification, future
      code paths) also has a structural cap.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUS_MJS = ROOT / "channel" / "bus.mjs"


def _read() -> str:
    return BUS_MJS.read_text(encoding="utf-8")


class TestHttpTimeoutConstant:
    def test_constant_is_defined(self):
        src = _read()
        assert "HTTP_TIMEOUT_MS" in src, (
            "Per-request HTTP timeout must be a named constant so it can be "
            "tuned without editing every fetch call (e-1667)."
        )

    def test_constant_reads_env_override(self):
        src = _read()
        assert "BEACON_BUS_HTTP_TIMEOUT_MS" in src, (
            "Env override is required so ops can raise the timeout during "
            "Cloud Run cold-start storms without redeploying (e-1667)."
        )

    def test_constant_logged_at_startup(self):
        src = _read()
        # Visibility: the startup log line includes http_timeout so support
        # can confirm the active value from bridge log alone.
        assert "http_timeout=" in src


class TestAbortControllerWrap:
    def test_apifetch_helper_exists(self):
        src = _read()
        assert "function apiFetch(" in src, (
            "The api{Get,Post,Put} helpers must share a single apiFetch "
            "wrapper that owns the AbortController (e-1667)."
        )

    def test_apifetch_uses_abort_controller(self):
        src = _read()
        # Extract the apiFetch body and assert it constructs an
        # AbortController and aborts on timeout.
        m = re.search(
            r"async function apiFetch\([^)]*\)\s*{(.*?)\n}\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate apiFetch function body"
        body = m.group(1)
        assert "new AbortController()" in body
        assert "ctrl.abort()" in body
        assert "HTTP_TIMEOUT_MS" in body
        assert "signal: ctrl.signal" in body

    def test_no_bare_fetch_in_api_helpers(self):
        """apiGet / apiPost / apiPut must call apiFetch, never bare fetch."""
        src = _read()
        for helper in ("apiGet", "apiPost", "apiPut"):
            m = re.search(
                rf"async function {helper}\([^)]*\)\s*{{(.*?)\n}}\n",
                src,
                re.DOTALL,
            )
            assert m, f"Could not locate {helper} function body"
            body = m.group(1)
            assert "apiFetch(" in body, (
                f"{helper} must call apiFetch (gets AbortController + "
                f"timeout for free), not bare fetch (e-1667)."
            )
            # Match bare `fetch(` (whitespace before paren, no preceding 'api')
            assert not re.search(r"(?<!\w)fetch\(", body), (
                f"{helper} still contains a bare fetch() call — every HTTP "
                f"call must go through apiFetch (e-1667)."
            )


class TestIterationWatchdog:
    def test_watchdog_helper_exists(self):
        src = _read()
        assert "function withWatchdog(" in src, (
            "withWatchdog wraps each loop step so a hung await never wedges "
            "the iteration (e-1667 defense-in-depth)."
        )

    def test_watchdog_uses_promise_race(self):
        src = _read()
        m = re.search(
            r"function withWatchdog\([^)]*\)\s*{(.*?)\n}\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate withWatchdog function body"
        body = m.group(1)
        assert "Promise.race" in body
        assert "setTimeout" in body
        assert "clearTimeout" in body, (
            "Timer must be cleared in finally so a fast-completing promise "
            "doesn't leak a setTimeout handle every iteration."
        )

    def test_iteration_watchdog_constant(self):
        src = _read()
        assert "ITERATION_WATCHDOG_MS" in src
        assert "BEACON_BUS_ITERATION_WATCHDOG_MS" in src

    def test_pollonce_wrapped_in_loop(self):
        src = _read()
        # Find the loop() body and check pollOnce/scheduler/heartbeat all
        # go through withWatchdog.
        m = re.search(
            r"async function loop\(\)\s*{(.*?)\n  }\n",
            src,
            re.DOTALL,
        )
        assert m, "Could not locate loop() function body"
        body = m.group(1)
        assert "withWatchdog(pollOnce()" in body, (
            "pollOnce must be watchdog-guarded — it's the most likely hang "
            "site (multiple awaits per call: apiGet, ackReceipt, mcp.notification)."
        )
        assert "withWatchdog(runSchedulerTick()" in body
        assert "withWatchdog(writePollHeartbeat()" in body, (
            "writePollHeartbeat hangs are the 2026-06-12 incident path — "
            "must be guarded even though apiPut now has timeout."
        )
