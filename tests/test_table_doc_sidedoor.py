"""Write-path side-door closure for table-docs (ms-131 e-4544, philosophy review).

SPEC 方針4: the type-check + append-only history must be the ONLY way a
table-doc's rows change. A generic `doc update --content ...` bypassed that (no
type-check, no history, and could silently drop `format: table`). These tests
pin that the side door is closed for content replacement, while the
frontmatter-only updates that detach/linkage rely on stay open.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BEACON_BIN = REPO / "bin" / "beacon"
COLS = json.dumps([{"key": "name", "type": "text"}])


def _run(args, **kw):
    return subprocess.run([str(BEACON_BIN)] + args, text=True,
                          capture_output=True, **kw)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    subprocess.run([str(BEACON_BIN), "init"], input="sd\ntest\n5\n1\n",
                   text=True, check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "add", "MS", "--untriaged"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "start", "ms-1"],
                   check=True, capture_output=True)
    return tmp_path


def _make_table(scope="memo", link=("--ms", "ms-1")):
    r = _run(["doc", "table", "create", "表", "--columns", COLS, "--scope", scope,
              *link, "--json"])
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["doc_id"]


def test_doc_update_content_refused_on_table_doc(proj):
    doc_id = _make_table()
    _run(["doc", "table", "add-row", doc_id, "--cells", '{"name":"A"}'])

    r = _run(["doc", "update", doc_id, "--content", "prose that would clobber the table"])
    assert r.returncode == 1
    assert "table-doc" in (r.stdout + r.stderr)

    # The doc is untouched: still a table, row + format intact.
    doc_path = proj / ".beacon" / "documents" / f"{doc_id}.md"
    text = doc_path.read_text()
    assert "format: table" in text
    assert "```beacon-table" in text
    model = json.loads(_run(["doc", "table", "show", doc_id, "--json"]).stdout)
    assert model["rows"][0]["cells"]["name"] == "A"


def test_doc_update_stdin_content_refused_on_table_doc(proj):
    doc_id = _make_table()
    _run(["doc", "table", "add-row", doc_id, "--cells", '{"name":"A"}'])
    r = subprocess.run([str(BEACON_BIN), "doc", "update", doc_id, "--stdin"],
                       input="clobber", text=True, capture_output=True)
    assert r.returncode == 1
    assert "table-doc" in (r.stdout + r.stderr)
    # The stdin path must leave the doc untouched too (symmetry with --content).
    text = (proj / ".beacon" / "documents" / f"{doc_id}.md").read_text()
    assert "format: table" in text and "clobber" not in text
    model = json.loads(_run(["doc", "table", "show", doc_id, "--json"]).stdout)
    assert model["rows"][0]["cells"]["name"] == "A"


def test_linkage_update_still_allowed_on_table_doc(proj):
    """Detach/relink (frontmatter-only, no --content) must remain open — e-4497
    depends on `doc update --target` working on table-docs."""
    doc_id = _make_table()
    assert doc_id in [d["doc_id"] for d in
                      json.loads(_run(["doc", "list", "--ms", "ms-1", "--json"]).stdout)]
    # detach
    d = _run(["doc", "update", doc_id, "--target", ""])
    assert d.returncode == 0, d.stderr
    assert doc_id not in [d2["doc_id"] for d2 in
                          json.loads(_run(["doc", "list", "--ms", "ms-1", "--json"]).stdout)]
    # relink
    assert _run(["doc", "update", doc_id, "--ms", "ms-1"]).returncode == 0
    # table payload survived the frontmatter-only updates
    assert json.loads(_run(["doc", "table", "show", doc_id, "--json"]).stdout)["columns"][0]["key"] == "name"


def test_content_update_still_works_on_markdown_doc(proj):
    """The guard is table-doc-specific: plain markdown docs update as before."""
    subprocess.run([str(BEACON_BIN), "doc", "add", "メモ", "--scope", "memo",
                    "--ms", "ms-1", "--content", "元"], check=True, capture_output=True)
    r = _run(["doc", "update", "メモ", "--content", "更新後"])
    assert r.returncode == 0, r.stderr
    assert "更新後" in _run(["doc", "show", "メモ"]).stdout
