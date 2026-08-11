"""Capability × profession coverage matrix — the ms-142 completeness harness
(e-5012, rebuilt on the ms-134 reclassification). Importable definitions; the
assertions live in ``test_capability_profession_matrix.py``.

The MS thesis: "declare a profession's manifest and every SHARED capability that
walks the Target/WorkItem abstraction lights up — with zero wiring." ms-134's
``capability_ledger`` is the NEGATIVE, STATIC guard (a profession-shared verb may
not reach a profession concrete — collection direct-read, symbol reach, or arm-name
direct-read; all tracked as one-way ratchet debt). This matrix is its POSITIVE,
BEHAVIORAL twin: actually RUN each abstraction-consuming capability against three
professions — dev, sales, and a SYNTHETIC profession built purely from a manifest
(``synth_profession``) whose shape is deliberately unlike dev/sales — and assert it
surfaces that profession's Target and work item.

Single source of truth for RED (leader 裁定 (A), ms-142 e-5012): this module holds
NO red registry of its own. Every "this capability misses a new profession" debt
lives in the ledger ratchets (``KNOWN_COLLECTION_COUPLING`` / ``KNOWN_SYMBOL_REACH``
/ ``KNOWN_ARM_REACH`` — the arm-name coupling class e-5012 adds). e-5013 is the
RECONCILE test that pins matrix ↔ ratchet consistency; this module is GREEN-only
(behavioral positive proof).

If the synthetic profession flows GREEN through every abstraction consumer, the
abstraction is genuinely declaration-driven; a capability that does NOT is either
fixed to consume the abstraction or listed in a ratchet with an owning MS.
"""
from __future__ import annotations

import occupation


# --- The three professions under test, each with one Target + one work item ---

def _dev_project():
    return {
        "name": "dev", "profession": "dev",
        "milestones": [
            {"id": "ms-1", "title": "M", "status": "in_progress",
             "occupation": {"session_id": "sv-dev"},
             "entries": [
                 {"id": "e-1", "type": "task", "description": "T",
                  "deadline": "2026-08-06", "status": "todo"},
                 {"id": "e-9", "type": "commit", "description": "did T",
                  "meta": {"session_id": "sess-1"}},
             ]},
        ],
    }


def _sales_project():
    return {
        "name": "sales", "profession": "sales", "milestones": [],
        "opportunities": [
            {"id": "opp-1", "label": "O", "status": "open", "phase": "lead",
             "occupation": {"session_id": "sv-sales"},
             "activities": [
                 {"id": "act-1", "description": "call", "deadline": "2026-08-06",
                  "status": "todo"},
             ],
             "communications": [{"id": "comm-1", "description": "deck"}]},
        ],
    }


def _synthetic_project():
    # imported lazily so this module has no hard dependency ordering with the
    # fixture module (both live under tests/).
    from synth_profession import build_synthetic_project
    return build_synthetic_project()


# name -> {project, target_id, work_item_id}. The synthetic profession is the one
# that stresses the abstraction (arms named nothing like dev/sales).
PROFESSIONS = {
    "dev": {"project": _dev_project, "target_id": "ms-1", "work_item_id": "e-1"},
    "sales": {"project": _sales_project, "target_id": "opp-1",
              "work_item_id": "act-1"},
    "compliance": {"project": _synthetic_project, "target_id": "obl-1",
                   "work_item_id": "duty-1"},
}


# --- GREEN probes: each RUNS a capability and returns whether it surfaced the
# profession's Target / work item. Signature: (project, target_id, work_item_id).

def _probe_project_targets(project, target_id, work_item_id):
    return any(r.get("id") == target_id
               for r in occupation.project_targets(project))


def _probe_iter_target_records(project, target_id, work_item_id):
    return any(r.get("id") == target_id
               for r in occupation.iter_target_records(project))


def _probe_iter_work_items(project, target_id, work_item_id):
    return any(wi.get("id") == work_item_id
               for wi, _t, _a in occupation.iter_work_items(project))


def _probe_iter_deadline_candidates(project, target_id, work_item_id):
    return any(c["item"].get("id") == work_item_id
               for c in occupation.iter_deadline_candidates(project))


GREEN_PROBES = {
    "project_targets": _probe_project_targets,
    "iter_target_records": _probe_iter_target_records,
    "iter_work_items": _probe_iter_work_items,
    "iter_deadline_candidates": _probe_iter_deadline_candidates,
}


# ===========================================================================
# ms-142 T5 (e-5160): the "絶対漏らすな capability × 全ターゲットクラス" matrix.
#
# The block above (e-5012) proves the ABSTRACTION ITERATORS are declaration-
# driven (profession rows × iterator columns). This block is the §5/§10 guard the
# ideal-image doc asks for: rows = every profession_manifest TARGET-CLASS (incl.
# operation, the T1 addition), columns = the five capabilities a new class must
# not silently lack — phase advancement / deadline / 業務→証跡 / completion-gate
# existence / claim. Each cell is 3-valued:
#
#   GREEN         — the capability behaviourally lights up for the class.
#   DECLARED N/A  — the manifest itself says the class lacks the arm this
#                   capability rides (operation declares work_item_arm=None →
#                   no deadline work; evidence_arms=[] → no 証跡). A declared
#                   absence is NOT a gap (the T4 principle), so the test asserts
#                   the behaviour AGREES (nothing lights up) rather than failing.
#   empty (fail)  — neither green nor a declared absence = a silent gap. CI fails.
#
# The N/A predicate reads the MANIFEST (not a hand-keyed table) so it stays
# declaration-driven: add a class that declares an evidence arm and its 証跡 cell
# flips from N/A to must-be-green automatically. RED debt stays in the ledger
# ratchets (leader 裁定 (A)); this matrix is green/na-only, same as the e-5012 half.
# ===========================================================================

