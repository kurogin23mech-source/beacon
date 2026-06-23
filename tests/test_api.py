"""API integration tests using FastAPI TestClient with in-memory Firestore mock."""

import sys
import os
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route operations.apply_operation through the in-memory mock instead of real
# Firestore. Must be set BEFORE importing operations / app.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

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

# ms-43 e-631: documents are queried by the search endpoint via list_documents.
_docs_store: dict[str, list[dict]] = {}


def mock_list_documents(project_id: str):
    return copy.deepcopy(_docs_store.get(project_id, []))


firestore_client.list_documents = mock_list_documents

from fastapi.testclient import TestClient
import app as app_module

# Disable auth for functional tests (auth tested separately below)
app_module._auth_enabled = False

app = app_module.app
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
    # Re-apply firestore_client mocks every test in case another test module's
    # module-level patches have replaced them during collection. Specifically,
    # tests/test_bus_directory.py:43-45 and tests/test_bus_transport.py:87-89
    # assign lambdas to firestore_client.get_project / save_project /
    # list_projects at import time (= pytest collection time), which silently
    # stomps the mocks we set up at our own module load (lines 37-39 above).
    # Without this re-application, get_project("new-project") would return
    # {"name": "test", "milestones": []} from the leaked lambda and
    # test_create_project would fail with 409 (project already exists).
    firestore_client.get_project = mock_get_project
    firestore_client.save_project = mock_save_project
    firestore_client.list_projects = mock_list_projects
    firestore_client.list_documents = mock_list_documents
    _store.clear()
    _store[PROJECT_ID] = copy.deepcopy(SEED_PROJECT)
    _docs_store.clear()
    yield
    _store.clear()
    _docs_store.clear()


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

def test_create_project():
    r = client.post("/api/projects/new-project",
                    json={"name": "New Project", "objective": "Test"})
    assert r.status_code == 200
    assert r.json()["status"] == "created"
    assert "new-project" in _store
    # New projects default to schema_version=2 (β subcollection) so concurrent
    # writes to different milestones can proceed in parallel. See e-632.
    assert _store["new-project"].get("schema_version") == 2


def test_existing_project_remains_legacy_v1():
    # The SEED_PROJECT fixture does NOT set schema_version, which mirrors
    # how pre-existing Firestore projects look on disk. apply_operation
    # must still route them through the legacy path without errors.
    assert "schema_version" not in _store[PROJECT_ID]
    # A simple mutation should succeed and not implicitly upgrade the schema.
    # e-1040 retired the /summary PATCH endpoint (no-op), so use a milestone
    # title mutation which still exercises apply_operation on v1.
    ms_id = _store[PROJECT_ID]["milestones"][0]["id"]
    r = client.patch(
        f"/api/projects/{PROJECT_ID}/milestones/{ms_id}",
        json={"title": "still v1"},
    )
    assert r.status_code == 200
    assert _store[PROJECT_ID]["milestones"][0]["title"] == "still v1"
    assert "schema_version" not in _store[PROJECT_ID]


def test_create_project_duplicate():
    r = client.post(f"/api/projects/{PROJECT_ID}",
                    json={"name": "Dupe"})
    assert r.status_code == 409


def test_get_project():
    r = client.get(f"/api/projects/{PROJECT_ID}")
    assert r.status_code == 200
    assert r.json()["name"] == "Test"
    assert len(r.json()["milestones"]) == 2


def test_get_project_enriches_task_counts():
    # ms-46 e-756: REST must return enriched milestones (total_tasks /
    # done_tasks) so that focus-reconnect on Tauri doesn't blank the counts
    # by overwriting WS-enriched state with raw REST data.
    r = client.get(f"/api/projects/{PROJECT_ID}")
    assert r.status_code == 200
    ms_by_id = {m["id"]: m for m in r.json()["milestones"]}
    # ms-1 has 1 todo task
    assert ms_by_id["ms-1"]["total_tasks"] == 1
    assert ms_by_id["ms-1"]["done_tasks"] == 0
    # ms-2 has no entries
    assert ms_by_id["ms-2"]["total_tasks"] == 0
    assert ms_by_id["ms-2"]["done_tasks"] == 0


