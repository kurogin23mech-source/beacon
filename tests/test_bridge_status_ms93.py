"""Receive-bridge classifier (ms-93 recipient-stability follow-up).

Pins the load-bearing distinction the profile-extractor bug report (2026-07-07)
turned on: ``last_poll_at`` empty = "no bridge ever started" (the silent
send-only orphan) vs present = "a bridge ran before" (died / aging). Conflating
them is what sent the first debugger down the wrong path.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "beacon_cli"))

from skills_helpers.bridge_status import (  # noqa: E402
    NEVER_STARTED,
    RECEIVING,
    STALE,
    _VERDICTS,
    classify_receive,
)


def test_never_started_when_no_poll_timestamp():
    """The orphaned worktree session: bridge=False, last_poll_at=None. This is
    NOT a died bridge — it never started."""
    row = {"session_id": "sv-1", "bridge": False, "last_poll_at": None}
    v = classify_receive(row)
    assert v["verdict"] == NEVER_STARTED
    assert v["can_receive_live"] is False
    assert "送信専用" in v["headline"]
    assert "catch-up" in v["advice"]


def test_never_started_treats_empty_string_like_none():
    v = classify_receive({"session_id": "sv-1", "bridge": False,
                          "last_poll_at": ""})
    assert v["verdict"] == NEVER_STARTED


def test_receiving_when_bridge_live_and_healthy():
    row = {
        "session_id": "sv-1", "bridge": True,
        "last_poll_at": "2026-07-08T00:00:00Z",
        "poll_health": {"healthy": True, "age_seconds": 1.2},
    }
    v = classify_receive(row)
    assert v["verdict"] == RECEIVING
    assert v["can_receive_live"] is True
    assert v["advice"] == ""


def test_stale_when_polled_before_but_not_live_now():
    """A bridge that DID run (poll timestamp present) but is no longer live is
    'stale', distinct from never_started — the debugger should look for a died
    process, not a missing install."""
    row = {
        "session_id": "sv-1", "bridge": False,
        "last_poll_at": "2026-07-07T10:00:00Z",
        "poll_health": {"healthy": False},
    }
    v = classify_receive(row)
    assert v["verdict"] == STALE
    assert v["can_receive_live"] is False
    assert "2026-07-07T10:00:00Z" in v["advice"]  # surfaces the last poll


def test_stale_when_bridge_flag_true_but_unhealthy():
    """bridge=True is not enough — an unhealthy poll (aging out) is still not
    live reception."""
    row = {
        "session_id": "sv-1", "bridge": True,
        "last_poll_at": "2026-07-07T10:00:00Z",
        "poll_health": {"healthy": False},
    }
    assert classify_receive(row)["verdict"] == STALE


def test_verdict_vocabulary_is_closed():
    """Every branch returns a verdict from the closed set (drift guard)."""
    rows = [
        {"bridge": False, "last_poll_at": None},
        {"bridge": True, "last_poll_at": "t", "poll_health": {"healthy": True}},
        {"bridge": False, "last_poll_at": "t"},
    ]
    for r in rows:
        assert classify_receive(r)["verdict"] in _VERDICTS
