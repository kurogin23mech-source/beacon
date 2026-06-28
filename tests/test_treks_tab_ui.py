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
# Project-tab Treks button — REMOVED in ms-97 / e-2606 (Phase 1b UI 復元)。
# SPEC tnJnByg8Ymw8T3DgPdJD AC1/AC3/AC4 で 「Trek は project と独立な
# first-class object なので project 階層配下に置かない」 が確定し、 e-2251
# の真逆実装は revert された。 hamburger menu top-level の Treks セクション
# (= TestWebUI_Sidebar 配下) が唯一の入口。 ms-95 e-2441 で
# TreksTabRemoved → TreksTabPresent に flip した assertions を、 今回 ms-97
# e-2606 で再度 TreksTabRemoved に戻す。
# ---------------------------------------------------------------------------

class TestWebUI_TreksTabRemoved:
    """ms-97 / e-2606: SPEC tnJnByg8Ymw8T3DgPdJD AC1/AC3/AC4 で project tab bar
    から Treks タブを削除し、 hamburger menu top-level に Treks セクションを
    復活させる方向に確定。 e-2251 の真逆実装を revert した。 hamburger Treks
    セクションが唯一の入口なので、 「タブが無い」 ことを構造的に pin する。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_treks_tab_button_removed(self):
        assert 'data-tab="treks"' not in self.src, (
            "Treks tab button must not be in the project tab-bar (ms-97 / e-2606)"
        )

    def test_treks_tab_render_branch_removed(self):
        assert "state.activeTab === 'treks'" not in self.src, (
            "activeTab === 'treks' render branch must be removed (ms-97 / e-2606)"
        )

    def test_treks_switch_tab_branch_removed(self):
        assert "if (tab === 'treks')" not in self.src, (
            "switch-tab dispatcher must not handle the treks tab anymore "
            "(ms-97 / e-2606 — list view moved to hamburger menu)"
        )


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
        # WHY → TREK TASKS → MEMBERS & AGENTS → ACTIVITY (5 sections only).
        # ms-86 / e-2226: section header was renamed from RECENT ACTIVITY to
        # ACTIVITY (= each row carries trek-local session ref + task id +
        # state-transition note, "recent" became misleading once show-all
        # toggle was added). Test follows the rename (ms-95 e-2441).
        # PULSE-ACK COMPLIANCE は ms-88 / e-2108 由来で mockup には無く、 user dogfood (2026-06-22)
        # で spec creep として撤去された (= AI / leader が server data を直接読めば足り、 human surface 不要)。
        for header in (
            "<span>WHY</span>",
            "<span>TREK TASKS</span>",
            "MEMBERS &amp; AGENTS",
            "<span>ACTIVITY</span>",
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

    def test_treks_tab_removed(self):
        # ms-97 / e-2606: SPEC tnJnByg8Ymw8T3DgPdJD AC1/AC3/AC4 で project tab
        # bar から Treks タブを削除し、 hamburger menu top-level に移した。
        # desktop bundle (= build.py が server/static/index.html から自動生成)
        # も同期して tab が無いこと、 を構造的に pin する。
        assert 'data-tab="treks"' not in self.src, (
            "desktop bundle must not carry the Treks tab (ms-97 / e-2606)"
        )

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
        # show-done ボタンは class="trek-task-show-done-btn..." で render される
        # (= hide-mode class が showDone=true 時に suffix される、 base class 名で grep)。
        assert 'class="trek-task-show-done-btn' in self.src
        assert 'data-action="trek-toggle-done"' in self.src

    def test_show_done_dispatcher_case_present(self):
        # dispatcher 側で case 'trek-toggle-done' が登録されていない限り
        # クリックは無視されるので、 配線済みであることを構造的に pin。
        assert "case 'trek-toggle-done':" in self.src

    def test_show_done_state_is_set_keyed_by_ms(self):
        # ms-id 単位の toggle 保持。 boolean 1 つだと別 MS の done 表示が
        # 同時に切り替わってしまうので、 Set 化されていることを pin。
        assert "openTrekShowDone: new Set()" in self.src

    def test_show_done_button_has_hide_mode_class_when_expanded(self):
        # done タスク表示中 (= showDone=true) はボタンに hide-mode class が付与され
        # gray にスタイル切替される。 done タスクと視覚揃え (= 2026-06-23 user 指摘)。
        assert "hide-mode" in self.src
        assert ".trek-task-show-done-btn.hide-mode" in self.src

    def test_done_child_task_tag_grayed_out(self):
        # done タスクの TASK タグは grayout 専用 CSS が当たる (= done と todo を
        # 視覚分離、 2026-06-23 user 指摘)。
        assert '.trek-child-task[data-task-state="done"] .trek-child-task-tag' in self.src


class TestWebUI_TrekFooterHidesProjectId:
    """2026-06-23 dogfood: e-2219 で header の project name 系は隠したが、 footer
    の `state.projectId` (= beacon-b95643 等) が残っており 「いまだに project
    名が見える」 と user 報告。 Trek 詳細を開いている時は footer も brand
    「Beacon」 に置換し、 state.project / state.projectId 関連表示を全 surface
    で抑制する。 mockup の footer 文言とは差分が出るが、 mockup より
    cross-project entity としての一貫性を優先 (= user 指示)。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_footer_left_branches_on_open_trek(self):
        # PLATFORM.getFooterLeftHTML が state.openTrekId で brand fallback する。
        idx = self.src.index("getFooterLeftHTML:")
        end = self.src.index(",", idx)
        block = self.src[idx:end]
        assert "state.openTrekId" in block, (
            "PLATFORM.getFooterLeftHTML must branch on state.openTrekId so the "
            "footer doesn't leak project id while a trek detail page is open "
            "(2026-06-23 dogfood)"
        )
        assert "Beacon" in block, (
            "PLATFORM.getFooterLeftHTML must show brand 'Beacon' fallback for "
            "open trek (mirrors header brand swap)"
        )

    def test_non_trek_footer_still_shows_project_id(self):
        # 通常 (non-trek) 経路では state.projectId が表示される (= retro / dashboard
        # 視点で 「今どの project に居るか」 を判別するため、 mockup footer と同じ)。
        idx = self.src.index("getFooterLeftHTML:")
        end = self.src.index(",", idx)
        block = self.src[idx:end]
        assert "state.projectId" in block, (
            "non-trek path must still surface state.projectId so users keep "
            "the project context when they aren't inside a trek"
        )


