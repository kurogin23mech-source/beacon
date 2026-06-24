"""Unit tests for ms-61 / e-2005 — PR lifecycle ↔ MS progress integration.

Covers two layers of the forcing function:

1. ``core.count_task_status`` recognises PR-specific terminal states
   (``approved`` / ``merged`` / ``closed``) as "done" for MS progress
   purposes. Before e-2005 only ``done`` / ``cancelled`` counted, so a
   PR ``beacon pr approve``'d (= status="approved") but never
   ``beacon pr merge``'d looked unfinished even though the human work
   was complete. ms-75 / ms-76 / ms-83 each lost a counted unit this
   way (= reported by user 2026-06-18).

2. ``core.plan_pr_sync`` / ``core.apply_pr_sync`` align beacon PR
   entries with GitHub state from ``gh pr list --state all --json``.
   This is the sync path that catches PRs merged through the GitHub UI
   without anyone running ``beacon pr merge``.

The 3 AC-required tests:
  - test_count_treats_pr_approved_as_done
  - test_count_treats_pr_merged_as_done
  - test_count_treats_pr_closed_as_done

Plus a few neighbouring guards so the change can't quietly regress.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_with_pr(pr_status: str, *, has_extra_task: bool = False) -> dict:
    """Build a single-MS project containing one PR entry in `pr_status`
    and (optionally) one done task as a baseline for the counter.
    """
    entries = [
        {
            "id": "e-100",
            "type": "pr",
            "description": "PR #1",
            "status": pr_status,
            "meta": {"url": "https://github.com/x/y/pull/1",
                     "pr_number": 1,
                     "pr_status": pr_status},
        },
    ]
    if has_extra_task:
        entries.append({
            "id": "e-101",
            "type": "task",
            "description": "baseline task",
            "status": "done",
        })
    return {
        "name": "t",
        "summary": "",
        "milestones": [
            {"id": "ms-1", "title": "x",
             "status": "in_progress", "entries": entries},
        ],
    }


# ---------------------------------------------------------------------------
# AC #1 of the 3 required tests — PR status="approved" must count as done
# ---------------------------------------------------------------------------

def test_count_treats_pr_approved_as_done():
    """A PR that has been `beacon pr approve`'d but not yet `pr merge`'d
    must still count as done for MS progress. This is the exact case the
    user observed in ms-75 / ms-76 / ms-83 (= 2026-06-18 report)."""
    data = _ms_with_pr("approved")
    entries = data["milestones"][0]["entries"]
    total, done = core.count_task_status(entries)
    assert total == 1
    assert done == 1


# ---------------------------------------------------------------------------
# AC #2 — PR status="done" (= post merge) still counts (regression guard)
# ---------------------------------------------------------------------------

def test_count_treats_pr_merged_as_done():
    """`beacon pr merge` sets entry.status="done", which already counted
    before e-2005. Lock that in so we don't break it while widening the
    "done" predicate."""
    data = _ms_with_pr("done")
    # Also tag meta.pr_status="merged" to simulate the post-merge shape.
    data["milestones"][0]["entries"][0]["meta"]["pr_status"] = "merged"
    entries = data["milestones"][0]["entries"]
    total, done = core.count_task_status(entries)
    assert total == 1
    assert done == 1


# ---------------------------------------------------------------------------
# AC #3 — PR status="closed" / "cancelled" counts as done (= no longer in-flight)
# ---------------------------------------------------------------------------

def test_count_treats_pr_closed_as_done():
    """A PR closed without merge (= GitHub state CLOSED) is no longer
    in-flight; it should not block MS progress from reaching 100 %."""
    # `pr_close` sets entry.status="cancelled" (pre-existing behaviour).
    data_cancelled = _ms_with_pr("cancelled")
    total, done = core.count_task_status(
        data_cancelled["milestones"][0]["entries"]
    )
    assert total == 1
    assert done == 1

    # Also accept raw "closed" status on the entry for forward-compat
    # (callers writing the entry.status directly).
    data_closed = _ms_with_pr("closed")
    total, done = core.count_task_status(
        data_closed["milestones"][0]["entries"]
    )
    assert total == 1
    assert done == 1


# ---------------------------------------------------------------------------
# Negative guards — in-flight PR statuses must NOT count as done
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pr_status", ["in_review", "open"])
def test_count_treats_inflight_pr_as_undone(pr_status: str):
    """A PR still in the review loop is not finished — counter must reflect
    that or the progress signal becomes meaningless."""
    data = _ms_with_pr(pr_status)
    entries = data["milestones"][0]["entries"]
    total, done = core.count_task_status(entries)
    assert total == 1
    assert done == 0


# ---------------------------------------------------------------------------
# plan_pr_sync — produces the right transitions from gh-pr-list rows
# ---------------------------------------------------------------------------

def test_plan_pr_sync_marks_merged_pr_for_merge():
    data = _ms_with_pr("approved")  # beacon side: approved, not yet merged
    gh_prs = [{"number": 1, "state": "MERGED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    assert len(actions) == 1
    assert actions[0]["action"] == "merge"
    assert actions[0]["pr_number"] == 1
    assert actions[0]["entry_id"] == "e-100"


def test_plan_pr_sync_marks_closed_pr_for_close():
    data = _ms_with_pr("in_review")  # beacon: still open in review
    gh_prs = [{"number": 1, "state": "CLOSED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    assert len(actions) == 1
    assert actions[0]["action"] == "close"


def test_plan_pr_sync_skips_aligned_entries():
    """When beacon already shows done/cancelled, no action is needed even
    if GitHub state is MERGED/CLOSED — guards against double-stamping."""
    data = _ms_with_pr("done")
    data["milestones"][0]["entries"][0]["meta"]["pr_status"] = "merged"
    gh_prs = [{"number": 1, "state": "MERGED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    assert len(actions) == 1
    assert actions[0]["action"] == "skip"


def test_plan_pr_sync_extracts_pr_number_from_url():
    """If meta.pr_number is missing but meta.url has /pull/<n>, the planner
    must still recover the number."""
    data = _ms_with_pr("approved")
    data["milestones"][0]["entries"][0]["meta"]["pr_number"] = None
    gh_prs = [{"number": 1, "state": "MERGED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    assert len(actions) == 1
    assert actions[0]["action"] == "merge"


# ---------------------------------------------------------------------------
# apply_pr_sync — mutates project state via existing pr_merge / pr_close
# ---------------------------------------------------------------------------

def test_apply_pr_sync_merges_marked_entries():
    data = _ms_with_pr("approved")
    gh_prs = [{"number": 1, "state": "MERGED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    summary = core.apply_pr_sync(data, actions)
    assert summary["merged"] == 1
    assert summary["closed"] == 0
    pr = data["milestones"][0]["entries"][0]
    assert pr["status"] == "done"
    assert pr["meta"]["pr_status"] == "merged"


def test_apply_pr_sync_closes_marked_entries():
    data = _ms_with_pr("in_review")
    gh_prs = [{"number": 1, "state": "CLOSED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    summary = core.apply_pr_sync(data, actions)
    assert summary["closed"] == 1
    pr = data["milestones"][0]["entries"][0]
    assert pr["status"] == "cancelled"
    assert pr["meta"]["pr_status"] == "closed"


# ---------------------------------------------------------------------------
# End-to-end: after sync, MS progress reflects the merged PR
# ---------------------------------------------------------------------------

def test_pr_sync_then_count_reflects_progress():
    """The whole point: a PR `pr approve`'d locally + merged on GitHub
    should reach 100 % done after `beacon pr sync` runs. Composes the
    counter fix and the sync path into a single end-to-end assertion."""
    data = _ms_with_pr("approved", has_extra_task=True)
    # Before sync — counter now reads 2/2 thanks to the count_task_status
    # fix alone (approved counts as done).
    total, done = core.count_task_status(
        data["milestones"][0]["entries"]
    )
    assert (total, done) == (2, 2)

    # Now also run the sync — state should reach the canonical "done"
    # form (= entry.status="done", meta.pr_status="merged").
    gh_prs = [{"number": 1, "state": "MERGED",
               "url": "https://github.com/x/y/pull/1"}]
    actions = core.plan_pr_sync(data, gh_prs)
    core.apply_pr_sync(data, actions)
    total2, done2 = core.count_task_status(
        data["milestones"][0]["entries"]
    )
    assert (total2, done2) == (2, 2)
    pr = data["milestones"][0]["entries"][0]
    assert pr["status"] == "done"
