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


# --- e-5851: surface-grained entries carry wedges + 大節/小節 structure ----------

def _seed_surface():
    """A surface-grained backfill-shaped log: two 小節 categories under one 大節
    (``area:``), each bullet carrying its machine-checkable wedge(s) in ``tags``."""
    data = {"name": "P", "profession": "dev"}
    dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "状態を一望する", "title": "status",
        "summary": "いま何が進行中かを1画面で把握できる",
        "tags": ["area:見失わない — 現在地と進捗の可視化",
                 "cli:beacon status", "api:GET /api/projects"]})
    dc.append_deliverable(data, {
        "source": {"target_id": "root", "kind": "root"},
        "category": "マイルストーンを管理する", "title": "milestone",
        "summary": "目的地を立て状態を回せる",
        "tags": ["area:見失わない — 現在地と進捗の可視化",
                 "cli:beacon milestone *"]})
    return data


def test_dev_render_emits_wedges_from_tags():
    out = dm.render_map(_seed_surface())
    # wedges appear as machine-checkable backtick `type:ident` tokens
    assert "`cli:beacon status`" in out
    assert "`api:GET /api/projects`" in out
    assert "`cli:beacon milestone *`" in out
    # the 散文 summary is the bullet body, wedge trails it
    assert "- いま何が進行中かを1画面で把握できる  `cli:beacon status`" in out


def test_dev_render_reconciles_via_check_map_drift():
    """The DERIVED map's wedges must be parseable by the SAME reconciler the
    hand-maintained doc used (e-5851 楔の機械照合維持)."""
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    # module filename has a hyphen, so import via importlib, not the import stmt.
    drift = importlib.import_module("check-map-drift")
    out = dm.render_map(_seed_surface())
    parsed = drift.parse_wedges(out)
    assert ("cli", "beacon status") in parsed
    assert ("api", "GET /api/projects") in parsed
    assert ("cli", "beacon milestone *") in parsed


def test_dev_render_emits_area_and_category_headers():
    out = dm.render_map(_seed_surface())
    # 大節 (area) as ## and 小節 (category) as ###
    assert "## 見失わない — 現在地と進捗の可視化" in out
    assert "### 状態を一望する" in out
    assert "### マイルストーンを管理する" in out
    # the area header appears ONCE even though two categories share it
    assert out.count("## 見失わない — 現在地と進捗の可視化") == 1


def test_dev_render_backward_compatible_without_wedges():
    """Coarse entries (no wedge/area tags) render as before — no header-level flip,
    no trailing wedges — so pre-e-5851 logs are unaffected."""
    out = dm.render_map(_seed())
    assert "## 機能 — 何ができるか" in out  # stays ## when no area is present
    assert "- 他セッションの DM で起きる `→ application-map`" in out


# --- e-5902: auto-completion entries are held out of the surface index ----------

def test_auto_completion_entries_excluded_from_surface_index():
    """A coarse auto-capture completion entry (AUTO_COMPLETION_TAG) must NOT appear
    in the surface index — it lands in the trailing 完遂 section, so the map stays a
    surface-単位 index, not a list of 完了理由 (e-5902 Done-when)."""
    data = _seed_surface()  # two surface entries with wedges
    dc.append_deliverable(data, {
        "source": {"target_id": "ms-42", "kind": "milestone"},
        "category": "feature-map", "title": "ms-42 done",
        "summary": "ms-42 の完了理由テキスト",
        "tags": [dc.AUTO_COMPLETION_TAG]})
    out = dm.render_map(data)
    # surface entries still in the index
    assert "### 状態を一望する" in out
    assert "`cli:beacon status`" in out
    # the completion entry is in the trailing section, NOT under a 機能 index heading
    assert "未 index 化の完遂" in out
    assert "ms-42 の完了理由テキスト (ms-42)" in out
    idx = out.index("未 index 化の完遂")
    # its 完了理由 appears only AFTER the trailing-section header (not in the index)
    assert out.index("ms-42 の完了理由テキスト") > idx


def test_completion_only_log_renders_just_the_trailing_section():
    """If the log holds ONLY auto-completion entries (no curated surfaces yet), the
    surface index is empty and only the 完遂 section shows — no phantom index."""
    data = {"name": "P", "profession": "dev"}
    dc.append_deliverable(data, {
        "source": {"target_id": "ms-1", "kind": "milestone"},
        "category": "feature-map", "title": "t", "summary": "ms-1 完遂",
        "tags": [dc.AUTO_COMPLETION_TAG]})
    out = dm.render_map(data)
    assert "未 index 化の完遂" in out
    assert "ms-1 完遂" in out
    assert "### " not in out  # no surface subsection rendered
