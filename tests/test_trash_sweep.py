"""Tests for the 30-day soft-delete sweep (ms-14 e-826 (4) / e-991 (6)).

Covers two layers:

- ``core.sweep_trashed_in_project`` — pure data transform. Cancelled
  milestones / tasks whose ``cancelled_at`` is older than the cutoff
  vanish; the swept ids are returned for changelog.

- ``POST /api/admin/trash/sweep`` — admin endpoint. Iterates every
  project, runs the pure transform inside a transaction, sweeps docs
  via ``firestore_client.sweep_trashed_documents``, and writes one
  ``trash.sweep.detail`` changelog entry per affected project.
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

import core  # noqa: E402
import firestore_client  # noqa: E402


def _iso_days_ago(days: int) -> str:
    return (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# core.sweep_trashed_in_project — pure data layer
# ---------------------------------------------------------------------------


def _seed_data() -> dict:
    """Project doc with a healthy mix of fresh / old / never-cancelled items."""
    return {
        "name": "Sweep test",
        "milestones": [
            # Active MS with active + recently-cancelled + old-cancelled tasks.
            {
                "id": "ms-1",
                "title": "Active",
                "status": "in_progress",
                "progress": 0,
                "entries": [
                    {
                        "id": "e-1", "type": "task", "description": "fresh todo",
                        "status": "todo", "date": "2026-06-01",
                        "created_at": "2026-06-01T00:00:00", "done_at": None,
                        "meta": {},
                    },
                    {
                        "id": "e-2",
                        "type": "task",
                        "description": "recently cancelled (keep)",
                        "status": "cancelled", "date": "2026-06-01",
                        "created_at": "2026-06-01T00:00:00", "done_at": None,
                        "meta": {
                            "cancelled_at": _iso_days_ago(5),
                            "cancelled_by": "u@e",
                            "cancel_reason": "still in 30d window",
                        },
                    },
                    {
                        "id": "e-3",
                        "type": "task",
                        "description": "stale cancelled (sweep)",
                        "status": "cancelled", "date": "2026-05-01",
                        "created_at": "2026-05-01T00:00:00", "done_at": None,
                        "meta": {
                            "cancelled_at": _iso_days_ago(45),
                            "cancelled_by": "u@e",
                            "cancel_reason": "stale",
                        },
                    },
                    {
                        "id": "e-4",
                        "type": "task",
                        "description": "no cancelled_at — must NOT be swept",
                        "status": "cancelled", "date": "2026-05-01",
                        "created_at": "2026-05-01T00:00:00", "done_at": None,
                        "meta": {"cancelled_by": "u@e"},  # no cancelled_at
                    },
                ],
            },
            # Recently-cancelled MS (keep).
            {
                "id": "ms-2",
                "title": "Recent cancel",
                "status": "cancelled",
                "progress": 0,
                "entries": [],
                "meta": {
                    "cancelled_at": _iso_days_ago(10),
                    "cancelled_by": "u@e",
                    "cancel_reason": "scope cut",
                },
            },
            # Old cancelled MS (sweep).
            {
                "id": "ms-3",
                "title": "Stale cancel",
                "status": "cancelled",
                "progress": 0,
                "entries": [],
                "meta": {
                    "cancelled_at": _iso_days_ago(60),
                    "cancelled_by": "u@e",
                    "cancel_reason": "old",
                },
            },
            # Cancelled MS without cancelled_at — legacy, must NOT be swept.
            {
                "id": "ms-4",
                "title": "Legacy no-ts",
                "status": "cancelled",
                "progress": 0,
                "entries": [],
                "meta": {"cancelled_by": "u@e"},
            },
        ],
    }


def test_sweep_removes_only_stale_and_returns_ids():
    data = _seed_data()
    result = core.sweep_trashed_in_project(data, days=30, apply=True)

    assert result["ms_purged_ids"] == ["ms-3"]
    assert result["task_purged_ids"] == ["e-3"]

    ms_ids = [m["id"] for m in data["milestones"]]
    assert "ms-3" not in ms_ids
    assert "ms-2" in ms_ids
    assert "ms-4" in ms_ids  # legacy stays — no timestamp to prove staleness
    assert "ms-1" in ms_ids

    active = next(m for m in data["milestones"] if m["id"] == "ms-1")
    entry_ids = [e["id"] for e in active["entries"]]
    assert "e-3" not in entry_ids
    assert "e-1" in entry_ids
    assert "e-2" in entry_ids
    assert "e-4" in entry_ids


def test_sweep_dry_run_leaves_data_untouched():
    data = _seed_data()
    snapshot = copy.deepcopy(data)
    result = core.sweep_trashed_in_project(data, days=30, apply=False)

    # The would-be ids are still reported so the caller can preview.
    assert result["ms_purged_ids"] == ["ms-3"]
    assert result["task_purged_ids"] == ["e-3"]
    # But the data is untouched.
    assert data == snapshot


def test_sweep_with_zero_days_window_purges_everything_dated():
    """``days=1`` (smallest accepted) catches any cancellation older than 1d
    but still skips entries without ``cancelled_at``."""
    data = _seed_data()
    result = core.sweep_trashed_in_project(data, days=1, apply=True)

    swept_ms = set(result["ms_purged_ids"])
    assert {"ms-2", "ms-3"}.issubset(swept_ms)
    assert "ms-4" not in swept_ms

    swept_tasks = set(result["task_purged_ids"])
    assert {"e-2", "e-3"}.issubset(swept_tasks)
    assert "e-4" not in swept_tasks


# ---------------------------------------------------------------------------
# POST /api/admin/trash/sweep — endpoint integration
# ---------------------------------------------------------------------------

# In-memory stores for the FastAPI integration tests.
_store: dict[str, dict] = {}
_changelog: dict[str, dict[str, dict]] = {}
_swept_docs: dict[str, list[str]] = {}


def mock_get_project(project_id: str):
    return copy.deepcopy(_store.get(project_id)) if project_id in _store else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_list_all_projects():
    return [{"project_id": pid} for pid in _store]


def mock_list_projects(*args, **kwargs):
    return mock_list_all_projects()


def mock_append_changelog(project_id: str, entry: dict) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    doc_id = now.strftime("%Y%m%dT%H%M%S.%f") + "-mock"
    payload = dict(entry)
    payload.setdefault("ts", now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    _changelog.setdefault(project_id, {})[doc_id] = payload
    return doc_id


def mock_list_changelog(project_id: str, *, since=None, limit=100):
    items = list(_changelog.get(project_id, {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    return [{"id": doc_id, **payload} for doc_id, payload in items[:limit]]


def mock_sweep_trashed_documents(project_id: str, *, days=30, dry_run=False):
    """Return the recorded swept list per project (test fixture)."""
    ids = _swept_docs.get(project_id, [])
    if not dry_run:
        _swept_docs[project_id] = []
    return list(ids)


def _rebind():
    import app as _app
    db_module = _app.db
    db_module.get_project = mock_get_project
    db_module.save_project = mock_save_project
    db_module.list_projects = mock_list_projects
    db_module.list_all_projects = mock_list_all_projects
    db_module.append_changelog = mock_append_changelog
    db_module.list_changelog = mock_list_changelog
    db_module.sweep_trashed_documents = mock_sweep_trashed_documents
    sys.modules["firestore_client"] = db_module


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False
client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset():
    _store.clear()
    _changelog.clear()
    _swept_docs.clear()
    _store["proj-a"] = _seed_data()
    _store["proj-b"] = {
        "name": "Empty",
        "milestones": [],
    }
    # proj-a has one stale doc waiting to be swept.
    _swept_docs["proj-a"] = ["doc-stale-1"]
    _rebind()
    yield
    _store.clear()
    _changelog.clear()
    _swept_docs.clear()


def test_admin_sweep_returns_summary_and_purges_per_project():
    r = client.post("/api/admin/trash/sweep?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 30
    assert body["dry_run"] is False
    assert body["projects_scanned"] == 2
    assert body["ms_purged"] == 1     # ms-3 on proj-a
    assert body["task_purged"] == 1   # e-3 on proj-a
    assert body["doc_purged"] == 1    # doc-stale-1 on proj-a
    by_project = {p["project_id"]: p for p in body["projects"]}
    assert "proj-a" in by_project
    assert by_project["proj-a"]["ms_purged"] == ["ms-3"]
    assert by_project["proj-a"]["task_purged"] == ["e-3"]
    assert by_project["proj-a"]["doc_purged"] == ["doc-stale-1"]
    # proj-b had nothing to sweep so it's not in the per-project list.
    assert "proj-b" not in by_project

    # Data actually mutated: ms-3 is gone from proj-a.
    proj_a_ms = [m["id"] for m in _store["proj-a"]["milestones"]]
    assert "ms-3" not in proj_a_ms


def test_admin_sweep_writes_changelog_detail_entries():
    client.post("/api/admin/trash/sweep?days=30")
    entries = mock_list_changelog("proj-a")
    detail = [e for e in entries if e.get("op") == "trash.sweep.detail"]
    assert len(detail) == 1
    payload = detail[0]["payload"]
    assert payload["days"] == 30
    assert payload["milestone_ids"] == ["ms-3"]
    assert payload["task_ids"] == ["e-3"]
    assert payload["doc_ids"] == ["doc-stale-1"]


def test_admin_sweep_dry_run_leaves_store_untouched():
    snapshot = copy.deepcopy(_store)
    r = client.post("/api/admin/trash/sweep?days=30&dry_run=true")
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    # Counts are still reported.
    assert body["ms_purged"] == 1
    assert body["task_purged"] == 1
    assert body["doc_purged"] == 1
    # But the underlying project data wasn't touched.
    assert _store == snapshot
    # And no detail changelog was written (dry runs don't record).
    entries = mock_list_changelog("proj-a")
    assert not any(e.get("op") == "trash.sweep.detail" for e in entries)
