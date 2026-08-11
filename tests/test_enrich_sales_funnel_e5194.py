"""ms-108 e-5194: the Web UI payload must carry a resolved sales funnel.

Regression guard for the blank 商談ボード: a sales project whose saved data has
no ``opportunity_phases`` key (not created via ``new_sales_project``, or predates
the seed) used to reach the browser with an empty funnel → zero kanban columns →
blank board. The CLI never hit this because it resolves configured-or-default via
``sales_entities.effective_phases``. These tests pin that the server enrich layer
now applies the SAME resolver so CLI and Web UI agree.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import routers_projects as rp  # noqa: E402


def _funnelless_sales():
    # A sales project with opportunities but NO funnel keys (cairn's shape).
    return {
        "profession": "sales",
        "opportunities": [{"id": "opp-3", "phase": "先方検討中"}],
        "milestones": [],
    }


def test_slim_enrich_resolves_default_opportunity_funnel():
    data = _funnelless_sales()
    enriched = rp._enrich_project_slim(data)
    # blank board came from opportunity_phases == [] ; now it must be the default funnel.
    assert len(enriched["opportunity_phases"]) > 0
    names = [p.get("name") for p in enriched["opportunity_phases"]]
    assert "先方検討中" in names  # the phase the deal is actually in is a real column
    assert enriched["account_phases"] and enriched["prospect_phases"]


def test_full_enrich_resolves_default_opportunity_funnel():
    enriched = rp._enrich_project(_funnelless_sales())
    assert len(enriched["opportunity_phases"]) > 0


def test_enrich_does_not_mutate_source():
    data = _funnelless_sales()
    rp._enrich_project_slim(data)
    # the resolver writes to the enriched copy, never the stored project.
    assert "opportunity_phases" not in data


def test_configured_funnel_passes_through_unchanged():
    # a sales project WITH a custom funnel must keep it (effective_phases returns
    # configured-when-present); the resolver must not overwrite it with the default.
    custom = [{"name": "独自A"}, {"name": "独自B"}]
    data = {"profession": "sales", "opportunity_phases": custom,
            "opportunities": [], "milestones": []}
    enriched = rp._enrich_project_slim(data)
    assert [p["name"] for p in enriched["opportunity_phases"]] == ["独自A", "独自B"]


def test_non_sales_is_a_noop():
    # dev projects have no funnel concept — the resolver must not inject one.
    enriched = rp._enrich_project_slim({"profession": "dev", "milestones": []})
    assert "opportunity_phases" not in enriched
