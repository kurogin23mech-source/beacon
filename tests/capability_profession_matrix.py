"""Capability × profession coverage matrix — the ms-142 completeness harness
(e-5012, rebuilt on the ms-134 reclassification). Importable definitions; the
assertions live in ``test_capability_profession_matrix.py``.

This module holds TWO sibling coverage matrices (asserted by two different test
files — check which one you are extending):
  1. e-5012 (this docstring below) — profession rows × abstraction-ITERATOR
     columns (``PROFESSIONS`` / ``GREEN_PROBES``); proves the iterators are
     declaration-driven. Asserted by ``test_capability_profession_matrix.py``.
  2. T5 e-5160 (the block after ``GREEN_PROBES``) — Target-CLASS rows × §5
     must-have-CAPABILITY columns (``TARGET_CLASSES`` /
     ``MUST_HAVE_CAPABILITIES``), 3-valued cells. Asserted by
     ``test_coverage_matrix_must_have_e5160.py``. Its probes take an extra
     ``kind`` arg and live in a ``{probe, na}`` registry — a DIFFERENT convention
     from block 1, so copy the block you are actually extending.

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
    # ms-142 e-5254: carry the shipped funnel vocabulary so the completion-gate
    # BEHAVIOUR probe can attempt a real terminal (決着) phase and observe the sales
    # judge gate refuse it. Without a funnel config the terminal is unrecognised and
    # the ban has nothing to fire on.
    import sales_entities as _se
    phases = [dict(p) for p in _se.DEFAULT_OPPORTUNITY_PHASES]
    entry_phase = _se.default_opportunity_phase({"opportunity_phases": phases})
    return {
        "name": "sales", "profession": "sales", "milestones": [],
        "opportunity_phases": phases,
        "opportunities": [
            {"id": "opp-1", "label": "O", "status": "open", "phase": entry_phase,
             "occupation": {"session_id": "sv-sales"},
             "activities": [
                 {"id": "act-1", "description": "call", "deadline": "2026-08-06",
                  "status": "todo"},
             ],
             "communications": [{"id": "comm-1", "description": "deck"}]},
        ],
    }


def _account_project():
    # ms-142 e-5256: Account as the 4th Target-class. It is ball-less (no
    # who_has_the_ball) and never-terminal (account_phases has no terminal phase),
    # both DECLARED in its state model — so its completion_gate cell is a DECLARED
    # N/A (never_terminal), while phase-advance (the phase ladder), deadline (a
    # nurturing nrt-), evidence (communications) and claim must be GREEN. No status
    # field (a 継続 relationship, tracked by phase).
    return {
        "name": "acct", "profession": "sales", "milestones": [],
        "accounts": [
            {"id": "acc-1", "name": "A", "label": "A", "phase": "リード",
             "phase_history": [], "occupation": {"session_id": "sv-acct"},
             "nurturings": [
                 {"id": "nrt-1", "description": "follow up",
                  "deadline": "2026-08-06", "status": "todo"},
             ],
             "communications": [{"id": "comm-a1", "description": "intro call"}]},
        ],
    }


def _synthetic_project():
    # imported lazily so this module has no hard dependency ordering with the
    # fixture module (both live under tests/).
    from synth_profession import build_synthetic_project
    return build_synthetic_project()


# name -> {project, target_id, work_item_id}. The third row stresses the abstraction
# (arms named nothing like dev/sales).
#
# TERMINOLOGY (ms-142 e-5144): two words name the same third row on DIFFERENT axes,
# and both are kept on purpose:
#   * ``compliance`` — the profession VALUE (the string a project.json's ``profession``
#     field carries; its target-class kind is ``obligation``). This is the registry KEY.
#   * ``synthetic`` — the ROLE that profession plays HERE: a fictional profession that
#     exists only in the test tree (``synth_profession.build_synthetic_project``) to
#     prove "declare a manifest ⇒ every shared capability lights up" without shipping a
#     real third occupation. It is why the factory is ``_synthetic_project``.
# So ``compliance`` (what it is) and ``synthetic`` (why it exists in tests) are not a
# naming drift — they label the value vs the role.
PROFESSIONS = {
    "dev": {"project": _dev_project, "target_id": "ms-1", "work_item_id": "e-1"},
    "sales": {"project": _sales_project, "target_id": "opp-1",
              "work_item_id": "act-1"},
    # profession VALUE (registry key); its ROLE here is the synthetic test fixture.
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


def _release_project():
    # Release as dev's L3 first-class Target (ms-142 §9 / e-5161): it is a
    # profession-default descriptor (kind=release), so it enters the manifest via
    # occupation.effective_descriptors with work_item_arm=None and evidence_arms=[]
    # — its deadline and 証跡 cells are DECLARED N/A (identical shape to operation),
    # while phase-advance (draft→published→deployed via the descriptor engine),
    # completion-gate (self-close-ban) and claim must be GREEN. The record is shaped
    # exactly as target_engine.create_target stamps a descriptor target (kind /
    # phase / who_has_the_ball / phase_history / work_items / evidence).
    return {
        "name": "rel", "profession": "dev", "milestones": [],
        "release_targets": [
            {"id": "rel-1", "label": "v1.0", "kind": "release",
             "status": "in_progress", "phase": "draft", "version": "1.0.0",
             "who_has_the_ball": "self",
             "occupation": {"session_id": "sv-rel"},
             "phase_history": [], "work_items": [], "evidence": []},
        ],
    }


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
# (milestones + opportunities + operations + accounts + the synthetic descriptor).
# ms-142 e-5256: ACCOUNTS joined the manifest (occupation.TARGET_COLLECTIONS) — an
# account has the full phase/work-item/evidence shape, so excluding it was a
# narrowing (option A, now withdrawn). Acquisitions still ride a separate
# persistence path and remain out of this manifest-scoped matrix.
# The row key is the target-class KIND. ``work_item_id`` is None when the class
# declares no work-item arm (operation) — that None is what makes the deadline
# cell a declared N/A, matched by _na_deadline reading work_item_arm. The
# ``obligation`` row's factory is _synthetic_project (not _obligation_project):
# it is THE synthetic descriptor profession, kind=obligation (see synth_profession).
TARGET_CLASSES = {
    "milestone": {"project": _dev_project, "target_id": "ms-1",
                  "work_item_id": "e-1"},
    "opportunity": {"project": _sales_project, "target_id": "opp-1",
                    "work_item_id": "act-1"},
    "operation": {"project": _operation_project, "target_id": "op-1",
                  "work_item_id": None},   # None → no work-item arm → deadline N/A
    # ms-142 e-5256: account is ball-less + never-terminal → completion_gate is a
    # declared N/A; phase-advance / deadline (nrt-) / evidence (communications) /
    # claim are GREEN. work_item_id=nrt-1 (nurturings are its work-item arm).
    "account": {"project": _account_project, "target_id": "acc-1",
                "work_item_id": "nrt-1"},
    # ms-142 e-5161: release is a profession-default (built-in-as-data) dev Target-
    # class. work_item_id=None → its deadline cell is a declared N/A (work_item_arm
    # =None), evidence_arms=[] → 証跡 N/A; phase-advance / completion-gate / claim GREEN.
    "release": {"project": _release_project, "target_id": "rel-1",
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
    """The class can be advanced through its declared state model (T2).

    NOTE: MUTATES ``project`` (calls set_target_state in-place). The test hands it
    a FRESH ``row["project"]()`` per cell so the mutation never leaks; a caller
    reusing this probe outside the test must do the same."""
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
            # Degenerate model: a single advanceable state and the target already
            # sits on it — the advance PATH exists (set_target_state routes this
            # class) but there is no distinct state to move to in this fixture. No
            # built-in class hits this (all declare ≥2 advanceable states / a
            # funnel); it is a defensive guard, not a live green cell.
            return True
        try:
            _r, _old, new = target_state.set_target_state(project, target_id, to)
            return new == to
        except Exception:
            return False
    # Funnel: the class advances through its declared phase ladder. The advance
    # capability exists via the class's phase verb (opportunity_phase / acc-
    # phase_set) — set_target_state defers the actual write (e-5169) but the
    # capability is present. ms-142 e-5256: the ball is ORTHOGONAL to phase
    # advancement — an opportunity carries one, an account does not, yet BOTH
    # advance their phases. So the check is the declared funnel shape, NOT the ball
    # (coupling them denied a ball-less funnel its phase-advance cell; leader 裁定).
    return model.get("shape") == target_state.SHAPE_FUNNEL


def _cap_deadline(project, kind, target_id, work_item_id):
    # Same "is this work item a deadline candidate" check as the e-5012 block's
    # _probe_iter_deadline_candidates — delegate rather than re-inline the
    # iter_deadline_candidates walk (single source of truth, maint review §2).
    return _probe_iter_deadline_candidates(project, target_id, work_item_id)


def _cap_evidence(project, kind, target_id, work_item_id):
    return any(t.get("id") == target_id
               for _ev, t, _a in occupation.iter_evidence(project))


def _a_gated_state(project, kind, model):
    """Return ONE state whose transition the completion gate must REFUSE (a
    routed/gated state, or — for a funnel — a config-terminal phase), else None.
    Used by the completion-gate BEHAVIOUR probe to actually attempt the banned
    transition instead of trusting a declared label (ms-142 e-5254)."""
    routed = model.get("routed_states") or {}
    if routed:
        return next(iter(routed))
    if model.get("shape") == target_state.SHAPE_FUNNEL:
        # a funnel's terminal (決着) phase is config-derived, not in routed_states.
        # Read the project's ONE funnel config (opportunity_phases) — the fixture
        # (_sales_project) carries it, so there is no DEFAULT fallback here (a 2nd
        # source of the terminal set would risk drifting from the config; maint
        # review e-5254). None if the project declares no terminal phase.
        import sales_entities as _se
        return next((p["name"] for p in _se.opportunity_phases(project)
                     if p.get("terminal")), None)
    return None


def _cap_completion_gate(project, kind, target_id, work_item_id):
    """GREEN iff the completion gate BEHAVIOURALLY fires — attempting a
    terminal/gated transition via the generic ``set_target_state`` path is actually
    REFUSED (the anti-self-close ban applies), NOT merely that a gate LABEL is
    declared (ms-142 e-5254: DECLARATION≠ENFORCEMENT — the drift-checker
    target_state.py promised itself). A class that declares a gate but leaves the
    terminal reachable (ban not wired) returns False → an EMPTY cell → CI fail.

    A never_terminal class has no terminal to ban (its N/A is handled by
    _na_completion_gate); this probe is only asked to light up for terminal classes.
    MUTATES ``project`` (attempts a transition) — the matrix hands a fresh project
    per cell, as with _cap_phase_advance."""
    model = target_state.state_model_for(project, kind)
    if not model:
        return False
    if model.get("never_terminal"):
        # no terminal to ban (account's 継続 relationship) — its cell is a declared
        # N/A (_na_completion_gate), so the gate must NOT light up here.
        return False
    banned = _a_gated_state(project, kind, model)
    if banned is None:
        # a terminal class with no reachable gated state → the ban cannot be proven
        # to fire (a label without an enforceable target is exactly the drift).
        return False
    try:
        target_state.set_target_state(project, target_id, banned)
        return False   # the ban did NOT fire — the gate is a label-only lie.
    except target_state.TargetStateError:
        return True    # the ban fired: the gate ENFORCES, not just declares.


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


def _na_completion_gate(tc):
    # ms-142 e-5256: a class that DECLARES it never settles (``never_terminal`` — no
    # 決着 grain, e.g. an account's 継続 relationship) has no completion gate. That is
    # a DECLARED absence, not a forgotten gate. GENERAL (not account-hardcoded): any
    # never-terminal class shares it, while a terminal class (milestone / operation /
    # opportunity / acquisition — never_terminal=False) must have a GREEN gate. Read
    # as a POSITIVE declaration (not "completion_gate is None") so a class that merely
    # forgot its gate is still an EMPTY (failing) cell, not a silent N/A.
    return bool((tc.get("state_model") or {}).get("never_terminal"))


# capability name -> {probe, na}. Slot contract (distinct from the e-5012 block's
# bare-function GREEN_PROBES, so read the signatures here, not there):
#   probe: (project, kind, target_id, work_item_id) -> bool  — True iff the
#          capability behaviourally lights up (GREEN).
#   na:    (manifest_tc) -> bool  — True iff the manifest DECLARES the class lacks
#          the arm this capability rides (a declared absence, not a gap).
# The names are the §5 "絶対漏らすな" set; the order matches §5's table.
MUST_HAVE_CAPABILITIES = {
    "phase_advance": {"probe": _cap_phase_advance, "na": _na_never},
    "deadline": {"probe": _cap_deadline, "na": _na_deadline},
    "evidence": {"probe": _cap_evidence, "na": _na_evidence},
    "completion_gate": {"probe": _cap_completion_gate, "na": _na_completion_gate},
    "claim": {"probe": _cap_claim, "na": _na_never},
}
