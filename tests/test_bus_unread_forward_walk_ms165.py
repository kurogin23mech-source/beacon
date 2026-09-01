"""ms-165 / e-5964 — /bus/unread forward-walk + client deadlock break.

Incident pinned by these tests (confirmed 2026-09-01 via VPS journalctl + MySQL):
parent→fork DMs were permanently undelivered (deliv=False) while fork→parent
DMs delivered fine — the fork bridge was alive and polling, but silently never
received. Root cause: ``GET /bus/unread`` over-fetched ONE oldest-first window
of ``raw_limit`` (≤400) events and filtered to the recipient afterwards. When
the recipient's ``since`` watermark sat far in the past AND the project's total
events past it exceeded 400 — dominated by a HIGH-FREQUENCY other recipient —
the window was entirely other-recipient events, the recipient's own events fell
beyond it, ``filtered`` was always empty, and (because the client watermark only
advances by the created_at of RETURNED events) the recipient could never catch
up: a permanent, silently-worsening deadlock. Fork/worktree sessions hit it
because their inbound watermark freezes at cold-start (they mostly send); a
long-lived parent receives continuously so its watermark tracks near-now.

The behavioural fix (the server walking forward past the barren window, and the
recipient's event being delivered) is covered end-to-end in
``tests/test_bus_transport.py`` (test_unread_forward_walk_reaches_addressed_event_past_window
and test_unread_empty_scan_reports_frontier_to_break_deadlock). These are
STRUCTURAL pins on the sources so a partial revert of the two-sided contract
(server emits a scan frontier ⇄ client consumes it to break the deadlock)
cannot land silently — the client safeguard in particular has no behavioural
Node harness.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUS_MJS = ROOT / "channel" / "bus.mjs"
ROUTERS = ROOT / "server" / "routers_projects.py"


def _bus() -> str:
    return BUS_MJS.read_text(encoding="utf-8")


def _routers() -> str:
    return ROUTERS.read_text(encoding="utf-8")


class TestServerForwardWalk:
    def test_scan_cap_constant_is_env_overridable(self):
        src = _routers()
        assert "_UNREAD_SCAN_CAP" in src, (
            "The forward-walk must be bounded by a named constant so the "
            "per-request memory bound (2026-08-20 OOM fix) stays tunable."
        )
        assert "BEACON_BUS_UNREAD_SCAN_CAP" in src, (
            "Scan cap must read an env override for prod tuning."
        )

    def test_frontier_header_name_is_a_constant(self):
        src = _routers()
        assert "_UNREAD_FRONTIER_HEADER" in src
        assert "X-Bus-Unread-Frontier" in src, (
            "The scan frontier header name is a shared client/server contract."
        )

    def test_unread_handler_walks_forward_in_batches(self):
        src = _routers()
        # The paging walk lives in a testable helper (not inline in the HTTP
        # handler); the handler must delegate to it. Pin the helper + the
        # forward-progress cursor advance + the handler's call site together.
        assert "def _walk_unread_events(" in src, (
            "The forward-walk must be a standalone helper so the paging seam is "
            "unit-testable independently of HTTP/auth/redaction."
        )
        assert "while scanned < scan_cap" in src, (
            "The walk must page forward under the scan cap, not return one "
            "over-fetch window and give up (the deadlock)."
        )
        assert "scan_since = last_created" in src, (
            "The walk must advance its paging cursor by the batch's last "
            "created_at to make forward progress."
        )
        assert re.search(r"filtered,\s*frontier\s*=\s*_walk_unread_events\(", src), (
            "list_unread_bus_events must delegate the walk to _walk_unread_events."
        )

    def test_unread_handler_emits_frontier_header(self):
        src = _routers()
        # The response must carry the frontier so the client can skip a barren
        # region. Pin that the header is set from the frontier variable.
        assert re.search(
            r"_UNREAD_FRONTIER_HEADER\s*:\s*frontier", src
        ), "The /unread response must expose the scan frontier as a header."


class TestClientDeadlockBreak:
    def test_header_aware_get_helper_exists(self):
        src = _bus()
        assert "apiGetWithHeaders" in src, (
            "The bridge needs a header-aware GET so pollOnce can read the "
            "frontier header (plain apiGet drops headers)."
        )

    def test_frontier_header_name_is_a_named_constant(self):
        src = _bus()
        # SSOT: the header name must live as a named constant (mirroring the
        # server's _UNREAD_FRONTIER_HEADER), not a bare literal at the read site,
        # so a rename is a one-line change and greppable across both sides.
        assert re.search(
            r"const\s+BUS_UNREAD_FRONTIER_HEADER\s*=\s*'x-bus-unread-frontier'", src
        ), "The frontier header name must be a named client-side constant."
        assert "headers.get(BUS_UNREAD_FRONTIER_HEADER)" in src, (
            "pollOnce must read the frontier header via the constant, not a "
            "bare string literal (avoids a silent client/server name drift)."
        )

    def test_pollonce_advances_watermark_forward_only_on_empty(self):
        src = _bus()
        # The load-bearing safeguard: on an empty result, jump the watermark to
        # the frontier ONLY when it is strictly ahead (never rewind), and
        # persist so a restart resumes past the barren backlog.
        assert re.search(r"frontier\s*>\s*bridgeLastSeen", src), (
            "Watermark advance must be forward-only (frontier > bridgeLastSeen)."
        )
        assert re.search(
            r"bridgeLastSeen\s*=\s*frontier", src
        ), "pollOnce must advance bridgeLastSeen to the frontier on empty scan."
        # persistence so the deadlock break survives a bridge restart.
        assert "persistDeliveryState()" in src
