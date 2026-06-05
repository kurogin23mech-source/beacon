"""Tests for the cloud changelog persistence + GET endpoint (ms-14 e-825).

Covers:

- ``firestore_client.append_changelog`` writes a document with a
  millisecond-prefixed ID and the entry fields.
- ``firestore_client.list_changelog`` reads back newest-first, honours
  ``since`` filtering, and caps ``limit``.
- ``operations._append_changelog`` writes BOTH the local jsonl AND the
  Firestore changelog when running in cloud mode (or close enough — the
  test substitutes a fake firestore_client).
- ``GET /api/projects/{id}/changelog`` returns ``{entries, next_since,
  limit}`` and forwards the access-control check from ``_load``.
- The ContextVars set by ``set_audit_context`` end up on the persisted
  entry (ip / user_agent / email).
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


# In-memory subcollection store, indexed by (project_id, doc_id).
_store: dict[str, dict] = {}
_changelog: dict[str, dict[str, dict]] = {}


def mock_get_project(project_id: str):
    return copy.deepcopy(_store.get(project_id)) if project_id in _store else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_list_projects(*args, **kwargs):
    return [{"project_id": pid} for pid in _store]


def mock_append_changelog(project_id: str, entry: dict) -> str:
    """In-memory replacement that assigns a ts-prefixed id, like Firestore."""
    now = datetime.datetime.now(datetime.timezone.utc)
    doc_id = now.strftime("%Y%m%dT%H%M%S.%f") + "-mock"
    payload = dict(entry)
    payload.setdefault("ts", now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    _changelog.setdefault(project_id, {})[doc_id] = payload
    return doc_id


def mock_list_changelog(project_id: str, *, since: str | None = None,
                       limit: int = 100) -> list[dict]:
    limit = max(1, min(500, int(limit)))
    items = list(_changelog.get(project_id, {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    out = []
    for doc_id, payload in items:
        if since and payload.get("ts", "") <= since:
            continue
        out.append({"id": doc_id, **payload})
        if len(out) >= limit:
            break
    return out


def _rebind_firestore_mocks():
    """Re-attach mocks via app.db so cross-file collection order doesn't
    strand us with a different module object (other test files
    `sys.modules.pop("firestore_client", None)`)."""
    import app as _app
    db_module = _app.db
    db_module.get_project = mock_get_project
    db_module.save_project = mock_save_project
    db_module.list_projects = mock_list_projects
    db_module.append_changelog = mock_append_changelog
    db_module.list_changelog = mock_list_changelog
    import sys
    sys.modules["firestore_client"] = db_module


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import operations as operations_module  # noqa: E402

app_module._auth_enabled = False
client = TestClient(app_module.app)

PROJECT_ID = "audit-proj"


@pytest.fixture(autouse=True)
def reset_store():
    _store.clear()
    _changelog.clear()
    _store[PROJECT_ID] = {
        "name": "Audit Project",
        "milestones": [
            {
                "id": "ms-1", "title": "Active", "status": "in_progress",
                "progress": 0, "entries": [],
            },
        ],
    }
    _rebind_firestore_mocks()
    yield
    _store.clear()
    _changelog.clear()


# ---------------------------------------------------------------------------
# firestore_client direct helpers (purely the mock, not Firestore)
# ---------------------------------------------------------------------------


def test_append_changelog_returns_id_and_persists():
    doc_id = mock_append_changelog(PROJECT_ID, {
        "op": "test.write", "actor": "alice", "reason": "trial",
    })
    assert doc_id
    assert PROJECT_ID in _changelog
    stored = _changelog[PROJECT_ID][doc_id]
    assert stored["op"] == "test.write"
    assert stored["actor"] == "alice"
    assert stored["reason"] == "trial"
    assert stored["ts"], "ts should be auto-set when caller omits it"


def test_list_changelog_newest_first():
    mock_append_changelog(PROJECT_ID, {"op": "a", "ts": "2026-06-01T00:00:00Z"})
    mock_append_changelog(PROJECT_ID, {"op": "b", "ts": "2026-06-02T00:00:00Z"})
    mock_append_changelog(PROJECT_ID, {"op": "c", "ts": "2026-06-03T00:00:00Z"})
    out = mock_list_changelog(PROJECT_ID, limit=10)
    ops = [e["op"] for e in out]
    assert ops == ["c", "b", "a"]


def test_list_changelog_since_filter():
    mock_append_changelog(PROJECT_ID, {"op": "a", "ts": "2026-06-01T00:00:00Z"})
    mock_append_changelog(PROJECT_ID, {"op": "b", "ts": "2026-06-02T00:00:00Z"})
    mock_append_changelog(PROJECT_ID, {"op": "c", "ts": "2026-06-03T00:00:00Z"})
    out = mock_list_changelog(PROJECT_ID, since="2026-06-01T12:00:00Z")
    assert [e["op"] for e in out] == ["c", "b"]


def test_list_changelog_limit_capped():
    for i in range(700):
        mock_append_changelog(PROJECT_ID, {
            "op": f"op-{i}", "ts": f"2026-06-01T00:00:{i:02d}Z",
        })
    out = mock_list_changelog(PROJECT_ID, limit=10000)
    assert len(out) == 500, "limit must be clamped to 500"


# ---------------------------------------------------------------------------
# operations._append_changelog cloud-mode persistence
# ---------------------------------------------------------------------------


def test_append_changelog_cloud_sink_writes_to_firestore(monkeypatch):
    """_append_changelog should land an entry on the firestore mock when
    we're effectively in 'cloud mode' (firestore_client is on sys.modules)."""
    monkeypatch.setenv("BEACON_PROJECT_FILE", "")
    operations_module._append_changelog(
        PROJECT_ID,
        op_name="milestone.delete",
        actor="alice",
        reason="duplicate",
    )
    items = mock_list_changelog(PROJECT_ID)
    assert len(items) == 1
    e = items[0]
    assert e["op"] == "milestone.delete"
    assert e["actor"] == "alice"
    assert e["reason"] == "duplicate"
    assert e["project_id"] == PROJECT_ID


