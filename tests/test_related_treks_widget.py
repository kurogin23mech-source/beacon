"""Related Treks widget presence on work-item detail (ms-69 / e-1664).

The widget is rendered inline on:
  - milestone expanded body (= ms-card body)
  - operation card (= renderOp())
  - task entry modal (= renderEntryModal())

When clicked, the widget jumps the user to the Treks tab and opens the
matching trek (= trek-jump-from-widget action).

Like the e-1659 tab check, this verifies presence in the source bundle
rather than rendering a headless browser. The Tauri bundle should mirror
the same widget calls (renderRelatedTreksWidget appears in renderMilestoneCard
expanded body, renderOp card, renderEntryModal).
"""

from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"


def _read(path):
    return path.read_text(encoding="utf-8")


class TestWebUI_RelatedTreksWidget:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_widget_render_function_present(self):
        assert "function renderRelatedTreksWidget(" in self.src

    def test_state_cache_initialised(self):
        # Cache is a plain dict keyed by 'ms:..' / 'op:..' / 'task:..'.
        assert "relatedTreks: {}," in self.src

    def test_dataSource_loader_present(self):
        assert "loadRelatedTreks: async" in self.src
        # Each item kind routes to a distinct REST path.
        assert "milestones" in self.src
        assert "operations" in self.src
        assert "entries" in self.src

    def test_widget_invoked_in_milestone_body(self):
        # renderMilestoneCard's expanded body calls
        # renderRelatedTreksWidget('milestone', ms.id) so the widget
        # appears in the expanded ms-card.
        assert "renderRelatedTreksWidget('milestone', ms.id)" in self.src

    def test_widget_invoked_in_operation_card(self):
        assert "renderRelatedTreksWidget('operation', op.id)" in self.src

    def test_widget_invoked_in_entry_modal(self):
        assert "renderRelatedTreksWidget('task', entry.id)" in self.src

    def test_toggle_ms_loads_related_treks(self):
        # The toggle-ms handler kicks off loadRelatedTreks lazily on the
        # first expand of a milestone.
        assert "loadRelatedTreks('milestone'" in self.src

    def test_open_entry_detail_loads_related_treks(self):
        assert "loadRelatedTreks('task'" in self.src

    def test_operations_tab_warms_related_treks(self):
        # switchTab('operations') pre-warms cache for every op on the project.
        assert "loadRelatedTreks('operation'" in self.src

    def test_widget_jump_action_handler(self):
        # Click handler that switches to Treks tab + opens the trek.
        assert "case 'trek-jump-from-widget':" in self.src
        assert "switchTab('treks').then" in self.src


class TestDesktopUI_RelatedTreksWidget:
    """Mirror the most important pins on the Tauri bundle so the widget
    can't silently regress in Desktop."""

    def setup_method(self, _method):
        self.src = _read(DESKTOP_INDEX)

    def test_widget_render_function_present(self):
        assert "function renderRelatedTreksWidget(" in self.src

    def test_widget_invoked_in_milestone_body(self):
        assert "renderRelatedTreksWidget('milestone', ms.id)" in self.src

    def test_widget_invoked_in_operation_card(self):
        assert "renderRelatedTreksWidget('operation', op.id)" in self.src

    def test_widget_invoked_in_entry_modal(self):
        assert "renderRelatedTreksWidget('task', entry.id)" in self.src

    def test_jump_action_present(self):
        assert "case 'trek-jump-from-widget':" in self.src
