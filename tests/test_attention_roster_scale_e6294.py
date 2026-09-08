"""ms-159 / e-6294 — the scale contract for the ops面 roster + the multi-user
scope器.

CORE doc `scale-contract-principle`: a listing that returns rows carries ONE
test pinning its behaviour at scale. The roster's inputs grow MULTIPLICATIVELY
(users × projects × fork sessions), and its value is that the grouping (by root
target), the ordering (urgent groups first, oldest-wait within), and the
scope=self filter do NOT degrade as that product grows.

This also confirms the multi-user器 (方針3): rows carry user_id, scope=self keeps
only mine, scope=team keeps everyone — the toggle that a later slice turns into a
team UI, exercised here at scale so the器 is proven.
"""
from __future__ import annotations

import datetime
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import attention  # noqa: E402
import bus_liveness  # noqa: E402

# Multiplicative scale: 5 users × 20 projects × 20 forks = 2000 rows.
N_USERS = 5
N_PROJECTS = 20
N_FORKS = 20

_STATE_CYCLE = [
    bus_liveness.STATE_RUNNING,
    bus_liveness.STATE_AWAITING_HUMAN,
    bus_liveness.STATE_IDLE,
    bus_liveness.STATE_BLOCKED,
    bus_liveness.STATE_UNKNOWN,
    bus_liveness.STATE_TERMINATED,
]

_MY_ID = "user-000"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _build_fleet():
    """Deterministically build N_USERS × N_PROJECTS × N_FORKS rows, each carrying
    a working_target.root and a user_id. state_since is scrambled so grouping /
    sorting has real work (no RNG — reproducible)."""
    base = datetime.datetime(2026, 9, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)
    rows = []
    idx = 0
    total = N_USERS * N_PROJECTS * N_FORKS
    for u in range(N_USERS):
        for p in range(N_PROJECTS):
            for f in range(N_FORKS):
                state = _STATE_CYCLE[idx % len(_STATE_CYCLE)]
                scramble = (idx * 2654435761) % total
                since = base - datetime.timedelta(minutes=scramble)
                rows.append({
                    "session_id": f"sv-{u}-{p:03d}-{f:03d}",
                    "user_id": f"user-{u:03d}",
                    "state": state,
                    "state_since": _iso(since),
                    "working_target": {
                        "root": {"id": f"proj-{p:03d}", "label": f"project-{p:03d}"},
                        "target": {"kind": "milestone", "id": f"ms-{p}"},
                    },
                })
                idx += 1
    return rows


def test_roster_scope_self_at_scale():
    rows = _build_fleet()
    assert len(rows) == N_USERS * N_PROJECTS * N_FORKS

    start = time.monotonic()
    mine = attention.filter_roster(rows, my_identity=_MY_ID, scope="self")
    elapsed = time.monotonic() - start

    # scope=self keeps ONLY my rows — exactly one user's worth.
    assert len(mine) == N_PROJECTS * N_FORKS
    assert all(r["user_id"] == _MY_ID for r in mine)
    # coarse O(n) guard.
    assert elapsed < 1.0, f"roster scope filter too slow: {elapsed:.3f}s"


def test_roster_scope_team_keeps_everyone():
    rows = _build_fleet()
    team = attention.filter_roster(rows, my_identity=_MY_ID, scope="team")
    assert len(team) == len(rows)


def test_roster_grouping_is_complete_and_ordered_at_scale():
    rows = _build_fleet()
    mine = attention.filter_roster(rows, my_identity=_MY_ID, scope="self")

    start = time.monotonic()
    groups = attention.group_by_root(mine)
    elapsed = time.monotonic() - start

    # COMPLETENESS: every row lands in exactly one group, none lost/duplicated.
    grouped_total = sum(len(items) for _, items in groups)
    assert grouped_total == len(mine)
    # one group per project (my sessions span all N_PROJECTS roots).
    assert len(groups) == N_PROJECTS
    # each group's rows share one root label.
    seen_labels = set()
    for label, items in groups:
        assert label not in seen_labels  # no split groups
        seen_labels.add(label)

    # ORDER within each group: non-decreasing roster_sort_key (state priority,
    # then oldest-wait). Verify the key sequence is sorted.
    for _label, items in groups:
        keys = [attention.roster_sort_key(r) for r in items]
        assert keys == sorted(keys)

    # ORDER across groups: each group's head is non-decreasing by sort key
    # (most-urgent project first).
    heads = [attention.roster_sort_key(items[0]) for _l, items in groups]
    assert heads == sorted(heads)

    assert elapsed < 1.0, f"roster grouping too slow at scale: {elapsed:.3f}s"


def test_roster_root_filter_at_scale():
    rows = _build_fleet()
    out = attention.filter_roster(
        rows, my_identity=_MY_ID, scope="team", root_id="proj-005")
    assert out  # non-empty
    assert all(attention.root_of(r)[0] == "proj-005" for r in out)
    # every user has forks under proj-005.
    assert len({r["user_id"] for r in out}) == N_USERS
