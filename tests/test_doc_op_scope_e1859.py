"""Regression tests for e-1859 — beacon doc CLI op scope support.

Original bug surface (LPS dogfood event 2dHJZHkOAGJ1I3kLog1d, 2026-06-16):
- `beacon doc update <id> --op <op-id>` flag missing (or wiring broken)
- `beacon doc update --ms <ms-id>` on an op-scoped doc silently kept
  `operation:` and added `milestone:` (= two-headed frontmatter), so
  `/beacon-operation-review` Step 2 (= `beacon doc list --scope spec
  --op <op-id>` discovery) could not reliably filter on op binding.
- Cloud-side list_documents only returned `milestone` field; `operation`
  was invisible to `--op` filtering even though it lived in frontmatter.
- save_document on cloud side never extracted `operation` from frontmatter
  to store as a top-level row column.

These tests cover the four AC items from the task plus one cross-mode
parity check. Local-mode tests drive the CLI through subprocess; cloud
parity tests target the DynamoDB / Firestore clients via moto / mocks.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


# ---------------------------------------------------------------------------
# Local-mode CLI tests (subprocess against bin/beacon + lib/commands.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def beacon_project(tmp_path, monkeypatch):
    """Init a fresh local-mode beacon project with 1 milestone and 2 operations."""
    monkeypatch.chdir(tmp_path)
    # ms-126 / e-4222: the milestone below is seeded with --untriaged as a setup
    # convenience (this suite tests doc scoping, not priority triage). Since
    # untriaged is now a machine-only capability — a human session
    # (conftest defaults BEACON_SESSION_KIND=human, propagated to subprocess
    # children) is refused — declare this seeding a machine session so the
    # --untriaged add is honoured.
    monkeypatch.delenv("BEACON_SESSION_KIND", raising=False)
    # Pipe answers into `beacon init` interactive prompt:
    # name, objective, retro_day (5=Fri), storage (1=local).
    init_in = "e1859-test\nFix e-1859\n5\n1\n"
    subprocess.run(
        [str(BEACON_BIN), "init"],
        input=init_in,
        text=True,
        check=True,
        capture_output=True,
    )
    subprocess.run([str(BEACON_BIN), "milestone", "add", "Test MS", "--untriaged"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "operation", "open", "Op One"],
                   check=True, capture_output=True)
    subprocess.run([str(BEACON_BIN), "operation", "open", "Op Two"],
                   check=True, capture_output=True)
    return tmp_path


def _list_docs_json(args=None):
    """Run `beacon doc list --json [...]` and return parsed list."""
    cmd = [str(BEACON_BIN), "doc", "list", "--json"] + list(args or [])
    out = subprocess.run(cmd, check=True, text=True, capture_output=True).stdout
    return json.loads(out.strip())


def _read_frontmatter(project_dir: Path, doc_id: str) -> dict:
    """Parse the YAML-ish frontmatter of a local-mode doc file."""
    p = project_dir / ".beacon" / "documents" / f"{doc_id}.md"
    text = p.read_text()
    assert text.startswith("---"), f"no frontmatter in {p}"
    end = text.find("\n---", 3)
    header = text[4:end]
    meta = {}
    for line in header.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta


# AC1: doc update --op flag wiring works end-to-end (= the CLI shell parses
# it, the env var reaches commands.py, and the doc's frontmatter changes).
def test_doc_update_op_flag_wires_through(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op SPEC", "--scope", "spec",
         "--op", "op-1", "--content", "body"],
        check=True, capture_output=True,
    )
    # Sanity: the doc starts op-bound.
    fm = _read_frontmatter(beacon_project, "op-spec")
    assert fm.get("operation") == "op-1"
    assert "milestone" not in fm

    # Switch the doc to op-2 via --op flag.
    subprocess.run(
        [str(BEACON_BIN), "doc", "update", "op-spec", "--op", "op-2"],
        check=True, capture_output=True,
    )
    fm = _read_frontmatter(beacon_project, "op-spec")
    assert fm.get("operation") == "op-2"
    assert "milestone" not in fm


# AC2 part 1: --ms on an op-scoped doc explicitly switches scope (= the old
# `operation:` field is removed, not kept alongside the new `milestone:`).
def test_doc_update_ms_drops_prior_operation(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op SPEC", "--scope", "spec",
         "--op", "op-1", "--content", "body"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(BEACON_BIN), "doc", "update", "op-spec", "--ms", "ms-1"],
        check=True, capture_output=True,
    )
    fm = _read_frontmatter(beacon_project, "op-spec")
    assert fm.get("milestone") == "ms-1"
    assert "operation" not in fm, (
        "e-1859: --ms must remove prior operation, not leave both set"
    )


# AC2 part 2: --op on a ms-scoped doc symmetrically drops `milestone`.
def test_doc_update_op_drops_prior_milestone(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "MS SPEC", "--scope", "spec",
         "--ms", "ms-1", "--content", "body"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(BEACON_BIN), "doc", "update", "ms-spec", "--op", "op-1"],
        check=True, capture_output=True,
    )
    fm = _read_frontmatter(beacon_project, "ms-spec")
    assert fm.get("operation") == "op-1"
    assert "milestone" not in fm, (
        "e-1859: --op must remove prior milestone, not leave both set"
    )


# AC2 part 3: update with neither --ms nor --op preserves whichever
# binding the doc already had (= "no flag, no change").
def test_doc_update_without_scope_flags_preserves_binding(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op SPEC", "--scope", "spec",
         "--op", "op-1", "--content", "body v1"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(BEACON_BIN), "doc", "update", "op-spec",
         "--content", "body v2"],
        check=True, capture_output=True,
    )
    fm = _read_frontmatter(beacon_project, "op-spec")
    assert fm.get("operation") == "op-1"
    assert "milestone" not in fm


# AC3: doc list --json output includes the `operation` field for op-scoped
# docs. Without this, downstream filters in Skills / Web UI silently treat
# op-scoped docs as scope-less.
def test_doc_list_json_includes_operation_field(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op SPEC", "--scope", "spec",
         "--op", "op-1", "--content", "body"],
        check=True, capture_output=True,
    )
    docs = _list_docs_json()
    [doc] = [d for d in docs if d["doc_id"] == "op-spec"]
    assert doc.get("operation") == "op-1"


# AC4: doc list --op <op-id> filter returns the matching docs (and only
# those). The LPS report observed this returning [] regardless of input.
def test_doc_list_op_filter_returns_matching_docs(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "A", "--scope", "spec",
         "--op", "op-1", "--content", "a"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "B", "--scope", "spec",
         "--op", "op-2", "--content", "b"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "C", "--scope", "memo",
         "--ms", "ms-1", "--content", "c"],
        check=True, capture_output=True,
    )

    op1 = _list_docs_json(["--op", "op-1"])
    assert {d["doc_id"] for d in op1} == {"a"}

    op2 = _list_docs_json(["--op", "op-2"])
    assert {d["doc_id"] for d in op2} == {"b"}

    op99 = _list_docs_json(["--op", "op-99"])
    assert op99 == []


# AC5: /beacon-operation-review Step 2 discovery query
# (`beacon doc list --scope spec --op <op-id>`) finds the SPEC doc.
def test_operation_review_step2_discovery_query(beacon_project):
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op-1 SPEC", "--scope", "spec",
         "--op", "op-1", "--content", "log fetch instructions"],
        check=True, capture_output=True,
    )
    # Add some noise: same op, different scope; and same scope, different op.
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op-1 Memo", "--scope", "memo",
         "--op", "op-1", "--content", "noise"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [str(BEACON_BIN), "doc", "add", "Op-2 SPEC", "--scope", "spec",
         "--op", "op-2", "--content", "noise"],
        check=True, capture_output=True,
    )

    docs = _list_docs_json(["--scope", "spec", "--op", "op-1"])
    assert {d["doc_id"] for d in docs} == {"op-1-spec"}, (
        f"Step 2 discovery should match exactly one doc, got {docs}"
    )


# ---------------------------------------------------------------------------
# Cloud-mode parity tests (DynamoDB via moto)
# ---------------------------------------------------------------------------


@pytest.fixture
def ddb(monkeypatch):
    """Boot moto-backed DynamoDB and yield the freshly-imported client."""
    monkeypatch.setenv("BEACON_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("BEACON_ENV", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    moto = pytest.importorskip("moto")
    import boto3

    mock_ctx = moto.mock_aws()
    mock_ctx.start()
    try:
        ddb_res = boto3.resource("dynamodb", region_name="us-east-1")
        for name, sk in [
            ("beacon-dev-documents", "doc_id"),
            ("beacon-dev-document_revisions", "revision_id"),
            ("beacon-dev-retros", "week"),
        ]:
            ddb_res.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": "project_id", "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "project_id", "AttributeType": "S"},
                    {"AttributeName": sk, "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        sys.modules.pop("dynamodb_client", None)
        import dynamodb_client as _db

        yield _db
    finally:
        mock_ctx.stop()
        sys.modules.pop("dynamodb_client", None)


PID = "proj-e1859"


def test_ddb_save_document_extracts_operation_from_frontmatter(ddb):
    """e-1859: cloud-side save_document must lift operation: from frontmatter
    into a row column, mirroring milestone: handling."""
    content = "---\nscope: spec\noperation: op-7\n---\nbody\n"
    new_id = ddb.save_document(PID, "", "Op SPEC", content, updated_by="alice")

    got = ddb.get_document(PID, new_id)
    assert got["operation"] == "op-7"
    assert got["scope"] == "spec"
    assert "milestone" not in got


def test_ddb_list_documents_includes_operation_field(ddb):
    """e-1859: list_documents must return `operation` in each entry so
    `--op` filtering and `/beacon-operation-review` Step 2 work."""
    content = "---\nscope: spec\noperation: op-7\n---\nbody\n"
    ddb.save_document(PID, "doc-a", "Op SPEC", content)
    ddb.save_document(PID, "doc-b", "Plain", "---\nscope: memo\n---\nx")

    listed = ddb.list_documents(PID)
    by_id = {d["doc_id"]: d for d in listed}
    assert by_id["doc-a"].get("operation") == "op-7"
    assert "operation" not in by_id["doc-b"], (
        "non-op-scoped docs must not get an empty `operation` field"
    )


def test_ddb_list_documents_falls_back_to_frontmatter(ddb):
    """e-1859: docs written before this fix (= operation in frontmatter but
    not in row) must still surface via list_documents — frontmatter is the
    fallback source so existing data is not orphaned."""
    # Simulate a pre-fix row: content has operation in frontmatter but the
    # top-level column was never written. ddb._table("documents") resolves
    # via the TABLES map to the env-suffixed Dynamo table name.
    ddb._table("documents").put_item(Item={
        "project_id": PID,
        "doc_id": "legacy",
        "title": "Legacy Op Doc",
        "content": "---\nscope: spec\noperation: op-legacy\n---\nbody",
        "scope": "spec",
        "updated_at": "2026-06-01T00:00:00",
    })
    listed = ddb.list_documents(PID)
    [doc] = [d for d in listed if d["doc_id"] == "legacy"]
    assert doc.get("operation") == "op-legacy"


# ---------------------------------------------------------------------------
# Cloud-mode parity tests (Firestore via source-level inspection)
#
# We deliberately do not boot a Firestore mock for save_document — pulling
# `firestore_client` into sys.modules under this test session has been
# observed to corrupt the get_db default for other tests (= test_api.py
# starts hitting live Firestore credentials). The same logic is exercised
# end-to-end on the DynamoDB side above; for Firestore we assert the
# source contains the same operation-extraction shape, which is cheap and
# leaves zero global side effects.
# ---------------------------------------------------------------------------


def test_firestore_client_extracts_operation_field():
    """Cheap source-level guard: firestore_client must call
    `_extract_frontmatter_field(..., "operation")` in save_document, the
    same way it already does for milestone. Without this, save_document
    silently drops op-scope info into the content blob with no indexable
    column, and list_documents has nothing to surface."""
    src = (REPO / "server" / "firestore_client.py").read_text()
    assert 'operation = data.get("operation")' in src or \
           'operation = _extract_frontmatter_field' in src, (
        "firestore_client.list_documents must surface operation field "
        "(e-1859 cloud-mode parity)"
    )
    # The save path must extract operation from the frontmatter so the
    # next list_documents call has a column to read.
    assert '_extract_frontmatter_field(content, "operation")' in src, (
        "firestore_client.save_document must extract operation from "
        "frontmatter (e-1859)"
    )
