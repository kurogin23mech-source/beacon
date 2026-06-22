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
        # ms-86 v2 (= mockups/trek-detail.html / e-2126): SCOPE + MEMBERS row
        # + CLAIMS placeholder + SUMMARIES were collapsed into the new
        # TREK TASKS / MEMBERS table / RECENT ACTIVITY (real) trio. Older
        # row classes intentionally dropped — assertions reflect the v2
        # invariants instead.
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
            ".trek-why-block",
            ".trek-task-row",
            ".trek-task-state",
            ".trek-task-children",
            ".trek-child-task",
            ".trek-members-table",
            ".trek-activity-row-v2",
            ".trek-cli-hint",
            ".trek-coming-soon",
        ):
            assert cls in self.src, f"CSS class {cls!r} missing"

    def test_detail_view_mock_sections(self):
        # ms-86 v2 section order (= mockups/trek-detail.html):
        # WHY → TREK TASKS → MEMBERS & AGENTS → RECENT ACTIVITY (5 sections only).
        # PULSE-ACK COMPLIANCE は ms-88 / e-2108 由来で mockup には無く、 user dogfood (2026-06-22)
        # で spec creep として撤去された (= AI / leader が server data を直接読めば足り、 human surface 不要)。
        for header in (
            "<span>WHY</span>",
            "<span>TREK TASKS</span>",
            "MEMBERS &amp; AGENTS",
            "<span>RECENT ACTIVITY</span>",
        ):
            assert header in self.src, f"section header {header!r} missing"
        # PULSE-ACK COMPLIANCE は撤去済 — もし再追加されたら mockup 整合を再評価
        assert "PULSE-ACK COMPLIANCE" not in self.src, (
            "PULSE-ACK COMPLIANCE は mockup スコープ外として 2026-06-22 に撤去済。 "
            "再追加するなら mockup 更新 + user 承認が必要"
        )

    def test_stop_card_invokes_cli(self):
        assert "STOP THIS TREK" in self.src
        assert "case 'trek-cli-hint':" in self.src

    def test_v2_task_state_machine_classes(self):
        # 5-state machine (= ms-88 / e-2107) must render with one CSS class
        # per state so the leader can scan badges at a glance.
        for state in ("working", "todo", "leader_review", "user_review", "done"):
            assert f".trek-task-state.{state}" in self.src, f"task-state.{state} missing"

    def test_v2_drops_placeholder_sections(self):
        # ACTIVE CLAIMS / SUMMARIES / SCOPE placeholders were intentionally
        # removed in v2. Match the rendered section header (= <span>X</span>)
        # so the assertion ignores narrative comments that still mention the
        # old names for historical context.
        assert "<span>ACTIVE CLAIMS</span>" not in self.src, "ACTIVE CLAIMS placeholder re-added"
        assert "<span>SUMMARIES</span>" not in self.src, "SUMMARIES placeholder re-added"
        assert "<span>SCOPE</span>" not in self.src, "SCOPE section re-added (use TREK TASKS)"


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
        # ms-86 v2 carry-over: same trek-* palette must survive the desktop
        # build so layer.js can render the new layout when it adopts the
        # 4-section treatment.
        for cls in (".trek-crumb", ".trek-head", ".trek-stop-card",
                    ".trek-task-row", ".trek-members-table",
                    ".trek-why-block", ".trek-activity-row-v2"):
            assert cls in self.src, f"CSS class {cls!r} missing"


# ---------------------------------------------------------------------------
# ms-86 v2 bug fixes (= 2026-06-22 dogfood) — pin behaviours so simplify
# passes don't silently regress them again.
# ---------------------------------------------------------------------------


class TestWebUI_TrekHeaderHidesProjectChrome:
    """e-2219: Trek detail page ヘッダから project 名 / project tag / version
    badge を隠す。 Trek は cross-project entity であり project に従属しない。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_header_branches_on_open_trek(self):
        # renderShell must branch on openTrekId so the project chrome is
        # suppressed when a trek detail page is on screen.
        assert "const trekOpen = !!state.openTrekId;" in self.src
        # Brand fallback used when trekOpen — mockup top-bar shows just "Beacon".
        assert '<span class="project-name brand">Beacon</span>' in self.src

    def test_project_chrome_still_rendered_when_no_trek(self):
        # The original project-name + header-tag + version-badge cluster
        # must still be reachable for the non-trek path.
        assert 'esc(PLATFORM.headerTag)' in self.src
        assert 'PLATFORM.versionBadgeHTML()' in self.src


class TestWebUI_TrekProjectSwitchClearsOpenTrek:
    """e-2220 (ms-43): Trek detail を開いた状態から hamburger menu 経由で project
    を選び直したり、 同じ project を再選択した時に main content が project
    page に戻ること (= state.openTrekId が解除される routing 経路)。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_menu_select_project_clears_open_trek(self):
        # menuSelectProject 内で state.openTrekId を明示クリアしている必要がある
        # (= 別 project 選択時 / 同 project 再選択時の両方を構造的にカバー)。
        assert "function menuSelectProject(" in self.src
        # state.openTrekId = null が menuSelectProject 配下に必ず存在する。
        idx = self.src.index("async function menuSelectProject(")
        end = self.src.index("\n}\n", idx)
        body = self.src[idx:end]
        assert "state.openTrekId = null" in body, (
            "menuSelectProject must reset state.openTrekId so trek detail "
            "doesn't bleed across project switches (e-2220 / ms-43)"
        )

    def test_select_project_clears_open_trek(self):
        # selectProject 自体も openTrekId を解除する (= URL / refresh / initial
        # bootstrap 経由の project 選択でも trek 状態が漏れないため)。
        idx = self.src.index("async function selectProject(projectId)")
        end = self.src.index("\nfunction toggleMilestone", idx)
        body = self.src[idx:end]
        assert "state.openTrekId = null" in body, (
            "selectProject must reset state.openTrekId so any project-load "
            "path drops the open trek (e-2220)"
        )


