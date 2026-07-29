"""CLI tests for the attack-list verbs (ms-132 e-4501).

`beacon acquisition attack-list` creates an attack-list (table-doc) linked to a
施策, and `beacon acquisition attack-lists` surfaces the施策's attack-lists with
per-phase counts. Exercises AC1 (紐づけ), AC2 (canonical row schema) and AC3 (show
under the施策), plus the guardrails (missing施策 rejected, non-attack-list
table-docs ignored).
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


def _seed_sales_entity(project_dir: Path, collection: str, entity: dict):
    p = project_dir / ".beacon" / "project.json"
    data = json.loads(p.read_text())
    data.setdefault(collection, []).append(entity)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    subprocess.run([str(BEACON_BIN), "init"], input="al\ntest\n5\n1\n",
                   text=True, check=True, capture_output=True)
    _seed_sales_entity(tmp_path, "acquisitions", {"id": "acq-1", "status": "todo"})
    return tmp_path


def _attach(acq_id, title, *extra):
    return _run(["acquisition", "attack-list", acq_id, title, "--json", *extra])


def test_attach_list_creates_linked_table(proj):
    r = _attach("acq-1", "攻略リスト")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    doc_id = out["doc_id"]
    assert out["format"] == "table"
    # Linked to the施策 (surfaces via the generic target filter).
    linked = json.loads(_run(["doc", "list", "--target", "acq-1", "--json"]).stdout)
    assert doc_id in [d["doc_id"] for d in linked]
    # Canonical row schema (AC2).
    keys = {c["key"] for c in out["columns"]}
    assert keys == {"account", "phase", "last_contact", "memo"}


def test_attach_list_rejects_missing_acquisition(proj):
    r = _run(["acquisition", "attack-list", "acq-9", "x"])
    assert r.returncode == 1
    assert "acquisition not found" in (r.stdout + r.stderr)


def test_attach_list_phase_override(proj):
    r = _attach("acq-1", "英語ファネル", "--phases", "cold,warm,hot")
    assert r.returncode == 0, r.stderr
    cols = {c["key"]: c for c in json.loads(r.stdout)["columns"]}
    assert cols["phase"]["values"] == ["cold", "warm", "hot"]


def test_lists_shows_attack_lists_with_phase_counts(proj):
    doc_id = json.loads(_attach("acq-1", "攻略リスト").stdout)["doc_id"]
    for acc, phase in [("acc-1", "未接触"), ("acc-2", "未接触"), ("acc-3", "連絡済")]:
        cells = json.dumps({"account": acc, "phase": phase})
        assert _run(["doc", "table", "add-row", doc_id,
                     "--cells", cells]).returncode == 0

    out = json.loads(_run(["acquisition", "attack-lists", "acq-1", "--json"]).stdout)
    assert out["acquisition"] == "acq-1"
    assert len(out["lists"]) == 1
    lst = out["lists"][0]
    assert lst["doc_id"] == doc_id
    assert lst["row_count"] == 3
    assert lst["phase_counts"] == {"未接触": 2, "連絡済": 1}


def test_lists_empty_when_no_attack_list(proj):
    out = json.loads(_run(["acquisition", "attack-lists", "acq-1", "--json"]).stdout)
    assert out == {"acquisition": "acq-1", "lists": []}


def test_lists_ignores_non_attack_list_table_docs(proj):
    # A plain (non-attack-list) table-doc linked to the same施策 is not surfaced.
    cols = json.dumps([{"key": "note", "type": "text"}])
    _run(["doc", "table", "create", "別表", "--columns", cols,
          "--scope", "memo", "--target", "acq-1"])
    attack_doc_id = json.loads(_attach("acq-1", "攻略リスト").stdout)["doc_id"]
    out = json.loads(_run(["acquisition", "attack-lists", "acq-1", "--json"]).stdout)
    # Only the attack-list surfaces — assert on the real doc_id, not the title.
    assert [l["doc_id"] for l in out["lists"]] == [attack_doc_id]


def test_lists_rejects_missing_acquisition(proj):
    r = _run(["acquisition", "attack-lists", "acq-9"])
    assert r.returncode == 1
    assert "acquisition not found" in (r.stdout + r.stderr)
