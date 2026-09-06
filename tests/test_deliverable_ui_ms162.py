"""Web UI deliverable (produced-value) panel render test (ms-162 e-5837).

`renderDeliverables(deliverables, opts)` + `renderDeliverableItem(d)` are the
portable render 部品 for the ms-155/160/161 deliverable projection, fed by
GET /api/projects/{id}/deliverables (resolved rows). They live in the SHARED
region of server/static/index.html (desktop/dist inherits them via
desktop/build.py). This test extracts them and exercises each resolve strategy
(changelog / rollup / doc / unresolved) plus the loading-vs-empty contract under
node, so a regression in the view fails CI.

This closes the 発端 of the ms-162 re-fork: deliverables were readable via CLI
but the UI could only show the arm's kind label, never the produced value itself.

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
    start = html.index("const _DLV_PREVIEW_CHARS")
    end = html.index("function isDeliverablesPending")
    block = html[start:end]
    assert "function renderDeliverables" in block, "renderDeliverables not found"
    assert "function renderDeliverableItem" in block, "renderDeliverableItem not found"
    return block


HARNESS = textwrap.dedent(r"""
    function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

    __RENDER_BLOCK__

    const A = (c,m) => { if(!c){ console.error("FAIL: "+m); process.exit(1); } };

    // --- changelog strategy: count_active + categories -----------------------
    const changelog = [{
      target_class: "milestone", kind: "feature-map", label: "機能",
      projector: "changelog", ref: "",
      resolved: { strategy: "changelog", found: true, count_active: 3,
        categories: [{category: "A1. 状態を一望する", count: 2},
                     {category: "A2. 記録する", count: 1}] },
    }];
    let h = renderDeliverables(changelog);
    A(h.includes("dlv-panel"), "panel rendered");
    A(h.includes("生み出した価値"), "panel label present");
    A(h.includes("機能"), "deliverable label shown");
    A(h.includes("projector=changelog"), "projector shown");
    A(h.includes("3 件の価値"), "count_active total shown");
    A(h.includes("A1. 状態を一望する") && h.includes("A2. 記録する"), "categories shown");
    A(h.includes(">2<") && h.includes(">1<"), "category counts shown");

    // --- rollup strategy: delivered/total + labels ---------------------------
    const rollup = [{
      target_class: "opportunity", kind: "won-deals", label: "成約",
      projector: "rollup", ref: "",
      resolved: { strategy: "rollup", found: true, count_delivered: 2,
        count_total: 5, labels: ["A社", "B社"] },
    }];
    h = renderDeliverables(rollup);
    A(h.includes("2/5 delivered"), "rollup delivered/total shown");
    A(h.includes("A社") && h.includes("B社"), "rollup labels shown");

    // --- doc strategy: title + preview ---------------------------------------
    const docd = [{
      target_class: "milestone", kind: "spec-map", label: "設計",
      projector: "doc", ref: "doc:x",
      resolved: { strategy: "doc", found: true, title: "全貌マップ",
        content: "これは本文。" .repeat(60) },
    }];
    h = renderDeliverables(docd);
    A(h.includes("全貌マップ"), "doc title shown");
    A(h.includes("文字)"), "doc length shown");
    A(h.includes("…"), "long doc preview is elided");

    // --- unresolved pointer must be VISIBLE, not hidden ----------------------
    const bad = [{
      target_class: "milestone", kind: "feature-map", label: "壊れ",
      projector: "changelog", ref: "map:x",
      resolved: { strategy: "changelog", found: false, error: "changelog missing" },
    }];
    h = renderDeliverables(bad);
    A(h.includes("未解決"), "unresolved deliverable is surfaced, not hidden");
    A(h.includes("changelog missing"), "unresolved error shown");

    // --- loaded + empty → '' (produced-but-invisible stays quiet) ------------
    A(renderDeliverables([]) === "", "loaded-empty → '' (no panel)");
    A(renderDeliverables(null) === "", "null (no opts) → ''");
    A(renderDeliverables([], { loading: false }) === "", "not-loading + empty → ''");

    // --- loading + empty → placeholder, NOT '' -------------------------------
    h = renderDeliverables([], { loading: true });
    A(h !== "", "loading + empty must render a placeholder");
    A(h.includes("dlv-panel-loading"), "placeholder class present");
    A(h.includes("読み込み中"), "placeholder says loading");
    A(!h.includes("dlv-panel-label"), "loading placeholder is not the populated label");

    // --- real data wins over the loading flag --------------------------------
    h = renderDeliverables(changelog, { loading: true });
    A(!h.includes("dlv-panel-loading"), "real deliverables win over loading flag");
    A(h.includes("機能"), "real content shown even if loading flag set");

    // --- html-escaping -------------------------------------------------------
    const xss = [{
      target_class: "milestone", kind: "k", label: "<script>&\"'",
      projector: "changelog", ref: "",
      resolved: { strategy: "changelog", found: true, count_active: 0, categories: [] },
    }];
    h = renderDeliverables(xss);
    A(!h.includes("<script>"), "label is html-escaped");
    A(h.includes("&lt;script&gt;"), "escaped entities present");

    console.log("ALL_PASS");
""")


def test_deliverable_panel_render(tmp_path):
    script = HARNESS.replace("__RENDER_BLOCK__", _extract_fns())
    p = tmp_path / "deliverable_test.mjs"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert result.returncode == 0, f"node render test failed:\n{result.stderr}\n{result.stdout}"
    assert "ALL_PASS" in result.stdout


def test_deliverable_panel_wired_into_dashboard():
    """The panel must be composed into the dashboard, lazy-loaded on project load
    via the shared getter, and backed by a single pending-sentinel getter — the
    same wiring discipline the decision-log uses (guards what the pure-fn test
    can't see)."""
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    assert "function isDeliverablesPending()" in html, \
        "the shared 'is the deliverable projection pending?' getter must exist"
    assert "function ensureDeliverablesLoaded()" in html, \
        "the lazy-loader must exist"
    assert "renderDeliverables(state.deliverables, { loading: isDeliverablesPending() })" in html, \
        "the dashboard must compose the panel via the shared getter"
    assert "ensureDeliverablesLoaded();" in html, \
        "the deliverable projection must be lazy-loaded on project load"
    assert "/api/projects/${state.projectId}/deliverables" in html, \
        "loadDeliverables must hit the deliverables read口"
