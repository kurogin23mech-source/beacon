"""ms-159 / e-6247 — the scale contract for `beacon attention`.

CORE doc `scale-contract-principle`: a listing that returns rows must carry ONE
test that pins its behaviour at scale. `beacon attention`'s inputs grow
MULTIPLICATIVELY (projects × fork sessions), and its whole value is that the
fold (drop running/idle/unknown) and the order (state_since oldest-first) do NOT
degrade as that product grows — otherwise the "one face for everything" collapses
back into noise exactly when parallelism is highest.

This pins, at a multiplicative session count:
  1. FOLD — only attention states survive; running/idle/unknown/clean-terminated
     never leak in, no matter how many there are.
  2. ORDER — survivors are in strictly non-decreasing state_since order (longest
     wait first), even though the input is interleaved/shuffled.
  3. COMPLETENESS — every attention-worthy row is kept (none dropped by the fold).
  4. it stays fast (O(n log n) sort, linear fold) — a coarse wall-clock guard so
     an accidental O(n²) regression trips the test.
"""

from __future__ import annotations

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import attention  # noqa: E402
import bus_liveness  # noqa: E402

# Multiplicative scale: 40 projects × 50 fork sessions = 2000 rows.
N_PROJECTS = 40
N_FORKS = 50

# The rotation of states across the generated fleet. Two of six are
# attention-worthy (awaiting_human, blocked); the rest must be folded.
_STATE_CYCLE = [
    bus_liveness.STATE_RUNNING,
    bus_liveness.STATE_AWAITING_HUMAN,
    bus_liveness.STATE_IDLE,
    bus_liveness.STATE_BLOCKED,
    bus_liveness.STATE_UNKNOWN,
    bus_liveness.STATE_TERMINATED,
]
_ATTENTION = {bus_liveness.STATE_AWAITING_HUMAN, bus_liveness.STATE_BLOCKED}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_fleet():
    """Deterministically build N_PROJECTS × N_FORKS session rows.

    state_since is derived from a scrambled index so the attention-worthy rows
    are interleaved (not pre-sorted) — the sort has real work to do. No RNG
    (Math.random/Date.now are unavailable in this codebase's spirit; keep it
    reproducible)."""
    base = datetime.datetime(2026, 9, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
    rows = []
    idx = 0
    for p in range(N_PROJECTS):
        for f in range(N_FORKS):
            state = _STATE_CYCLE[idx % len(_STATE_CYCLE)]
            # Scramble state_since so ordering is not the insertion order:
            # a multiplicative hash spreads timestamps pseudo-randomly but
            # reproducibly across ~the fleet size in minutes.
            scramble = (idx * 2654435761) % (N_PROJECTS * N_FORKS)
            since = base - datetime.timedelta(minutes=scramble)
            rows.append({
                "session_id": f"sv-{p:03d}-{f:03d}",
                "project_id": f"proj-{p:03d}",
                "project_name": f"project-{p:03d}",
                "state": state,
                "state_since": _iso(since),
            })
            idx += 1
    return rows


def test_attention_scale_fold_order_and_completeness():
    rows = _build_fleet()
    total = N_PROJECTS * N_FORKS
    assert len(rows) == total

    expected_kept = sum(1 for r in rows if r["state"] in _ATTENTION)

    start = time.monotonic()
    out = attention.filter_attention(rows)
    elapsed = time.monotonic() - start

    # 1 + 3. FOLD + COMPLETENESS: exactly the attention rows, nothing else.
    assert len(out) == expected_kept
    assert all(r["state"] in _ATTENTION for r in out)
    folded = {bus_liveness.STATE_RUNNING, bus_liveness.STATE_IDLE,
              bus_liveness.STATE_UNKNOWN, bus_liveness.STATE_TERMINATED}
    assert not any(r["state"] in folded for r in out)

    # 2. ORDER: strictly non-decreasing state_since (longest wait first).
    since_seq = [r["state_since"] for r in out]
    assert since_seq == sorted(since_seq)

    # 4. Coarse speed guard: 2000 rows must fold+sort well under a second (an
    #    O(n²) regression would blow this).
    assert elapsed < 1.0, f"attention fold+sort too slow at scale: {elapsed:.3f}s"


def test_attention_scale_no_state_since_rows_sort_last_at_scale():
    # A slug of rows missing state_since (e.g. a marker that predates the field)
    # must still fold correctly and land AFTER all datable waits, not crash the
    # sort or jump the queue.
    rows = _build_fleet()
    for i in range(0, len(rows), 7):
        if rows[i]["state"] in _ATTENTION:
            rows[i].pop("state_since", None)
    out = attention.filter_attention(rows)
    # Partition: rows with a state_since come first, undated ones last.
    dated = [r for r in out if r.get("state_since")]
    undated = [r for r in out if not r.get("state_since")]
    assert out == dated + undated
    # And the dated portion is still ordered.
    since_seq = [r["state_since"] for r in dated]
    assert since_seq == sorted(since_seq)
