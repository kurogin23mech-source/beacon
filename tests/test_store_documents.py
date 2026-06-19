"""ms-84 Phase 2 — LocalStore document read passthrough.

Tests the rich-shape output of LocalStore.list_documents() and
get_document() so that cmd_doc_list / cmd_doc_show / _spec_exists_for_ms
can call Store directly instead of branching on _is_cloud_mode().
"""

import os
import sys
import tempfile

import pytest

THIS = os.path.dirname(__file__)
LIB = os.path.normpath(os.path.join(THIS, "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)


@pytest.fixture
def localstore_with_docs():
    """Create a LocalStore over a tmp project with three documents."""
    from store_local import LocalStore

    td = tempfile.mkdtemp(prefix="ms84-localdocs-")
    beacon_dir = os.path.join(td, ".beacon")
    docs_dir = os.path.join(beacon_dir, "documents")
    os.makedirs(docs_dir)
    pf = os.path.join(beacon_dir, "project.json")
    with open(pf, "w", encoding="utf-8") as f:
        f.write('{"milestones": []}')

    # Doc 1: full frontmatter — SPEC for ms-84
    with open(os.path.join(docs_dir, "spec-ms-84.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "scope: spec\n"
            "milestone: ms-84\n"
            "operation: op-1\n"
            "trek_id: tk-aaa\n"
            "---\n"
            "# SPEC for ms-84\n\nbody\n"
        )
    # Doc 2: memo, no frontmatter beyond scope
    with open(os.path.join(docs_dir, "memo-1.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "scope: memo\n"
            "---\n"
            "# Just a memo\n\nbody\n"
        )
    # Doc 3: trashed memo (soft-deleted)
    with open(os.path.join(docs_dir, "memo-trashed.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "scope: memo\n"
            "status: cancelled\n"
            "trashed_at: 2026-06-19T00:00:00\n"
            "trashed_by: user@example.com\n"
            "trash_reason: outdated\n"
            "---\n"
            "# Trashed memo\n\nbody\n"
        )

    store = LocalStore(pf)
    return store, td


def test_list_documents_returns_rich_shape(localstore_with_docs):
    store, _td = localstore_with_docs
    docs = store.list_documents()
    assert len(docs) == 3
    by_id = {d["doc_id"]: d for d in docs}

    spec = by_id["spec-ms-84"]
    assert spec["scope"] == "spec"
    assert spec["milestone"] == "ms-84"
    assert spec["operation"] == "op-1"
    assert spec["trek_id"] == "tk-aaa"
    assert spec["title"] == "SPEC for ms-84"
    assert "updated_at" in spec and spec["updated_at"]

    memo = by_id["memo-1"]
    assert memo["scope"] == "memo"
    # Optional fields stay absent when frontmatter does not declare them
    assert "milestone" not in memo
    assert "operation" not in memo
    assert "status" not in memo

    trashed = by_id["memo-trashed"]
    assert trashed["status"] == "cancelled"
    assert trashed["trashed_at"] == "2026-06-19T00:00:00"
    assert trashed["trashed_by"] == "user@example.com"
    assert trashed["trash_reason"] == "outdated"


def test_get_document_includes_content_and_metadata(localstore_with_docs):
    store, _td = localstore_with_docs
    doc = store.get_document("spec-ms-84")
    assert doc["doc_id"] == "spec-ms-84"
    assert doc["title"] == "SPEC for ms-84"
    assert doc["scope"] == "spec"
    assert doc["milestone"] == "ms-84"
    assert doc["operation"] == "op-1"
    assert doc["trek_id"] == "tk-aaa"
    assert "content" in doc
    assert "SPEC for ms-84" in doc["content"]


def test_get_document_returns_empty_when_missing(localstore_with_docs):
    store, _td = localstore_with_docs
    assert store.get_document("does-not-exist") == {}


def test_list_documents_empty_when_no_docs_dir():
    """A project with no documents/ directory yields []."""
    from store_local import LocalStore

    td = tempfile.mkdtemp(prefix="ms84-empty-")
    beacon_dir = os.path.join(td, ".beacon")
    os.makedirs(beacon_dir)
    pf = os.path.join(beacon_dir, "project.json")
    with open(pf, "w", encoding="utf-8") as f:
        f.write("{}")

    store = LocalStore(pf)
    assert store.list_documents() == []
    assert store.get_document("anything") == {}
