"""Target-linkage tests for table-docs (ms-131 e-4497).

A table-doc links to any Target via the *same* frontmatter mechanism regular
docs use (SPEC 方針6 — 汎用). Covers: create linked to milestone / opportunity /
acquisition; the linked doc surfaces under `doc list --target/--ms`; the sales
Target must exist; and attach → detach → relink via the shared
`doc update --target` (the detach = `--target ""`, added uniformly so it works for
every doc, tables included).
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


def _seed_sales_entity(project_dir: Path, collection: str, entity: dict):
    # ms-148 e-5414: SQLite is the source of truth; project.json is only a
    # read-only mirror, so editing it directly no longer reaches the CLI. Seed
    # the entity THROUGH the store (the CLI subprocess reads the same db).
    import sys
    sys.path.insert(0, str(REPO / "lib"))
    from store_sqlite import SqliteStore
    store = SqliteStore(str(project_dir / ".beacon" / "project.json"))

    def _op(data):
        data.setdefault(collection, []).append(entity)
        return data, None

    store.apply(_op, validate=False)


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    subprocess.run([str(BEACON_BIN), "init"], input="lk\ntest\n5\n1\n",
                   text=True, check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "add", "MS One", "--untriaged"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "milestone", "start", "ms-1"],
                   check=True, capture_output=True)
    return tmp_path


def _linked_ids(target_flag, target_id):
    out = _run(["doc", "list", target_flag, target_id, "--json"])
    return [d["doc_id"] for d in json.loads(out.stdout)]


def test_create_linked_to_milestone(proj):
    r = _run(["doc", "table", "create", "MSリスト", "--columns", COLS,
              "--scope", "memo", "--ms", "ms-1", "--json"])
    assert r.returncode == 0, r.stderr
    doc_id = json.loads(r.stdout)["doc_id"]
    assert doc_id in _linked_ids("--ms", "ms-1")


def test_create_linked_to_acquisition(proj):
    _seed_sales_entity(proj, "acquisitions", {"id": "acq-1", "status": "active"})
    r = _run(["doc", "table", "create", "獲得", "--columns", COLS,
              "--scope", "memo", "--target", "acq-1", "--json"])
    assert r.returncode == 0, r.stderr
    doc_id = json.loads(r.stdout)["doc_id"]
    assert doc_id in _linked_ids("--target", "acq-1")


def test_create_linked_to_opportunity(proj):
    _seed_sales_entity(proj, "opportunities", {"id": "opp-1", "status": "open"})
    r = _run(["doc", "table", "create", "商談表", "--columns", COLS,
              "--scope", "memo", "--target", "opp-1", "--json"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["doc_id"] in _linked_ids("--target", "opp-1")


def test_link_to_missing_sales_target_is_rejected(proj):
    r = _run(["doc", "table", "create", "x", "--columns", COLS,
              "--scope", "memo", "--target", "acq-9"])
    assert r.returncode == 1
    assert "acquisition not found" in (r.stdout + r.stderr)


def test_detach_and_relink(proj):
    _seed_sales_entity(proj, "acquisitions", {"id": "acq-1", "status": "active"})
    _run(["doc", "table", "create", "獲得", "--columns", COLS,
          "--scope", "memo", "--target", "acq-1"])
    assert _linked_ids("--target", "acq-1") == ["獲得"]

    # Detach via the shared mechanism (--target "") — clean exit, unlinked.
    d = _run(["doc", "update", "獲得", "--target", ""])
    assert d.returncode == 0, d.stderr
    assert _linked_ids("--target", "acq-1") == []

    # Relink, and the table payload survives the round-trip.
    _run(["doc", "update", "獲得", "--target", "acq-1"])
    assert _linked_ids("--target", "acq-1") == ["獲得"]
    show = _run(["doc", "table", "show", "獲得"])
    assert show.returncode == 0
    assert "| name |" in show.stdout  # table columns survived detach→relink


def test_detach_on_plain_markdown_doc(proj):
    """The detach extension is uniform: it works for regular markdown docs too
    (not just tables), proving it follows the shared linkage mechanism."""
    _seed_sales_entity(proj, "acquisitions", {"id": "acq-2", "status": "active"})
    subprocess.run([str(BEACON_BIN), "doc", "add", "メモ", "--scope", "memo",
                    "--target", "acq-2", "--content", "body"],
                   check=True, capture_output=True)
    assert _linked_ids("--target", "acq-2") == ["メモ"]
    d = _run(["doc", "update", "メモ", "--target", ""])
    assert d.returncode == 0, d.stderr
    assert _linked_ids("--target", "acq-2") == []
