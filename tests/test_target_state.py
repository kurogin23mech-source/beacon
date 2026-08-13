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

def test_all_builtin_classes_declare_a_state_model():
    # ms-142 e-5256: iterate BUILTIN_STATE_MODELS.keys() (not a hand-listed tuple) so
    # a NEW built-in class (account was the 4th → now 5 builtins) cannot slip past
    # this uniformity check by being forgotten in the loop (maint review 縮退 fix).
    for kind in ts.BUILTIN_STATE_MODELS:
        model = ts.state_model_for(None, kind)
        assert model is not None, kind
        # uniform declared keys, whatever the shape. never_terminal is UNIVERSAL
        # (every model declares it, ms-142 e-5256).
        assert {"kind", "shape", "state_field", "advanceable_states",
                "routed_states", "ball_field", "monotonic",
                "never_terminal"} <= set(model), kind
        assert model["shape"] in (ts.SHAPE_STATUS_ENUM, ts.SHAPE_TRANSITION_TABLE,
                                  ts.SHAPE_FUNNEL, ts.SHAPE_PHASES)


def test_every_funnel_declares_funnel_seam():
    # ms-142 e-5256 anti-blind-spot (maint/ax review): funnel_seam is read with
    # ``model.get("funnel_seam") is None``, which would conflate an OMITTED key
    # (a forgotten declaration) with a None-DECLARATION (account's deliberate "no
    # generic seam"). Enforce that EVERY SHAPE_FUNNEL builtin declares the key, so an
    # omit is fail-visible here (explicit None stays legal). A seam-less funnel
    # (funnel_seam=None) MUST also declare ``phase_verb`` for the generic error.
    for kind, model in ts.BUILTIN_STATE_MODELS.items():
        if model["shape"] != ts.SHAPE_FUNNEL:
            continue
        assert "funnel_seam" in model, (
            f"{kind}: SHAPE_FUNNEL model must DECLARE funnel_seam (an omit is "
            f"indistinguishable from a None-declaration).")
        if model["funnel_seam"] is None:
            assert model.get("phase_verb"), (
                f"{kind}: a seam-less funnel (funnel_seam=None) must declare "
                f"phase_verb so the generic set_target_state error names its verb.")


def test_shapes_match_leader_option_a():
    assert ts.state_model_for(None, "milestone")["shape"] == ts.SHAPE_STATUS_ENUM
    assert ts.state_model_for(None, "operation")["shape"] == ts.SHAPE_TRANSITION_TABLE
    assert ts.state_model_for(None, "acquisition")["shape"] == ts.SHAPE_TRANSITION_TABLE
    assert ts.state_model_for(None, "opportunity")["shape"] == ts.SHAPE_FUNNEL


# ---------------------------------------------------------------------------
# Completion gate (ms-142 T3 / e-5158): EVERY target-class declares which gate
# guards its terminal — the anti-self-close capability's existence, checkable
# from one field (what the T5 coverage matrix will enforce). Scope B: the two
# previously-ungated classes (acquisition / descriptor) get the lightweight
# structural ban, not the full spine.
# ---------------------------------------------------------------------------

def test_every_class_declares_a_completion_gate():
    # ms-142 e-5256: the invariant is now "completion_gate non-null XOR never_terminal"
    # — a class EITHER settles behind a gate OR is never-terminal (account: no 決着
    # grain → gate=None). account is included so a regression that drops its
    # never_terminal (leaving gate=None with never_terminal=False) fails here.
    expected = {
        "milestone": ts.GATE_SPINE,
        "operation": ts.GATE_SPINE,
        "opportunity": ts.GATE_SALES_JUDGE,
        "acquisition": ts.GATE_SELF_CLOSE_BAN,
        "account": None,   # never-terminal → declared gate absence
    }
    # every built-in must be covered (derive-checked so a new class is not forgotten).
    assert set(expected) == set(ts.BUILTIN_STATE_MODELS)
    for kind, gate in expected.items():
        model = ts.state_model_for(None, kind)
        assert ts.completion_gate_for(model) == gate, kind
        # non-null XOR never_terminal (the coverage matrix depends on this).
        has_gate = ts.completion_gate_for(model) is not None
        assert has_gate != bool(model.get("never_terminal")), kind


def test_descriptor_class_declares_self_close_ban():
    data = {"name": "L", "profession": "legal", "milestones": [],
            "target_classes": [_MATTER]}
    model = ts.state_model_for(data, "matter")
    assert ts.completion_gate_for(model) == ts.GATE_SELF_CLOSE_BAN


