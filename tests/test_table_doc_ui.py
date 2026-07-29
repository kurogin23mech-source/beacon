"""Web UI table-doc rendering tests (ms-131 e-4498).

The Documents tab renders a table-doc (format:table) as a read-only HTML table
instead of markdown. The render helpers live in the SHARED region of
server/static/index.html (so desktop/dist inherits them via desktop/build.py).
This test extracts those pure functions and exercises them under node with
browser globals stubbed, so a regression in the table rendering fails CI.

Skipped when node is not installed (the functions are still covered by the
Python model/CLI tests; this only guards the browser-side render).
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


def _extract_render_block() -> str:
    with open(INDEX_HTML, encoding="utf-8") as f:
        html = f.read()
    start = html.index("function docFormatIsTable")
    end = html.index("function renderDocumentsSection")
    block = html[start:end]
    assert "function renderDocBody" in block, "render helpers not found in SHARED region"
    return block


HARNESS = textwrap.dedent(r"""
    // --- browser global stubs ---
    function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
    const marked = { parse: (s) => "[MD]" + s };
    let state = { project: { opportunities: [{id:"opp-3", title:"Acme商談"}],
                             milestones: [{id:"ms-1", title:"MS One"}] } };

    __RENDER_BLOCK__

    const A = (c,m) => { if(!c){ console.error("FAIL: "+m); process.exit(1); } };
    const payload = {
      columns: [{key:"name",label:"名前",type:"text"},
                {key:"opp",label:"商談",type:"ref"},
                {key:"won",type:"bool"}],
      rows: [{id:"r1", cells:{name:"A", opp:"opp-3", won:true}},
             {id:"r2", cells:{name:"B"}, removed_at:"T9"}]
    };
    const tableContent = "---\nscope: memo\nformat: table\n---\n# 顧客\n\n```beacon-table\n"
      + JSON.stringify(payload) + "\n```\n";

    A(docFormatIsTable(tableContent), "detect format:table");
    A(!docFormatIsTable("---\nscope: memo\n---\n# x\n\nbody"), "plain md is not table");
    A(!docFormatIsTable("no frontmatter"), "no frontmatter is not table");

    const h = renderTableDocHtml(tableContent);
    A(h.includes("<th>名前</th>") && h.includes("<th>商談</th>") && h.includes("<th>won</th>"),
      "headers use label or key");
    A(h.includes("Acme商談"), "ref cell resolves to referenced label");
    A(h.includes("✓"), "bool cell rendered as check");
    A(!/>\s*B\s*</.test(h), "tombstoned row excluded from render");
    A(h.includes("1 行が削除済み"), "removed-row count surfaced");

    A(renderDocBody("---\nformat: table\n---\n# x\n\n```beacon-table\n{bad}\n```").startsWith("[MD]"),
      "malformed table falls back to markdown");
    A(renderDocBody("plain body").startsWith("[MD]"), "non-table uses markdown");

    // No edit affordances in the rendered table (view-only).
    A(!/contenteditable|data-action|<input|<button/.test(h), "render is view-only (no edit UI)");

    console.log("ALL_PASS");
""")


def test_table_doc_ui_render(tmp_path):
    script = HARNESS.replace("__RENDER_BLOCK__", _extract_render_block())
    p = tmp_path / "render_test.mjs"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert result.returncode == 0, f"node render test failed:\n{result.stderr}\n{result.stdout}"
    assert "ALL_PASS" in result.stdout