def test_get_project_not_found():
    r = client.get("/api/projects/nonexistent")
    assert r.status_code == 404


def test_get_project_slim_drops_entries(client_for_slim=None):
    # ms-84 / e-2326 — ?slim=true returns the project with entries[] omitted
    # from each milestone, but keeps the computed total_tasks / done_tasks so
    # the dashboard card summary stays accurate. Web UI uses this on initial
    # mount so the WS frame limit (1 MiB) can't be tripped on first paint.
    r = client.get(f"/api/projects/{PROJECT_ID}?slim=true")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Test"
    for ms in body["milestones"]:
        assert "entries" not in ms, f"milestone {ms['id']} leaked entries in slim mode"
        assert "total_tasks" in ms
        assert "done_tasks" in ms


def test_get_project_slim_drops_tab_scoped_arrays():
    # ms-84 / e-2326 follow-up — slim must also drop top-level pushes /
    # deployments / worktree_sessions so the WS frame stays under Cloud Run's
    # effective WS tolerance. These are fetched via tab-specific endpoints
    # when the user switches to Releases / Worktree tab.
    r = client.get(f"/api/projects/{PROJECT_ID}?slim=true")
    assert r.status_code == 200
    body = r.json()
    for tab_scoped in ("pushes", "deployments", "worktree_sessions"):
        assert tab_scoped not in body, (
            f"slim leaked tab-scoped array '{tab_scoped}' "
            f"(WS frame will be inflated for projects that have any history)"
        )


def test_get_project_pushes_endpoint():
    r = client.get(f"/api/projects/{PROJECT_ID}/pushes")
    assert r.status_code == 200
    body = r.json()
    assert "pushes" in body
    assert isinstance(body["pushes"], list)


def test_get_project_deployments_endpoint():
    r = client.get(f"/api/projects/{PROJECT_ID}/deployments")
    assert r.status_code == 200
    body = r.json()
    assert "deployments" in body
    assert isinstance(body["deployments"], list)


def test_get_project_worktree_sessions_endpoint():
    r = client.get(f"/api/projects/{PROJECT_ID}/worktree-sessions")
    assert r.status_code == 200
    body = r.json()
    assert "worktree_sessions" in body
    assert isinstance(body["worktree_sessions"], list)


def test_get_milestone_entries_returns_full_tree():
    # ms-84 / e-2326 — pair endpoint to the slim broadcast. Web UI calls this
    # per-MS when the user expands a card; payload shape matches the legacy
    # nested entries[] tree from _enrich_project so the SHARED render code
    # keeps working unchanged.
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries")
    assert r.status_code == 200
    body = r.json()
    assert body["milestone_id"] == "ms-1"
    assert isinstance(body["entries"], list)
    # ms-1 fixture has 1 todo task → exactly one entry in the tree.
    assert len(body["entries"]) >= 1


def test_get_milestone_entries_missing_milestone():
    r = client.get(f"/api/projects/{PROJECT_ID}/milestones/ms-nonexistent/entries")
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

def test_update_summary_is_deprecated_no_op():
    """e-1040 completed: PATCH /summary writes are no-op.

    The endpoint returns 200 with the previously-stored value (so legacy
    callers don't crash) and a Deprecation header so machine consumers
    can detect the contract change.
    """
    # Seed an existing value to verify it's preserved through the no-op.
    _store[PROJECT_ID]["summary"] = "previous value"

    r = client.patch(f"/api/projects/{PROJECT_ID}/summary",
                     json={"text": "would-be new summary"})

    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "previous value"  # untouched
    assert body.get("write_ignored") is True
    assert body.get("deprecated_since") == "e-1040"
    # HTTP deprecation signals present.
    assert r.headers.get("Deprecation") == "true"
    assert "Sunset" in r.headers
    # Storage was NOT mutated.
    assert _store[PROJECT_ID]["summary"] == "previous value"


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def test_auth_required_when_enabled():
    """When auth is enabled, requests without a token should get 401."""
    original = app_module._auth_enabled
    try:
        app_module._auth_enabled = True
        r = client.get("/api/projects")
        assert r.status_code == 401
    finally:
        app_module._auth_enabled = original


