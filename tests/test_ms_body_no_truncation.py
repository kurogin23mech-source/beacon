"""ms-43 / e-2304 — pin tests for MS card body truncation fix.

Background — the Web UI's milestone card had:

    .ms-card.expanded .ms-body { max-height: 8000px; overflow: hidden; }

For high-volume milestones (ms-43 reached 160 entries), rendered height
exceeded 8000px and bottom entries became silently unreachable (= in the
DOM but clipped, no scroll). User dogfood (2026-06-23, e-2302 incident)
showed search hits not appearing in the expanded MS task list.

These tests pin the fix so future "simplify the CSS" edits cannot
silently re-introduce a clamp.

They are deliberately CSS-text-grep tests (same pattern as
test_treks_tab_ui.py) so they run with zero browser dependency.

ACs covered:
  AC #1 — ms-43 (and any) MS surface lets all entries reach DOM without
          a CSS max-height clamp on the body.
  AC #2 — same render path serves task / commit / PR entries (= they
          all flow through `renderEntry` under `.ms-entries`), so the
          fix automatically covers PR / commit lists in MS cards.
  AC #3 — decision rationale is in the CSS comment block (see
          `.ms-card.expanded .ms-body`): pagination / virtualize
          deferred, outer page scroll handles navigation. Aligns with
          ms-86 e-2226 ACTIVITY pagination plan (= avoid two-tier).
  AC #4 — `renderMilestoneCard` maps `entries.map(renderEntry)` without
          any slice/limit, so first / middle / last entries are all
          emitted as long as no CSS clamp is in place.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


class TestMsBodyNoMaxHeightClamp:
    """The expanded MS body must not have a finite max-height clamp."""

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_no_finite_max_height_on_expanded_body(self):
        """The `.ms-card.expanded .ms-body` rule must not pin a finite
        max-height (8000px, 5000px, any explicit pixel cap). It must be
        `none` so the body can grow to fit all entries."""
        # Capture the `.ms-card.expanded .ms-body { ... }` block.
        m = re.search(
            r"\.ms-card\.expanded\s+\.ms-body\s*\{([^}]*)\}",
            self.src,
        )
        assert m is not None, (
            "`.ms-card.expanded .ms-body` CSS rule disappeared — "
            "renderMilestoneCard relies on it to reveal the body."
        )
        block = m.group(1)
        # Must not contain a numeric px / em / rem cap.
        forbidden = re.search(
            r"max-height\s*:\s*\d+(?:px|em|rem|vh)\s*;",
            block,
        )
        assert forbidden is None, (
            "ms-43 / e-2304 regression: `.ms-card.expanded .ms-body` has "
            f"a finite max-height clamp: `{forbidden.group(0)}`. This will "
            "silently clip MS cards with many entries (ms-43 hit 160+ "
            "entries and exceeded the old 8000px cap). Use "
            "`max-height: none` so all entries are reachable; rely on the "
            "outer page scroll for navigation."
        )
        # Positive assertion: rule uses `none`.
        assert re.search(r"max-height\s*:\s*none\s*;", block), (
            "`.ms-card.expanded .ms-body` should set `max-height: none` "
            "to let the body grow naturally."
        )

    def test_no_overflow_hidden_on_expanded_body(self):
        """Even if max-height is generous, `overflow: hidden` on the
        expanded body would still clip. The expanded state must override
        the closed-state `overflow: hidden` with `visible`."""
        m = re.search(
            r"\.ms-card\.expanded\s+\.ms-body\s*\{([^}]*)\}",
            self.src,
        )
        assert m is not None
        block = m.group(1)
        # Negative: no overflow:hidden lingering on the expanded state.
        assert not re.search(r"overflow\s*:\s*hidden\s*;", block), (
            "Expanded MS body has `overflow: hidden`; this clips child "
            "entries below the visible area. Use `overflow: visible`."
        )


class TestMsEntriesRenderedWithoutSlice:
    """renderMilestoneCard must emit every entry, not a head-N slice."""

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_render_milestone_card_uses_full_entries_map(self):
        """The expanded-body branch in `renderMilestoneCard` must call
        `entries.map(renderEntry)` without a preceding `.slice(...)`
        truncation (= no `entries.slice(0, N).map(...)`)."""
        # Locate the renderMilestoneCard function body.
        start = self.src.find("function renderMilestoneCard(")
        assert start != -1, "renderMilestoneCard function missing"
        # Crude end-of-function detection — far enough to cover the body.
        body = self.src[start : start + 4000]
        # Negative: no truncating slice on `entries`.
        bad = re.search(r"entries\.slice\s*\(\s*0\s*,\s*\d+\s*\)", body)
        assert bad is None, (
            "renderMilestoneCard truncates entries with "
            f"`{bad.group(0)}` — this is the failure mode e-2304 fixed. "
            "Render every entry; rely on outer page scroll."
        )
        # Positive: full `.map(renderEntry)` is invoked on entries.
        assert "entries.map(renderEntry)" in body, (
            "renderMilestoneCard no longer maps over the full entries "
            "array; large MS cards will silently lose rows."
        )


class TestMsBodyTruncationFixIsDocumented:
    """The CSS rule must carry a comment explaining the decision so
    future edits don't blindly re-add a clamp. This is AC #3 (decision
    1-line) realised inline in the source where it is enforced."""

    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)

    def test_decision_comment_present_near_ms_body_rule(self):
        idx = self.src.find(".ms-card.expanded .ms-body")
        assert idx != -1
        # Look back ~600 chars for the e-2304 marker in a comment.
        preamble = self.src[max(0, idx - 800) : idx]
        assert "e-2304" in preamble, (
            "Decision comment for ms-43 / e-2304 is missing above the "
            "`.ms-card.expanded .ms-body` rule. Future edits need the "
            "rationale to avoid re-clamping."
        )
