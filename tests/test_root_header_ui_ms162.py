"""Web UI root-target header render test (ms-162 e-5835, 簡素化 fix).

The dashboard has a portable header for the ROOT target — the apex target that
holds every other target beneath it (ms-153 の root 合成,
root_target.project_as_root_target). It composes the root-owned narrative
(大目的 objective / 経緯 summary) with the arms rollup (work-item 源 / decision の
承認形 / deliverable の産物).

ms-162 fix (user 要望): the header NO LONGER renders (a) the work-item progress
rollup (43% バー + 完遂/進行中) nor (b) the class-level deliverable 集約 union
(application-map dump). Both cluttered the project top. The header is now just
objective + arm chips + 直近の流れ. Per-target deliverable lives in the target
detail sub-tab; the class-level union re-homes to a root deliverable tab later.

`renderRootHeader(root)` is a pure function of one root object (like
renderDecisionLog / renderTargetRow), so it lives in the SHARED region of
server/static/index.html and desktop/dist inherits it via desktop/build.py. This
test extracts it and exercises it under node with `esc` stubbed, so a regression
in the view fails CI. It also pins the "空でも壊れない" contract.

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
    start = html.index("function renderRootHeader(root")
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

    // --- 1. full root: objective + arms + summary, NO rollup / NO union -----
    let h = renderRootHeader(full);
    A(h.includes("root-header"), "section rendered");
    A(h.includes("大目的テキスト"), "objective shown");
    A(h.includes("A → B → C の流れ"), "summary shown");
    A(h.includes("target") && h.includes("completion_approval") && h.includes("achievement"),
      "all three arms chips shown");
    // user 要望で除去: 進捗率 rollup と deliverable 集約 union は header に出さない
    A(!/\d+%/.test(h), "progress percent must NOT render");
    A(!h.includes("完遂") && !h.includes("進行中"),
      "rollup counts (完遂 / 進行中) must NOT render");
    A(!h.includes("root-hdr-rollup") && !h.includes("root-hdr-bar"),
      "no rollup bar markup in the header");
    A(!h.includes("dlv-panel") && !h.includes("生み出した価値"),
      "deliverable 集約 union must NOT render in the header");

    // --- 2. null / undefined root → '' -------------------------------------
    A(renderRootHeader(null) === "", "null root → empty");
    A(renderRootHeader(undefined) === "", "undefined root → empty");

    // --- 3. work-items only (no narrative / no arms) → '' -------------------
    // rollup を消したので、objective/summary/arm を持たない root は空になる。
    A(renderRootHeader({ work_items_total: 5, work_items_done: 2, arms: {} }) === "",
      "work-items-only root now renders empty (rollup removed)");

    // --- 4. fully bare root → '' -------------------------------------------
    A(renderRootHeader({ narrative: {}, arms: {}, work_items_total: 0 }) === "",
      "bare root → empty");

    // --- 5. partial arms: only present mechanisms become chips -------------
    h = renderRootHeader({ narrative: { objective: "x" },
                           arms: { decision: { kind: "child_completion" } } });
    A(h.includes("child_completion"), "present arm rendered");
    A(!h.includes("achievement"), "absent deliverable arm produces no chip");

    // --- 6. objective-only root renders; no empty summary/arms blocks ------
    h = renderRootHeader({ narrative: { objective: "只これだけ" }, arms: {} });
    A(h.includes("root-header") && h.includes("只これだけ"),
      "objective-only header renders");
    A(!h.includes("root-hdr-summary"), "no empty summary block when summary absent");
    A(!h.includes("root-hdr-arms"), "no arms block when arms empty");

    // --- 7. html-escaping of narrative ------------------------------------
    h = renderRootHeader({ narrative: { objective: "<script>&\"'" }, arms: {} });
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