import target_state    # noqa: E402
import claim_view      # noqa: E402


def _operation_project():
    # Operation as a first-class Target (ms-142 §8): it is in the manifest
    # (target_collections seed) with work_item_arm=None and evidence_arms=[] — so
    # its deadline and 証跡 cells are DECLARED N/A, while phase-advance (status
    # lifecycle), completion-gate (spine) and claim must be GREEN.
    return {
        "name": "ops", "profession": "dev", "milestones": [],
        "operations": [
            {"id": "op-1", "title": "health check", "status": "todo",
             "occupation": {"session_id": "sv-op"},
             "entries": [
                 {"id": "e-op1", "type": "operation_task",
                  "description": "prep", "status": "todo"},
             ]},
        ],
    }


# name -> {project, target_id, work_item_id}. Rows are the manifest Target-classes
# (milestones + opportunities + operations + the synthetic descriptor). Accounts /
# acquisitions ride a separate persistence path and are deliberately NOT manifest
# Target-classes (see occupation.TARGET_COLLECTIONS), so they are out of this
# manifest-scoped matrix, matching every other ms-142 capability.
TARGET_CLASSES = {
    "milestone": {"project": _dev_project, "target_id": "ms-1",
                  "work_item_id": "e-1"},
    "opportunity": {"project": _sales_project, "target_id": "opp-1",
                    "work_item_id": "act-1"},
    "operation": {"project": _operation_project, "target_id": "op-1",
                  "work_item_id": None},
    "obligation": {"project": _synthetic_project, "target_id": "obl-1",
                   "work_item_id": "duty-1"},
}


def _manifest_tc(project, kind):
    for tc in occupation.profession_manifest(project)["target_classes"]:
        if tc["kind"] == kind:
            return tc
    return None


def _find_target_record(project, kind, target_id):
    tc = _manifest_tc(project, kind)
    if tc is None:
        return None
    for rec in project.get(tc["collection"], []) or []:
        if isinstance(rec, dict) and rec.get("id") == target_id:
            return rec
    return None


# --- capability probes: (project, kind, target_id, work_item_id) -> bool GREEN ---

def _cap_phase_advance(project, kind, target_id, work_item_id):
    """The class can be advanced through its declared state model (T2)."""
    model = target_state.state_model_for(project, kind)
    if not model or not model.get("state_field"):
        return False
    adv = model.get("advanceable_states")
    if adv:
        rec = _find_target_record(project, kind, target_id)
        if rec is None:
            return False
        cur = rec.get(model["state_field"], "")
        to = next((s for s in adv if s != cur), None)
        if to is None:
            return True   # a single advanceable non-terminal state; model routes it
        try:
            _r, _old, new = target_state.set_target_state(project, target_id, to)
            return new == to
        except Exception:
            return False
    # Opportunity funnel: set_target_state defers the transition (e-5169 follow-up);
    # the advance capability itself exists via the sales judge / phase verb. It is
    # present iff the model declares a funnel carrying the ball (phase_ball, T2).
    return (model.get("shape") == target_state.SHAPE_FUNNEL
            and bool(model.get("ball_field")))


def _cap_deadline(project, kind, target_id, work_item_id):
    return any(c["item"].get("id") == work_item_id
               for c in occupation.iter_deadline_candidates(project))


def _cap_evidence(project, kind, target_id, work_item_id):
    return any(t.get("id") == target_id
               for _ev, t, _a in occupation.iter_evidence(project))


def _cap_completion_gate(project, kind, target_id, work_item_id):
    return target_state.completion_gate_for(
        target_state.state_model_for(project, kind)) is not None


def _cap_claim(project, kind, target_id, work_item_id):
    return target_id in claim_view.build_claim_views(project)


# --- N/A predicates: (manifest tc) -> bool DECLARED-ABSENT (manifest-driven) ---

def _na_never(tc):
    return False


def _na_deadline(tc):
    # No work-item arm → the class carries no work items, hence no work-item
    # deadlines (operation). Declared, not a gap.
    return tc.get("work_item_arm") is None


def _na_evidence(tc):
    return not tc.get("evidence_arms")


# capability name -> {probe, na}. The names are the §5 "絶対漏らすな" set; the
# order matches §5's table.
MUST_HAVE_CAPABILITIES = {
    "phase_advance": {"probe": _cap_phase_advance, "na": _na_never},
    "deadline": {"probe": _cap_deadline, "na": _na_deadline},
    "evidence": {"probe": _cap_evidence, "na": _na_evidence},
    "completion_gate": {"probe": _cap_completion_gate, "na": _na_never},
    "claim": {"probe": _cap_claim, "na": _na_never},
}
