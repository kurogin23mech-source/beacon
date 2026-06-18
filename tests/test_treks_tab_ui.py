"""Web UI navigation tests for the v3 Trek refactor (ms-69 / e-1659 v3).

Background — v1 (initial e-1659) added a "Treks" tab on the project page.
v2 reshaped the trek detail view to mock-faithful sections but kept the
tab. v3 (this version) realises the mock's actual navigation:

  * Treks live in the **sidebar** (= MENU / PROJECTS / TREKS / ONLINE AGENTS),
    not as a project-level tab.
  * The header gets a **Settings button** and an **Avatar dropdown**.
  * **Settings overlay** has two tabs: Projects & Members / Agents & Treks.
  * Trek detail is opened from the sidebar and replaces the project view
    full-page (not embedded in any tab).

These tests pin the structural pieces so future "simplify" edits can't
silently regress the mock navigation.
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"


def _read(path):
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# v1/v2 tab pollution must be gone (= the original Treks tab on project page)
# ---------------------------------------------------------------------------

class TestWebUI_TreksTabRemoved:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_no_treks_tab_button(self):
        assert 'data-tab="treks"' not in self.src, (
            "Treks tab button should have been removed from tab-bar (v3 refactor)"
        )

    def test_no_treks_tab_render_branch(self):
        assert "state.activeTab === 'treks'" not in self.src, (
            "activeTab === 'treks' branch should have been removed"
        )

    def test_no_treks_switch_tab_branch(self):
        assert "if (tab === 'treks')" not in self.src


# ---------------------------------------------------------------------------
# Trek state + dataSource (still needed; reused by sidebar + detail page)
# ---------------------------------------------------------------------------

class TestWebUI_TrekState:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_state_fields_initialised(self):
        for field in (
            "treks: [],",
            "openTrekId: null,",
            "openTrekDocs: [],",
            "onlineSessions: [],",
            "accountMenuOpen: false,",
            "settingsOpen: false,",
            "settingsTab: 'members',",
        ):
            assert field in self.src, f"state field {field!r} missing"

    def test_data_source_methods(self):
        for fn in (
            "loadTreks: async",
            "loadTrekDocs: async",
            "loadOnlineSessions: async",
        ):
            assert fn in self.src, f"dataSource.{fn} missing"

    def test_detail_render_function_present(self):
        assert "function _renderTrekDetail(" in self.src
        # Trek detail is now opened at top of renderMainContent (= full-page
        # mode, not via the project tab dispatcher).
        assert "if (state.openTrekId) {" in self.src


# ---------------------------------------------------------------------------
# Header chrome: Settings button + Avatar + account-menu
# ---------------------------------------------------------------------------

class TestWebUI_HeaderChrome:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_settings_button_in_header(self):
        assert 'class="header-settings-btn"' in self.src
        assert 'data-action="open-settings"' in self.src

    def test_avatar_in_header(self):
        assert 'class="header-avatar"' in self.src
        assert 'data-action="toggle-account-menu"' in self.src

    def test_account_menu_block(self):
        # Dropdown shown when accountMenuOpen state is true.
        assert 'class="account-menu' in self.src
        assert "account-menu-head" in self.src
        assert "account-menu-email" in self.src
        assert "Sign out" in self.src


# ---------------------------------------------------------------------------
# Sidebar 4-section nav (= mock TREKS + ONLINE AGENTS not just projects/account)
# ---------------------------------------------------------------------------

class TestWebUI_Sidebar:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_sidebar_has_projects_section(self):
        # The existing "Projects" section is preserved.
        assert '>Projects<' in self.src

    def test_sidebar_has_treks_section(self):
        assert '>Treks<' in self.src
        # Click a sidebar trek → menu-open-trek action.
        assert "case 'menu-open-trek':" in self.src

    def test_sidebar_has_online_agents_section(self):
        assert '>Online Agents<' in self.src
        # Live session rows use the mock palette class.
        assert "sidebar-agent-row" in self.src


# ---------------------------------------------------------------------------
# Settings overlay (2 tabs)
# ---------------------------------------------------------------------------

class TestWebUI_SettingsOverlay:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_settings_root_present(self):
        assert 'id="settings-root"' in self.src
        assert 'class="settings-overlay"' in self.src

    def test_two_tabs_wired(self):
        assert "Projects &amp; Members" in self.src
        assert "Agents &amp; Treks" in self.src
        assert "case 'settings-set-tab':" in self.src

    def test_close_handler(self):
        assert "case 'close-settings':" in self.src

    def test_render_functions_present(self):
        for fn in (
            "function renderSettings(",
            "function _renderSettingsMembersTab(",
            "function _renderSettingsAgentsTab(",
            "function openSettings(",
            "function closeSettings(",
            "function toggleAccountMenu(",
        ):
            assert fn in self.src, f"function {fn!r} missing"

    def test_agents_table_present_in_agents_tab_template(self):
        assert 'class="settings-agents-table"' in self.src


# ---------------------------------------------------------------------------
# Trek detail view — mock parity (carried over from v2 with the same shape)
# ---------------------------------------------------------------------------

class TestWebUI_TrekDetailMockParity:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_palette_classes_present(self):
        for cls in (
            ".trek-crumb",
            ".trek-head",
            ".trek-title-row",
            ".trek-badge",
            ".trek-stats",
            ".trek-meta",
            ".trek-stop-card",
            ".trek-archive-card",
            ".trek-section",
            ".trek-scope-row",
            ".trek-member-row",
            ".trek-claim-row",
            ".trek-activity-row",
            ".trek-summary-row",
            ".trek-cli-hint",
            ".trek-coming-soon",
        ):
            assert cls in self.src, f"CSS class {cls!r} missing"

    def test_detail_view_mock_sections(self):
        for header in (
            "SCOPE",
            "MEMBERS &amp; SESSIONS",
            "ACTIVE CLAIMS",
            "RECENT ACTIVITY",
            "SUMMARIES",
        ):
            assert header in self.src

    def test_stop_card_invokes_cli(self):
        assert "STOP THIS TREK" in self.src
        assert "case 'trek-cli-hint':" in self.src

    def test_unimplemented_sections_placeholders(self):
        assert "ms-55" in self.src
        assert "e-1696" in self.src
        assert ("e-1697" in self.src) or ("beacon morning" in self.src)
        assert ("e-1698" in self.src) or ("beacon trek summary" in self.src)


# ---------------------------------------------------------------------------
# Desktop bundle: structural mirrors of the same surface
# ---------------------------------------------------------------------------

class TestDesktopUI_v3Parity:
    def setup_method(self, _method):
        self.src = _read(DESKTOP_INDEX)

    def test_no_treks_tab(self):
        assert 'data-tab="treks"' not in self.src

    def test_header_chrome_present(self):
        for s in (
            'class="header-settings-btn"',
            'class="header-avatar"',
            'class="account-menu',
        ):
            assert s in self.src, f"{s!r} missing from desktop bundle"

    def test_sidebar_agent_row_css_carried(self):
        # The sidebar TREKS / ONLINE AGENTS render functions live in
        # desktop/layer.js (= Tauri has its own renderMenu); only the CSS
        # is shared via the build. Verify the palette survives so that when
        # layer.js gets the same 4-section treatment (follow-up task), it
        # has the right styles to render against.
        assert "sidebar-agent-row" in self.src

    def test_settings_overlay_root_present(self):
        # build.py mounts <div id="settings-root"> into Tauri body. The
        # renderSettings function is in the shared region (= callable from
        # any future layer.js Settings button), so the panel can paint.
        assert 'id="settings-root"' in self.src
        # Render functions came across in shared JS.
        assert "function renderSettings(" in self.src

    def test_trek_detail_palette_carried(self):
        for cls in (".trek-crumb", ".trek-head", ".trek-stop-card",
                    ".trek-scope-row", ".trek-member-row"):
            assert cls in self.src, f"CSS class {cls!r} missing"
