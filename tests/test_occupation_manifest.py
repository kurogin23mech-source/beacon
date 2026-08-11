"""Unit tests for the instantiation manifest (ms-142 e-5008 / SPEC
BcQ0OUTjOrwTnUltRqmb 設計方針 1).

``occupation.profession_manifest`` is the SINGLE read-path for "what slots a
profession fills": each Target collection's id/arms plus the one thing that was
nowhere declared before — WHICH arm holds work items vs evidence, and HOW a work
item is identified inside a shared arm. These tests pin:

  * dev and sales resolve to the SAME manifest shape (identically-keyed dicts),
    so arm-walking L2 capabilities consume one contract regardless of occupation.
  * work_item_arm / evidence_arms are classified correctly per occupation
    (dev entries: task vs commit by ``type``; sales activities vs communications).
  * the classification is DECLARATIVE — a descriptor-defined occupation lights up
    its work-item / evidence arms with NO edit to occupation.py (ms-142 の芯).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation as occ  # noqa: E402
import target_engine as te  # noqa: E402


# Every target-class entry in the manifest carries exactly these keys, whatever
# the occupation — this identity is the occupation-agnostic contract. ms-142 T2
# (e-5157) adds ``state_model`` (from which ``phase_ball`` is now derived).
CLASS_KEYS = {"kind", "collection", "id_field", "id_prefix", "narrowing",
              "arms", "work_item_arm", "evidence_arms", "phase_ball",
              "state_model"}


def _dev():
    return {"name": "d", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                            "entries": []}]}


def _sales():
    return {"name": "s", "profession": "sales", "milestones": [],
            "opportunities": [{"id": "opp-1", "label": "O", "status": "open",
                               "phase": "lead", "who_has_the_ball": "self",
                               "activities": [], "communications": []}]}


def _class(manifest, collection):
    for tc in manifest["target_classes"]:
        if tc["collection"] == collection:
            return tc
    raise AssertionError(f"{collection} not in manifest")


# ---------------------------------------------------------------------------
# Shape identity across occupations.
# ---------------------------------------------------------------------------

def test_manifest_top_level_shape():
    m = occ.profession_manifest(_dev())
    assert m["profession"] == "dev"
    assert isinstance(m["target_classes"], list) and m["target_classes"]
    for tc in m["target_classes"]:
        assert set(tc) == CLASS_KEYS


def test_dev_and_sales_same_shape():
    dev = occ.profession_manifest(_dev())
    sales = occ.profession_manifest(_sales())
    assert sales["profession"] == "sales"
    # Same per-class key set, class-for-class, regardless of occupation.
    dev_keysets = [set(tc) for tc in dev["target_classes"]]
    sales_keysets = [set(tc) for tc in sales["target_classes"]]
    assert dev_keysets and sales_keysets
    assert all(ks == CLASS_KEYS for ks in dev_keysets + sales_keysets)


# ---------------------------------------------------------------------------
# Development arm classification: entries mixes tasks (work) + commits
# (evidence), discriminated by ``type``.
# ---------------------------------------------------------------------------

def test_dev_milestone_arm_roles():
    ms = _class(occ.profession_manifest(_dev()), "milestones")
    assert ms["kind"] == "milestone"
    assert ms["id_prefix"] == "ms-"
    assert ms["narrowing"] is True
    assert ms["work_item_arm"] == {"arm": "entries", "item_type": "task",
                                   "kind": "task", "id_prefix": "e-"}
    assert ms["evidence_arms"] == [{"arm": "entries", "item_type": "commit"}]
    assert ms["phase_ball"] is None
    # ms-142 T2: a milestone's state model is a permissive status enum with no
    # ball; its terminal/gated states (the review-gate + delete targets) are
    # listed so phase_ball derives None from state_field == "status".
    assert ms["state_model"]["shape"] == "status_enum"
    assert ms["state_model"]["state_field"] == "status"
    assert ms["state_model"]["ball_field"] is None
    assert {"done", "observing"} <= set(ms["state_model"]["gated_states"])


# ---------------------------------------------------------------------------
# Sales arm classification: activities arm is ALL work items, communications is
# a separate evidence arm; the opportunity carries a phase + ball.
# ---------------------------------------------------------------------------

def test_sales_opportunity_arm_roles():
    opp = _class(occ.profession_manifest(_sales()), "opportunities")
    assert opp["kind"] == "opportunity"
    assert opp["id_prefix"] == "opp-"
    assert opp["work_item_arm"] == {"arm": "activities", "item_type": None,
                                    "kind": "activity", "id_prefix": "act-"}
    assert opp["evidence_arms"] == [{"arm": "communications", "item_type": None}]
    assert opp["phase_ball"] == {"phase_field": "phase",
                                 "ball_field": "who_has_the_ball"}
    # ms-142 T2: the opportunity state model is a config-derived funnel carrying a
    # ball — exactly the pair phase_ball is derived from (state_field != "status"
    # AND a ball_field).
    assert opp["state_model"]["shape"] == "funnel"
    assert opp["state_model"]["state_field"] == "phase"
    assert opp["state_model"]["ball_field"] == "who_has_the_ball"


def test_manifest_scoped_to_target_collections():
    # The manifest surfaces exactly the aggregatable Target collections
    # (``target_collections`` = milestones + opportunities + operations, ms-142
    # e-5156), matching ``iter_target_records`` and the deadline enumeration scope.
    # Sales accounts / acquisitions are Targets but ride a different persistence
    # path and are not walked here — so the manifest does NOT surface them
    # (behavior parity, not a gap). The armless work_item_arm=None case is pinned
    # by the descriptor test and by the operation test below.
    cols = {tc["collection"]
            for tc in occ.profession_manifest(_sales())["target_classes"]}
    assert cols == {"milestones", "opportunities", "operations"}


def test_operation_is_a_manifest_target_class_without_work_item_arm():
    # ms-142 e-5156 (T1): an Operation is a first-class manifest Target beside a
    # Milestone — so projection / claim / deadline enumeration reach it through the
    # same abstraction. Per the T1 裁定 it declares NO work_item_arm yet (its
    # OperationTasks keep their own ``operation task done`` L3 path), and it has no
    # funnel phase/ball (it moves through a status lifecycle). id_prefix is op-.
    op = _class(occ.profession_manifest(_dev()), "operations")
    assert op["kind"] == "operation"
    assert op["id_prefix"] == "op-"
    assert op["work_item_arm"] is None
    assert op["evidence_arms"] == []
    assert op["phase_ball"] is None
    # ms-142 T2: an operation's state model is a monotonic status transition
    # table whose only terminal is ``closed`` (routed through operation close);
    # no ball, so phase_ball derives None.
    assert op["state_model"]["shape"] == "transition_table"
    assert op["state_model"]["state_field"] == "status"
    assert op["state_model"]["ball_field"] is None
    assert op["state_model"]["gated_states"] == ["closed"]
    assert set(op) == CLASS_KEYS


# ---------------------------------------------------------------------------
# Declarative: a descriptor-defined occupation lights up its arms with NO code
# edit here (the "declare, don't wire" contract at the heart of ms-142).
# ---------------------------------------------------------------------------

_LEGAL = {
    "kind": "matter", "label": "案件", "profession": "legal",
    "type": "single-shot", "id_prefix": "mat-", "collection": "matters",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [{"key": "counterparty", "label": "相手方", "required": True}],
    "phases": [{"key": "open", "label": "受任"},
               {"key": "closed", "label": "完了", "terminal": True}],
}


def _legal():
    data = {"name": "L", "profession": "legal", "milestones": [],
            "target_classes": [_LEGAL]}
    te.create_target(data, _LEGAL, label="X社 訴訟",
                     fields={"counterparty": "X社"})
    return data


def test_descriptor_occupation_lights_up_arms():
    mat = _class(occ.profession_manifest(_legal()), "matters")
    assert mat["kind"] == "matter"
    assert mat["id_prefix"] == "mat-"
    # Thick-frame default arms (work_items / evidence) classify without any edit
    # to occupation.py — declare the manifest and the arms light up.
    assert mat["work_item_arm"] == {"arm": "work_items", "item_type": None, "kind": "work_item"}
    assert mat["evidence_arms"] == [{"arm": "evidence", "item_type": None}]
    assert set(mat) == CLASS_KEYS
    # ms-142 T2: a descriptor's state model is derived from its declared phases
    # (shape ``phases``); because target_engine seeds every descriptor target
    # with a ball, its phase_ball is now DERIVED as present — an intended
    # correction of the old hardcoded None (which never modelled descriptors).
    assert mat["state_model"]["shape"] == "phases"
    assert mat["state_model"]["state_field"] == "phase"
    assert mat["phase_ball"] == {"phase_field": "phase",
                                 "ball_field": "who_has_the_ball"}
    # the descriptor's terminal phase (``closed``) is the routed/close-via state.
    assert "closed" in mat["state_model"]["gated_states"]


def test_descriptor_custom_arms_have_no_workitem_arm():
    # A descriptor whose arms are neither work_items nor evidence declares no
    # work-item / evidence arm — honest, not an inferred guess.
    contract = {
        "kind": "contract", "label": "契約", "profession": "legal",
        "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
        "decomposition": {"id_field": "id", "arms": ["clauses"]},
        "fields": [], "phases": [{"key": "drafting", "label": "起草"}],
    }
    data = {"name": "L", "profession": "legal", "milestones": [],
            "target_classes": [contract]}
    te.create_target(data, contract, label="NDA")
    ctr = _class(occ.profession_manifest(data), "contracts")
    assert ctr["arms"] == ("clauses",)
    assert ctr["work_item_arm"] is None
    assert ctr["evidence_arms"] == []


# ---------------------------------------------------------------------------
# Non-breaking: the manifest is a VIEW; dev/sales projects with no descriptors
# always surface the two built-in Target collections.
# ---------------------------------------------------------------------------

def test_builtin_collections_always_present():
    cols = {tc["collection"] for tc in occ.profession_manifest(_dev())["target_classes"]}
    assert {"milestones", "opportunities"} <= cols
