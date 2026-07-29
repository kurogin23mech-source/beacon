"""Consolidated regression e2e for the table-doc primitive (ms-131 e-4499).

Two guarantees the per-feature suites touch individually, pinned here as whole
flows:

1. The full table lifecycle end to end — create → add-row → set-cell → show →
   link — with the append-only history verified to grow and preserve every past
   value at each step (SPEC 受入条件 3).
2. Existing markdown docs are non-regressed while table-docs coexist in the same
   project: add / list / show / update on a markdown doc behave exactly as before
   (SPEC 受入条件 8).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BEACON_BIN = REPO / "bin" / "beacon"


def _run(args, **kw):
    return subprocess.run([str(BEACON_BIN)] + args, text=True,
                          capture_output=True, **kw)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    subprocess.run([str(BEACON_BIN), "init"], input="e2e\nlifecycle\n5\n1\n",
                   text=True, check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "add", "MS", "--untriaged"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "start", "ms-1"],
                   check=True, capture_output=True)
    return tmp_path


COLS = json.dumps([
    {"key": "name", "label": "名前", "type": "text"},
    {"key": "phase", "label": "フェーズ", "type": "enum", "values": ["lead", "meeting", "won"]},
    {"key": "amount", "label": "金額", "type": "number"},
])


def _show(doc_id):
    out = _run(["doc", "table", "show", doc_id, "--json"])
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_full_lifecycle_preserves_history(proj):
    # create
    r = _run(["doc", "table", "create", "顧客リスト", "--columns", COLS,
              "--scope", "memo", "--ms", "ms-1", "--json"])
    assert r.returncode == 0, r.stderr
    doc_id = json.loads(r.stdout)["doc_id"]

    # add-row x2
    assert _run(["doc", "table", "add-row", doc_id, "--cells",
                 '{"name":"Acme","phase":"lead","amount":"100"}']).returncode == 0
    assert _run(["doc", "table", "add-row", doc_id, "--cells",
                 '{"name":"Beta","phase":"lead"}']).returncode == 0

    # set-cell twice on the SAME cell — every prior value must survive in history
    assert _run(["doc", "table", "set-cell", doc_id, "r1", "phase", "meeting"]).returncode == 0
    assert _run(["doc", "table", "set-cell", doc_id, "r1", "phase", "won"]).returncode == 0

    model = _show(doc_id)
    r1 = next(r for r in model["rows"] if r["id"] == "r1")
    assert r1["cells"]["phase"] == "won"
    ops = [(h["op"], h.get("old"), h.get("new")) for h in r1["history"]]
    assert ops == [
        ("add_row", None, None),        # seed (old/new absent on add)
        ("set_cell", "lead", "meeting"),
        ("set_cell", "meeting", "won"),
    ], ops
    # add_row seed keeps the initial cells snapshot
    assert r1["history"][0]["cells"] == {"name": "Acme", "phase": "lead", "amount": 100}

    # rm-row is a tombstone: audit trail survives, active view shrinks
    assert _run(["doc", "table", "rm-row", doc_id, "r2"]).returncode == 0
    model = _show(doc_id)
    assert [r["id"] for r in model["rows"]] == ["r1"]
    assert model["removed_count"] == 1

    # link surfaces the table-doc under its milestone; detach removes it
    assert doc_id in [d["doc_id"] for d in
                      json.loads(_run(["doc", "list", "--ms", "ms-1", "--json"]).stdout)]
    assert _run(["doc", "update", doc_id, "--target", ""]).returncode == 0
    assert doc_id not in [d["doc_id"] for d in
                          json.loads(_run(["doc", "list", "--ms", "ms-1", "--json"]).stdout)]
    # ...and the table payload survived the detach round-trip
    assert _show(doc_id)["rows"][0]["cells"]["phase"] == "won"


def test_markdown_docs_non_regressed_alongside_tables(proj):
    # A table-doc and a markdown doc live side by side.
    _run(["doc", "table", "create", "表", "--columns",
          json.dumps([{"key": "a", "type": "text"}]), "--scope", "memo", "--ms", "ms-1"])
    subprocess.run([str(BEACON_BIN), "doc", "add", "普通メモ", "--scope", "memo",
                    "--ms", "ms-1", "--content", "元の本文"],
                   check=True, capture_output=True)

    # add + list: both docs listed; markdown doc has no table format
    docs = json.loads(_run(["doc", "list", "--json"]).stdout)
    by_id = {d["doc_id"]: d for d in docs}
    assert "普通メモ" in by_id and "表" in by_id
    assert by_id["表"].get("format") == "table"
    assert "format" not in by_id["普通メモ"]  # markdown default omits the key

    # show: markdown content is returned verbatim (marked untouched)
    show = _run(["doc", "show", "普通メモ"])
    assert show.returncode == 0 and "元の本文" in show.stdout
    assert "format: table" not in show.stdout

    # update: markdown update still works and doesn't gain a table format
    up = _run(["doc", "update", "普通メモ", "--content", "更新後の本文"])
    assert up.returncode == 0, up.stderr
    show2 = _run(["doc", "show", "普通メモ"])
    assert "更新後の本文" in show2.stdout
    assert "format: table" not in show2.stdout

    # the coexisting table-doc is unaffected by markdown-doc operations
    tbl = json.loads(_run(["doc", "table", "show", "表", "--json"]).stdout)
    assert [c["key"] for c in tbl["columns"]] == ["a"]


def test_row_ops_refuse_on_markdown_doc(proj):
    """A markdown doc must reject table row operations (format guard)."""
    subprocess.run([str(BEACON_BIN), "doc", "add", "メモ", "--scope", "memo",
                    "--ms", "ms-1", "--content", "body"],
                   check=True, capture_output=True)
    r = _run(["doc", "table", "add-row", "メモ", "--cells", '{"x":"1"}'])
    assert r.returncode == 1
    assert "table-doc" in (r.stdout + r.stderr)
