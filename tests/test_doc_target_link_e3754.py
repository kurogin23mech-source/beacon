"""Tests for ms-109 e-3754 — target-class-agnostic document linkage.

Documents historically linked to a Target via three HARDCODED frontmatter
keys (``milestone`` / ``operation`` / ``trek_id``), a set frozen in the
development era. Sales Targets (Account / Opportunity), added later, had no
linkage key at all — so "docs link to milestones but not customers" was a
wiring gap, not a design intent.

e-3754 collapses this to one canonical ``target`` key holding a single
prefixed id (``acc-1`` / ``opp-3`` / ``ms-5`` …); the target-class is derived
from the id prefix. Legacy docs still resolve via a tolerant read
(``work_model.doc_target``) so no data migration is needed.

Local-mode CLI tests drive bin/beacon through subprocess (mirrors the e-1859
test style); the tolerant-read + prefix helpers are also unit-checked directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BEACON_BIN = REPO / "bin" / "beacon"

sys.path.insert(0, str(REPO / "lib"))
import work_model  # noqa: E402


# ---------------------------------------------------------------------------
# Pure helper unit checks (no I/O)
# ---------------------------------------------------------------------------


def test_target_kind_derives_class_from_prefix():
    assert work_model.target_kind("acc-1") == "account"
    assert work_model.target_kind("opp-3") == "opportunity"
    assert work_model.target_kind("op-2") == "operation"
    assert work_model.target_kind("ms-5") == "milestone"
    assert work_model.target_kind("tk-abc123") == "trek"
    # op- (operation) and opp- (opportunity) must not collide.
    assert work_model.target_kind("op-9") != work_model.target_kind("opp-9")
    assert work_model.target_kind("") == ""
    assert work_model.target_kind("weird-1") == ""


def test_doc_target_reads_canonical_first_then_legacy():
    assert work_model.doc_target({"target": "acc-1"}) == "acc-1"
    assert work_model.doc_target({"milestone": "ms-5"}) == "ms-5"
    assert work_model.doc_target({"operation": "op-2"}) == "op-2"
    assert work_model.doc_target({"trek_id": "tk-x"}) == "tk-x"
    # canonical wins over legacy when both present.
    assert work_model.doc_target({"target": "acc-1", "milestone": "ms-5"}) == "acc-1"
    assert work_model.doc_target({}) == ""


def test_legacy_link_key_for_dual_write():
    assert work_model.legacy_link_key_for("ms-5") == "milestone"
    assert work_model.legacy_link_key_for("op-2") == "operation"
    assert work_model.legacy_link_key_for("tk-x") == "trek_id"
    # New sales classes never had a legacy key.
    assert work_model.legacy_link_key_for("acc-1") == ""
    assert work_model.legacy_link_key_for("opp-3") == ""


# ---------------------------------------------------------------------------
# Local-mode CLI tests (subprocess against bin/beacon + lib/commands.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def beacon_project(tmp_path, monkeypatch):
    """Fresh local-mode SALES project with 1 account + 1 opportunity.

    ms-115 e-3785: a project is single-profession (a project owns only its
    occupation's target-classes), so accounts/opportunities live in a *sales*
    project — a milestone can no longer be created alongside them. The ``--ms``
    linkage tests below target a bare ``ms-1`` id, which ``doc add`` accepts
    leniently (only account/opportunity ids are existence-validated), so the
    milestone need not exist for the doc-linkage behaviour under test.
    """
    monkeypatch.chdir(tmp_path)
    env = {**os.environ, "BEACON_PROFESSION": "sales"}
    # init prompt answers: name, objective, retro_day (5=Fri), storage (1=local).
    subprocess.run(
        [str(BEACON_BIN), "init"],
        input="e3754-test\nFix e3754\n5\n1\n",
        text=True, check=True, capture_output=True, env=env,
    )
    subprocess.run([str(BEACON_BIN), "account", "add", "Acme Corp"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "opportunity", "add", "Acme deal",
                    "--account", "acc-1"],
                   check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def dev_project(tmp_path, monkeypatch):
    """Fresh local-mode DEV project with 1 milestone (ms-1) — for the ``--ms``
    dual-write path, which records a doc entry *under* the milestone and so
    needs it to exist. ms-115: milestones live only in a dev project, so this is
    a separate fixture from the sales ``beacon_project``.
    """
    monkeypatch.chdir(tmp_path)
    subprocess.run(
        [str(BEACON_BIN), "init"],
        input="e3754-dev\nFix e3754\n5\n1\n",
        text=True, check=True, capture_output=True,
    )
    subprocess.run([str(BEACON_BIN), "milestone", "add", "Test MS"],
                   check=True, capture_output=True)
    return tmp_path


def _run(*args, check=True):
    return subprocess.run([str(BEACON_BIN), *args], text=True,
                          check=check, capture_output=True)


def _list_docs_json(args=None):
    out = _run("doc", "list", "--json", *(args or [])).stdout
    return json.loads(out.strip())


def _read_frontmatter(project_dir: Path, doc_id: str) -> dict:
    p = project_dir / ".beacon" / "documents" / f"{doc_id}.md"
    text = p.read_text()
    assert text.startswith("---"), f"no frontmatter in {p}"
    end = text.find("\n---", 3)
    meta = {}
    for line in text[4:end].split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta


# AC: --account writes the canonical target only (a customer Target had no
# legacy milestone/operation key, so none should appear).
def test_doc_add_account_writes_target_only(beacon_project):
    _run("doc", "add", "Acme dossier", "--scope", "spec",
         "--account", "acc-1", "--content", "顧客について分かったこと")
    fm = _read_frontmatter(beacon_project, "acme-dossier")
    assert fm.get("target") == "acc-1"
    assert "milestone" not in fm
    assert "operation" not in fm


# AC: doc list --account <id> surfaces a customer's docs (the dossier use case).
def test_doc_list_account_filter(beacon_project):
    _run("doc", "add", "Acme dossier", "--scope", "spec",
         "--account", "acc-1", "--content", "body")
    # A doc linked to a different Target (the opportunity, a real target in this
    # sales project) must be excluded from the account filter.
    _run("doc", "add", "Unrelated", "--scope", "spec",
         "--opportunity", "opp-1", "--content", "body")
    ids = {d["doc_id"] for d in _list_docs_json(["--account", "acc-1"])}
    assert "acme-dossier" in ids
    assert "unrelated" not in ids


# AC: doc list --json carries the target field for downstream filters / UI.
def test_doc_list_json_includes_target(beacon_project):
    _run("doc", "add", "Acme dossier", "--scope", "spec",
         "--account", "acc-1", "--content", "body")
    [doc] = [d for d in _list_docs_json() if d["doc_id"] == "acme-dossier"]
    assert doc.get("target") == "acc-1"


# AC: linking to a non-existent Account is refused (hard-validated).
def test_doc_add_unknown_account_errors(beacon_project):
    r = _run("doc", "add", "bad", "--scope", "spec",
             "--account", "acc-999", "--content", "x", check=False)
    assert r.returncode != 0
    assert "not found" in (r.stdout + r.stderr)


# AC: --opportunity links to a deal via the same canonical key.
def test_doc_add_opportunity_writes_target(beacon_project):
    _run("doc", "add", "Deal notes", "--scope", "spec",
         "--opportunity", "opp-1", "--content", "body")
    fm = _read_frontmatter(beacon_project, "deal-notes")
    assert fm.get("target") == "opp-1"
    ids = {d["doc_id"] for d in _list_docs_json(["--opportunity", "opp-1"])}
    assert "deal-notes" in ids


# Back-compat: --ms dual-writes both the canonical target and the legacy
# milestone key so existing readers (--ms filter, operation SPEC discovery)
# keep working.
def test_doc_add_ms_dual_writes_target_and_milestone(dev_project):
    _run("doc", "add", "MS spec", "--scope", "spec",
         "--ms", "ms-1", "--content", "body")
    fm = _read_frontmatter(dev_project, "ms-spec")
    assert fm.get("target") == "ms-1"
    assert fm.get("milestone") == "ms-1"
    # Legacy --ms filter still works …
    assert {d["doc_id"] for d in _list_docs_json(["--ms", "ms-1"])} >= {"ms-spec"}
    # … and so does the new generic --target filter.
    assert {d["doc_id"] for d in _list_docs_json(["--target", "ms-1"])} >= {"ms-spec"}


# Migration-free back-compat: a legacy doc carrying ONLY milestone: (no
# canonical target) must still resolve under --target via the tolerant read.
def test_tolerant_read_of_legacy_milestone_only_doc(beacon_project):
    docs_dir = beacon_project / ".beacon" / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "legacy-doc.md").write_text(
        "---\nscope: spec\nmilestone: ms-1\n---\n\n# Legacy doc\nbody\n"
    )
    ids = {d["doc_id"] for d in _list_docs_json(["--target", "ms-1"])}
    assert "legacy-doc" in ids
