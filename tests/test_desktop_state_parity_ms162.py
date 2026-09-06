"""Desktop (Tauri) state-slot parity for the ms-162 decision/sub-tab feature.

Regression guard for the crash the PR #698 review caught: the SHARED render
region (server/static/index.html, flows into desktop/dist via desktop/build.py)
*dereferences* three state slots and one dataSource method with method / index
access — which throw a TypeError if the slot is undefined:

  - state.targetTab[ms.id]        (renderMilestoneCard, on expanding a card)
  - dataSource.loadDecisions()    (ensureDecisionsLoaded)
  - state.decisions / decisionsLoading (decision-log lazy-load guard)

These were added to the web state/dataSource (server/static) but the desktop
platform layer (desktop/layer.js) supplies its OWN state literal + dataSource,
so it must define them too — otherwise the generated desktop bundle crashes when
any milestone card is opened. desktop-dist-freshness only checks that dist is
regenerated, NOT that these slots resolve at runtime, so this test closes that
specific gap.

Scope note: this is deliberately a *targeted* check on the known method/index
dereferences, not a general "every state.X in SHARED must be in layer.js" parity
(most `state.X` reads are harmless when undefined; only method/index access
crashes — a general check has too many platform-justified false positives). A
general dereference-aware drift check is a recommended follow-up.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "server", "static", "index.html")
LAYER = os.path.join(REPO, "desktop", "layer.js")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _shared_region(web: str) -> str:
    m = re.search(r"// @BUILD:SHARED-START\n(.*?)// @BUILD:SHARED-END", web, re.DOTALL)
    assert m, "SHARED markers not found in server/static/index.html"
    # Drop SKIP (web-only) blocks so we only inspect what flows into desktop/dist.
    return re.sub(r"// @BUILD:SKIP-START\n.*?// @BUILD:SKIP-END\n?", "",
                  m.group(1), flags=re.DOTALL)


def _strip_comments(js: str) -> str:
    return re.sub(r"//[^\n]*", "", js)


def _defines_state_slot(layer: str, slot: str) -> bool:
    """A slot counts as defined if it appears as a key in the `let state = {...}`
    literal (keys may share a line, e.g. `a: 1, b: 2`) or as a `state.X =`
    assignment somewhere in the layer."""
    js = _strip_comments(layer)
    lit = re.search(r"\blet state = \{(.*?)\n\};", js, re.DOTALL)
    body = lit.group(1) if lit else ""
    if re.search(r"(?<![.\w])" + re.escape(slot) + r"\s*:", body):
        return True
    return bool(re.search(r"\bstate\." + re.escape(slot) + r"\s*=", js))


def _defines_datasource_method(layer: str, name: str) -> bool:
    js = _strip_comments(layer)
    m = re.search(r"\bconst dataSource = \{(.*?)\n\};", js, re.DOTALL)
    body = m.group(1) if m else ""
    return bool(re.search(r"(?<![.\w])" + re.escape(name) + r"\s*:", body))


def test_shared_actually_dereferences_these():
    """Guard the guard: if SHARED stops using these (feature removed), this test
    should be updated — assert the dereferences are really present so the parity
    assertions below are meaningful."""
    shared = _shared_region(_read(WEB))
    assert "state.targetTab[" in shared, "expected state.targetTab[...] index access in SHARED"
    assert "dataSource.loadDecisions(" in shared, "expected dataSource.loadDecisions() call in SHARED"
    assert "state.decisions" in shared, "expected state.decisions usage in SHARED"
    # ms-162 e-5837: the deliverable panel adds a new method call + state read to
    # SHARED, so desktop/layer.js must supply them too or the desktop bundle
    # crashes on project load.
    assert "dataSource.loadDeliverables(" in shared, "expected dataSource.loadDeliverables() call in SHARED"
    assert "state.deliverables" in shared, "expected state.deliverables usage in SHARED"


def test_layer_defines_targetTab():
    assert _defines_state_slot(_read(LAYER), "targetTab"), (
        "desktop/layer.js state must define `targetTab` — SHARED render does "
        "state.targetTab[ms.id] and undefined[ms.id] crashes the desktop app "
        "when a milestone card is expanded."
    )


def test_layer_defines_decisions_slots():
    layer = _read(LAYER)
    assert _defines_state_slot(layer, "decisions"), (
        "desktop/layer.js state must define `decisions` (decision-log cache)."
    )
    assert _defines_state_slot(layer, "decisionsLoading"), (
        "desktop/layer.js state must define `decisionsLoading`."
    )


def test_layer_defines_loadDecisions():
    assert _defines_datasource_method(_read(LAYER), "loadDecisions"), (
        "desktop/layer.js dataSource must define `loadDecisions` — SHARED calls "
        "dataSource.loadDecisions() and calling undefined crashes the desktop app."
    )


def test_layer_defines_deliverables_slots():
    layer = _read(LAYER)
    assert _defines_state_slot(layer, "deliverables"), (
        "desktop/layer.js state must define `deliverables` (produced-value cache) "
        "— SHARED renderDeliverables(state.deliverables, ...) reads it (ms-162 e-5837)."
    )
    assert _defines_state_slot(layer, "deliverablesLoading"), (
        "desktop/layer.js state must define `deliverablesLoading`."
    )


def test_layer_defines_loadDeliverables():
    assert _defines_datasource_method(_read(LAYER), "loadDeliverables"), (
        "desktop/layer.js dataSource must define `loadDeliverables` — SHARED calls "
        "dataSource.loadDeliverables() and calling undefined crashes the desktop app."
    )
