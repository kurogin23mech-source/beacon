"""Web UI Acquisition (顧客獲得 / 施策) read-only view tests (ms-132 e-4562).

The sales shell gains an Acquisition tab that lists 施策 as first-class cards and,
under each, its linked attack-list (table-doc) with 打診フェーズ progress. The render
helpers live in the SHARED region of server/static/index.html (so desktop/dist
inherits them via desktop/build.py). This test extracts those pure functions and
exercises them under node with browser globals stubbed, so a regression in the
view fails CI. The view is read-only — advancing phases stays a CLI/Skill action.

Skipped when node is not installed (the acquisition model/CLI is covered by the
Python tests; this only guards the browser-side render).
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


def _slice(html: str, start_marker: str, end_marker: str) -> str:
    start = html.index(start_marker)
    end = html.index(end_marker)
    assert start < end, f"markers out of order: {start_marker} / {end_marker}"
    return html[start:end]


def _extract_blocks() -> str:
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    # table-doc helpers (reused by the attack-list render) + the acquisition view.
    helpers = _slice(html, "function docFormatIsTable", "function renderDocumentsSection")
    acq = _slice(html, "function renderAcquisitionsSection", "function _salesPhaseFilterClass")
    assert "function _renderAttackList" in acq, "acquisition render helpers not found"
    return helpers + "\n" + acq


HARNESS = textwrap.dedent(r"""
    // --- browser global stubs ---
    function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
    const marked = { parse: (s) => "[MD]" + s };

    const payload = {
      columns: [
        {key:"account", label:"対象顧客", type:"ref", ref:"acc"},
        {key:"phase", label:"打診フェーズ", type:"enum", values:["未接触","連絡済","返信あり","アポ"]},
        {key:"memo", label:"メモ", type:"text"}
      ],
      rows: [
        {id:"r1", cells:{account:"acc-1", phase:"連絡済"}},
        {id:"r2", cells:{account:"acc-2", phase:"返信あり"}},
        {id:"r3", cells:{account:"acc-3", phase:"連絡済"}},
        {id:"r4", cells:{account:"acc-4", phase:"未接触"}, removed_at:"T9"}
      ]
    };
    const attackListContent = "---\nscope: memo\nformat: table\ntarget: acq-1\n---\n# 攻略\n\n"
      + "```beacon-table\n" + JSON.stringify(payload) + "\n```\n";

    let state = {
      project: {
        acquisitions: [
          {id:"acq-1", label:"獲得A", status:"in_progress", description:"Q3 の新規開拓"},
          {id:"acq-2", label:"打ち切ったやつ", status:"cancelled"},
          {id:"acq-3", label:"獲得C", status:"todo"}
        ],
        opportunities: [], milestones: [], accounts: [{id:"acc-1"},{id:"acc-2"},{id:"acc-3"}]
      },
      documents: [{doc_id:"d1", title:"攻略リスト", target:"acq-1"}],
      attackListContent: { d1: attackListContent },
      hiddenAcqStatuses: new Set(),
      openAttackLists: new Set()
    };

    __RENDER_BLOCK__

    const A = (c,m) => { if(!c){ console.error("FAIL: "+m); process.exit(1); } };

    // --- 1. lists active 施策, excludes cancelled --------------------------
    let h = renderAcquisitionsSection();
    A(h.includes("acq-1") && h.includes("獲得A"), "active acquisition listed");
    A(h.includes("acq-3") && h.includes("獲得C"), "todo acquisition listed");
    A(!h.includes("打ち切ったやつ"), "cancelled acquisition excluded from active view");
    A(h.includes('data-action="acq-filter-all"'), "status filter bar present");
    A(h.includes("Q3 の新規開拓"), "acquisition description shown");

    // --- 2. attack-list phase progress (from cached table content) ---------
    A(h.includes("攻略リスト"), "linked attack-list title shown");
    A(h.includes('data-action="toggle-acq-list"') && h.includes('data-doc-id="d1"'),
      "attack-list is a toggle (expand/collapse)");
    A(/連絡済\s*<b>2<\/b>/.test(h), "phase 連絡済 count = 2 (tombstoned row excluded)");
    A(/返信あり\s*<b>1<\/b>/.test(h), "phase 返信あり count = 1");
    A(/未接触\s*<b>0<\/b>/.test(h), "phase 未接触 count = 0");
    A(h.includes("acq-phase-bar") && h.includes("acq-phase-seg"), "phase progress bar rendered");
    A(h.includes("3 件"), "active row count (4 rows - 1 tombstoned = 3)");

    // collapsed by default: the full table is not rendered until toggled.
    A(!h.includes('class="table-doc"'), "attack-list table collapsed by default");

    // --- 3. expanded shows the reused table-doc render (view-only) ---------
    state.openAttackLists = new Set(["d1"]);
    h = renderAcquisitionsSection();
    A(h.includes('class="table-doc"'), "expanded attack-list renders the table-doc");
    A(h.includes("<th>打診フェーズ</th>"), "table header from columns");
    // The rendered table body must carry no edit affordances (read-only view).
    const tbl = h.slice(h.indexOf('class="table-doc"'));
    A(!/contenteditable|<input|<textarea/.test(tbl), "attack-list table is view-only");

    // --- 4. status filter hides a status -----------------------------------
    state.hiddenAcqStatuses = new Set(["in_progress"]);
    h = renderAcquisitionsSection();
    A(!h.includes("獲得A"), "filtered-out status hidden");
    A(h.includes("獲得C"), "non-filtered status still shown");

    // --- 5. acquisition with no attack-list --------------------------------
    state.hiddenAcqStatuses = new Set();
    state.documents = [];
    h = renderAcquisitionsSection();
    A(h.includes("アタックリスト未作成"), "no-list hint shown when 施策 has no attack-list");

    // --- 6. empty acquisitions --------------------------------------------
    state.project.acquisitions = [];
    h = renderAcquisitionsSection();
    A(h.includes("顧客獲得ターゲット (施策) がありません"), "empty state message");

    console.log("ALL_PASS");
""")


def test_acquisition_ui_render(tmp_path):
    script = HARNESS.replace("__RENDER_BLOCK__", _extract_blocks())
    p = tmp_path / "acq_ui_test.mjs"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert result.returncode == 0, f"node render test failed:\n{result.stderr}\n{result.stdout}"
    assert "ALL_PASS" in result.stdout
