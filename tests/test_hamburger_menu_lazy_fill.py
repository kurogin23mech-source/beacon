"""Regression test for ms-95 / e-2215 — hamburger menu lazy fill pattern.

Background: before e-2215, `renderMenu()` in both `server/static/index.html`
and `desktop/layer.js` awaited HTTP fetches (loadProjects + loadOnlineSessions
in Web, list_projects + cloud_list_projects in Tauri) BEFORE the first paint.
On a fresh page load (= cache cold) the first hamburger click felt frozen
for 300ms–1s while the menu DOM stayed empty.

The fix paints the menu shell immediately with loading placeholders, then
fires the fetches in the background and refills each section in place as
data arrives. These tests pin that property at the source level so a future
"simplify" / refactor edit cannot silently regress it.

These are source-pinning tests — they do NOT spin up a browser. They
assert structural properties of the JS code (`renderMenu()` body) that
correspond to the lazy-fill contract.
"""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_LAYER = REPO_ROOT / "desktop" / "layer.js"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_line_comments(src: str) -> str:
    """Strip `// …` line comments from JS source, preserving newlines so
    file positions remain meaningful. Naive but sufficient for our checks
    (`//` inside string literals would yield false positives, but the
    renderMenu() bodies we target don't contain such patterns).
    """
    out = []
    for line in src.splitlines(keepends=True):
        # Only strip when `//` is not inside an obvious string. Heuristic:
        # if the substring before `//` has unbalanced quote count, treat
        # `//` as content. For the renderMenu() body this is safe.
        idx = line.find("//")
        while idx != -1:
            prefix = line[:idx]
            if prefix.count('"') % 2 == 0 and prefix.count("'") % 2 == 0:
                line = prefix + "\n" if line.endswith("\n") else prefix
                break
            idx = line.find("//", idx + 2)
        out.append(line)
    return "".join(out)


def _extract_render_menu(src: str) -> str:
    """Return the body of `async function renderMenu()` up to its matching
    closing brace. Strips `// …` comments first so explanatory comments
    quoting the legacy (pre-fix) code don't trigger false positives in the
    "must not contain X" assertions.
    """
    m = re.search(r"async function renderMenu\s*\(\s*\)\s*\{", src)
    assert m, "renderMenu() function not found in source"
    start = m.end()
    depth = 1
    i = start
    in_string = None  # ', ", or `
    escape = False
    while i < len(src) and depth > 0:
        ch = src[i]
        if escape:
            escape = False
        elif ch == "\\" and in_string in ("'", '"', "`"):
            escape = True
        elif in_string:
            if ch == in_string:
                in_string = None
        elif ch in ("'", '"', "`"):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    body = src[start : i - 1]
    return _strip_line_comments(body)


# ---------------------------------------------------------------------------
# Web UI (server/static/index.html) — Promise-style lazy fill
# ---------------------------------------------------------------------------

