"""Server-side tests for trash/restore (ms-14 e-826 / e-991).

Mirrors the test_api.py pattern: FastAPI TestClient + in-memory
firestore_client mocks. Covers the new endpoints landed for cloud
trash parity:

  - POST /api/projects/{id}/milestones/{ms_id}/restore
  - POST /api/projects/{id}/entries/{entry_id}/restore
  - DELETE /api/projects/{id}/documents/{doc_id} (now soft + reason)
  - POST /api/projects/{id}/documents/{doc_id}/restore
  - GET  /api/projects/{id}/trash?days=N (unified across kinds)
  - GET  /api/projects/{id}/documents?include_trashed=true

Lost-update guards / cross-project leaks are exercised in test_api.py
already; here we focus on the new shapes and error semantics.
"""

from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402

# In-memory project store. Each test gets a clean copy via the reset_store fixture.
_store: dict[str, dict] = {}
_docs_store: dict[str, dict[str, dict]] = {}


def mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_list_projects(*args, **kwargs):
    return list(_store.keys())


def mock_list_documents(project_id: str, include_trashed: bool = False):
    docs = _docs_store.get(project_id, {})
    out = []
    for doc_id, data in docs.items():
        is_trashed = bool(data.get("deleted"))
        if is_trashed and not include_trashed:
            continue
        entry = {
            "doc_id": doc_id,
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if is_trashed:
            entry["trashed"] = True
            entry["trashed_at"] = data.get("deleted_at", "")
            entry["trashed_by"] = data.get("deleted_by", "")
            if data.get("trash_reason"):
                entry["trash_reason"] = data["trash_reason"]
        out.append(entry)
    return out


def mock_get_document(project_id: str, doc_id: str):
    docs = _docs_store.get(project_id, {})
    if doc_id not in docs:
        return None
    return {"doc_id": doc_id, **copy.deepcopy(docs[doc_id])}


def mock_delete_document(project_id: str, doc_id: str, deleted_by: str = "unknown",
                         reason: str = ""):
    docs = _docs_store.get(project_id, {})
    if doc_id not in docs:
        return False
    now = datetime.datetime.now().isoformat()
    docs[doc_id]["deleted"] = True
    docs[doc_id]["deleted_at"] = now
    docs[doc_id]["deleted_by"] = deleted_by
    if reason:
        docs[doc_id]["trash_reason"] = reason
    else:
        docs[doc_id].pop("trash_reason", None)
    # Clear restore stamps if re-trashing
    for k in ("restored_at", "restored_by", "restore_reason"):
        docs[doc_id].pop(k, None)
    return True


def mock_restore_document(project_id: str, doc_id: str, restored_by: str = "unknown",
                          reason: str = ""):
    docs = _docs_store.get(project_id, {})
    if doc_id not in docs:
        return False
    if not docs[doc_id].get("deleted"):
        return False
    now = datetime.datetime.now().isoformat()
    for k in ("deleted", "deleted_at", "deleted_by", "trash_reason"):
        docs[doc_id].pop(k, None)
    docs[doc_id]["restored_at"] = now
    docs[doc_id]["restored_by"] = restored_by
    if reason:
        docs[doc_id]["restore_reason"] = reason
    return True


def mock_list_trashed_documents(project_id: str, days: int | None = 30):
    docs = _docs_store.get(project_id, {})
    cutoff = None
    if days is not None:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    now = datetime.datetime.now()
    out = []
    for doc_id, data in docs.items():
        if not data.get("deleted"):
            continue
        deleted_at = data.get("deleted_at", "")
        if cutoff and deleted_at and deleted_at < cutoff:
            continue
        days_ago = None
        if deleted_at:
            try:
                dt = datetime.datetime.fromisoformat(deleted_at)
                days_ago = max(0, (now - dt).days)
            except (ValueError, TypeError):
                pass
        out.append({
            "doc_id": doc_id,
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "trashed_at": deleted_at or None,
            "trashed_by": data.get("deleted_by", ""),
            "trash_reason": data.get("trash_reason", ""),
            "days_ago": days_ago,
        })
    out.sort(key=lambda x: x["trashed_at"] or "", reverse=True)
    return out


# NOTE: We intentionally do NOT bind firestore_client.* to mocks at module
# level. Doing so causes test collection-order races with other test files
# (test_api.py / test_operations.py) that also patch the same module — the
# last importer wins and the loser's tests then see the wrong _store. Bind
# only inside the autouse fixture so every test in *this* file gets the
# right mocks, while other test files keep theirs.


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False
client = TestClient(app_module.app)

PROJECT_ID = "trash-proj"


def _iso_days_ago(days: int) -> str:
    return (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()


def _seed_project() -> dict:
    """Fixture project with one cancelled MS, one cancelled task, and one live MS."""
    return {
        "name": "Trash Test",
        "milestones": [
            {
                "id": "ms-1", "title": "Live milestone", "status": "in_progress",
                "progress": 0, "entries": [
                    {
                        "id": "e-101", "type": "task", "description": "Active task",
                        "status": "todo", "date": "2026-06-01",
                        "created_at": "2026-06-01T00:00:00",
                        "done_at": None, "meta": {},
                    },
                    {
                        "id": "e-102", "type": "task",
                        "description": "Cancelled child task",
                        "status": "cancelled", "date": "2026-06-01",
                        "created_at": "2026-06-01T00:00:00",
                        "done_at": None,
                        "meta": {
                            "cancelled_at": _iso_days_ago(2),
                            "cancelled_by": "actor@example.com",
                            "cancel_reason": "obsolete",
                        },
                    },
                ],
            },
            {
                "id": "ms-99", "title": "Cancelled milestone", "status": "cancelled",
                "progress": 0, "entries": [],
                "meta": {
                    "cancelled_at": _iso_days_ago(3),
                    "cancelled_by": "actor@example.com",
                    "cancel_reason": "scope cut",
                },
            },
        ],
    }


def _rebind_firestore_mocks():
    """Re-attach mocks to firestore_client.

    Other test files (test_operations.py, test_doctor_dup_ids.py,
    test_trash_restore.py, ...) pop ``firestore_client`` from
    ``sys.modules`` mid-suite. We can't ``import firestore_client``
    afresh here because that gives us a *new* module object — the
    ``app`` module still has ``db = <old firestore_client>`` bound at
    its own import time, and it's the **old** object's attributes that
    need to point at the mocks. Reach into ``app.db`` (the live alias)
    and ``operations`` (which also does its own lazy import) so all
    call sites see our mocks.
    """
    import app as _app
    db_module = _app.db
    db_module.get_project = mock_get_project
    db_module.save_project = mock_save_project
    db_module.list_projects = mock_list_projects
    db_module.list_documents = mock_list_documents
    db_module.get_document = mock_get_document
    db_module.delete_document = mock_delete_document
    db_module.restore_document = mock_restore_document
    db_module.list_trashed_documents = mock_list_trashed_documents
    # ``operations._apply_mock`` does ``import firestore_client as db``
    # inside the function body, which re-uses whatever is on sys.modules.
    # Put the live (already-patched) module back so the import inside
    # apply_operation finds the mocked version, not a freshly-loaded one.
    import sys
    sys.modules["firestore_client"] = db_module


@pytest.fixture(autouse=True)
def reset_store():
    _store.clear()
    _docs_store.clear()
    _store[PROJECT_ID] = _seed_project()
    _rebind_firestore_mocks()
    _docs_store[PROJECT_ID] = {
        "doc-live": {
            "title": "Live doc", "content": "## live",
            "scope": "memo",
            "updated_at": "2026-06-04T10:00:00",
        },
        "doc-trashed-recent": {
            "title": "Recent trashed doc", "content": "## body",
            "scope": "memo",
            "updated_at": "2026-06-01T10:00:00",
            "deleted": True,
            "deleted_at": _iso_days_ago(2),
            "deleted_by": "user@example.com",
            "trash_reason": "duplicate",
        },
        "doc-trashed-old": {
            "title": "Old trashed doc", "content": "## body",
            "scope": "spec",
            "updated_at": "2025-12-01T10:00:00",
            "deleted": True,
            "deleted_at": _iso_days_ago(60),
            "deleted_by": "user@example.com",
            "trash_reason": "stale",
        },
    }
    yield
    _store.clear()
    _docs_store.clear()


# ---------------------------------------------------------------------------
# Milestone restore
# ---------------------------------------------------------------------------


def test_restore_milestone_flips_status_back_to_todo():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-99/restore",
                    json={"reason": "back in scope"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "ms-99"
    assert body["status"] == "todo"

    ms = next(m for m in _store[PROJECT_ID]["milestones"] if m["id"] == "ms-99")
    assert ms["status"] == "todo"
    meta = ms.get("meta", {})
    assert "cancelled_at" not in meta
    assert "cancel_reason" not in meta
    assert meta.get("restored_at")
    assert meta.get("restore_reason") == "back in scope"


def test_restore_milestone_rejects_active_ms():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/restore",
                    json={"reason": "oops"})
    assert r.status_code == 400
    assert "not cancelled" in r.json()["detail"].lower()


def test_restore_milestone_unknown():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-404/restore",
                    json={"reason": ""})
    assert r.status_code == 400


def test_restore_milestone_works_without_body():
    """FastAPI must accept the empty POST when the caller has no reason to record."""
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-99/restore")
    assert r.status_code == 200
    ms = next(m for m in _store[PROJECT_ID]["milestones"] if m["id"] == "ms-99")
    assert ms["status"] == "todo"


# ---------------------------------------------------------------------------
# Entry restore
# ---------------------------------------------------------------------------


def test_restore_entry_flips_cancelled_back_to_todo():
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-102/restore",
                    json={"reason": "needed after all"})
    assert r.status_code == 200
    assert r.json()["status"] == "todo"

    ms = next(m for m in _store[PROJECT_ID]["milestones"] if m["id"] == "ms-1")
    entry = next(e for e in ms["entries"] if e["id"] == "e-102")
    assert entry["status"] == "todo"
    meta = entry.get("meta", {})
    assert "cancelled_at" not in meta
    assert meta.get("restored_at")
    assert meta.get("restore_reason") == "needed after all"