class TestWebUI_TrekMembersTableSessionFullColumn:
    """2026-06-23 dogfood: MEMBERS & AGENTS table の session 列を 2 列に分割。
    既存 s-N badge (= session-local) と並んで raw session id 短縮形
    (= sv-77...a648e110) を別列で表示し、 leader / member が自分の raw sid を
    1 列で読めるようにする。 click で clipboard コピーは両セル共通。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_thead_has_five_columns(self):
        # thead の <th> ラベル順序 (user / session / session id / role / task)
        # を render 文字列内で構造的に pin する。
        thead_marker = "<table class=\"trek-members-table\">"
        idx = self.src.index(thead_marker)
        end = self.src.index("</thead>", idx)
        block = self.src[idx:end]
        for label in ("<th>user</th>", "<th>session</th>", "<th>session id</th>",
                      "<th>role</th>", "<th>task</th>"):
            assert label in block, f"thead missing column header {label!r}"

    def test_separate_session_local_and_full_cells(self):
        # td 単位で session-local-cell と session-full-cell が render される
        # (= 1 td に span 2 つ並べる従来案ではなく、 5 列 table に再構築)。
        assert 'class="session-local-cell"' in self.src
        assert 'class="session-full-cell"' in self.src

    def test_session_full_cell_is_clickable_for_copy(self):
        # session-full cell にも copy-trek-sid action が紐づき、 既存の
        # handleCommonAction 'copy-trek-sid' case で clipboard コピーが走る。
        assert "session-full-clickable" in self.src
        assert 'data-action="copy-trek-sid"' in self.src
        assert "case 'copy-trek-sid':" in self.src

    def test_session_full_cell_uses_short_session_id_helper(self):
        # session-full セルは _trekShortSessionId helper で短縮済 raw sid を
        # 表示する (= 既存 helper を再利用し、 短縮ルール一元化)。
        # ms-86 / e-2225 — comment marker rewrite に追従、 構造的 anchor
        # (= _renderTrekMembersTable の body 全体) で block を取り直す。
        fn_marker = "function _renderTrekMembersTable("
        idx = self.src.index(fn_marker)
        # End at the closing of the same function — find the next top-level
        # ``function `` declaration after ``fn_marker``.
        end = self.src.index("\nfunction ", idx + len(fn_marker))
        block = self.src[idx:end]
        assert "_trekShortSessionId(fullSid)" in block, (
            "session-full cell must reuse _trekShortSessionId helper so the "
            "shortening rule (sv-77e815…a648e110) stays consistent"
        )

    def test_mockup_synced_to_five_columns(self):
        # mockup 自体も 5 列構造に同期されている (= visual reference と実装の
        # drift を構造的に防ぐ)。
        mockup = _read(REPO_ROOT / "mockups" / "trek-detail.html")
        idx = mockup.index('<table class="members-table">')
        end = mockup.index("</thead>", idx)
        block = mockup[idx:end]
        for label in ("<th>user</th>", "<th>session</th>", "<th>session id</th>",
                      "<th>role</th>", "<th>task</th>"):
            assert label in block, f"mockup thead missing {label!r}"


# ---------------------------------------------------------------------------
# ms-97 / e-2606..e-2609 (Phase 1b UI 復元) — SPEC tnJnByg8Ymw8T3DgPdJD の
# UI/動線系 AC を構造的に pin する。 e-2251 で merge された真逆実装の
# revert + leader_session_id / project-wide warning / scope approval surface
# の 3 件を追加。
# ---------------------------------------------------------------------------


class TestWebUI_TreksHamburgerSectionWired:
    """ms-97 / e-2606 (AC3 / AC4): hamburger menu top-level に Treks セクション
    が復活し、 各行 click で trek detail に直接遷移する経路が wire 済み。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_hamburger_has_treks_section_title(self):
        assert '<div class="menu-section-title">Treks</div>' in self.src, (
            "hamburger menu must have a top-level Treks section "
            "(ms-97 / e-2606 AC3)"
        )

    def test_hamburger_treks_list_container_present(self):
        assert 'id="menu-treks-list"' in self.src, (
            "hamburger Treks section must render into #menu-treks-list "
            "for lazy fill (ms-97 / e-2606)"
        )

    def test_menu_open_trek_action_registered(self):
        assert 'data-action="menu-open-trek"' in self.src, (
            "hamburger Trek rows must wire data-action='menu-open-trek' "
            "so click opens trek detail (ms-97 / e-2606)"
        )
        assert "case 'menu-open-trek':" in self.src

    def test_open_trek_lazy_loads_treks_when_empty(self):
        # openTrek() が直接呼ばれた経路 (= shared link / trek-jump-from-widget)
        # で state.treks 未 load のままだと _renderTrekDetail が「Trek not found」
        # を出してしまうため、 openTrek 自身が lazy load する。
        idx = self.src.index("async function openTrek(")
        end = self.src.index("\nfunction closeTrek(", idx)
        block = self.src[idx:end]
        assert "dataSource.loadTreks()" in block, (
            "openTrek must lazy-load state.treks when empty so direct entries "
            "(shared link / widget) don't show 'Trek not found' (ms-97 / e-2606)"
        )


