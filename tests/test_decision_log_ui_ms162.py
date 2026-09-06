"""Web UI decision-log render test (ms-162 e-5956, AX advisory b & c).

`renderDecisionLog(decisions, opts)` is the portable render 部品 for the ms-154
decision-arm stream, shared verbatim by the milestone card sub-tab and the
opportunity / account modals (SHARED region of server/static/index.html,
inherited by desktop/dist via desktop/build.py).

This pins two AX-advisory fixes from the PR#698 independent review:

  (b) loading vs empty — the stream loads lazily; while it is still loading the
      per-target filtered result is empty only because nothing is fetched yet.
      With {loading:true} the panel must show a placeholder, NOT '' and NOT an
      "empty" state, so a target's decisions never look absent mid-fetch.

  (c) label language parity — the panel heading reads "decision" to match the
      English sub-tab label (siblings work-item / evidence / decision). The old
      heading 決定ログ split languages with the tab that opens it; this test
      fails if that regresses.

Skipped when node is not installed.
"""

import os
import shutil
import subprocess
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(REPO, "server", "static", "index.html")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")


def _extract_fns() -> str:
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    start = html.index("const DECIDED_BY_LABEL")
    end = html.index("function ensureDecisionsLoaded")
    block = html[start:end]
    assert "function renderDecisionLog" in block, "renderDecisionLog not found"
    assert "function renderDecisionEvent" in block, "renderDecisionEvent not found"
    return block


HARNESS = textwrap.dedent(r"""
    function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

    __RENDER_BLOCK__

    const A = (c,m) => { if(!c){ console.error("FAIL: "+m); process.exit(1); } };

    const one = [{ decision_id: "d-1", kind: "completion_approval",
                   decision: "ms を observing にする", rationale: "AC 達成",
                   decided_by: "autonomous-AI", created_at: "2026-08-29T00:00:00Z",
                   related: { target_id: "ms-9" } }];

    // --- c. populated panel heading is "decision", not 決定ログ --------------
    let h = renderDecisionLog(one);
    A(h.includes("dec-log-label"), "panel label rendered");
    A(/decision\b/.test(h), "heading reads 'decision' (matches English sub-tab)");
    A(!h.includes("決定ログ"), "heading must not regress to 決定ログ (language split)");
    A(h.includes("ms を observing にする"), "event body rendered");
    A(h.includes(">1<") || h.includes("dec-count"), "count badge present");

    // --- loaded + empty → '' (produced-but-invisible stays quiet) ----------
    A(renderDecisionLog([]) === "", "loaded-empty → '' (no panel)");
    A(renderDecisionLog(null) === "", "null (no opts) → ''");
    A(renderDecisionLog([], {}) === "", "loaded-empty with opts → ''");
    A(renderDecisionLog([], { loading: false }) === "",
      "explicit not-loading + empty → ''");

    // --- b. loading + empty → placeholder, NOT '' and NOT empty-state ------
    h = renderDecisionLog([], { loading: true });
    A(h !== "", "loading + empty must render a placeholder, not ''");
    A(h.includes("dec-log-loading"), "placeholder uses dec-log-loading");
    A(h.includes("読み込み中"), "placeholder says loading");
    A(!h.includes("dec-log-label"), "loading placeholder is not the populated label");

    // --- b. loading is ignored once decisions actually exist ---------------
    h = renderDecisionLog(one, { loading: true });
    A(!h.includes("dec-log-loading"), "real decisions win over the loading flag");
    A(h.includes("ms を observing にする"), "real content shown even if loading flag set");

    console.log("ALL_PASS");
""")


def test_decision_log_render(tmp_path):
    script = HARNESS.replace("__RENDER_BLOCK__", _extract_fns())
    p = tmp_path / "decision_log_test.mjs"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert result.returncode == 0, f"node render test failed:\n{result.stderr}\n{result.stdout}"
    assert "ALL_PASS" in result.stdout


def test_decision_loading_wired_at_every_callsite():
    """Every decision-log callsite must feed the loading flag through the ONE
    shared getter isDecisionsPending() — not a re-typed `state.decisions === null`
    sentinel. Guards the wiring the pure-fn test can't see, at all 3 callsites
    (ms card sub-tab + opportunity modal + account modal). PR#722 review
    consensus: AX + maintainability both flagged the inline-sentinel duplication
    and the account/opp callsites being untested (ms-162 e-5956 b)."""
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    # single truth source getter exists
    assert "function isDecisionsPending()" in html, \
        "the shared 'is the decision stream pending?' getter must exist"
    assert "return state.decisions === null;" in html, \
        "isDecisionsPending must be the one place that reads the null sentinel"
    # ms card sub-tab: loading-aware count (… not 0) + loading passed to the panel
    assert "const decLoading = isDecisionsPending();" in html, \
        "MS card must derive decLoading from the shared getter, not an inline sentinel"
    assert "const decCount = decLoading ? '…' : decisions.length;" in html, \
        "decision tab count must show … while loading, not 0"
    assert "tab('decision', 'decision', decCount)" in html, \
        "decision tab must render the loading-aware count"
    assert "renderDecisionLog(decisions, { loading: decLoading })" in html, \
        "decision tab content must pass the loading flag through"
    # opportunity + account modals: both must pass loading via the shared getter.
    # (Maint#1: these callsites were previously untested and could silently break.)
    modal_calls = html.count(
        "renderDecisionLog(decisionsForTarget(state.decisions, "
    )
    assert modal_calls >= 2, \
        f"expected the 2 modal callsites, found {modal_calls}"
    assert html.count(
        "), { loading: isDecisionsPending() })"
    ) >= 2, "both opportunity & account modal decision logs must pass loading via the getter"
    # the inline sentinel must NOT be passed to renderDecisionLog anymore
    assert "loading: state.decisions === null" not in html, \
        "no callsite may re-type the null sentinel inline; all go through the getter"


def test_root_header_approximate_open_has_self_describing_title():
    """AX#2 (PR#722): the fallback 未完了(概算) count must carry a tooltip that
    says WHY it is approximate, so a zero-context reader doesn't misread it as a
    fault / unloaded data. The server-provided 進行中 path carries no such note."""
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    assert "const openTitle = openProvided" in html, \
        "root header must compute a title only for the approximate (fallback) case"
    assert "総数−完遂で概算" in html, \
        "the tooltip must explain the count is an estimate from total-minus-done"
    assert '<span class="root-hdr-count"${openTitle}>' in html, \
        "the count span must carry the conditional title attribute"
