"""Tests for cloud-first identity endpoints (ms-62 / e-1509).

Three endpoints under /api/me/:

  * GET  /api/me/projects   — list user's project memberships with role
  * POST /api/me/machine    — get-or-mint a machine_id for a fingerprint
  * POST /api/me/heartbeat  — get-or-mint a session_id for identity tuple

The tests run against the FastAPI TestClient with firestore_client mocked
through in-memory dicts so they don't need a real Firestore connection.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client

# ---------------------------------------------------------------------------
# In-memory mocks (kept minimal — only the functions the new endpoints call)
# ---------------------------------------------------------------------------

_projects: dict[str, dict] = {}
_machines: dict[tuple[str, str], str] = {}  # (user_id, fingerprint) → machine_id
_sessions_by_tuple: dict[tuple[str, str, int], str] = {}  # tuple → sid
_session_records: dict[str, dict] = {}  # sid → record


def reset_state():
    _projects.clear()
    _machines.clear()
    _sessions_by_tuple.clear()
    _session_records.clear()


def mock_list_projects(user_id=None, include_archived=False):
    out = []
    for pid, data in _projects.items():
        if not include_archived and data.get("archived"):
            continue
        if user_id:
            owner = data.get("owner")
            if owner:
                members = [m.get("user_id") for m in data.get("members", [])]
                if owner != user_id and user_id not in members:
                    continue
        out.append({
            "project_id": pid,
            "name": data.get("name", ""),
            "objective": data.get("objective", ""),
            "archived": data.get("archived", False),
        })
    return out


def mock_get_project(project_id: str):
    return _projects.get(project_id)


def mock_save_project(project_id: str, data: dict):
    _projects[project_id] = data


def mock_get_or_mint_machine(user_id: str, fingerprint: str, *, hostname="", agent=""):
    key = (user_id, fingerprint)
    if key in _machines:
        return _machines[key], False
    mid = f"mc-{len(_machines):08x}"
    _machines[key] = mid
    return mid, True


def mock_get_or_mint_session_by_tuple(
    project_id, machine_id, parent_pid, *, user_id, cwd="", metadata=None
):
    key = (project_id, machine_id, parent_pid)
    now = "2026-06-12T00:00:00.000000Z"
    if key in _sessions_by_tuple:
        sid = _sessions_by_tuple[key]
        rec = _session_records[sid]
        rec["last_heartbeat_at"] = now
        if cwd:
            rec["cwd"] = cwd
        return {
            "session_id": sid,
            "minted": False,
            "last_heartbeat_at": now,
            "created_at": rec["created_at"],
        }
    sid = f"sv-{machine_id[3:11]}-{parent_pid}-{len(_session_records):04x}"
    _sessions_by_tuple[key] = sid
    _session_records[sid] = {
        "session_id": sid,
        "user_id": user_id,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "cwd": cwd,
        "created_at": now,
        "last_heartbeat_at": now,
        **(metadata or {}),
    }
    return {
        "session_id": sid,
        "minted": True,
        "last_heartbeat_at": now,
        "created_at": now,
    }


firestore_client.list_projects = mock_list_projects
firestore_client.get_project = mock_get_project
firestore_client.save_project = mock_save_project
firestore_client.get_or_mint_machine = mock_get_or_mint_machine
firestore_client.get_or_mint_session_by_tuple = mock_get_or_mint_session_by_tuple

from fastapi.testclient import TestClient
import app as app_module

# Disable auth so we can inject the user dict via dependency override.
app_module._auth_enabled = False

# Override require_auth to return a fixed user. The endpoint code reads
# user["sub"] so we must populate that.
async def _mock_require_auth():
    return {"sub": "user-alice", "email": "alice@example.com"}


client = TestClient(app_module.app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    # Re-apply firestore_client mocks every test in case another test module
    # (e.g. tests/test_api.py) has replaced them via its own autouse fixture.
    # See tests/test_api.py:reset_store for the same pattern + rationale.
    firestore_client.list_projects = mock_list_projects
    firestore_client.get_project = mock_get_project
    firestore_client.save_project = mock_save_project
    firestore_client.get_or_mint_machine = mock_get_or_mint_machine
    firestore_client.get_or_mint_session_by_tuple = mock_get_or_mint_session_by_tuple
    # Install dependency override at test time so we don't leak into other
    # test modules (e.g. test_api.py expects the default require_auth path).
    app_module.app.dependency_overrides[app_module.require_auth] = _mock_require_auth
    reset_state()
    yield
    reset_state()
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)


def test_me_projects_returns_only_member_projects():
    _projects["proj-a"] = {
        "name": "Project A", "owner": "user-alice",
        "members": [],
    }
    _projects["proj-b"] = {
        "name": "Project B", "owner": "user-bob",
        "members": [{"user_id": "user-alice", "role": "editor"}],
    }
    _projects["proj-c"] = {
        "name": "Project C", "owner": "user-bob",
        "members": [],
    }
    r = client.get("/api/me/projects")
    assert r.status_code == 200
    data = r.json()
    ids = sorted(x["id"] for x in data)
    assert ids == ["proj-a", "proj-b"]
    by_id = {x["id"]: x for x in data}
    assert by_id["proj-a"]["role"] == "owner"
    assert by_id["proj-b"]["role"] == "editor"


def test_me_projects_includes_name():
    _projects["proj-a"] = {
        "name": "My Cool Project", "owner": "user-alice",
        "members": [],
    }
    r = client.get("/api/me/projects")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "My Cool Project"


def test_me_machine_mints_on_first_call():
    r = client.post(
        "/api/me/machine",
        json={"fingerprint": "macbook-alice"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["minted"] is True
    assert body["machine_id"].startswith("mc-")
    assert body["fingerprint"] == "macbook-alice"


def test_me_machine_returns_same_id_for_same_fingerprint():
    r1 = client.post("/api/me/machine", json={"fingerprint": "macbook-alice"})
    r2 = client.post("/api/me/machine", json={"fingerprint": "macbook-alice"})
    assert r1.json()["machine_id"] == r2.json()["machine_id"]
    assert r1.json()["minted"] is True
    assert r2.json()["minted"] is False


def test_me_machine_distinct_ids_for_distinct_fingerprints():
    r1 = client.post("/api/me/machine", json={"fingerprint": "macbook"})
    r2 = client.post("/api/me/machine", json={"fingerprint": "thinkpad"})
    assert r1.json()["machine_id"] != r2.json()["machine_id"]


def test_me_machine_requires_fingerprint():
    r = client.post("/api/me/machine", json={"fingerprint": ""})
    assert r.status_code == 400


def test_me_heartbeat_mints_on_first_tuple():
    _projects["proj-a"] = {
        "name": "P", "owner": "user-alice", "members": [],
    }
    r = client.post("/api/me/heartbeat", json={
        "project_id": "proj-a",
        "machine_id": "mc-abc123",
        "parent_pid": 1234,
        "cwd": "/home/alice/work",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["minted"] is True
    assert body["session_id"].startswith("sv-")
    assert body["last_heartbeat_at"]
    assert body["created_at"] == body["last_heartbeat_at"]


def test_me_heartbeat_returns_same_sid_for_same_tuple():
    _projects["proj-a"] = {
        "name": "P", "owner": "user-alice", "members": [],
    }
    body = {
        "project_id": "proj-a", "machine_id": "mc-abc123",
        "parent_pid": 1234, "cwd": "/x",
    }
    r1 = client.post("/api/me/heartbeat", json=body)
    r2 = client.post("/api/me/heartbeat", json=body)
    assert r1.json()["session_id"] == r2.json()["session_id"]
    assert r1.json()["minted"] is True
    assert r2.json()["minted"] is False


def test_me_heartbeat_distinct_sid_for_distinct_parent_pid():
    """Multi-bclaude in same cwd: different terminals → different parent_pids."""
    _projects["proj-a"] = {
        "name": "P", "owner": "user-alice", "members": [],
    }
    base = {
        "project_id": "proj-a", "machine_id": "mc-abc123", "cwd": "/x",
    }
    r1 = client.post("/api/me/heartbeat", json={**base, "parent_pid": 1234})
    r2 = client.post("/api/me/heartbeat", json={**base, "parent_pid": 5678})
    assert r1.json()["session_id"] != r2.json()["session_id"]
    assert r1.json()["minted"] is True
    assert r2.json()["minted"] is True


def test_me_heartbeat_distinct_sid_for_distinct_machine():
    """Cross-machine: different machine_ids → different sids."""
    _projects["proj-a"] = {
        "name": "P", "owner": "user-alice", "members": [],
    }
    base = {
        "project_id": "proj-a", "parent_pid": 1234, "cwd": "/x",
    }
    r1 = client.post("/api/me/heartbeat", json={**base, "machine_id": "mc-mac"})
    r2 = client.post("/api/me/heartbeat", json={**base, "machine_id": "mc-win"})
    assert r1.json()["session_id"] != r2.json()["session_id"]


def test_me_heartbeat_rejects_non_member():
    """Membership boundary: non-member cannot materialise sessions."""
    _projects["proj-secret"] = {
        "name": "Secret", "owner": "user-bob",
        "members": [{"user_id": "user-carol", "role": "editor"}],
    }
    r = client.post("/api/me/heartbeat", json={
        "project_id": "proj-secret",
        "machine_id": "mc-abc",
        "parent_pid": 1234,
    })
    assert r.status_code == 403


def test_me_heartbeat_404_for_missing_project():
    r = client.post("/api/me/heartbeat", json={
        "project_id": "proj-nonexistent",
        "machine_id": "mc-abc",
        "parent_pid": 1234,
    })
    assert r.status_code == 404


def test_me_heartbeat_requires_positive_parent_pid():
    _projects["proj-a"] = {
        "name": "P", "owner": "user-alice", "members": [],
    }
    r = client.post("/api/me/heartbeat", json={
        "project_id": "proj-a",
        "machine_id": "mc-abc",
        "parent_pid": 0,
    })
    assert r.status_code == 400
