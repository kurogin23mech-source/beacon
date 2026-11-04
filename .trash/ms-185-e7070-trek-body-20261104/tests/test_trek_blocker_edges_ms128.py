"""ms-128 方針4 (e-4365) chunk 2: blocker edges + write-time cycle rejection.

The dependency graph is a first-class ledger ``trek_doc["target_blockers"]`` =
{target_id: [blocker_target_id, ...]}, kept separate from ``task_states`` so a
state write (which replaces the entry) can't wipe edges. Edge ``a → b`` means
"a depends on b" (a is blocked by b).

``add_blocker`` atomically records the edge and transitions the target to
``block``. It rejects self-blocks and cycles at write time (方針4 二段循環検知
第 (i) 段), and no-ops when the blocker is already satisfied. AND auto-unblock
and the dynamic (scheduler-time) cycle re-scan land in chunk 3.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402


# --- basic edge recording ---------------------------------------------------

def test_add_blocker_records_edge_and_blocks_target():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.get_blockers(doc, "A") == ["B"]
    assert trek.get_task_state(doc, "A") == "block"


def test_edges_live_outside_task_states():
    # The edge ledger is a distinct top-level key, not on the state entry.
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.TARGET_BLOCKERS_KEY in doc
    assert "A" in doc[trek.TARGET_BLOCKERS_KEY]
    # ...and the state entry does NOT carry the blocker list.
    assert "blocker_target_ids" not in doc["task_states"]["A"]


def test_get_blockers_returns_a_copy():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    got = trek.get_blockers(doc, "A")
    got.append("MUTANT")
    assert trek.get_blockers(doc, "A") == ["B"]  # ledger untouched


def test_multiple_blockers_preserved_in_order():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    assert trek.get_blockers(doc, "A") == ["B", "C"]


def test_add_blocker_is_idempotent():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.get_blockers(doc, "A") == ["B"]
    assert trek.get_task_state(doc, "A") == "block"


def test_add_blocker_from_working_target():
    doc: dict = {}
    trek.set_task_state(doc, task_id="A", state="working")
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.get_task_state(doc, "A") == "block"


# --- guards -----------------------------------------------------------------

@pytest.mark.parametrize("target,blocker", [("", "B"), ("A", "")])
def test_add_blocker_requires_both_ids(target, blocker):
    with pytest.raises(ValueError):
        trek.add_blocker({}, target_id=target, blocker_target_id=blocker)


def test_self_block_rejected():
    with pytest.raises(ValueError):
        trek.add_blocker({}, target_id="A", blocker_target_id="A")


def test_direct_cycle_rejected():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    with pytest.raises(ValueError):
        trek.add_blocker(doc, target_id="B", blocker_target_id="A")
    # The rejected edge left no trace.
    assert trek.get_blockers(doc, "B") == []


def test_transitive_cycle_rejected():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="B", blocker_target_id="C")
    with pytest.raises(ValueError):
        trek.add_blocker(doc, target_id="C", blocker_target_id="A")
    assert trek.get_blockers(doc, "C") == []


def test_non_cycle_dag_edge_allowed():
    # Diamond: A→B, A→C, B→D, C→D — no cycle, all edges accepted.
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    trek.add_blocker(doc, target_id="B", blocker_target_id="D")
    trek.add_blocker(doc, target_id="C", blocker_target_id="D")
    assert trek.get_blockers(doc, "A") == ["B", "C"]
    assert trek.get_blockers(doc, "B") == ["D"]


def test_target_in_leader_review_is_not_blockable():
    doc: dict = {}
    trek.set_task_state(doc, task_id="A", state="working")
    trek.set_task_state(doc, task_id="A", state="leader_review")
    with pytest.raises(ValueError):
        trek.add_blocker(doc, target_id="A", blocker_target_id="Z")


# --- satisfied-blocker no-op ------------------------------------------------

@pytest.mark.parametrize("sat_state", ["leader_review", "user_review"])
def test_satisfied_blocker_is_noop(sat_state):
    doc: dict = {}
    # Drive B to a satisfied state.
    trek.set_task_state(doc, task_id="B", state="working")
    trek.set_task_state(doc, task_id="B", state="leader_review")
    if sat_state == "user_review":
        # e-4373: user_review は leader_review 経由が唯一の入口。
        trek.set_task_state(doc, task_id="B", state="user_review")
    # Blocking A on the already-satisfied B is a no-op: no edge, A stays todo.
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.get_blockers(doc, "A") == []
    assert trek.get_task_state(doc, "A") == "todo"


def test_blocker_is_satisfied_predicate():
    doc: dict = {}
    assert trek.blocker_is_satisfied(doc, "B") is False  # untracked = todo
    trek.set_task_state(doc, task_id="B", state="working")
    assert trek.blocker_is_satisfied(doc, "B") is False
    trek.set_task_state(doc, task_id="B", state="leader_review")
    assert trek.blocker_is_satisfied(doc, "B") is True


def test_one_satisfied_one_unsatisfied_blocker_still_blocks():
    doc: dict = {}
    # C is satisfied, B is not. Blocking A on both → only B is recorded, A blocks.
    trek.set_task_state(doc, task_id="C", state="working")
    trek.set_task_state(doc, task_id="C", state="leader_review")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")  # no-op (satisfied)
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")  # real
    assert trek.get_blockers(doc, "A") == ["B"]
    assert trek.get_task_state(doc, "A") == "block"


# --- review hardening (2026-07-29) -----------------------------------------

def test_add_blocker_returns_status_applied():
    doc: dict = {}
    r = trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert r["applied"] is True
    assert r["reason"] == "applied"
    assert r["state"] == "block"


def test_add_blocker_returns_noop_satisfied():
    doc: dict = {}
    trek.set_task_state(doc, task_id="B", state="working")
    trek.set_task_state(doc, task_id="B", state="leader_review")
    r = trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert r["applied"] is False
    assert r["reason"] == "noop-satisfied"


def test_add_blocker_returns_noop_existing():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    r = trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert r["applied"] is False
    assert r["reason"] == "noop-existing"


@pytest.mark.parametrize("setup,kind", [
    (("A", "A"), "self_block"),
    (("", "B"), "missing_id"),
])
def test_blocker_error_kinds(setup, kind):
    with pytest.raises(trek.TrekBlockerError) as ei:
        trek.add_blocker({}, target_id=setup[0], blocker_target_id=setup[1])
    assert ei.value.kind == kind


def test_cycle_error_kind():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    with pytest.raises(trek.TrekBlockerError) as ei:
        trek.add_blocker(doc, target_id="B", blocker_target_id="A")
    assert ei.value.kind == "cycle"


def test_not_blockable_error_kind():
    doc: dict = {}
    trek.set_task_state(doc, task_id="A", state="working")
    trek.set_task_state(doc, task_id="A", state="leader_review")
    with pytest.raises(trek.TrekBlockerError) as ei:
        trek.add_blocker(doc, target_id="A", blocker_target_id="Z")
    assert ei.value.kind == "not_blockable"


def test_not_blockable_leaves_no_phantom_edge():
    # Atomicity: a rejected block on a handed-off target records no edge.
    doc: dict = {}
    trek.set_task_state(doc, task_id="A", state="working")
    trek.set_task_state(doc, task_id="A", state="leader_review")
    with pytest.raises(trek.TrekBlockerError):
        trek.add_blocker(doc, target_id="A", blocker_target_id="Z")
    assert trek.get_blockers(doc, "A") == []


def test_remove_blocker_drops_edge():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.add_blocker(doc, target_id="A", blocker_target_id="C")
    assert trek.remove_blocker(doc, target_id="A", blocker_target_id="B") is True
    assert trek.get_blockers(doc, "A") == ["C"]


def test_remove_blocker_idempotent():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.remove_blocker(doc, target_id="A", blocker_target_id="Z") is False


def test_remove_last_blocker_clears_ledger_entry():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    trek.remove_blocker(doc, target_id="A", blocker_target_id="B")
    assert "A" not in (doc.get(trek.TARGET_BLOCKERS_KEY) or {})


def test_remove_blocker_then_reconcile_unblocks():
    doc: dict = {}
    trek.add_blocker(doc, target_id="A", blocker_target_id="B")
    assert trek.get_task_state(doc, "A") == "block"
    trek.remove_blocker(doc, target_id="A", blocker_target_id="B")
    trek.reconcile_blocks(doc)
    assert trek.get_task_state(doc, "A") == "todo"  # edge-less block recovers