def test_auth_invalid_token():
    """When auth is enabled, an invalid token should get 401."""
    original = app_module._auth_enabled
    try:
        app_module._auth_enabled = True
        r = client.get("/api/projects",
                       headers={"Authorization": "Bearer invalid-token"})
        assert r.status_code == 401
    finally:
        app_module._auth_enabled = original


def test_health_no_auth():
    """Health endpoint should work without auth regardless."""
    original = app_module._auth_enabled
    try:
        app_module._auth_enabled = True
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
    finally:
        app_module._auth_enabled = original


# ---------------------------------------------------------------------------
# ms-43 e-631: unified cross-entity search endpoint
# ---------------------------------------------------------------------------

def test_search_endpoint_returns_unified_results():
    """/api/projects/{id}/search should return milestones, tasks, commits,
    documents, etc. in a single response with facets."""
    # Add a document so we can verify cross-entity coverage.
    _docs_store[PROJECT_ID] = [
        {"doc_id": "spec-1", "title": "First milestone SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Design doc for first milestone.",
         "updated_at": "2026-05-11T00:00:00Z"},
    ]

    r = client.get(f"/api/projects/{PROJECT_ID}/search", params={"q": "first"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body
    assert "facets" in body
    assert "total" in body

    types = {r["entity_type"] for r in body["results"]}
    # "First milestone" matches the milestone title; "First milestone SPEC"
    # matches the document title. Both must appear.
    assert "milestone" in types, types
    assert "document" in types, types


def test_search_endpoint_type_filter():
    """type=document should restrict to documents only."""
    _docs_store[PROJECT_ID] = [
        {"doc_id": "spec-1", "title": "Auth SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Auth design", "updated_at": "2026-05-11T00:00:00Z"},
    ]
    r = client.get(f"/api/projects/{PROJECT_ID}/search",
                   params={"q": "auth", "type": "document"})
    assert r.status_code == 200, r.text
    body = r.json()
    types = {x["entity_type"] for x in body["results"]}
    assert types <= {"document"}, types


def test_search_endpoint_empty_query_returns_recent():
    """Empty q + no filters should return recent activity (≤ limit), not 400."""
    r = client.get(f"/api/projects/{PROJECT_ID}/search")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 0
    assert isinstance(body["results"], list)


def test_search_endpoint_facets_present():
    """Facets must be returned so the Web UI can render filter chips."""
    r = client.get(f"/api/projects/{PROJECT_ID}/search", params={"q": "milestone"})
    assert r.status_code == 200
    body = r.json()
    assert "type" in body["facets"]
    assert "status" in body["facets"]


def test_search_endpoint_scope_filter_documents():
    """e-616: scope filter restricts documents to a single scope."""
    _docs_store[PROJECT_ID] = [
        {"doc_id": "core-1", "title": "Memory layer principle",
         "scope": "core", "content": "Memory layer is passive",
         "updated_at": "2026-05-01T00:00:00Z"},
        {"doc_id": "spec-1", "title": "Auth SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Auth design", "updated_at": "2026-05-11T00:00:00Z"},
    ]
    r = client.get(f"/api/projects/{PROJECT_ID}/search",
                   params={"type": "document", "scope": "spec"})
    assert r.status_code == 200, r.text
    body = r.json()
    scopes = {x.get("scope") for x in body["results"]}
    assert scopes == {"spec"}, scopes
