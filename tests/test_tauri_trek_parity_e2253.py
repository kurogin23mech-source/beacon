"""Tauri Desktop App parity for Trek state / dataSource (ms-86 / e-2253).

Pins the contract introduced by e-2253: every Trek-axis change that landed
in the Web UI for e-2248 (server aggregate endpoints), e-2249 (lookup
helpers state.project-independent), e-2250 (URL = ?trek=<id>, header
brand-only) is mirrored into the Tauri Desktop App. The Tauri build merges
``desktop/layer.js`` (= Tauri-specific PLATFORM / dataSource / state init,
the SKIP-block equivalent of Web) with the SHARED region extracted from
``server/static/index.html`` to produce ``desktop/dist/index.html``.

These tests are **structural source-level pins** — same regime as
``test_treks_tab_ui.py`` /
``test_trek_lookup_helpers_state_project_independent.py`` /
``test_trek_url_axis_and_header_independence.py``. They never spin up
Tauri or run any JS; they just grep the source files to catch regressions
that would silently break the Desktop build (e.g. a future state-init
trim that drops ``openTrekScope`` and bounces Tauri users back to the
project selector on the first Trek click).

The CORE doc covering the dual-layer architecture is
``K8AhPgjpDG3mEa4eVm37`` (Tauri/Web UI 二層アーキテクチャ).
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LAYER_JS = REPO_ROOT / "desktop" / "layer.js"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tauri ``state`` init declares every Trek-axis slot that SHARED touches
# (= Web initializes these in its own SKIP block; Tauri must mirror them,
#  same regime as ``showCancelled`` / ``searchResults`` etc.).
# ---------------------------------------------------------------------------


class TestTauriStateInitDeclaresTrekSlots:
    """If any of these are missing, the first Trek-row click in Tauri
    throws on ``state.openTrekScope.milestones.find`` /
    ``state.openTrekExpanded.has`` / array spread and bounces the user
    back to the project selector. The Web SKIP block declares the same
    fields at server/static/index.html L2747-2768.
    """

    def test_treks_field_initialized(self):
        src = _read(LAYER_JS)
        assert "treks: []," in src

    def test_open_trek_id_field_initialized(self):
        src = _read(LAYER_JS)
        assert "openTrekId: null," in src

    def test_open_trek_docs_field_initialized(self):
        src = _read(LAYER_JS)
        assert "openTrekDocs: []," in src

    def test_open_trek_scope_field_initialized_with_shape(self):
        # e-2249 SPEC: cross-project aggregate shape is
        # ``{ milestones: [], operations: [], tasks: [] }``. Helpers
        # destructure these arrays unconditionally so the init must
        # match the shape exactly (= no defensive fallback in SHARED).
        src = _read(LAYER_JS)
        assert (
            "openTrekScope: { milestones: [], operations: [], tasks: [] },"
            in src
        )

    def test_open_trek_expanded_field_initialized_as_set(self):
        # ms-86 / e-2126 — accordion expansion state. Must be a Set
        # because SHARED calls .has() / .add() / .delete().
        src = _read(LAYER_JS)
        assert "openTrekExpanded: new Set()," in src

    def test_open_trek_show_done_field_initialized_as_set(self):
        # ms-86 / e-2224 — per-MS "show done" toggle.
        src = _read(LAYER_JS)
        assert "openTrekShowDone: new Set()," in src

    def test_related_treks_field_initialized_as_object(self):
        # ms-69 / e-1664 — reverse lookup cache, keyed by
        # 'ms:ms-69' / 'op:op-1' / 'task:e-1656'.
        src = _read(LAYER_JS)
        assert "relatedTreks: {}," in src


# ---------------------------------------------------------------------------
# Tauri ``dataSource`` exposes the 4 Trek load methods SHARED calls
# (= openTrek invokes loadTrekDocs + loadTrekScope; settings dispatcher
#  invokes loadTreks; widget callbacks invoke loadRelatedTreks).
# ---------------------------------------------------------------------------


class TestTauriDataSourceHasTrekMethods:
    """The SHARED region calls these as ``dataSource.loadXxx(...)`` —
    missing entries throw ``is not a function`` at call time, which is
    a silent break that only fires after the user clicks into a Trek.
    The Web equivalents live at server/static/index.html L3894-3937.
    """

    def test_load_treks_present(self):
        src = _read(LAYER_JS)
        assert "loadTreks: async () =>" in src

    def test_load_trek_docs_present(self):
        src = _read(LAYER_JS)
        assert "loadTrekDocs: async (trekId) =>" in src

    def test_load_trek_scope_present(self):
        # The e-2249 contract: 3 endpoints in parallel, populate
        # state.openTrekScope with the shape {milestones, operations, tasks}.
        src = _read(LAYER_JS)
        assert "loadTrekScope: async (trekId) =>" in src
        body_start = src.index("loadTrekScope: async (trekId) =>")
        # The body must hit all 3 aggregate endpoints (= e-2248).
        body_end = src.index("loadRelatedTreks", body_start)
        body = src[body_start:body_end]
        assert "/milestones" in body
        assert "/operations" in body
        assert "/tasks" in body
        assert "Promise.all" in body

    def test_load_related_treks_present(self):
        src = _read(LAYER_JS)
        assert "loadRelatedTreks: async (kind, id) =>" in src


# ---------------------------------------------------------------------------
# Tauri PLATFORM.getFooterLeftHTML respects state.openTrekId (e-2250 AC #1)
# ---------------------------------------------------------------------------


class TestTauriFooterRespectsTrekDetail:
    """Mirrors the Web e-2250 footer rule: when a Trek is open, the
    footer left strip swaps from project/mode identity to brand-only
    "Beacon" so the cross-project Trek view doesn't leak which
    project's backend Tauri is connected to (= same cognitive-axis
    argument as the header brand-only branch).
    """

    def test_footer_left_swaps_to_brand_when_trek_open(self):
        src = _read(LAYER_JS)
        assert "getFooterLeftHTML: () => state.openTrekId" in src
        assert "Beacon" in src


# ---------------------------------------------------------------------------
# Built dist/index.html carries all the parity bits end-to-end
# (= confirms desktop/build.py merge actually ships the layer.js changes).
# ---------------------------------------------------------------------------


class TestDesktopDistCarriesParity:
    def test_dist_has_trek_state_slots(self):
        src = _read(DESKTOP_INDEX)
        assert "openTrekId: null," in src
        assert (
            "openTrekScope: { milestones: [], operations: [], tasks: [] },"
            in src
        )
        assert "openTrekExpanded: new Set()," in src

    def test_dist_has_trek_datasource_methods(self):
        src = _read(DESKTOP_INDEX)
        assert "loadTreks: async () =>" in src
        assert "loadTrekDocs: async (trekId) =>" in src
        assert "loadTrekScope: async (trekId) =>" in src
        assert "loadRelatedTreks: async (kind, id) =>" in src

    def test_dist_footer_respects_trek_open(self):
        src = _read(DESKTOP_INDEX)
        assert "getFooterLeftHTML: () => state.openTrekId" in src

    def test_dist_has_shared_url_helpers(self):
        # e-2250 URL helpers come in via the SHARED region. The dist
        # must contain them — if SHARED markers shift and these drop,
        # Trek deep-links (= ?trek=<id> cold opens) silently break.
        src = _read(DESKTOP_INDEX)
        assert "function getTrekIdFromUrl()" in src
        assert "function _setUrlForTrekDetail(trekId)" in src
        assert "function _setUrlForProjectView(projectId)" in src

    def test_dist_has_e2270_render_suppression(self):
        # e-2270 fix: renderOnDataChange early-return when openTrekId set,
        # to stop WS-driven full re-render flicker on the Trek detail view.
        # Lives in SHARED region; the dist must carry it.
        src = _read(DESKTOP_INDEX)
        assert "function renderOnDataChange()" in src
        body_start = src.index("function renderOnDataChange()")
        body = src[body_start:body_start + 2000]
        assert "if (state.openTrekId) return;" in body

    def test_dist_has_e2249_helpers(self):
        # e-2249: lookup helpers refactored to take ``scope`` instead of
        # walking state.project. The SHARED region defines them; dist
        # must carry them so Tauri Trek detail can resolve task / MS
        # entries across projects.
        src = _read(DESKTOP_INDEX)
        assert "_trekLookupTask" in src
        assert "_trekLookupMilestone" in src