def test_restore_entry_rejects_active_task():
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-101/restore", json={})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Document delete (soft) + restore
# ---------------------------------------------------------------------------


def test_delete_document_records_reason():
    r = client.request(
        "DELETE",
        f"/api/projects/{PROJECT_ID}/documents/doc-live",
        json={"reason": "duplicate of doc-other"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    doc = _docs_store[PROJECT_ID]["doc-live"]
    assert doc["deleted"] is True
    assert doc["trash_reason"] == "duplicate of doc-other"
    assert doc.get("deleted_by")


def test_delete_document_without_reason():
    r = client.delete(f"/api/projects/{PROJECT_ID}/documents/doc-live")
    assert r.status_code == 200
    doc = _docs_store[PROJECT_ID]["doc-live"]
    assert doc["deleted"] is True
    assert "trash_reason" not in doc


def test_restore_document_clears_trash_fields():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/documents/doc-trashed-recent/restore",
        json={"reason": "needed for retro"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "restored"
    doc = _docs_store[PROJECT_ID]["doc-trashed-recent"]
    assert "deleted" not in doc
    assert "deleted_at" not in doc
    assert "trash_reason" not in doc
    assert doc.get("restored_at")
    assert doc.get("restore_reason") == "needed for retro"


def test_restore_document_conflict_on_live_doc():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/documents/doc-live/restore",
        json={"reason": ""},
    )
    assert r.status_code == 409
    assert "not in trash" in r.json()["detail"].lower()


def test_restore_document_not_found():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/documents/doc-nope/restore",
        json={"reason": ""},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Unified GET /trash
# ---------------------------------------------------------------------------


def test_list_trash_30day_window_filters_old_docs():
    r = client.get(f"/api/projects/{PROJECT_ID}/trash?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 30
    # Cancelled MS / task within window
    ms_ids = [m["id"] for m in body["milestones"]]
    assert ms_ids == ["ms-99"]
    task_ids = [t["id"] for t in body["tasks"]]
    assert task_ids == ["e-102"]
    # Only the recent trashed doc; the 60-day-old one is filtered out
    doc_ids = [d["doc_id"] for d in body["documents"]]
    assert doc_ids == ["doc-trashed-recent"]


def test_list_trash_days_zero_returns_all_history():
    r = client.get(f"/api/projects/{PROJECT_ID}/trash?days=0")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 0
    doc_ids = sorted(d["doc_id"] for d in body["documents"])
    assert doc_ids == ["doc-trashed-old", "doc-trashed-recent"]


def test_list_trash_includes_audit_fields():
    r = client.get(f"/api/projects/{PROJECT_ID}/trash?days=30")
    body = r.json()
    ms_entry = body["milestones"][0]
    assert ms_entry["cancelled_by"] == "actor@example.com"
    assert ms_entry["cancel_reason"] == "scope cut"
    assert ms_entry["days_ago"] is not None
    doc_entry = body["documents"][0]
    assert doc_entry["trash_reason"] == "duplicate"
    assert doc_entry["days_ago"] is not None


# ---------------------------------------------------------------------------
# Documents listing with include_trashed
# ---------------------------------------------------------------------------


def test_list_documents_default_hides_trashed():
    r = client.get(f"/api/projects/{PROJECT_ID}/documents")
    assert r.status_code == 200
    ids = [d["doc_id"] for d in r.json()]
    assert ids == ["doc-live"]


def test_list_documents_with_include_trashed_returns_all():
    r = client.get(f"/api/projects/{PROJECT_ID}/documents?include_trashed=true")
    assert r.status_code == 200
    docs = r.json()
    by_id = {d["doc_id"]: d for d in docs}
    assert set(by_id) == {"doc-live", "doc-trashed-recent", "doc-trashed-old"}
    assert by_id["doc-trashed-recent"]["trashed"] is True
    assert by_id["doc-trashed-recent"]["trash_reason"] == "duplicate"
    assert "trashed" not in by_id["doc-live"]
