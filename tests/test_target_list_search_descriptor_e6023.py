"""Regression: the retro/search meta-index enumerates descriptor-class Targets,
not just milestones + operations (ms-164 e-6023).

`retro_query._build_meta_index` hardcoded `data["milestones"] + data["operations"]`,
so a non-dev target's entries (a sales opportunity's activity meta: source /
actor / session_id) were never indexed and were invisible to the search
post-filters. This locks in that it walks every target class via
`occupation.iter_target_records`, so an opportunity's entry meta is indexed.

Adjudication note (e-6023): `cmd_target_list` was NOT generalised. It lists only
`target-transition-approval` entries, and those exist solely on
`_SPINE_ENFORCED_KINDS = {milestone, operation}` (see transition_approval), so a
descriptor/sales target can never hold one — its milestones+operations read is
exact, classified reviewed-legitimate in capability_ledger.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import retro_query  # noqa: E402

OPPORTUNITY = {
    "kind": "opportunity", "label": "商談", "profession": "sales",
    "type": "single-shot", "id_prefix": "opp-", "collection": "opportunities",
    "decomposition": {"id_field": "id", "arms": ["activities"]},
    "phases": [{"key": "lead", "label": "リード"},
               {"key": "won", "label": "受注", "terminal": True}],
}


def _project():
    return {
        "name": "t", "milestones": [], "operations": [],
        "target_classes": [OPPORTUNITY],
        "opportunities": [{
            "id": "opp-1", "status": "lead",
            "entries": [
                {"id": "e-900", "type": "activity",
                 "meta": {"source": "human", "actor": {"machine": "m1"}}},
            ],
        }],
    }


def test_meta_index_covers_descriptor_class_entries():
    """_build_meta_index must index an opportunity's entry meta so the
    source/actor/session post-filters can see non-dev entries."""
    idx = retro_query._build_meta_index(_project())
    assert "e-900" in idx, (
        "opportunity's activity entry meta was not indexed — the meta index did "
        "not enumerate the descriptor class")
    assert idx["e-900"].get("source") == "human"


def test_entry_claimant_stays_milestone_scoped():
    """Lock the deliberate asymmetry (PR#719 maintainability §2/§7): _build_meta_index
    was generalised to all target classes, but _entry_claimant is INTENTIONALLY left
    milestone-scoped because its ``ms_id`` back-ref is a milestone-only field. Without
    this guard, a future generalisation of _entry_claimant to iter_target_records would
    pass CI silently (an opportunity carries no ms_id, so the milestone loop just
    skips). This test fixes the expected behaviour on both sides of the branch."""
    project = {
        "name": "t", "milestones": [
            {"id": "ms-1", "status": "in_progress", "claim_holder": "alice",
             "entries": []},
        ],
        "operations": [], "target_classes": [OPPORTUNITY],
        "opportunities": [
            {"id": "opp-1", "status": "lead", "claim_holder": "bob", "entries": []},
        ],
    }
    # A milestone-attributed result resolves its claim holder.
    ms_result = {"id": "e-1", "ms_id": "ms-1"}
    assert retro_query._entry_claimant(ms_result, {}, project) == "alice"
    # A non-dev result (no ms_id) must NOT pick up the opportunity's claim_holder
    # via this milestone-scoped lookup — it returns "" (its claim, if any, comes
    # from meta.claim_holder, checked earlier in the function).
    opp_result = {"id": "e-2", "ms_id": ""}
    assert retro_query._entry_claimant(opp_result, {}, project) == ""


def test_meta_index_still_covers_milestones_and_operations():
    """Regression guard: generalising to iter_target_records must not drop the
    dev/ops entries the old hardcode covered."""
    project = {
        "name": "t",
        "milestones": [{"id": "ms-1", "status": "in_progress",
                        "entries": [{"id": "e-1", "type": "commit",
                                     "meta": {"source": "human"}}]}],
        "operations": [{"id": "op-1", "status": "open",
                        "entries": [{"id": "e-2", "type": "run_record",
                                     "meta": {"source": "auto-op"}}]}],
    }
    idx = retro_query._build_meta_index(project)
    assert "e-1" in idx and "e-2" in idx
