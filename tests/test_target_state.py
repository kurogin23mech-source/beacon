"""Unit tests for the declarative target state model + ``set_target_state``
primitive (ms-142 e-5157 / T2, class-engine ideal §5 "フェイズ前進").

These pin the T2 contract and, above all, the leader's caution 1 — the
COMPLETION-GATE NON-BYPASS: ``set_target_state`` writes only NON-terminal
transitions, so it is structurally impossible to land a milestone on
done/observing behind the ms-119 目的達成 review gate through this path.

Covered:
  * all four built-in classes declare a uniform state model (+ descriptor derive);
  * phase_ball is DERIVED from the state model, value-invariant with the old
    hardcoded ``_ARM_PHASE_BALL`` for the built-ins;
  * set_target_state advances non-terminal states per shape (permissive enum,
    monotonic table, descriptor phases) and refuses every terminal / gated one
    with a pointer to the class verb;
  * the monotonic guard still rejects illegal jumps.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import target_state as ts  # noqa: E402
import target_engine as te  # noqa: E402


# ---------------------------------------------------------------------------
# Declarations: all four built-ins + descriptor derive a uniform model.
# ---------------------------------------------------------------------------

def test_all_four_builtin_classes_declare_a_state_model():
    for kind in ("milestone", "operation", "opportunity", "acquisition"):
        model = ts.state_model_for(None, kind)
        assert model is not None, kind
        # uniform declared keys, whatever the shape.
        assert {"kind", "shape", "state_field", "advanceable_states",
                "routed_states", "ball_field", "monotonic"} <= set(model)
        assert model["shape"] in (ts.SHAPE_STATUS_ENUM, ts.SHAPE_TRANSITION_TABLE,
                                  ts.SHAPE_FUNNEL, ts.SHAPE_PHASES)


def test_shapes_match_leader_option_a():
    assert ts.state_model_for(None, "milestone")["shape"] == ts.SHAPE_STATUS_ENUM
    assert ts.state_model_for(None, "operation")["shape"] == ts.SHAPE_TRANSITION_TABLE
    assert ts.state_model_for(None, "acquisition")["shape"] == ts.SHAPE_TRANSITION_TABLE
    assert ts.state_model_for(None, "opportunity")["shape"] == ts.SHAPE_FUNNEL


_MATTER = {
    "kind": "matter", "label": "案件", "profession": "legal",
    "type": "single-shot", "id_prefix": "mat-", "collection": "matters",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [],
    "phases": [{"key": "open", "label": "受任"},
               {"key": "review", "label": "レビュー"},
               {"key": "closed", "label": "完了", "terminal": True}],
}


def test_descriptor_state_model_is_derived_from_phases():
    data = {"name": "L", "profession": "legal", "milestones": [],
            "target_classes": [_MATTER]}
    model = ts.state_model_for(data, "matter")
    assert model["shape"] == ts.SHAPE_PHASES
    assert model["state_field"] == "phase"
    # non-terminal phases are advanceable; the terminal phase is routed (close-via).
    assert model["advanceable_states"] == ("open", "review")
    assert "closed" in model["routed_states"]
    assert model["ball_field"] == "who_has_the_ball"


# ---------------------------------------------------------------------------
# phase_ball derivation — value invariance for the built-ins.
# ---------------------------------------------------------------------------

def test_phase_ball_derivation_matches_old_hardcode():
    assert ts.derive_phase_ball(ts.state_model_for(None, "milestone")) is None
    assert ts.derive_phase_ball(ts.state_model_for(None, "operation")) is None
    assert ts.derive_phase_ball(ts.state_model_for(None, "opportunity")) == {
        "phase_field": "phase", "ball_field": "who_has_the_ball"}
    assert ts.derive_phase_ball(None) is None


# ---------------------------------------------------------------------------
# THE critical invariant: completion-gate non-bypass.
# ---------------------------------------------------------------------------

def _dev_with_ms(status="in_progress"):
    return {"name": "d", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "M", "status": status,
                            "entries": []}]}


@pytest.mark.parametrize("terminal", ["done", "observing", "cancelled",
                                      "in_review", "approved"])
def test_set_target_state_refuses_milestone_terminal_transitions(terminal):
    """set_target_state can NEVER write a milestone's terminal / gate-managed
    state — the structural completion-gate non-bypass (leader caution 1)."""
    data = _dev_with_ms()
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, "ms-1", terminal)
    # the record is untouched (no partial write) ...
    assert data["milestones"][0]["status"] == "in_progress"
    # ... and the error routes the caller to the class verb / review gate.
    msg = str(exc.value)
    assert "terminal" in msg or "gated" in msg
    if terminal in ("done", "observing"):
        assert "review" in msg  # the 目的達成 gate is named


def test_set_target_state_advances_milestone_non_terminal():
    data = _dev_with_ms(status="todo")
    rec, old, new = ts.set_target_state(data, "ms-1", "in_progress",
                                        actor="claude", reason="start")
    assert (old, new) == ("todo", "in_progress")
    assert data["milestones"][0]["status"] == "in_progress"
    # stamped in the class's own meta convention.
    assert data["milestones"][0]["meta"]["in_progress_at"]
    assert data["milestones"][0]["meta"]["in_progress_by"] == "claude"


# ---------------------------------------------------------------------------
# Operation: monotonic table — non-terminal advances, closed is refused, an
# illegal jump raises.
# ---------------------------------------------------------------------------

def _dev_with_op(status="todo"):
    return {"name": "d", "profession": "dev", "milestones": [],
            "operations": [{"id": "op-1", "label": "O", "status": status}]}


def test_set_target_state_advances_operation_monotonic():
    data = _dev_with_op("todo")
    _, old, new = ts.set_target_state(data, "op-1", "open")
    assert (old, new) == ("todo", "open")
    assert data["operations"][0]["status"] == "open"


def test_set_target_state_refuses_operation_close():
    data = _dev_with_op("open")
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, "op-1", "closed")
    assert "operation close" in str(exc.value)
    assert data["operations"][0]["status"] == "open"


def test_set_target_state_rejects_illegal_operation_jump():
    # open → todo is a backward jump the monotonic table forbids.
    data = _dev_with_op("open")
    with pytest.raises(ts.TargetStateError):
        ts.set_target_state(data, "op-1", "todo")
    assert data["operations"][0]["status"] == "open"


# ---------------------------------------------------------------------------
# Acquisition: also monotonic, but rides a SECONDARY (non-manifest) collection —
# so this pins that _resolve reaches it (the operation test alone would not).
# ---------------------------------------------------------------------------

def _sales_with_acq(status="todo"):
    return {"name": "s", "profession": "sales", "milestones": [],
            "acquisitions": [{"id": "acq-1", "label": "A", "status": status}]}


def test_set_target_state_advances_acquisition_monotonic():
    data = _sales_with_acq("todo")
    _, old, new = ts.set_target_state(data, "acq-1", "in_progress")
    assert (old, new) == ("todo", "in_progress")
    assert data["acquisitions"][0]["status"] == "in_progress"


def test_set_target_state_refuses_acquisition_terminal():
    data = _sales_with_acq("in_progress")
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, "acq-1", "done")
    assert "acquisition status" in str(exc.value)
    assert data["acquisitions"][0]["status"] == "in_progress"


def test_set_target_state_rejects_illegal_acquisition_jump():
    # in_progress → todo is a backward jump the monotonic table forbids.
    data = _sales_with_acq("in_progress")
    with pytest.raises(ts.TargetStateError):
        ts.set_target_state(data, "acq-1", "todo")
    assert data["acquisitions"][0]["status"] == "in_progress"


# ---------------------------------------------------------------------------
# Descriptor: delegates to target_engine for non-terminal phases; the terminal
# phase is refused (route through close).
# ---------------------------------------------------------------------------

def _legal_with_matter():
    data = {"name": "L", "profession": "legal", "milestones": [],
            "target_classes": [_MATTER]}
    te.create_target(data, _MATTER, label="X社 訴訟")  # starts at phase "open"
    return data


def test_set_target_state_advances_descriptor_phase():
    data = _legal_with_matter()
    mat_id = data["matters"][0]["id"]
    _, old, new = ts.set_target_state(data, mat_id, "review", actor="claude")
    assert (old, new) == ("open", "review")
    assert data["matters"][0]["phase"] == "review"


def test_set_target_state_refuses_descriptor_terminal_phase():
    data = _legal_with_matter()
    mat_id = data["matters"][0]["id"]
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, mat_id, "closed")
    assert "close" in str(exc.value)
    assert data["matters"][0]["phase"] == "open"


# ---------------------------------------------------------------------------
# Opportunity funnel: declared for phase_ball, transition deferred (follow-up).
# ---------------------------------------------------------------------------

def test_set_target_state_defers_opportunity_funnel():
    data = {"name": "s", "profession": "sales", "milestones": [],
            "opportunities": [{"id": "opp-1", "label": "O", "status": "open",
                               "phase": "lead", "who_has_the_ball": "self",
                               "activities": [], "communications": []}]}
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, "opp-1", "qualified")
    assert "not yet routed" in str(exc.value)
    # phase untouched — no desync of the mirrored status.
    assert data["opportunities"][0]["phase"] == "lead"


def test_set_target_state_unknown_target():
    with pytest.raises(ts.TargetStateError):
        ts.set_target_state(_dev_with_ms(), "ms-999", "in_progress")
