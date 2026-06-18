"""Logic tests for server/dynamodb_client.py Phase 2 (e-1544).

Covers the 9 functions ported in Phase 2:
- list_retros / get_retro / save_retro
- list_documents / get_document / save_document
- list_document_revisions / get_document_revision
- delete_document (soft) / sweep_trashed_documents (hard + cascade)

Uses moto's mock_aws to run the real boto3 code paths against an
in-memory DynamoDB so schema choices (PK=project_id, SK=doc_id /
revision_id="{doc_id}#{rev:06d}") are exercised end-to-end, not just
syntax-checked.
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


@pytest.fixture
def db(monkeypatch):
    """Boot a moto-backed DynamoDB and return the freshly-imported client.

    The module is reimported per test so the lazy ``_RESOURCE`` cache
    does not leak between tests (different mock_aws contexts).
    """
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
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, sk in [
            ("beacon-dev-documents", "doc_id"),
            ("beacon-dev-document_revisions", "revision_id"),
            ("beacon-dev-retros", "week"),
        ]:
            ddb.create_table(
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


PID = "proj-x"


def test_retros_roundtrip_and_desc_order(db):
    assert db.list_retros(PID) == []
    db.save_retro(PID, "2026-W24", "## what went well\n- ...")
    db.save_retro(PID, "2026-W23", "older")

    got = db.get_retro(PID, "2026-W24")
    assert got and got["content"].startswith("## what")

    listed = db.list_retros(PID)
    assert [r["week"] for r in listed] == ["2026-W24", "2026-W23"]


def test_save_document_autogen_id_and_frontmatter_extraction(db):
    content_md = "---\nscope: core\nmilestone: ms-64\n---\n# title body\n"
    new_id = db.save_document(PID, "", "Test doc", content_md, updated_by="alice")
    assert new_id and len(new_id) == 20

    got = db.get_document(PID, new_id)
    assert got["scope"] == "core"
    assert got["milestone"] == "ms-64"
    assert got["updated_by"] == "alice"


def test_list_documents_excludes_soft_deleted(db):
    db.save_document(PID, "doc-a", "Doc A", "body")
    db.save_document(PID, "doc-b", "Doc B", "body")
    assert db.delete_document(PID, "doc-a") is True

    listed = db.list_documents(PID)
    ids = [d["doc_id"] for d in listed]
    assert "doc-a" not in ids
    assert "doc-b" in ids


def test_revisions_pushed_on_overwrite_and_isolated_per_doc(db):
    # Doc A: 3 saves -> 2 revisions (v1, v2) stored, v3 is current
    db.save_document(PID, "doc-a", "Doc A", "body 1")
    db.save_document(PID, "doc-a", "Doc A v2", "body 2", updated_by="bob")
    db.save_document(PID, "doc-a", "Doc A v3", "body 3", updated_by="carol")

    revs = db.list_document_revisions(PID, "doc-a")
    assert [r["rev"] for r in revs] == [2, 1]  # DESC

    rev1 = db.get_document_revision(PID, "doc-a", 1)
    assert rev1["title"] == "Doc A"
    assert rev1["content"] == "body 1"
    rev2 = db.get_document_revision(PID, "doc-a", 2)
    assert rev2["title"] == "Doc A v2"
    assert rev2["content"] == "body 2"
    assert db.get_document_revision(PID, "doc-a", 99) is None

    # Doc B revisions must not bleed into Doc A
    db.save_document(PID, "doc-b", "Doc B", "x")
    db.save_document(PID, "doc-b", "Doc B v2", "y")
    assert len(db.list_document_revisions(PID, "doc-b")) == 1
    assert len(db.list_document_revisions(PID, "doc-a")) == 2


def test_delete_document_clears_restore_stamps_and_records_reason(db):
    db.save_document(PID, "doc-a", "Doc A", "body")
    # Simulate a prior restore
    db._table("documents").update_item(
        Key={"project_id": PID, "doc_id": "doc-a"},
        UpdateExpression="SET restored_at = :r, restored_by = :rb, restore_reason = :rr",
        ExpressionAttributeValues={":r": "2026-01-01", ":rb": "x", ":rr": "test"},
    )

    assert db.delete_document(PID, "doc-a", deleted_by="dave", reason="dup") is True
    after = db.get_document(PID, "doc-a")
    assert after["deleted"] is True
    assert after["deleted_by"] == "dave"
    assert after["trash_reason"] == "dup"
    assert "restored_at" not in after
    assert "restored_by" not in after
    assert "restore_reason" not in after


def test_delete_document_without_reason_removes_trash_reason(db):
    db.save_document(PID, "doc-a", "Doc A", "body")
    assert db.delete_document(PID, "doc-a") is True
    after = db.get_document(PID, "doc-a")
    assert "trash_reason" not in after


def test_delete_document_missing_returns_false(db):
    assert db.delete_document(PID, "nope") is False


def test_sweep_trashed_documents_respects_cutoff_and_cascades_revisions(db):
    db.save_document(PID, "doc-a", "Doc A", "body 1")
    db.save_document(PID, "doc-a", "Doc A v2", "body 2")  # produces 1 revision
    db.delete_document(PID, "doc-a")

    # Backdate deleted_at past the 30d cutoff
    old = (datetime.datetime.now() - datetime.timedelta(days=40)).isoformat()
    db._table("documents").update_item(
        Key={"project_id": PID, "doc_id": "doc-a"},
        UpdateExpression="SET deleted_at = :d",
        ExpressionAttributeValues={":d": old},
    )

    # dry_run identifies but does not delete
    assert db.sweep_trashed_documents(PID, days=30, dry_run=True) == ["doc-a"]
    assert db.get_document(PID, "doc-a") is not None

    # Real sweep: doc removed and revisions cascaded
    assert db.sweep_trashed_documents(PID, days=30, dry_run=False) == ["doc-a"]
    assert db.get_document(PID, "doc-a") is None
    assert db.list_document_revisions(PID, "doc-a") == []


def test_sweep_skips_recent_deletions(db):
    db.save_document(PID, "doc-a", "Doc A", "body")
    db.delete_document(PID, "doc-a")
    # deleted_at is "now", well inside the 30d window
    assert db.sweep_trashed_documents(PID, days=30, dry_run=True) == []


def test_list_documents_frontmatter_milestone_fallback(db):
    db.save_document(
        PID,
        "fm-doc",
        "fm doc",
        "---\nscope: memo\nmilestone: ms-99\n---\nbody",
    )
    listed = db.list_documents(PID)
    fm = next(d for d in listed if d["doc_id"] == "fm-doc")
    assert fm.get("milestone") == "ms-99"
