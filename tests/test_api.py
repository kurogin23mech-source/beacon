"""API integration tests using FastAPI TestClient with in-memory Firestore mock."""

import sys
import os
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Mock firestore_client before importing app
import firestore_client

_store: dict[str, dict] = {}


def mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_list_projects():
    return [
        {"project_id": pid, "name": data.get("name", ""), "objective": data.get("objective", "")}
        for pid, data in _store.items()
    ]


firestore_client.get_project = mock_get_project
firestore_client.save_project = mock_save_project
firestore_client.list_projects = mock_list_projects

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

PROJECT_ID = "test-project"
SEED_PROJECT = {
    "name": "Test",
    "milestones": [
        {
            "id": "ms-1", "title": "First milestone", "status": "in_progress",
            "progress": 20, "target_date": "2026-06-01", "commits": [],
            "entries": [
                {
                    "id": "e-1", "type": "task", "description": "Task one",
                    "status": "todo", "date": "2026-05-11",
                    "created_at": "2026-05-11", "done_at": None, "meta": {},
                },
            ],
        },
        {
            "id": "ms-2", "title": "Second milestone", "status": "todo",
            "progress": 0, "target_date": "", "commits": [], "entries": [],
        },
    ],
}


@pytest.fixture(autouse=True)
def reset_store():
    _store.clear()
    _store[PROJECT_ID] = copy.deepcopy(SEED_PROJECT)
    yield
    _store.clear()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

def test_list_projects():
    r = client.get("/api/projects")
    assert r.status_code == 200
    projects = r.json()
    assert len(projects) == 1
    assert projects[0]["project_id"] == PROJECT_ID


def test_create_project():
    r = client.post("/api/projects/new-project",
                    json={"name": "New Project", "objective": "Test"})
    assert r.status_code == 200
    assert r.json()["status"] == "created"
    assert "new-project" in _store


def test_create_project_duplicate():
    r = client.post(f"/api/projects/{PROJECT_ID}",
                    json={"name": "Dupe"})
    assert r.status_code == 409


def test_get_project():
    r = client.get(f"/api/projects/{PROJECT_ID}")
    assert r.status_code == 200
    assert r.json()["name"] == "Test"
    assert len(r.json()["milestones"]) == 2


def test_get_project_not_found():
    r = client.get("/api/projects/nonexistent")
    assert r.status_code == 404


def test_put_project():
    new_data = {"name": "Updated", "milestones": []}
    r = client.put(f"/api/projects/{PROJECT_ID}", json=new_data)
    assert r.status_code == 200
    assert _store[PROJECT_ID]["name"] == "Updated"


def test_put_project_invalid():
    r = client.put(f"/api/projects/{PROJECT_ID}", json={"bad": "data"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

def test_create_milestone():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones",
                    json={"title": "New MS", "target_date": "2026-12-31"})
    assert r.status_code == 200
    assert r.json()["ms_id"] == "ms-3"
    assert len(_store[PROJECT_ID]["milestones"]) == 3


def test_create_milestone_with_description():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones",
                    json={"title": "Desc MS", "description": "A goal"})
    assert r.status_code == 200
    ms_id = r.json()["ms_id"]
    ms = next(m for m in _store[PROJECT_ID]["milestones"] if m["id"] == ms_id)
    assert ms["description"] == "A goal"


def test_get_milestone():
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-1")
    assert r.status_code == 200
    assert r.json()["title"] == "First milestone"
    assert r.json()["total_tasks"] == 1


def test_get_milestone_not_found():
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-99")
    assert r.status_code == 404


def test_update_milestone():
    r = client.patch(f"/api/projects/{PROJECT_ID}/milestones/ms-1",
                     json={"title": "Updated title", "progress": "50"})
    assert r.status_code == 200
    assert r.json()["title"] == "Updated title"
    assert r.json()["progress"] == 50


def test_update_milestone_description():
    r = client.patch(f"/api/projects/{PROJECT_ID}/milestones/ms-1",
                     json={"description": "Updated desc"})
    assert r.status_code == 200
    ms = _store[PROJECT_ID]["milestones"][0]
    assert ms["description"] == "Updated desc"


def test_update_milestone_invalid_status():
    r = client.patch(f"/api/projects/{PROJECT_ID}/milestones/ms-1",
                     json={"status": "bogus"})
    assert r.status_code == 400


def test_start_milestone():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-2/start")
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"
    # ms-1 should be deactivated
    assert _store[PROJECT_ID]["milestones"][0]["status"] == "todo"


def test_done_milestone():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/done")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_delete_milestone():
    r = client.delete(f"/api/projects/{PROJECT_ID}/milestones/ms-1")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def test_create_entry():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries",
                    json={"description": "New task", "date": "2026-05-11"})
    assert r.status_code == 200
    assert r.json()["entry_id"] == "e-2"


def test_create_entry_with_detail():
    r = client.post(f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries",
                    json={"description": "Detailed task", "detail": "Some details"})
    assert r.status_code == 200
    entries = _store[PROJECT_ID]["milestones"][0]["entries"]
    assert entries[-1]["detail"] == "Some details"


def test_update_entry():
    r = client.patch(f"/api/projects/{PROJECT_ID}/entries/e-1",
                     json={"description": "Updated task"})
    assert r.status_code == 200
    assert r.json()["description"] == "Updated task"


def test_update_entry_not_found():
    r = client.patch(f"/api/projects/{PROJECT_ID}/entries/e-99",
                     json={"description": "nope"})
    assert r.status_code == 400


def test_done_entry():
    r = client.post(f"/api/projects/{PROJECT_ID}/entries/e-1/done")
    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_delete_entry():
    r = client.delete(f"/api/projects/{PROJECT_ID}/entries/e-1")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

def test_log_commit():
    r = client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "logged"


def test_log_duplicate():
    client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1",
    })
    r = client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1",
    })
    assert r.json()["status"] == "duplicate"


def test_log_with_progress():
    r = client.post(f"/api/projects/{PROJECT_ID}/log", json={
        "hash": "abc1234", "message": "Fix bug",
        "date": "2026-05-11", "ms_id": "ms-1", "progress": "40",
    })
    assert r.status_code == 200
    assert _store[PROJECT_ID]["milestones"][0]["progress"] == 40


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_update_summary():
    r = client.patch(f"/api/projects/{PROJECT_ID}/summary",
                     json={"text": "New summary"})
    assert r.status_code == 200
    assert _store[PROJECT_ID]["summary"] == "New summary"
