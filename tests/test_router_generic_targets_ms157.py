"""ms-157 e-5749 — the project REST payload carries a profession-agnostic
``targets`` array (occupation.project_targets), so a descriptor-defined target-
class (a new occupation added by DATA, not code) surfaces through the router with
ZERO wiring. The milestone-specific enrichment stays byte-for-byte, so every
existing profession endpoint is unchanged; ``targets`` is purely additive.

Exercises the pure ``_enrich_project`` helper directly (no FastAPI/auth harness):
that is the single function every GET /api/projects/{id} response flows through.
"""

from __future__ import annotations

import os
import sys

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import target_engine as te  # noqa: E402
import routers_projects as rp  # noqa: E402


_CONTRACT = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": ["clauses"]},
    "phases": [{"key": "drafting", "label": "起草"},
               {"key": "signed", "label": "締結", "terminal": True}],
}


def _dev():
    return {"name": "d", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                            "entries": [
                                {"id": "e-1", "type": "task", "status": "done",
                                 "description": "t"},
                                {"id": "e-2", "type": "task", "status": "todo",
                                 "description": "u"}]}]}


def _backoffice():
    data = {"name": "bo", "profession": "backoffice", "milestones": [],
            "target_classes": [_CONTRACT]}
    te.create_target(data, _CONTRACT, label="A社 NDA")
    return data


def test_enrich_emits_generic_targets_for_dev():
    enriched = rp._enrich_project(_dev())
    # generic cross-class view is present and includes the milestone as a target
    ids = [t["id"] for t in enriched["targets"]]
    assert "ms-1" in ids
    # milestone-specific enrichment is UNCHANGED (still computes task counts)
    ms = enriched["milestones"][0]
    assert ms["total_tasks"] == 2 and ms["done_tasks"] == 1


def test_enrich_surfaces_descriptor_target_with_zero_router_wiring():
    enriched = rp._enrich_project(_backoffice())
    # the descriptor-defined contract appears in the uniform targets array,
    # though NO router code names "contracts" anywhere.
    contract = next((t for t in enriched["targets"] if t["id"] == "ctr-1"), None)
    assert contract is not None
    assert contract["kind"] == "contract"
    # the raw descriptor collection is still carried untouched (additive payload)
    assert enriched["contracts"][0]["id"] == "ctr-1"


def test_targets_is_purely_additive():
    data = _dev()
    enriched = rp._enrich_project(data)
    # dropping the new key reproduces the pre-e-5749 milestone-only shape:
    # every other key equals what the old enrichment produced.
    assert "targets" in enriched
    rest = {k: v for k, v in enriched.items() if k != "targets"}
    # milestones enriched, sales funnels resolved (empty for dev), nothing else new
    assert set(rest.keys()) == set(data.keys())
