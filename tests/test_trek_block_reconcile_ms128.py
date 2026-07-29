"""ms-128 方針4 (e-4365) chunk 3: AND auto-unblock, rollback cascade, cycle re-scan.

Edges persist after unblock (the ledger is the durable dependency record), so a
single idempotent ``reconcile_blocks`` keeps the ``block`` *state* consistent
with the *edges* every server tick:

  * AND auto-unblock: a blocked target whose blockers are all satisfied
    (leader_review+) returns to ``todo``.
  * Rollback: if a blocker regresses below satisfied, a dependent still in
    ``todo`` is re-blocked; a dependent already ``working`` is warned, not yanked.
  * ``detect_blocker_cycles`` is the defensive whole-graph re-scan (add_blocker
    already rejects cycles at write time, so a healthy graph returns []).

Scheduler/digest wiring + leader-first escalation land in chunk 4.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402


def _satisfy(doc, tid):
    """Drive a target to leader_review (= satisfied blocker state)."""
    if trek.get_task_state(doc, tid) == "todo":
        trek.set_task_state(doc, task_id=tid, state="working")
    trek.set_task_state(doc, task_id=tid, state="leader_review")


# --- AND auto-unblock -------------------------------------------------------

def test_blockers_all_satisfied_predicate():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    assert trek.blockers_all_satisfied(doc, "A") is False
    _satisfy(doc, "B")
    assert trek.blockers_all_satisfied(doc, "A") is False  # C still open
    _satisfy(doc, "C")
    assert trek.blockers_all_satisfied(doc, "A") is True


def test_no_blockers_is_vacuously_satisfied():
    assert trek.blockers_all_satisfied({}, "A") is True


def test_partial_satisfy_keeps_block():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    _satisfy(doc, "B")
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "block"
    assert result["unblocked"] == []


def test_all_satisfied_unblocks_to_todo_and_keeps_edges():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    _satisfy(doc, "B")
    _satisfy(doc, "C")
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "todo"
    assert result["unblocked"] == ["A"]
    # Edges are the durable dependency record — kept after unblock.
    assert trek.get_blockers(doc, "A") == ["B", "C"]


def test_reconcile_is_idempotent():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    _satisfy(doc, "B")
    r1 = trek.reconcile_blocks(doc)
    r2 = trek.reconcile_blocks(doc)
    assert r1["unblocked"] == ["A"]
    assert r2["unblocked"] == []  # nothing left to do
    assert trek.get_task_state(doc, "A") == "todo"


def test_chained_dependency_unblocks_in_order():
    # A→B→C: C satisfied unblocks B; then B satisfied unblocks A.
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="B", blocker_target_id="C")
    _satisfy(doc, "C")
    trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "B") == "todo"
    assert trek.get_task_state(doc, "A") == "block"  # B not satisfied yet
    _satisfy(doc, "B")
    trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "todo"


# --- rollback cascade -------------------------------------------------------

def test_rollback_reblocks_todo_dependent():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    _satisfy(doc, "B")
    trek.reconcile_blocks(doc)              # A → todo
    assert trek.get_task_state(doc, "A") == "todo"
    trek.set_task_state(doc, task_id="B", state="working")  # B regresses
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "block"
    assert result["reblocked"] == ["A"]


def test_rollback_warns_but_does_not_yank_working_dependent():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    _satisfy(doc, "B")
    trek.reconcile_blocks(doc)                               # A → todo
    trek.set_task_state(doc, task_id="A", state="working")   # A claimed & in flight
    trek.set_task_state(doc, task_id="B", state="working")   # B regresses
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "working"        # NOT yanked
    # warnings key is "task_id" to match the digest blocked_queue rows.
    assert result["warnings"] == [{"task_id": "A", "unsatisfied": ["B"]}]


def test_handed_off_dependent_is_left_alone():
    # A depends on B; A itself reaches leader_review; B regressing must not
    # re-block A (its own work is already merged).
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    _satisfy(doc, "B")
    trek.reconcile_blocks(doc)                                # A → todo
    trek.set_task_state(doc, task_id="A", state="working")
    trek.set_task_state(doc, task_id="A", state="leader_review")
    trek.set_task_state(doc, task_id="B", state="working")    # B regresses
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "leader_review"
    assert result == {"unblocked": [], "reblocked": [], "warnings": []}


# --- dynamic cycle re-scan --------------------------------------------------

def test_cycle_scan_clean_on_dag():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="B", blocker_target_id="C")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")  # diamond, no cycle
    assert trek.detect_blocker_cycles(doc) == []


def test_cycle_scan_finds_injected_cycle():
    # Bypass add_blocker's write-time guard to simulate a dynamic cycle.
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="B", blocker_target_id="C")
    doc[trek.TARGET_BLOCKERS_KEY]["C"] = ["A"]
    cycles = trek.detect_blocker_cycles(doc)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"A", "B", "C"}
    # canonicalised to start at the smallest id
    assert cycles[0][0] == "A"


def test_cycle_scan_finds_two_node_cycle():
    doc: dict = {}
    doc[trek.TARGET_BLOCKERS_KEY] = {"A": ["B"], "B": ["A"]}
    cycles = trek.detect_blocker_cycles(doc)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"A", "B"}


def test_reconcile_empty_graph_is_noop():
    result = trek.reconcile_blocks({})
    assert result == {"unblocked": [], "reblocked": [], "warnings": []}


# --- edge-less block recovery (AX review 2026-07-29) ------------------------

def test_edgeless_block_auto_recovers():
    # A block with no blocker edges (vacuously satisfied) must not stall forever.
    doc: dict = {"task_states": {
        "A": {"state": "block", "updated_at": "2026-07-29T00:00:00Z"},
    }}
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "todo"
    assert result["unblocked"] == ["A"]


def test_block_with_edges_not_treated_as_edgeless():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")  # B unsatisfied
    result = trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "block"  # still waiting
    assert result["unblocked"] == []
