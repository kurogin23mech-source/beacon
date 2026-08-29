"""Unit tests for deliverable_map — the map projector (ms-161 e-5824 / 受入条件3+5).

Assert (a) summarize_map groups ACTIVE entries by category (retired/superseded
excluded), profession-independent; (b) the dev render is application-map-flavoured;
(c) an unknown profession gets the generic render; (d) empty log renders cleanly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import deliverable_changelog as dc  # noqa: E402
import deliverable_map as dm  # noqa: E402


def _seed(profession="dev"):
    data = {"name": "P", "profession": profession, "milestones": []}
    dc.append_deliverable(data, {
        "source": {"target_id": "ms-1", "kind": "milestone"},
        "category": "feature-map", "title": "DM idle-wake",
        "summary": "他セッションの DM で起きる", "ref": "application-map"})
    dc.append_deliverable(data, {
        "source": {"target_id": "ms-2", "kind": "milestone"},
        "category": "feature-map", "title": "claim",
        "summary": "作業の二重取りを防ぐ"})
    return data


# --- summarize_map: profession-independent grouping (受入条件3) ----------------

def test_summarize_groups_active_by_category():
    s = dm.summarize_map(_seed())
    assert s["profession"] == "dev"
    assert s["total"] == 2
    assert len(s["categories"]) == 1
    cat = s["categories"][0]
    assert cat["category"] == "feature-map"
    assert cat["count"] == 2
    assert [e["title"] for e in cat["entries"]] == ["DM idle-wake", "claim"]


def test_summarize_excludes_retired_and_superseded():
    data = _seed()
    # retire the first, supersede is exercised in changelog tests; here retire one
    first_id = data[dc.CHANGELOG_KEY][0]["id"]
    dc.retire_deliverable(data, first_id)
    s = dm.summarize_map(data)
    assert s["total"] == 1
    assert [e["title"] for e in s["categories"][0]["entries"]] == ["claim"]


def test_summarize_preserves_category_first_seen_order():
    data = {"name": "P", "profession": "dev"}
    dc.append_deliverable(data, {"source": {"target_id": "ms-1", "kind": "milestone"},
                                 "category": "beta", "title": "b", "summary": "b"})
    dc.append_deliverable(data, {"source": {"target_id": "ms-2", "kind": "milestone"},
                                 "category": "alpha", "title": "a", "summary": "a"})
    dc.append_deliverable(data, {"source": {"target_id": "ms-3", "kind": "milestone"},
                                 "category": "beta", "title": "b2", "summary": "b2"})
    cats = [c["category"] for c in dm.summarize_map(data)["categories"]]
    assert cats == ["beta", "alpha"]  # first-seen order, not sorted


# --- render_map: dev = application-map-flavoured (方針4) -----------------------

def test_dev_render_is_application_map_flavoured():
    out = dm.render_map(_seed())
    assert "アプリケーション全貌マップ" in out
    # pretty heading for the known dev category token
    assert "機能" in out
    # produced-value summaries appear as bullets with the drill-down ref wedge
    assert "- 他セッションの DM で起きる `→ application-map`" in out
    assert "- 作業の二重取りを防ぐ" in out


def test_dev_render_empty_log_is_clean():
    out = dm.render_map({"name": "P", "profession": "dev"})
    assert "まだ記録された成果がありません" in out


def test_render_defaults_to_project_profession():
    # a sales project (no bespoke render yet) → generic render
    data = _seed(profession="sales")
    out = dm.render_map(data)
    assert "汎用 render" in out
    assert "sales" in out


def test_render_profession_override_previews_other():
    data = _seed(profession="dev")
    # force the generic render for a hypothetical profession
    out = dm.render_map(data, profession="backoffice")
    assert "backoffice" in out
    assert "汎用 render" in out