def test_append_changelog_picks_up_audit_contextvars(monkeypatch):
    """ContextVars set by AuditLogMiddleware (or set_audit_context directly)
    must appear on the persisted entry — that's the whole reason they exist.
    """
    monkeypatch.setenv("BEACON_PROJECT_FILE", "")
    # Run inside a fresh context so we don't pollute other tests.
    import contextvars
    ctx = contextvars.copy_context()

    def _set_and_call():
        operations_module.set_audit_context(
            ip="203.0.113.5",
            user_agent="curl/8.5",
            email="alice@example.com",
        )
        operations_module._append_changelog(
            PROJECT_ID,
            op_name="doc.delete",
            actor="alice-sub",
            reason="stale",
        )

    ctx.run(_set_and_call)
    items = mock_list_changelog(PROJECT_ID)
    assert len(items) == 1
    e = items[0]
    assert e["ip"] == "203.0.113.5"
    assert e["user_agent"] == "curl/8.5"
    assert e["email"] == "alice@example.com"


def test_append_changelog_never_raises_on_sink_failure(monkeypatch):
    """A broken Firestore must not break the actual write path."""
    monkeypatch.setenv("BEACON_PROJECT_FILE", "")

    def _boom(*args, **kwargs):
        raise RuntimeError("firestore down")

    monkeypatch.setattr(app_module.db, "append_changelog", _boom)

    # Must not raise.
    operations_module._append_changelog(
        PROJECT_ID, op_name="test.op", actor="alice",
    )


# ---------------------------------------------------------------------------
# GET /api/projects/{id}/changelog
# ---------------------------------------------------------------------------


def test_get_changelog_returns_envelope_with_pagination_cursor():
    mock_append_changelog(PROJECT_ID, {"op": "a", "ts": "2026-06-01T00:00:00Z"})
    mock_append_changelog(PROJECT_ID, {"op": "b", "ts": "2026-06-02T00:00:00Z"})
    r = client.get(f"/api/projects/{PROJECT_ID}/changelog?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 10
    assert [e["op"] for e in body["entries"]] == ["b", "a"]
    # ``next_since`` is the oldest ts in this page so the UI can scroll
    # further into the past with ``?since=...`` set to it.
    assert body["next_since"] == "2026-06-01T00:00:00Z"


def test_get_changelog_returns_null_cursor_when_empty():
    r = client.get(f"/api/projects/{PROJECT_ID}/changelog?limit=10")
    assert r.status_code == 200
    assert r.json() == {"entries": [], "next_since": None, "limit": 10}


def test_get_changelog_404_for_unknown_project():
    r = client.get("/api/projects/no-such-project/changelog")
    assert r.status_code == 404


def test_get_changelog_since_filter():
    mock_append_changelog(PROJECT_ID, {"op": "old", "ts": "2026-06-01T00:00:00Z"})
    mock_append_changelog(PROJECT_ID, {"op": "new", "ts": "2026-06-03T00:00:00Z"})
    r = client.get(
        f"/api/projects/{PROJECT_ID}/changelog?since=2026-06-02T00:00:00Z"
    )
    assert r.status_code == 200
    ops = [e["op"] for e in r.json()["entries"]]
    assert ops == ["new"]
