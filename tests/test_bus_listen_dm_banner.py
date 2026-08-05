"""Inline pending-DM banner pin for bus listen / receive (ms-70 / e-1715).

When a cross-user DM bus envelope arrives mid-session (= terminal is
already open and ``beacon bus listen`` / ``beacon bus receive`` is
streaming events), the ms-70 / e-1713 dispatcher gate has already
written an ``approval_status="pending"`` sidecar row and downgraded
``effective_delivery`` to ``propose-to-ai``. But until e-1715 the
listen / receive CLIs just printed the event JSON line — there was
no structural cue for the AI session consuming the stream that it
**must stop and ask the user** before acting.

e-1715 closes that gap by emitting an inline banner line
(``[DM-PENDING-APPROVAL] event_id=... from=... at=... -- ask user
before acting``) immediately before the event JSON line whenever the
sidecar lookup reports pending. The banner is the structural cue:
AI sessions reading the stream see the prefix and know to gate.

These tests pin the three AC items from e-1715:

  (a) sidecar present + pending → banner appears in stream
      immediately before the JSON line for that event_id
  (b) no sidecar row → banner suppressed (legacy stream shape
      preserved bit-for-bit)
  (c) sidecar present but already approved / denied → banner
      suppressed (= ``build_pending_lookup`` filters non-pending
      rows; a stale sidecar never re-fires the banner)

Implementation is split across two pure helpers in ``lib/dm_pending``
(``format_inline_dm_banner`` + ``build_pending_lookup``) plus a thin
wrapper ``lib/cmd_bus._print_event_with_banner``. Splitting it this
way means the CLI logic (= ``cmd_bus_listen`` / ``cmd_bus_receive``)
stays one tiny diff away from the legacy behaviour: change the print
call site, leave the loop / cursor / auto-ack handling untouched.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout

# Make lib/ importable without packaging gymnastics — same pattern as
# tests/test_session_start_pending_dm.py.
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "lib")
)

import dm_pending  # noqa: E402


# ---------------------------------------------------------------------------
# helper-level tests (pure, no I/O)
# ---------------------------------------------------------------------------

def test_format_inline_dm_banner_contains_prefix_and_fields():
    """Banner must start with the prefix and surface event_id / sender / time.

    The prefix is the AI gate's structural detection point — any change
    requires the module docstring contract to be updated in lockstep.
    """
    line = dm_pending.format_inline_dm_banner(
        "ev-123",
        sender_user_id="alice@example.com",
        created_at="2026-06-17T10:00:00.000Z",
    )
    assert line.startswith(dm_pending.BANNER_PREFIX)
    assert "event_id=ev-123" in line
    assert "from=alice@example.com" in line
    assert "at=2026-06-17T10:00:00.000Z" in line
    assert dm_pending.BANNER_SUFFIX in line
    # Single-line contract: no embedded newlines so a per-line splitter
    # (= Monitor / shell loops) never confuses banner with JSON.
    assert "\n" not in line


def test_format_inline_dm_banner_handles_missing_fields():
    """Empty / missing sender or created_at must still produce a parsable
    banner — fail-soft so a future sidecar schema tweak does not crash."""
    line = dm_pending.format_inline_dm_banner("ev-456")
    assert line.startswith(dm_pending.BANNER_PREFIX)
    assert "event_id=ev-456" in line
    assert "from=(unknown)" in line
    assert "at=(unknown)" in line


def test_build_pending_lookup_indexes_by_event_id():
    """A pending row must be reachable via event_id O(1) lookup."""
    rows = [
        {"event_id": "ev-1", "approval_status": "pending",
         "sender_user_id": "alice", "created_at": "T1"},
        {"event_id": "ev-2", "approval_status": "pending",
         "sender_user_id": "bob", "created_at": "T2"},
    ]
    lookup = dm_pending.build_pending_lookup(rows)
    assert set(lookup.keys()) == {"ev-1", "ev-2"}
    assert lookup["ev-1"]["sender_user_id"] == "alice"
    assert lookup["ev-2"]["created_at"] == "T2"


def test_build_pending_lookup_filters_non_pending():
    """approved / denied rows must NOT enter the lookup map (AC c)."""
    rows = [
        {"event_id": "ev-pending", "approval_status": "pending"},
        {"event_id": "ev-approved", "approval_status": "approved"},
        {"event_id": "ev-denied", "approval_status": "denied"},
        {"event_id": "ev-no-status"},  # missing key
    ]
    lookup = dm_pending.build_pending_lookup(rows)
    assert set(lookup.keys()) == {"ev-pending"}


def test_build_pending_lookup_handles_empty_inputs():
    """None / [] must collapse to empty dict so the CLI degrades silently."""
    assert dm_pending.build_pending_lookup(None) == {}
    assert dm_pending.build_pending_lookup([]) == {}


# ---------------------------------------------------------------------------
# AC (a / b / c): _print_event_with_banner integration
# ---------------------------------------------------------------------------

def _capture_print(ev, lookup) -> str:
    """Run _print_event_with_banner under stdout capture and return the
    full emitted text. Keeps each test focused on the output contract."""
    # Import inside the helper so the test module loads even when
    # lib/commands.py has an import-time error (= isolation).
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "lib")
    )
    import commands  # noqa: E402
    import cmd_bus  # noqa: E402  (ms-127 e-4803)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_bus._print_event_with_banner(ev, lookup)
    return buf.getvalue()


def test_print_event_with_pending_sidecar_emits_banner_then_json():
    """AC (a): pending sidecar present → banner line precedes the JSON line.

    The structural contract is "banner first, then JSON". AI sessions
    reading the stream line by line see the banner and know to gate
    before they ever parse the event payload.
    """
    ev = {
        "event_id": "ev-live-1",
        "channel": "dm",
        "delivery": "propose-to-ai",
        "payload": {"text": "please commit"},
    }
    lookup = {
        "ev-live-1": {
            "event_id": "ev-live-1",
            "approval_status": "pending",
            "sender_user_id": "alice",
            "created_at": "2026-06-17T10:00:00.000Z",
        }
    }
    out = _capture_print(ev, lookup)
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 2, (
        f"expected banner+JSON (2 lines), got {len(lines)}: {lines!r}"
    )
    assert lines[0].startswith(dm_pending.BANNER_PREFIX)
    assert "event_id=ev-live-1" in lines[0]
    # JSON line must remain a complete, parsable event object so legacy
    # consumers (jq pipelines, Monitor) still work bit-for-bit.
    parsed = json.loads(lines[1])
    assert parsed["event_id"] == "ev-live-1"
    assert parsed["channel"] == "dm"


def test_print_event_without_sidecar_emits_json_only():
    """AC (b): no sidecar → legacy single-line JSON output, no banner."""
    ev = {"event_id": "ev-plain-1", "channel": "general"}
    out = _capture_print(ev, lookup={})
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 1, (
        f"expected JSON-only (1 line), got: {lines!r}"
    )
    # Banner prefix must not appear anywhere — defense in depth in case
    # a future change accidentally double-emits.
    assert dm_pending.BANNER_PREFIX not in out
    parsed = json.loads(lines[0])
    assert parsed["event_id"] == "ev-plain-1"


def test_print_event_with_decided_sidecar_emits_no_banner():
    """AC (c): sidecar exists but already approved → banner suppressed.

    ``build_pending_lookup`` is the structural filter — already-decided
    rows never enter the lookup map. This test pins the wire-through:
    the lookup is the only authority the CLI consults, so an approved
    row passed via the raw row dict (= simulating a future bypass) is
    correctly excluded.
    """
    ev = {"event_id": "ev-approved-1", "channel": "dm"}
    raw_rows = [
        {"event_id": "ev-approved-1", "approval_status": "approved",
         "sender_user_id": "alice", "created_at": "T1"},
    ]
    lookup = dm_pending.build_pending_lookup(raw_rows)
    assert lookup == {}, (
        "build_pending_lookup must filter approved rows out"
    )
    out = _capture_print(ev, lookup=lookup)
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 1
    assert dm_pending.BANNER_PREFIX not in out


def test_print_event_with_unrelated_pending_does_not_misfire():
    """Pending row exists for a DIFFERENT event_id → banner must NOT fire
    for the current event. Prevents a "any pending anywhere → banner on
    everything" misread of the lookup contract.
    """
    ev = {"event_id": "ev-current", "channel": "general"}
    lookup = {
        "ev-other": {
            "event_id": "ev-other",
            "approval_status": "pending",
        }
    }
    out = _capture_print(ev, lookup=lookup)
    lines = out.rstrip("\n").split("\n")
    assert len(lines) == 1
    assert dm_pending.BANNER_PREFIX not in out


# ---------------------------------------------------------------------------
# AC supporting: lookup fetch failure must degrade silently
# ---------------------------------------------------------------------------

def test_fetch_pending_dm_lookup_returns_empty_on_http_error():
    """``_fetch_pending_dm_lookup`` must swallow client errors and return
    ``{}`` so the legacy listen / receive stream keeps flowing when an
    older server (= no /dm/pending endpoint) or transient network issue
    happens. AC: do NOT break existing output."""
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "lib")
    )
    import commands  # noqa: E402
    import cmd_bus  # noqa: E402  (ms-127 e-4803)

    class _FailingClient:
        def get(self, _path):
            raise RuntimeError("API error 404: not found")

    out = cmd_bus._fetch_pending_dm_lookup(_FailingClient(), "proj-x")
    assert out == {}


def test_fetch_pending_dm_lookup_returns_empty_on_non_list_payload():
    """A non-list payload (= future schema bug / proxy injection) must
    NOT crash the lookup builder — return empty dict instead."""
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "lib")
    )
    import commands  # noqa: E402
    import cmd_bus  # noqa: E402  (ms-127 e-4803)

    class _BadShapeClient:
        def get(self, _path):
            return {"unexpected": "shape"}  # dict, not list

    out = cmd_bus._fetch_pending_dm_lookup(_BadShapeClient(), "proj-x")
    assert out == {}


def test_fetch_pending_dm_lookup_indexes_pending_rows():
    """Happy path: the lookup must contain pending rows keyed by event_id."""
    sys.path.insert(
        0, os.path.join(os.path.dirname(__file__), "..", "lib")
    )
    import commands  # noqa: E402
    import cmd_bus  # noqa: E402  (ms-127 e-4803)

    class _OkClient:
        def get(self, path):
            assert "/dm/pending" in path
            return [
                {"event_id": "ev-A", "approval_status": "pending",
                 "sender_user_id": "alice", "created_at": "T1"},
                {"event_id": "ev-B", "approval_status": "approved"},
            ]

    out = cmd_bus._fetch_pending_dm_lookup(_OkClient(), "proj-x")
    assert set(out.keys()) == {"ev-A"}
