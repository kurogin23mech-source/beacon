"""Web UI root-target header render test (ms-162 e-5835).

The dashboard gains a portable header for the ROOT target — the apex target that
holds every other target beneath it (ms-153 の root 合成,
root_target.project_as_root_target). It composes the root-owned narrative
(大目的 objective / 経緯 summary) with the arms rollup (work-item 源 / decision の
承認形 / deliverable の産物) and the work-item rollup (配下 target の総数・完遂・進行中).

`renderRootHeader(root)` is a pure function of one root object (like renderDecisionLog
/ renderTargetRow), so it lives in the SHARED region of server/static/index.html and
desktop/dist inherits it via desktop/build.py. This test extracts it and exercises it
under node with `esc` stubbed, so a regression in the view fails CI. It also pins the
"空でも壊れない" contract: an absent / narrative-less root must degrade gracefully
rather than crash or duplicate the legacy Objective banner.

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


def _extract_fn() -> str:
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    start = html.index("function renderRootHeader(root)")
    # the pure fn ends at the next top-level comment block that follows it
    end = html.index("// ms-162 — target 詳細サブタブの次元分類", start)
    block = html[start:end]
    assert "root-header" in block, "renderRootHeader body not found"
    return block


HARNESS = textwrap.dedent(r"""
    function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

    __RENDER_BLOCK__

    const A = (c,m) => { if(!c){ console.error("FAIL: "+m); process.exit(1); } };

    const full = {
      work_items_total: 155, work_items_done: 67, work_items_open: 88,
      narrative: { objective: "大目的テキスト", summary: "A → B → C の流れ" },
      arms: {
        work_item_arm: { item_type: "target" },
        decision: { kind: "completion_approval" },
        deliverable: { kind: "achievement" },
      },
    };

    // --- 1. full root: objective + rollup + arms + summary ------------------
    let h = renderRootHeader(full);
    A(h.includes("root-header"), "section rendered");
    A(h.includes("大目的テキスト"), "objective shown");
    A(h.includes("A → B → C の流れ"), "summary shown");
    A(/43%/.test(h), "rollup pct = round(67/155) = 43%");
    A(h.includes("67/155 完遂") && h.includes("88 進行中"),
      "rollup counts shown, server-provided work_items_open labelled 進行中");
    A(h.includes("target") && h.includes("completion_approval") && h.includes("achievement"),
      "all three arms chips shown");
    A(h.includes('style="width:43%"'), "progress bar width follows pct");

    // --- 2. null / undefined root → '' (shell omits) -----------------------
    A(renderRootHeader(null) === "", "null root → empty");
    A(renderRootHeader(undefined) === "", "undefined root → empty");

    // --- 3. narrative-less root but has work items → rollup only -----------
    // (production before the server projects narrative/arms: must NOT crash,
    //  must still show the rollup, must NOT emit an empty objective div)
    h = renderRootHeader({ work_items_total: 5, work_items_done: 2, arms: {} });
    A(h.includes("root-header"), "rollup-only header still renders");
    A(/40%/.test(h) && h.includes("2/5 完遂"), "rollup shown without narrative");
    A(!h.includes("root-hdr-objective"), "no empty objective block when narrative absent");
    A(!h.includes("root-hdr-summary"), "no empty summary block when narrative absent");
    A(!h.includes("root-hdr-arms"), "no arms block when arms empty");

    // --- 4. fully bare root → '' (nothing to show) -------------------------
    A(renderRootHeader({ narrative: {}, arms: {}, work_items_total: 0 }) === "",
      "bare root → empty");

    // --- 5. partial arms: only present mechanisms become chips -------------
    h = renderRootHeader({ narrative: { objective: "x" },
                           arms: { decision: { kind: "child_completion" } } });
    A(h.includes("child_completion"), "present arm rendered");
    A(!h.includes("deliverable"), "absent arm produces no chip");

    // --- 6. open falls back to total-done when work_items_open missing -----
    // ms-162 e-5956 (AX advisory a): the fallback total-done includes cancelled
    // work, so it must NOT claim "進行中" — it is labelled 未完了(概算) instead.
    h = renderRootHeader({ work_items_total: 10, work_items_done: 4,
                           narrative: { objective: "y" } });
    A(h.includes("4/10 完遂") && h.includes("6 未完了(概算)"),
      "derived open is labelled 未完了(概算), not 進行中 (cancelled not miscounted)");
    A(!/6 進行中/.test(h), "derived open must not claim 進行中");

    // --- 6b. server-provided work_items_open is trusted as 進行中 -----------
    h = renderRootHeader({ work_items_total: 10, work_items_done: 4,
                           work_items_open: 3, narrative: { objective: "y" } });
    A(h.includes("3 進行中"), "server-provided open labelled 進行中");
    A(!h.includes("未完了(概算)"), "no 概算 label when server provides open");

    // --- 7. html-escaping of narrative/arms --------------------------------
    h = renderRootHeader({ narrative: { objective: "<script>&\"'" },
                           arms: {}, work_items_total: 0 });
    A(!h.includes("<script>"), "objective is html-escaped");
    A(h.includes("&lt;script&gt;"), "escaped entities present");

    console.log("ALL_PASS");
""")


def test_root_header_render(tmp_path):
    script = HARNESS.replace("__RENDER_BLOCK__", _extract_fn())
    p = tmp_path / "root_header_test.mjs"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert result.returncode == 0, f"node render test failed:\n{result.stderr}\n{result.stdout}"
    assert "ALL_PASS" in result.stdout