class TestWebUI_TrekListCrossProject:
    """ms-97 / e-2607 (AC2): hamburger Treks list が 「user が creator or
    member の全 Trek」 を出す。 project context 切替で list 内容不変、
    created_at 降順 sort を client 側でも defensive に固定。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_no_project_filter_on_trek_list(self):
        # hamburger Treks section の render 関数 (= renderMenu 内の closure)
        # は state.projectId を参照しない。
        idx = self.src.index("const renderTreksSection = () => {")
        end = self.src.index("};", idx)
        block = self.src[idx:end]
        assert "state.projectId" not in block, (
            "hamburger renderTreksSection must not filter by state.projectId "
            "— Trek is project-independent (ms-97 / e-2607 AC2)"
        )

    def test_default_sort_is_created_at_desc(self):
        # closure 内で sort 比較関数が created_at で並び替えていること。
        idx = self.src.index("const renderTreksSection = () => {")
        end = self.src.index("};", idx)
        block = self.src[idx:end]
        assert "created_at" in block and ".sort(" in block, (
            "hamburger renderTreksSection must explicitly sort by created_at "
            "for defensive client-side ordering (ms-97 / e-2607 AC2)"
        )


class TestWebUI_TrekDetailLeaderRow:
    """ms-97 / e-2608 (AC5): Trek detail に leader_session_id を 1 行表示する。
    request-invite の宛先 / leader_review 権限の所在を user / 他 session が
    視認できる経路。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_leader_row_html_present(self):
        assert "trek-leader-row" in self.src
        assert "trek-leader-sid" in self.src
        assert "trek-leader-label" in self.src

    def test_leader_row_uses_leader_session_id_field(self):
        # 直前で leaderSid を t.leader_session_id から引いている。
        idx = self.src.index("const leaderSid = t.leader_session_id")
        # leader 不在 (= 空文字列) の場合は行ごと省略する三項分岐がある。
        idx2 = self.src.index("leaderRowHtml", idx)
        block = self.src[idx2 : idx2 + 400]
        assert "leaderSid" in block, "leaderRowHtml must branch on leaderSid"


