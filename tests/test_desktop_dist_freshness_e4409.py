"""desktop/dist/index.html is the current build output (ms-133 e-4409).

e-4409 suspected the Tauri desktop UI had drifted from the Web UI (sales tabs
not showing, `desktop/dist/index.html` ~969 lines "behind" `server/static/
index.html`). Investigation found:

  * both frontends DO carry the sales projection (Opportunities/Accounts/
    Acquisition), so the visible symptom was already resolved; and
  * the line-count gap is the EXPECTED assembly difference — `desktop/build.py`
    extracts CSS + the SHARED JS block from the web page and combines them with
    `desktop/layer.js`, so dist is a different (smaller) artifact by design, not
    a stale copy.

What was MISSING was a guard that dist stays the faithful build output: the
existing frontend-drift checker compares tabs + action handlers, but nothing
asserts that `dist == build(web, layer)`. So a future edit to the SHARED block
in `server/static/index.html` that isn't followed by `python3 desktop/build.py`
would silently ship a stale desktop app (the real drift risk behind e-4409).

This test is that guard: it regenerates dist from its sources and requires it to
match byte-for-byte. Repair when it fails: `python3 desktop/build.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "server" / "static" / "index.html"
LAYER = ROOT / "desktop" / "layer.js"
DIST = ROOT / "desktop" / "dist" / "index.html"
BUILD = ROOT / "desktop" / "build.py"


def _load_build():
    spec = importlib.util.spec_from_file_location("_desktop_build", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.skipif(not (WEB.exists() and LAYER.exists() and BUILD.exists()),
                    reason="desktop build sources not present")
def test_dist_matches_current_build_output():
    """dist must equal build(web, layer) — i.e. it was rebuilt after the last
    change to the SHARED block or layer.js. Byte-for-byte, because build.py
    writes deterministically."""
    build = _load_build()
    regenerated = build.build(
        WEB.read_text(encoding="utf-8"),
        LAYER.read_text(encoding="utf-8"),
    )
    committed = DIST.read_text(encoding="utf-8")
    assert regenerated == committed, (
        "desktop/dist/index.html is stale vs its build sources "
        "(server/static/index.html SHARED block + desktop/layer.js). "
        "Rebuild it: `python3 desktop/build.py`"
    )


@pytest.mark.skipif(not (WEB.exists() and DIST.exists()),
                    reason="frontends not present")
def test_sales_projection_present_in_both_frontends():
    """The e-4409 symptom guard: the sales occupation UI (isSales branch) must
    exist in BOTH the web page and the desktop build, so a sales project's tabs
    render on either surface."""
    web = WEB.read_text(encoding="utf-8")
    dist = DIST.read_text(encoding="utf-8")
    assert "isSales" in web
    assert "isSales" in dist