def test_completion_gate_for_none_model():
    assert ts.completion_gate_for(None) is None


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
# Opportunity funnel: non-terminal advance routes through the sales seam;
# terminal (決着) is refused as the sales judge gate's completion claim (e-5169).
# ---------------------------------------------------------------------------

def _sales_with_opp(phase="商談準備"):
    # Use the shipped funnel vocabulary so opportunity_phase_is_terminal /
    # _opportunity_status_for_phase resolve against real phase defs.
    import sales_entities as se
    return {"name": "s", "profession": "sales", "milestones": [],
            "opportunity_phases": [dict(p) for p in se.DEFAULT_OPPORTUNITY_PHASES],
            "opportunities": [{"id": "opp-1", "label": "O", "status": "open",
                               "phase": phase, "who_has_the_ball": "self",
                               "activities": [], "communications": []}]}


def test_set_target_state_advances_opportunity_funnel_non_terminal():
    data = _sales_with_opp("商談準備")
    import sales_entities as se
    nxt = se.next_opportunity_phase(data, "商談準備")
    rec, old, new = ts.set_target_state(data, "opp-1", nxt)
    assert (old, new) == ("商談準備", nxt)
    assert data["opportunities"][0]["phase"] == nxt
    # status mirror stays "open" for a non-terminal phase (invariant with phase_set).
    assert data["opportunities"][0]["status"] == "open"


def test_set_target_state_refuses_opportunity_terminal_phase():
    data = _sales_with_opp("提案")
    import sales_entities as se
    terminal = next(p["name"] for p in se.opportunity_phases(data)
                    if p.get("terminal"))
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, "opp-1", terminal)
    assert "terminal" in str(exc.value)
    # phase untouched — the settlement must go through the sales judge gate.
    assert data["opportunities"][0]["phase"] == "提案"


def test_set_target_state_funnel_matches_phase_set_status_mirror():
    """The status projection is byte-identical whether a non-terminal phase is
    reached via set_target_state or the legacy phase_set write (status 同期不変)."""
    import sales_entities as se
    a = _sales_with_opp("商談準備")
    b = _sales_with_opp("商談準備")
    nxt = se.next_opportunity_phase(a, "商談準備")
    ts.set_target_state(a, "opp-1", nxt)
    se.advance_funnel_phase(b, b["opportunities"][0], nxt)
    assert a["opportunities"][0]["status"] == b["opportunities"][0]["status"]
    assert a["opportunities"][0]["phase"] == b["opportunities"][0]["phase"]


# ---------------------------------------------------------------------------
# Account funnel: a SEAM-LESS funnel (funnel_seam=None). set_target_state must NOT
# run the opportunity status-mirror writer on it (that would silently write a bogus
# ``status`` field onto a record that has none) — it routes to the class verb via the
# model-declared phase_verb (ms-142 e-5256, leader request-changes fix 2).
# ---------------------------------------------------------------------------

def _sales_with_account(phase="リード"):
    return {"name": "s", "profession": "sales", "milestones": [],
            "accounts": [{"id": "acc-1", "name": "A", "label": "A", "phase": phase,
                          "phase_history": [], "nurturings": [], "communications": []}]}


def test_set_target_state_on_seamless_funnel_routes_to_class_verb():
    data = _sales_with_account("リード")
    with pytest.raises(ts.TargetStateError) as exc:
        ts.set_target_state(data, "acc-1", "未成約顧客")
    # the error names the class's own phase verb (from the model's phase_verb hint,
    # with the real target id interpolated) — no opportunity/account text hardcoded
    # in set_target_state.
    msg = str(exc.value)
    assert "acc-1" in msg and "account phase" in msg
    # crucially: the wrong (opportunity) seam did NOT run — phase unchanged and NO
    # bogus status field was written onto the account.
    assert data["accounts"][0]["phase"] == "リード"
    assert "status" not in data["accounts"][0]


def test_seamless_funnel_phase_still_advances_via_class_verb():
    # The real path (acc- phase_set) still works and writes no status mirror — proves
    # routing away from set_target_state did not break account phase advancement.
    import sales_entities as se
    data = _sales_with_account("リード")
    se.phase_set(data, "acc-1", "未成約顧客", at="2026-08-13")
    assert data["accounts"][0]["phase"] == "未成約顧客"
    assert "status" not in data["accounts"][0]


def test_set_target_state_unknown_target():
    with pytest.raises(ts.TargetStateError):
        ts.set_target_state(_dev_with_ms(), "ms-999", "in_progress")