class TestWebUI_TrekProjectWideWarning:
    """ms-97 / e-2608 (AC8 / AC31): project-wide scope (= narrowing key なし) を
    持つ既存 trek に warning bar を出す。 SPEC AC7 で新規追加は reject される
    が、 既存 trek は grandfathered なので cleanup 誘導を UI 側で促す。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_warning_bar_css_present(self):
        for cls in (".trek-warning-bar", ".trek-warning-chip"):
            assert cls in self.src, f"CSS class {cls!r} missing"

    def test_warning_bar_filters_project_wide_entries(self):
        idx = self.src.index("const projectWideEntries = scope.filter(")
        end = self.src.index(");", idx)
        block = self.src[idx:end]
        # narrowing key 3 種をすべて欠くものを project-wide と判定する。
        for key in ("milestone", "operation", "task", "project"):
            assert key in block, (
                f"project-wide filter must consider {key} key "
                "(ms-97 / e-2608 AC8)"
            )


class TestWebUI_TrekScopeApprovalSurface:
    """ms-97 / e-2609 (AC23 / AC25): trek doc の pending_scope_changes を
    1 click で approve / reject する surface を Trek detail に追加する。
    backend (= e-2611) 未着地時は空 array で section が完全省略される
    progressive disclosure 設計。
    """

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_pending_section_helper_present(self):
        assert "function _renderTrekScopeApprovals(" in self.src

    def test_pending_section_header_present(self):
        assert "PENDING SCOPE CHANGES" in self.src

    def test_approve_and_reject_actions_registered(self):
        for action in ("trek-scope-approve", "trek-scope-reject"):
            assert f'data-action="{action}"' in self.src, (
                f"data-action='{action}' must be wired in pending row "
                "(ms-97 / e-2609 AC23/AC25)"
            )
            assert f"case '{action}':" in self.src, (
                f"handleAction must dispatch '{action}' case"
            )

    def test_pending_section_omitted_when_field_empty(self):
        # helper の早期 return: pending.length === 0 → 空文字列。
        idx = self.src.index("function _renderTrekScopeApprovals(")
        end = self.src.index("\nfunction _renderTrekDetail(", idx)
        block = self.src[idx:end]
        assert "if (pending.length === 0) return ''" in block, (
            "_renderTrekScopeApprovals must return '' when pending list is "
            "empty so the section disappears (progressive disclosure, "
            "ms-97 / e-2609)"
        )

    def test_pending_row_carries_pending_id(self):
        # approve / reject button の data-pending-id を経由して server に
        # 1 click で id が渡る経路を pin する。
        assert 'data-pending-id="' in self.src

    def test_approve_endpoint_posts_to_scope_approve(self):
        # handleAction の trek-scope-approve case で /scope-approve エンドポイント
        # を POST 経由で叩いていることを構造的に pin (= endpoint 名が drift しない
        # ように tied)。
        idx = self.src.index("case 'trek-scope-approve':")
        end = self.src.index("case 'trek-scope-reject':", idx)
        block = self.src[idx:end]
        assert "/scope-approve" in block and "POST" in block

    def test_reject_endpoint_posts_to_scope_reject(self):
        idx = self.src.index("case 'trek-scope-reject':")
        # 大きめにブロック取って、 次の case まで。
        end = self.src.index("    case 'trek-cli-hint':", idx)
        block = self.src[idx:end]
        assert "/scope-reject" in block and "POST" in block
