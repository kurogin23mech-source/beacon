"""Trek lookup helper refactor tests (ms-86 / e-2249).

Pins the contract introduced by e-2249 SPEC ``IgCFK8I34cBfPco3UE06``
設計方針 5: UI lookup helpers (= ``_trekLookupTask`` / ``_trekLookupMilestone``)
must NOT read ``state.project``. They take the cross-project Trek scope
aggregate (= populated by ``dataSource.loadTrekScope`` from the e-2248
endpoints) as an explicit argument so the Trek detail view works no matter
which project the caller currently has open (= or no project at all).

Strategy: source-level structural pins via grep on
``server/static/index.html`` and ``desktop/dist/index.html``. JavaScript
unit testing infrastructure isn't set up in this repo (= existing UI
tests are all source-grep based, see ``tests/test_treks_tab_ui.py``), so
the new helpers are pinned the same way.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Old helper names removed (= dead in the source after the rename)
# ---------------------------------------------------------------------------


class TestOldHelpersRemoved:
    def test_web_ui_no_old_lookup_task_in_project(self):
        src = _read(WEB_INDEX)
        assert "_trekLookupTaskInProject" not in src, (
            "_trekLookupTaskInProject was renamed to _trekLookupTask "
            "(e-2249, signature now takes scope dict). Re-run grep on "
            "server/static/index.html and update remaining call sites."
        )

    def test_web_ui_no_old_lookup_milestone_in_project(self):
        src = _read(WEB_INDEX)
        assert "_trekLookupMilestoneInProject" not in src, (
            "_trekLookupMilestoneInProject was renamed to _trekLookupMilestone."
        )

    def test_desktop_dist_no_old_helper_names(self):
        # parity: desktop/build.py must have been re-run after the rename.
        src = _read(DESKTOP_INDEX)
        assert "_trekLookupTaskInProject" not in src
        assert "_trekLookupMilestoneInProject" not in src


# ---------------------------------------------------------------------------
# New helpers exist and have the expected scope argument shape
# ---------------------------------------------------------------------------


class TestNewHelperSignatures:
    def test_web_ui_lookup_task_takes_scope_arg(self):
        src = _read(WEB_INDEX)
        assert "function _trekLookupTask(scope, taskId)" in src, (
            "_trekLookupTask must take (scope, taskId) so callers cannot "
            "fall back to state.project (e-2249 AC #2)."
        )

    def test_web_ui_lookup_milestone_takes_scope_arg(self):
        src = _read(WEB_INDEX)
        assert "function _trekLookupMilestone(scope, msId)" in src

    def test_desktop_parity_for_helper_signatures(self):
        src = _read(DESKTOP_INDEX)
        assert "function _trekLookupTask(scope, taskId)" in src
        assert "function _trekLookupMilestone(scope, msId)" in src


# ---------------------------------------------------------------------------
# state.project is no longer touched by Trek lookup helpers
# ---------------------------------------------------------------------------


class TestNoStateProjectInTrekLookups:
    """e-2249 AC #1 — helper bodies don't reference state.project.

    Trek-related ``state.project`` references in OTHER code (= legacy
    project view) are out of scope for this task; we only assert that
    the two refactored helpers' bodies are state.project-free.
    """

    def _extract_function_body(self, src: str, signature: str) -> str:
        """Return the body of a top-level JS function by its signature line."""
        start = src.index(signature)
        # Walk braces to find the matching close. The signature line
        # includes "{" at the end so depth starts at 1 after the line.
        i = src.index("{", start)
        depth = 1
        j = i + 1
        while j < len(src) and depth > 0:
            ch = src[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            j += 1
        return src[i:j]

    def test_lookup_task_body_no_state_project(self):
        src = _read(WEB_INDEX)
        body = self._extract_function_body(
            src, "function _trekLookupTask(scope, taskId)"
        )
        assert "state.project" not in body, (
            "_trekLookupTask body must not read state.project "
            "(e-2249 AC #1). It should derive everything from the scope arg."
        )

    def test_lookup_milestone_body_no_state_project(self):
        src = _read(WEB_INDEX)
        body = self._extract_function_body(
            src, "function _trekLookupMilestone(scope, msId)"
        )
        assert "state.project" not in body


# ---------------------------------------------------------------------------
# state.openTrekScope state field + dataSource.loadTrekScope present
# ---------------------------------------------------------------------------


class TestStateAndDataSourceWiring:
    def test_state_open_trek_scope_initialised(self):
        # The default shape carries empty arrays so render-time defensive
        # checks don't have to crash when openTrek hasn't run yet.
        src = _read(WEB_INDEX)
        assert "openTrekScope:" in src
        assert "milestones: []" in src and "operations: []" in src and "tasks: []" in src

    def test_data_source_has_load_trek_scope(self):
        src = _read(WEB_INDEX)
        assert "loadTrekScope: async (trekId)" in src, (
            "dataSource.loadTrekScope is the wiring point that hits the "
            "e-2248 aggregate endpoints; without it state.openTrekScope "
            "stays empty and the detail view degrades to bare ids."
        )

    def test_load_trek_scope_calls_all_three_endpoints(self):
        # Must hit /milestones, /operations, /tasks for the aggregate.
        src = _read(WEB_INDEX)
        # The three endpoint paths must appear in the loader.
        for path in (
            "/milestones`",
            "/operations`",
            "/tasks`",
        ):
            assert path in src, f"loadTrekScope missing endpoint reference {path!r}"

    def test_open_trek_invokes_load_trek_scope(self):
        # openTrek() is the entry point that primes state.openTrekScope;
        # without this wire-up the detail view never sees aggregate data.
        src = _read(WEB_INDEX)
        assert "dataSource.loadTrekScope(trekId)" in src

    def test_close_trek_resets_open_trek_scope(self):
        # Reset back to the empty shape so re-opening a different Trek
        # doesn't inherit the previous Trek's data while loading.
        src = _read(WEB_INDEX)
        # The close path includes the canonical empty shape assignment.
        assert (
            "state.openTrekScope = { milestones: [], operations: [], tasks: [] }"
            in src
        )

    def test_desktop_parity_for_shared_region_pieces(self):
        # desktop/dist/index.html is built from the @BUILD:SHARED region
        # plus desktop/layer.js (= Tauri state + Tauri-side dataSource).
        # The dataSource definition (= `loadTrekScope: async (trekId)`)
        # lives inside a @BUILD:SKIP block in server/static (web-only
        # impl); Tauri's equivalent belongs in desktop/layer.js, which is
        # e-2253's job (= Tauri parity follow-up). For e-2249 we only
        # assert that the SHARED pieces — the call site in openTrek(),
        # the helper bodies, and the state.openTrekScope reference in
        # the renderer — survived the desktop build.
        src = _read(DESKTOP_INDEX)
        assert "dataSource.loadTrekScope(trekId)" in src, (
            "openTrek() must call dataSource.loadTrekScope in the SHARED "
            "region so both Web UI and Tauri see the same wiring."
        )
        assert "state.openTrekScope" in src  # via helpers / openTrek()
        assert "_trekLookupTask(scope, taskId)" in src
        assert "_trekLookupMilestone(scope, msId)" in src


# ---------------------------------------------------------------------------
# Renderers receive scope explicitly (= no global fish)
# ---------------------------------------------------------------------------


class TestRendererSignatures:
    def test_render_trek_task_row_takes_scope(self):
        src = _read(WEB_INDEX)
        assert "function _renderTrekTaskRow(t, s, slotIndex, scope)" in src

    def test_render_trek_members_table_takes_scope(self):
        src = _read(WEB_INDEX)
        assert "function _renderTrekMembersTable(t, scopeAggregate)" in src

    def test_render_trek_detail_passes_scope_aggregate_down(self):
        src = _read(WEB_INDEX)
        # The aggregate is sourced from state.openTrekScope and threaded
        # down to both consumers.
        assert "const scopeAggregate = state.openTrekScope" in src
        assert (
            "_renderTrekTaskRow(t, s, i, scopeAggregate)" in src
        ), "scope aggregate must be threaded into the TREK TASKS row renderer"
        assert (
            "_renderTrekMembersTable(t, scopeAggregate)" in src
        ), "scope aggregate must be threaded into the MEMBERS table renderer"

    def test_desktop_parity_for_renderer_signatures(self):
        src = _read(DESKTOP_INDEX)
        assert "function _renderTrekTaskRow(t, s, slotIndex, scope)" in src
        assert "function _renderTrekMembersTable(t, scopeAggregate)" in src
