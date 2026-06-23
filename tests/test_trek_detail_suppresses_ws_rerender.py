"""Trek detail must suppress WS-driven re-render (ms-86 / e-2270).

2026-06-23 dogfood observed that after PR #196 (e-2250) landed, opening
a Trek detail produced visible flicker on the screen. Root cause: every
WebSocket ``project`` frame arrives at ``ws.onmessage`` → diff check →
``renderOnDataChange()`` → ``renderMsListArea()`` → ``#ms-list-container``
not found (= Trek view, not dashboard) → fall through to ``render()`` →
the entire shell + main DOM is rewritten on every push.

Trek detail is independent of ``state.project`` (= it reads from
``state.openTrekScope``, the cross-project aggregate fetched once by
``openTrek``, per e-2240 / e-2249). So a project frame for the
background project carries no Trek-relevant delta and should not
trigger a re-render. The fix is an early-return guard at the top of
``renderOnDataChange()``.

This file pins the guard structurally so a future refactor of the
render dispatch can't silently remove it and reintroduce the flicker.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_function_body(src: str, signature: str) -> str:
    start = src.index(signature)
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


class TestRenderOnDataChangeSkipsWhenTrekOpen:
    def test_web_ui_has_early_return_at_top(self):
        src = _read(WEB_INDEX)
        body = _extract_function_body(src, "function renderOnDataChange()")
        # The guard must appear before the body's first conditional / call so
        # WS pushes can't fall through to renderMsListArea / render() while
        # Trek detail is open. We look for the literal early-return idiom
        # the fix uses.
        guard_idx = body.find("if (state.openTrekId) return;")
        assert guard_idx != -1, (
            "renderOnDataChange must early-return when state.openTrekId is "
            "truthy (= e-2270 ちらつき修正). Trek detail is independent of "
            "state.project and a WS project frame carries no Trek-relevant "
            "delta."
        )
        # Guard must come BEFORE the actual render dispatch call (= `render()`
        # or `renderMsListArea()` invocations, paren-suffixed), not after —
        # otherwise the flicker still happens before we bail out. Identifier
        # references inside the leading comment are intentionally skipped by
        # requiring the open-paren suffix.
        m = re.search(r"\brenderMsListArea\s*\(", body)
        if m:
            assert guard_idx < m.start(), (
                "The state.openTrekId guard must come before "
                "renderMsListArea() in renderOnDataChange — otherwise the "
                "fall-through to render() still fires."
            )

    def test_desktop_parity_has_same_guard(self):
        src = _read(DESKTOP_INDEX)
        body = _extract_function_body(src, "function renderOnDataChange()")
        assert "if (state.openTrekId) return;" in body, (
            "desktop/dist/index.html must mirror the guard via "
            "desktop/build.py regeneration."
        )

    def test_comment_explains_why(self):
        # Future readers need the rationale (Trek detail is state.project-
        # independent, see e-2240 / e-2249) inline so the guard isn't
        # mistaken for dead code and removed.
        src = _read(WEB_INDEX)
        body = _extract_function_body(src, "function renderOnDataChange()")
        assert "e-2270" in body
        assert "state.openTrekScope" in body or "openTrekScope" in body
