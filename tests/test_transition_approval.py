"""Unit tests for the profession-neutral target-transition approval primitive.

Covers the two gating razors:
  - over-gating: routine transitions (todo->active, ->waiting, ->observing) are
    NOT gated.
  - double-gating: sales opportunity advances ARE attainment transitions, but
    the NEW spine primitive does NOT enforce them (sales judge already does).
Plus the pure entry build + verdict (approve / reject) machinery.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import transition_approval as ta


# --- is_attainment_transition: the profession-neutral truth ---

def test_milestone_completion_is_attainment():
    assert ta.is_attainment_transition("milestone", "in_progress", "done")
    assert ta.is_attainment_transition("milestone", "in_progress", "closed")
    # ms-119: observing is a completion claim in this codebase (運用改善フェーズ =
    # 基本目的達成が前提), so it is an attainment transition like done.
    assert ta.is_attainment_transition("milestone", "in_progress", "observing")


def test_milestone_routine_is_not_attainment():
    # the user's example: todo -> active needs no gate
    assert not ta.is_attainment_transition("milestone", "todo", "in_progress")
    assert not ta.is_attainment_transition("milestone", "in_progress", "waiting")


def test_operation_close_is_attainment():
    assert ta.is_attainment_transition("operation", "open", "closed")
    assert not ta.is_attainment_transition("operation", "todo", "open")


def test_opportunity_forward_and_terminal_are_attainment():
    funnel = ["lead", "qualified", "proposal", "won"]
    assert ta.is_attainment_transition("opportunity", "lead", "qualified", funnel=funnel)
    assert ta.is_attainment_transition("opportunity", "qualified", "proposal", funnel=funnel)
    assert ta.is_attainment_transition("opportunity", "proposal", "terminal")
    # a corrective backward jump is not an attainment claim
    assert not ta.is_attainment_transition("opportunity", "proposal", "qualified", funnel=funnel)


# --- requires_spine_approval: what the NEW primitive enforces today ---

def test_spine_enforces_dev_and_ops_completion():
    assert ta.requires_spine_approval("milestone", "in_progress", "done")
    assert ta.requires_spine_approval("operation", "open", "closed")
    # ms-119: observing is a completion claim → gated like done (抜け穴を塞ぐ)
    assert ta.requires_spine_approval("milestone", "in_progress", "observing")


def test_spine_does_not_overgate_routine():
    assert not ta.requires_spine_approval("milestone", "todo", "in_progress")
    assert not ta.requires_spine_approval("milestone", "in_progress", "waiting")


def test_spine_does_not_double_gate_sales():
    # opportunity advance IS an attainment transition ...
    funnel = ["lead", "qualified", "proposal", "won"]
    assert ta.is_attainment_transition("opportunity", "lead", "qualified", funnel=funnel)
    # ... but the spine defers to the existing sales judge projection (no 2nd gate)
    assert not ta.requires_spine_approval("opportunity", "lead", "qualified", funnel=funnel)
    assert not ta.requires_spine_approval("opportunity", "proposal", "terminal")


# --- build + verdict: pure entry machinery ---

def test_build_transition_approval_shape():
    e = ta.build_transition_approval(
        entry_id="e-999", target_id="ms-5", target_kind="milestone",
        old_state="in_progress", new_state="done",
        intent="AC 3/3 満たし本番稼働2週間 incident ゼロ", evidence=["e-100", "e-101"],
        actor="kida", created_at="2026-07-22T00:00:00Z",
    )
    assert e["type"] == "target-transition-approval"
    assert e["status"] == "pending"
    assert e["meta"]["approval_status"] == "pending"
    assert e["meta"]["new_state"] == "done"
    assert e["meta"]["evidence"] == ["e-100", "e-101"]


def test_approve_returns_new_state_and_flips_status():
    e = ta.build_transition_approval(
        entry_id="e-999", target_id="ms-5", target_kind="milestone",
        old_state="in_progress", new_state="done", intent="…",
        created_at="2026-07-22T00:00:00Z",
    )
    applied = ta.append_verdict(e, status="approved", rationale="OK", actor="kida",
                                at="2026-07-22T01:00:00Z")
    assert applied == "done"                      # caller executes this transition
    assert e["status"] == "approved"
    assert e["meta"]["approval_status"] == "approved"
    assert len(e["meta"]["approval_history"]) == 1


def test_reject_returns_none_and_cancels():
    e = ta.build_transition_approval(
        entry_id="e-999", target_id="ms-5", target_kind="milestone",
        old_state="in_progress", new_state="done", intent="…",
        created_at="2026-07-22T00:00:00Z",
    )
    applied = ta.append_verdict(e, status="rejected", rationale="AC5 未達", actor="kida",
                                at="2026-07-22T01:00:00Z")
    assert applied is None                         # transition does NOT execute
    assert e["status"] == "cancelled"
    assert e["meta"]["approval_status"] == "rejected"


def test_invalid_verdict_raises():
    e = ta.build_transition_approval(
        entry_id="e-999", target_id="ms-5", target_kind="milestone",
        old_state="in_progress", new_state="done", intent="…",
        created_at="2026-07-22T00:00:00Z",
    )
    try:
        ta.append_verdict(e, status="maybe", at="2026-07-22T01:00:00Z")
        assert False, "expected ValueError"
    except ValueError:
        pass
