"""Web UI 基本 evidence (commit が work-item を閉じる) render test (ms-162 e-5836).

A completed work-item (task) carries, as child entries, the commit(s) that resolved
it (the `beacon log --finalize --resolves` structure: the commit is nested under the
task with meta.resolves = task id). The dashboard surfaces this as a compact
"✓ <hash>" badge on the work-item row so "what closed this task" is legible without
expanding — the basic (L2 built-in) evidence, distinct from the L3 declaration-driven
budget / structured-evidence-3-values which this view deliberately does NOT show.

`closingCommits` / `closingEvidenceBadge` are pure functions (only esc), so this test
extracts them and exercises them under node. Skipped when node is not installed.
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
    start = html.index("function closingCommits(entry)")
    end = html.index("\nfunction renderMilestoneCard", start)
    block = html[start:end]
    assert "function closingEvidenceBadge" in block, "closingEvidenceBadge not found"
    return block


HARNESS = textwrap.dedent(r"""
    function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

    __RENDER_BLOCK__

    const A = (c,m) => { if(!c){ console.error("FAIL: "+m); process.exit(1); } };

    // done task with one resolving commit child (the real shape)
    const doneTask = { id: "e-5835", type: "task", status: "done", entries: [
      { id: "e-5904", type: "commit", meta: { hash: "66bd5818abc", resolves: "e-5835" } },
    ]};

    // --- 1. closingCommits reads the resolving commit hash (7-char) ---------
    let hs = closingCommits(doneTask);
    A(hs.length === 1 && hs[0] === "66bd581", "single resolving commit, 7-char hash");

    // --- 2. badge renders ✓ + hash -----------------------------------------
    let b = closingEvidenceBadge(doneTask);
    A(b.includes("entry-evidence") && b.includes("66bd581") && b.includes("✓"),
      "evidence badge shows ✓ + hash");

    // --- 3. resolves as an ARRAY containing the task id --------------------
    hs = closingCommits({ id: "e-1", type: "task", entries: [
      { type: "commit", meta: { hash: "deadbeef", resolves: ["e-9", "e-1"] } },
    ]});
    A(hs.length === 1 && hs[0] === "deadbee", "array resolves matched by membership");

    // --- 4. commit child that resolves a DIFFERENT task is ignored ---------
    hs = closingCommits({ id: "e-1", type: "task", entries: [
      { type: "commit", meta: { hash: "aaaaaaa", resolves: "e-2" } },
    ]});
    A(hs.length === 0, "non-matching resolves ignored");
    A(closingEvidenceBadge({ id: "e-1", type: "task", entries: [
      { type: "commit", meta: { hash: "aaaaaaa", resolves: "e-2" } } ]}) === "",
      "no badge when nothing closed this item");

    // --- 5. task with no children / no commits → empty ---------------------
    A(closingCommits({ id: "e-3", type: "task" }).length === 0, "no children → empty");
    A(closingCommits(null).length === 0, "null → empty");
    A(closingEvidenceBadge({ id: "e-3", type: "task", entries: [] }) === "",
      "no evidence → empty badge");

    // --- 6. multiple resolving commits are all listed ---------------------
    hs = closingCommits({ id: "e-1", type: "task", entries: [
      { type: "commit", meta: { hash: "1111111aa", resolves: "e-1" } },
      { type: "commit", meta: { hash: "2222222bb", resolves: "e-1" } },
      { type: "note",   meta: { hash: "3333333cc", resolves: "e-1" } },
    ]});
    A(hs.length === 2 && hs[0] === "1111111" && hs[1] === "2222222",
      "two commits listed; non-commit (note) excluded");

    // --- 7. commit child without a hash is skipped ------------------------
    A(closingCommits({ id: "e-1", type: "task", entries: [
      { type: "commit", meta: { resolves: "e-1" } } ]}).length === 0,
      "commit without hash skipped");

    console.log("ALL_PASS");
""")


def test_closing_evidence_render(tmp_path):
    script = HARNESS.replace("__RENDER_BLOCK__", _extract_fns())
    p = tmp_path / "closing_evidence_test.mjs"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert result.returncode == 0, f"node render test failed:\n{result.stderr}\n{result.stdout}"
    assert "ALL_PASS" in result.stdout
