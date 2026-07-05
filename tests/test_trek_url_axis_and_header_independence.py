"""Trek detail URL axis + header independence tests (ms-86 / e-2250).

Pins the contract introduced by e-2250 SPEC ``IgCFK8I34cBfPco3UE06``
設計方針 2 + 3:

  * **AC #1 (ヘッダ project 名完全 hide)**: header swaps to brand
    "Beacon" only when ``state.openTrekId`` is set (already covered by
    e-2219, but pinned here so a future header refactor can't regress
    cross-project Trek presentation).
  * **AC #2 (URL = ?trek=<id> solo)**: ``openTrek()`` rewrites the URL
    to drop ``?project=`` and set ``?trek=<trek_id>``.
  * **AC #3 (URL restore on close)**: ``closeTrek()`` rewrites back to
    ``?project=<state.projectId>`` (or no query when no project).
  * **AC #4 (state.openTrekId と state.project は独立軸)**: enforced
    structurally — ``selectProject`` URL sync no longer overwrites
    while a Trek is open.
  * **AC #5 (Tauri parity)**: SHARED region (= openTrek / closeTrek /
    URL helpers / init() Trek wiring) is regenerated into
    ``desktop/dist/index.html`` by ``desktop/build.py``.

Strategy: structural source-level pins, matching the existing pattern
in ``tests/test_treks_tab_ui.py`` /
``tests/test_trek_lookup_helpers_state_project_independent.py``.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Header is already brand-only when openTrek is set (e-2219 + e-2250 pin)
# ---------------------------------------------------------------------------


class TestHeaderHidesProjectInTrekView:
    def test_header_uses_brand_when_trek_is_open(self):
        # When trek detail is rendered, the header replaces the project
        # name + tag + version badge with a single brand label so users
        # never see "this Trek belongs to project X" ambient noise.
        src = _read(WEB_INDEX)
        # The brand-only branch is conditioned on ``trekOpen``.
        assert "const trekOpen = !!state.openTrekId;" in src
        assert "project-name brand" in src
        assert ">Beacon<" in src

    def test_header_shows_project_name_when_no_trek(self):
        src = _read(WEB_INDEX)
        # The non-Trek branch still surfaces the project name + tag.
        assert 'class="project-name"' in src
        assert "esc(p.name)" in src
        assert "PLATFORM.headerTag" in src

    def test_footer_swaps_to_brand_when_trek_open(self):
        # Footer parity: same conditional, so the bottom-left strip
        # doesn't leak the project id while the user is in Trek detail.
        src = _read(WEB_INDEX)
        assert "getFooterLeftHTML: () => state.openTrekId" in src


# ---------------------------------------------------------------------------
# URL axis helpers exist and are wired into openTrek / closeTrek
# ---------------------------------------------------------------------------


class TestUrlAxisHelpers:
    def test_get_trek_id_from_url_helper_present(self):
        src = _read(WEB_INDEX)
        assert "function getTrekIdFromUrl()" in src
        assert "URLSearchParams(window.location.search).get('trek')" in src

    def test_set_url_for_trek_detail_uses_replace_state(self):
        # replaceState (not pushState) because previous URL is not a
        # meaningful Back target — same regime as the project URL sync.
        src = _read(WEB_INDEX)
        assert "function _setUrlForTrekDetail(trekId)" in src
        body_start = src.index("function _setUrlForTrekDetail")
        body_end = src.index("function _setUrlForProjectView")
        body = src[body_start:body_end]
        assert "history.replaceState" in body
        assert "params.set('trek'" in body

    def test_set_url_for_trek_detail_drops_project_param(self):
        # AC #2: Trek detail URL must be ?trek=<id> solo. No ?project=.
        src = _read(WEB_INDEX)
        body_start = src.index("function _setUrlForTrekDetail")
        body_end = src.index("function _setUrlForProjectView")
        body = src[body_start:body_end]
        # Body builds a fresh URLSearchParams and only writes the trek
        # key — no ``set('project'`` and no copy-from-existing.
        assert "new URLSearchParams()" in body, (
            "_setUrlForTrekDetail must start from an empty params dict so "
            "the project key is dropped, not preserved."
        )
        assert "set('project'" not in body

    def test_set_url_for_project_view_restores_project(self):
        src = _read(WEB_INDEX)
        assert "function _setUrlForProjectView(projectId)" in src
        body_start = src.index("function _setUrlForProjectView")
        # Take a slice large enough to cover the function body.
        body = src[body_start:body_start + 800]
        assert "history.replaceState" in body
        assert "params.set('project'" in body


class TestOpenCloseTrekWireUrl:
    def test_open_trek_calls_url_for_detail(self):
        src = _read(WEB_INDEX)
        # openTrek must invoke the URL helper so the address bar reflects
        # the Trek immediately after the state mutation.
        idx = src.index("async function openTrek(trekId)")
        body = src[idx:idx + 1200]
        assert "_setUrlForTrekDetail(trekId)" in body

    def test_close_trek_calls_url_for_project(self):
        src = _read(WEB_INDEX)
        idx = src.index("function closeTrek()")
        body = src[idx:idx + 800]
        assert "_setUrlForProjectView(state.projectId" in body


# ---------------------------------------------------------------------------
# State axes stay independent (= AC #4)
# ---------------------------------------------------------------------------


class TestProjectUrlSyncSkippedDuringTrek:
    def test_load_project_url_sync_skips_when_trek_open(self):
        # ms-43 e-1275's URL sync inside selectProject must NOT overwrite
        # ?trek=<id> while a Trek detail is open; otherwise a Settings
        # peek would silently flip the address bar back to a project URL.
        src = _read(WEB_INDEX)
        # Locate the e-1275 block.
        marker = "ms-43 e-1275: keep window.location.search in sync"
        idx = src.index(marker)
        block = src[idx:idx + 1500]
        assert "!state.openTrekId" in block, (
            "selectProject URL sync must guard on !state.openTrekId so "
            "Trek detail's address bar stays at ?trek=<id>."
        )


# ---------------------------------------------------------------------------
# Bootstrap parses ?trek= and honors it
# ---------------------------------------------------------------------------


class TestInitHonorsTrekParam:
    def test_init_reads_trek_param(self):
        src = _read(WEB_INDEX)
        idx = src.index("async function init()")
        body = src[idx:idx + 3000]
        assert "getTrekIdFromUrl()" in body, (
            "init() must read ?trek=<id> so a shared link opens Trek detail"
        )

    def test_init_opens_trek_after_project_load(self):
        src = _read(WEB_INDEX)
        idx = src.index("async function init()")
        # init() grew past 2200 chars as more startup steps landed; the
        # openTrek(urlTrekId) call now sits ~2400 chars in. Widen the slice
        # so this pin stays anchored to actual behavior rather than to a
        # stale offset.
        body = src[idx:idx + 3000]
        assert "openTrek(urlTrekId)" in body


# ---------------------------------------------------------------------------
# Tauri parity (= SHARED region regenerated by desktop/build.py)
# ---------------------------------------------------------------------------


class TestDesktopParityForSharedPieces:
    def test_desktop_dist_has_url_helpers(self):
        src = _read(DESKTOP_INDEX)
        assert "function getTrekIdFromUrl()" in src
        assert "function _setUrlForTrekDetail(trekId)" in src
        assert "function _setUrlForProjectView(projectId)" in src

    def test_desktop_dist_has_open_close_url_wiring(self):
        src = _read(DESKTOP_INDEX)
        assert "_setUrlForTrekDetail(trekId)" in src
        assert "_setUrlForProjectView(state.projectId" in src

    def test_web_only_project_url_sync_guard_present(self):
        # The ms-43 e-1275 URL sync block lives in the web-only
        # selectProject path (Tauri has its own load_project_json route
        # in desktop/layer.js with no URL bar to sync). The Trek guard
        # therefore only needs to land on the web side; Tauri's address
        # bar is decorative and the e-2253 follow-up will mirror the
        # state.openTrekId conditional there if it ever matters.
        src = _read(WEB_INDEX)
        marker = "ms-43 e-1275: keep window.location.search in sync"
        idx = src.index(marker)
        block = src[idx:idx + 1500]
        assert "!state.openTrekId" in block
