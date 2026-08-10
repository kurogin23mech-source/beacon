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
