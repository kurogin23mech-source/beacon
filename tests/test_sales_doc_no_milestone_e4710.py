"""Regression test for the sales-doc-in-a-milestone-less-project bugs
(ms-134 e-4710 / e-4711 / e-4712), fixed structurally by e-4720.

Before the fix, ``doc add`` / ``doc update`` recorded their side-effect entry
via the dev concrete ``core.save_entry(ms_id=…)``, which auto-picks the active
milestone and RAISES "No active milestone" in a project that has none — every
sales project. So the document write succeeded but the command exited non-zero
(e-4710) and the raised error shadowed ``--account`` linkage (e-4711).

This test drives the REAL CLI (``bin/beacon``) against a temp sales project with
zero milestones and asserts every doc verb exits 0 — the end-to-end proof that
the occupation abstraction (``record_target_entry``) no-ops instead of erroring.
"""

import json
import os
import subprocess

import pytest

REPO = os.path.join(os.path.dirname(__file__), "..")
BEACON = os.path.join(REPO, "bin", "beacon")

_SALES_PROJECT = {
    "name": "SalesDocRegression",
    "profession": "sales",
    "milestones": [],
    "opportunities": [{"id": "opp-1", "label": "Acme 商談", "phase": "商談準備",
                       "status": "open", "account_id": "acc-1", "activities": []}],
    "accounts": [{"id": "acc-1", "name": "Acme", "label": "Acme",
                  "phase": "リード", "contacts": []}],
    "documents": [],
}


@pytest.fixture()
def sales_project(tmp_path):
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir()
    (beacon_dir / "project.json").write_text(
        json.dumps(_SALES_PROJECT, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _run(cwd, *args):
    return subprocess.run([BEACON, *args], cwd=str(cwd),
                          capture_output=True, text=True)


def test_doc_add_no_target_succeeds_in_sales(sales_project):
    """e-4710: a doc with no Target in a milestone-less project exits 0."""
    r = _run(sales_project, "doc", "add", "顧客メモ", "--scope", "memo",
             "--content", "初回接触の所感")
    assert r.returncode == 0, r.stderr


def test_doc_add_linked_to_account_succeeds_and_resolves(sales_project):
    """e-4711: --account links the doc and the write completes (exit 0)."""
    r = _run(sales_project, "doc", "add", "Acme dossier", "--scope", "memo",
             "--account", "acc-1", "--content", "組織図メモ")
    assert r.returncode == 0, r.stderr
    # the linkage resolves via `doc list --account`.
    lst = _run(sales_project, "doc", "list", "--account", "acc-1", "--json")
    assert lst.returncode == 0, lst.stderr
    docs = json.loads(lst.stdout)
    assert len(docs) == 1 and docs[0].get("target") == "acc-1"


def test_doc_update_succeeds_in_sales(sales_project):
    """e-4710 (update case): updating a doc in a sales project exits 0."""
    _run(sales_project, "doc", "add", "Acme dossier", "--scope", "memo",
         "--account", "acc-1", "--content", "v1")
    r = _run(sales_project, "doc", "update", "acme-dossier", "--content", "v2")
    assert r.returncode == 0, r.stderr


def test_doc_delete_succeeds_in_sales(sales_project):
    """e-4712: a doc can be removed in a sales project (exit 0)."""
    _run(sales_project, "doc", "add", "捨てるメモ", "--scope", "memo",
         "--content", "x")
    r = _run(sales_project, "doc", "delete", "捨てるメモ", "--reason", "cleanup")
    assert r.returncode == 0, r.stderr
