"""End-to-end CLI tests for table-doc row operations (ms-131 e-4496).

Drives the real ``bin/beacon doc table ...`` verbs against a fresh local-mode
project (subprocess, like the other doc CLI suites) to prove the whole chain —
bash dispatch → commands.py handler → table_doc / table_type model → write-back —
works, that type-checking engages on both write paths, that history is preserved,
and that existing markdown docs are unaffected (backward compat).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BEACON_BIN = REPO / "bin" / "beacon"

COLUMNS = json.dumps([
    {"key": "name", "label": "名前", "type": "text"},
    {"key": "phase", "label": "フェーズ", "type": "enum", "values": ["lead", "meeting", "won"]},
    {"key": "amount", "label": "金額", "type": "number"},
])


def _run(args, **kw):
    return subprocess.run([str(BEACON_BIN)] + args, text=True,
                          capture_output=True, **kw)


@pytest.fixture
def beacon_project(tmp_path, monkeypatch):
    """Fresh local-mode project with one active milestone (so entry recording
    on doc create has a target)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    subprocess.run([str(BEACON_BIN), "init"],
                   input="tbl-test\nbuild table-doc\n5\n1\n",
                   text=True, check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "add", "Test MS", "--untriaged"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "start", "ms-1"],
                   check=True, capture_output=True)
    return tmp_path


def _create_table(scope="memo"):
    r = _run(["doc", "table", "create", "顧客リスト", "--columns", COLUMNS,
              "--scope", scope, "--json"])
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["doc_id"]


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def test_create_table_doc(beacon_project):
    out = _run(["doc", "table", "create", "顧客リスト", "--columns", COLUMNS,
                "--scope", "memo", "--json"])
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["format"] == "table"
    assert payload["columns"] == ["name", "phase", "amount"]
    # The doc file carries format: table in its frontmatter.
    doc_path = beacon_project / ".beacon" / "documents" / f"{payload['doc_id']}.md"
    text = doc_path.read_text()
    assert "format: table" in text
    assert "```beacon-table" in text


def test_create_requires_columns(beacon_project):
    r = _run(["doc", "table", "create", "x", "--scope", "memo"])
    assert r.returncode == 1
    assert "columns" in (r.stdout + r.stderr).lower()


def test_create_rejects_bad_column_type(beacon_project):
    bad = json.dumps([{"key": "a", "type": "bogus"}])
    r = _run(["doc", "table", "create", "x", "--columns", bad, "--scope", "memo"])
    assert r.returncode == 1


# ---------------------------------------------------------------------------
# add-row / set-cell / rm-row / show — the operating loop
# ---------------------------------------------------------------------------

def test_row_lifecycle_and_history(beacon_project):
    doc_id = _create_table()

    # add-row (numeric string coerces to number).
    r = _run(["doc", "table", "add-row", doc_id, "--cells",
              '{"name":"Acme","phase":"lead","amount":"100"}', "--json"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["row_id"] == "r1"

    # set-cell updates and preserves the old value in history.
    assert _run(["doc", "table", "set-cell", doc_id, "r1", "phase", "meeting"]).returncode == 0

    show = _run(["doc", "table", "show", doc_id, "--json"])
    assert show.returncode == 0, show.stderr
    model = json.loads(show.stdout)
    row = model["rows"][0]
    assert row["cells"] == {"name": "Acme", "phase": "meeting", "amount": 100}
    ops = [h["op"] for h in row["history"]]
    assert ops == ["add_row", "set_cell"]
    assert row["history"][-1]["old"] == "lead"  # past value retained
    assert row["history"][-1]["new"] == "meeting"


def test_add_row_type_check_rejects_bad_enum(beacon_project):
    doc_id = _create_table()
    r = _run(["doc", "table", "add-row", doc_id, "--cells", '{"phase":"lost"}'])
    assert r.returncode == 1
    assert "enum" in (r.stdout + r.stderr)


def test_set_cell_type_check_rejects_bad_number(beacon_project):
    doc_id = _create_table()
    _run(["doc", "table", "add-row", doc_id, "--cells", '{"name":"Acme"}'])
    r = _run(["doc", "table", "set-cell", doc_id, "r1", "amount", "not-a-number"])
    assert r.returncode == 1
    assert "number" in (r.stdout + r.stderr)


def test_rm_row_soft_deletes(beacon_project):
    doc_id = _create_table()
    _run(["doc", "table", "add-row", doc_id, "--cells", '{"name":"Acme"}'])
    _run(["doc", "table", "add-row", doc_id, "--cells", '{"name":"Beta"}'])
    assert _run(["doc", "table", "rm-row", doc_id, "r2"]).returncode == 0

    # show (rendered) excludes the tombstoned row...
    rendered = _run(["doc", "table", "show", doc_id]).stdout
    assert "Acme" in rendered and "Beta" not in rendered
    # ...but the row + its history survive in the underlying file (audit trail).
    doc_path = beacon_project / ".beacon" / "documents" / f"{doc_id}.md"
    assert "Beta" in doc_path.read_text()
    assert "rm_row" in doc_path.read_text()


def test_show_renders_markdown_table(beacon_project):
    doc_id = _create_table()
    _run(["doc", "table", "add-row", doc_id, "--cells",
          '{"name":"Acme","phase":"won","amount":"5"}'])
    rendered = _run(["doc", "table", "show", doc_id]).stdout
    assert "| 名前 | フェーズ | 金額 |" in rendered
    assert "Acme" in rendered


# ---------------------------------------------------------------------------
# guards + backward compat
# ---------------------------------------------------------------------------

def test_row_ops_refuse_non_table_doc(beacon_project):
    subprocess.run([str(BEACON_BIN), "doc", "add", "plain", "--scope", "memo",
                    "--content", "just markdown"], check=True, capture_output=True)
    r = _run(["doc", "table", "add-row", "plain", "--cells", '{"x":"1"}'])
    assert r.returncode == 1
    assert "table-doc" in (r.stdout + r.stderr)


def test_show_missing_doc_exits_nonzero(beacon_project):
    r = _run(["doc", "table", "show", "does-not-exist"])
    assert r.returncode != 0


def test_existing_markdown_doc_untouched(beacon_project):
    """A plain markdown doc still round-trips through add/show unchanged
    once table-doc support exists (backward compat — SPEC 受入条件 8)."""
    subprocess.run([str(BEACON_BIN), "doc", "add", "普通のメモ", "--scope", "memo",
                    "--content", "本文はそのまま"], check=True, capture_output=True)
    show = _run(["doc", "show", "普通のメモ"])
    assert show.returncode == 0
    assert "本文はそのまま" in show.stdout
    assert "format: table" not in show.stdout