class TestWebUI_TrekStopCardMaxWidth:
    """e-2221: STOP card は画面幅の半分程度 (= mockup L137-141 halt-block の
    max-width 700px) で center 揃え。 leaf width で読みやすい形にする。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_stop_card_has_max_width(self):
        # CSS 矩形 .trek-stop-card { ... } 内に max-width が含まれる。
        idx = self.src.index(".trek-stop-card {")
        end = self.src.index("}", idx)
        block = self.src[idx:end]
        assert "max-width" in block, (
            ".trek-stop-card must have max-width so it doesn't span full leaf "
            "width (e-2221 / mockup halt-block max-width: 700px)"
        )


class TestWebUI_TrekChevronAccordionWired:
    """e-2222: TREK TASKS の MS / Op 行 chevron クリックで配下 task が accordion
    展開される。 既存 _trekLookupMilestoneInProject helper を流用し、 user の
    expand 状態は state.openTrekExpanded で保持する。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_scope_toggle_action_handler_present(self):
        # data-action="trek-scope-toggle" が chevron に紐づき、 dispatcher
        # 側の handleCommonAction で case 処理されている。
        assert 'data-action="trek-scope-toggle"' in self.src
        assert "case 'trek-scope-toggle':" in self.src

    def test_open_trek_expanded_state_field(self):
        # 既存 state.openTrekExpanded (= Set<string>) が dispatcher で
        # ms-id を toggle する経路を持つ。
        assert "openTrekExpanded: new Set()" in self.src


class TestWebUI_TrekTaskLeafOpensEntryModal:
    """e-2223: task leaf 行クリックで既存 entry-detail-modal が overlay 表示
    される。 既存 openEntryDetail / entry-detail-modal 機構をそのまま流用し、
    leaf 行に data-action="open-entry-detail" + data-entry-id を渡す。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_leaf_row_wires_open_entry_detail(self):
        # leaf task row 描画関数 (_renderTrekTaskRow の s.task 分岐) で
        # open-entry-detail action と data-entry-id が出力されていること。
        idx = self.src.index("// Leaf task row.")
        end = self.src.index("// Operation row", idx)
        leaf_block = self.src[idx:end]
        assert 'data-action="open-entry-detail"' in leaf_block, (
            "leaf task row must wire open-entry-detail action so clicking "
            "opens the existing entry-detail-modal overlay (e-2223)"
        )
        assert 'data-entry-id=' in leaf_block

    def test_open_entry_detail_handler_still_dispatched(self):
        # handleCommonAction の既存 case を流用する想定。 リネームされていない
        # ことを確認することで wiring が空振りしない構造を pin する。
        assert "case 'open-entry-detail':" in self.src


class TestWebUI_TrekShowDoneToggle:
    """e-2224: show done ボタンが accordion 展開中に表示され、 click で done
    状態 task が inline 追加表示される (toggle)。 toggle 状態は MS id 単位で
    state.openTrekShowDone (Set) に保持し、 別 Trek / 別 MS で混じらない。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_show_done_button_rendered(self):
        # show-done ボタンは class="trek-task-show-done-btn" でレンダリングされ、
        # data-action="trek-toggle-done" を持つ。
        assert 'class="trek-task-show-done-btn"' in self.src
        assert 'data-action="trek-toggle-done"' in self.src

    def test_show_done_dispatcher_case_present(self):
        # dispatcher 側で case 'trek-toggle-done' が登録されていない限り
        # クリックは無視されるので、 配線済みであることを構造的に pin。
        assert "case 'trek-toggle-done':" in self.src

    def test_show_done_state_is_set_keyed_by_ms(self):
        # ms-id 単位の toggle 保持。 boolean 1 つだと別 MS の done 表示が
        # 同時に切り替わってしまうので、 Set 化されていることを pin。
        assert "openTrekShowDone: new Set()" in self.src