class TestWebUI_RenderMenuLazyFill:
    def setup_method(self, _method):
        self.src = _read(WEB_INDEX)
        self.body = _extract_render_menu(self.src)

    def test_no_await_loadprojects_before_paint(self):
        """`renderMenu()` must not block on `await loadProjects()` —
        that was the regression: empty DOM until HTTP returned."""
        assert "await loadProjects()" not in self.body, (
            "renderMenu() must NOT await loadProjects() — that blocks the "
            "first paint and reintroduces the e-2215 freeze. Use a "
            "background .then() refill instead."
        )

    def test_no_await_loadonlinesessions_before_paint(self):
        """Same for online sessions — must not block first paint."""
        assert "await dataSource.loadOnlineSessions()" not in self.body, (
            "renderMenu() must NOT await loadOnlineSessions() — fire it "
            "in the background and refill in place."
        )
        # Also forbid the previous Promise.all([sessionLoad]) wait pattern.
        assert "await Promise.all([sessionLoad])" not in self.body, (
            "renderMenu() must NOT await Promise.all([sessionLoad]) — "
            "that was the e-2215 regression vector."
        )

    def test_first_paint_writes_menu_root_innerhtml(self):
        """The shell must hit `root.innerHTML = \\`...\\`` synchronously,
        BEFORE any fetch chain runs, so the slide-in animation starts in
        the same frame as the click."""
        # Find the first occurrence of `root.innerHTML = `` and any
        # `loadProjects()` / `dataSource.loadOnlineSessions()` calls.
        innerhtml_pos = self.body.find("root.innerHTML = `")
        assert innerhtml_pos != -1, (
            "renderMenu() must assign to `root.innerHTML` to render the "
            "shell — got something else."
        )
        # loadProjects() can appear later in the body (background fill).
        loadp_pos = self.body.find("loadProjects()")
        if loadp_pos != -1:
            assert loadp_pos > innerhtml_pos, (
                "loadProjects() must be invoked AFTER the first paint "
                "(= after `root.innerHTML = ...`), not before."
            )
        loado_pos = self.body.find("dataSource.loadOnlineSessions()")
        if loado_pos != -1:
            assert loado_pos > innerhtml_pos, (
                "loadOnlineSessions() must be invoked AFTER the first "
                "paint, not before."
            )

    def test_has_placeholder_section_targets(self):
        """The lazy-fill pattern requires per-section DOM containers that
        the background fetches can refill in place."""
        assert 'id="menu-projects-list"' in self.body, (
            "renderMenu() must expose `id=\"menu-projects-list\"` so the "
            "background fetch can refill the Projects section in place."
        )
        assert 'id="menu-sessions-list"' in self.body, (
            "renderMenu() must expose `id=\"menu-sessions-list\"` so the "
            "background fetch can refill the Online Agents section in place."
        )

    def test_background_fill_uses_then_not_await(self):
        """Background fetches must use `.then(...)` so they don't block.
        At least one `loadProjects().then(` or `loadOnlineSessions().then(`
        must appear after the first paint."""
        has_then = (
            "loadProjects().then(" in self.body
            or "dataSource.loadOnlineSessions().then(" in self.body
        )
        assert has_then, (
            "renderMenu() must invoke at least one loader via .then() so "
            "the menu paints immediately and refills in place."
        )


# ---------------------------------------------------------------------------
# Desktop / Tauri (desktop/layer.js) — invoke-style lazy fill
# ---------------------------------------------------------------------------

class TestDesktopLayer_RenderMenuLazyFill:
    def setup_method(self, _method):
        self.src = _read(DESKTOP_LAYER)
        self.body = _extract_render_menu(self.src)

    def test_no_await_invoke_list_projects_before_paint(self):
        """The Tauri `renderMenu()` previously did
        `await invoke('list_projects')` BEFORE writing menu-root, leaving
        the panel invisible during the IPC round-trip."""
        # Allow `invoke('list_projects')` to appear, but not as a top-level
        # `await invoke('list_projects')` before the first paint.
        innerhtml_pos = self.body.find("root.innerHTML = `")
        assert innerhtml_pos != -1, (
            "Tauri renderMenu() must assign root.innerHTML for the shell."
        )
        before = self.body[:innerhtml_pos]
        assert "await invoke('list_projects')" not in before, (
            "Tauri renderMenu() must NOT await invoke('list_projects') "
            "before the first paint — fire it in the background instead."
        )
        assert "await invoke('cloud_list_projects')" not in before, (
            "Tauri renderMenu() must NOT await invoke('cloud_list_projects') "
            "before the first paint."
        )

    def test_has_placeholder_section_target(self):
        """Tauri lazy fill uses a single `menu-projects-body` container that
        the background fetches refill once both local + cloud lists are
        ready."""
        assert 'id="menu-projects-body"' in self.body, (
            "Tauri renderMenu() must expose `id=\"menu-projects-body\"` "
            "for the background refill."
        )

    def test_background_fill_uses_promise_all_then(self):
        """The Tauri fix uses `Promise.all([localP, cloudP]).then(...)` to
        refill once both invokes resolve."""
        assert "Promise.all(" in self.body and ".then(" in self.body, (
            "Tauri renderMenu() must compose its two invokes via "
            "Promise.all([...]).then(...) so neither blocks the first paint."
        )
