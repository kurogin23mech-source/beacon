"""ms-128 方針4 (e-4365) chunk 4: block/cycle surfacing in the leader digest.

The server tick calls ``reconcile_blocks`` each sweep (wired in app.py). The
leader digest is how the leader learns state, so blocked targets and dependency
cycles must appear there:

  * ``build_task_state_aggregate`` counts ``block`` in its own bucket (not
    collapsed to todo) and emits a ``blocked_queue`` with each blocked target's
    blockers + still-unsatisfied blockers.
  * ``build_leader_digest_payload`` surfaces ``blocked_queue_count`` /
    ``blocker_cycle_count`` in the summary, a ``blocker_cycles`` list, and a body
    line that tells the leader to try autonomous cycle resolution first
    (方針4 leader-first escalation surface).
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402
import trek_scheduler as scheduler  # noqa: E402

_NOW = datetime.datetime(2026, 7, 29, tzinfo=datetime.timezone.utc)


def _blocked_trek():
    doc = {"trek_id": "tk-blk", "task_states": {}}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    return doc


# --- aggregate --------------------------------------------------------------

def test_block_has_its_own_count_bucket():
    doc = _blocked_trek()
    agg = scheduler.build_task_state_aggregate(doc)
    assert agg["counts"]["block"] == 1
    # ...and it is NOT double-counted as todo.
    assert agg["counts"]["todo"] == 0


def test_blocked_queue_lists_blockers_and_unsatisfied():
    doc = _blocked_trek()
    agg = scheduler.build_task_state_aggregate(doc)
    assert len(agg["blocked_queue"]) == 1
    row = agg["blocked_queue"][0]
    assert row["task_id"] == "A"
    assert row["blockers"] == ["B", "C"]
    assert set(row["unsatisfied"]) == {"B", "C"}


def test_blocked_queue_shrinks_unsatisfied_as_blockers_progress():
    doc = _blocked_trek()
    # Satisfy B (leader_review); C still open.
    trek.set_task_state(doc, task_id="B", state="working")
    trek.set_task_state(doc, task_id="B", state="leader_review")
    agg = scheduler.build_task_state_aggregate(doc)
    row = agg["blocked_queue"][0]
    assert row["unsatisfied"] == ["C"]           # B dropped off
    assert row["blockers"] == ["B", "C"]         # full edge list retained


def test_overall_state_not_disturbed_by_block():
    # A blocked-only trek keeps overall_state at todo (block collapses there,
    # per the conservative precedence — many consumers read this token).
    doc = _blocked_trek()
    agg = scheduler.build_task_state_aggregate(doc)
    assert agg["overall_state"] == "todo"


# --- digest -----------------------------------------------------------------

def test_digest_surfaces_blocked_count_and_body_line():
    doc = _blocked_trek()
    dig = scheduler.build_leader_digest_payload(doc, now=_NOW)
    assert dig["summary"]["blocked_queue_count"] == 1
    assert dig["summary"]["blocker_cycle_count"] == 0
    assert dig["blocker_cycles"] == []
    assert "blocked (依存待ち): 1 件" in dig["body"]


def test_digest_clean_trek_has_no_block_line():
    doc = {"trek_id": "tk-clean", "task_states": {
        "e-a": {"state": "working"},
    }}
    dig = scheduler.build_leader_digest_payload(doc, now=_NOW)
    assert dig["summary"]["blocked_queue_count"] == 0
    assert dig["blocker_cycles"] == []
    assert "依存待ち" not in dig["body"]
    assert "依存の循環" not in dig["body"]


def test_digest_surfaces_cycle_with_leader_first_escalation_text():
    doc = _blocked_trek()
    # Inject a cycle bypassing add_blocker's write-time guard.
    doc[trek.TARGET_BLOCKERS_KEY]["C"] = ["A"]
    dig = scheduler.build_leader_digest_payload(doc, now=_NOW)
    assert dig["summary"]["blocker_cycle_count"] == 1
    assert len(dig["blocker_cycles"]) == 1
    assert "依存の循環" in dig["body"]
    # leader-first escalation: try autonomous resolution before escalating to user
    assert "まずリーダー" in dig["body"]


def test_digest_shape_keys_present():
    # Regression guard on the payload contract the leader-side Skill reads.
    doc = _blocked_trek()
    dig = scheduler.build_leader_digest_payload(doc, now=_NOW)
    for key in ("blocked_queue_count", "blocker_cycle_count"):
        assert key in dig["summary"]
    assert "blocker_cycles" in dig
    assert "blocked_queue" in dig["task_state_aggregate"]


# --- working-regressed-blocker surface (AX review 2026-07-29) ---------------

def _working_with_regressed_blocker():
    doc = {"trek_id": "tk-wr", "task_states": {}}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.set_task_state(doc, task_id="B", state="working")
    trek.set_task_state(doc, task_id="B", state="leader_review")  # satisfy
    trek.reconcile_blocks(doc)                                    # A → todo
    trek.set_task_state(doc, task_id="A", state="working")        # A in flight
    trek.set_task_state(doc, task_id="B", state="working")        # B regresses
    return doc


def test_aggregate_surfaces_working_regressed_blockers():
    doc = _working_with_regressed_blocker()
    agg = scheduler.build_task_state_aggregate(doc)
    wr = agg["working_regressed_blockers"]
    assert len(wr) == 1
    assert wr[0]["task_id"] == "A"
    assert wr[0]["unsatisfied"] == ["B"]


def test_digest_surfaces_working_regressed_count_and_body():
    doc = _working_with_regressed_blocker()
    dig = scheduler.build_leader_digest_payload(doc, now=_NOW)
    assert dig["summary"]["working_regressed_blocker_count"] == 1
    assert "依存退行中に作業続行" in dig["body"]


def test_digest_cycle_body_names_unblock_command():
    doc = _blocked_trek()
    doc[trek.TARGET_BLOCKERS_KEY]["C"] = ["A"]
    dig = scheduler.build_leader_digest_payload(doc, now=_NOW)
    assert "beacon trek unblock" in dig["body"]
