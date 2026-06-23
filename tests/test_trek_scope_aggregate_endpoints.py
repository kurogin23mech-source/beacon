"""Trek scope aggregate endpoint tests (ms-86 / e-2248).

Pins the contract of three new endpoints introduced by e-2248 SPEC
(``IgCFK8I34cBfPco3UE06``):

  * **GET /api/treks/{trek_id}/milestones**
  * **GET /api/treks/{trek_id}/operations**
  * **GET /api/treks/{trek_id}/tasks**

Each iterates the Trek's ``scope`` to collect project_ids, walks every
covered project's milestones / operations / tasks, dedupes, and attaches
``project_id`` (and for tasks: ``milestone_id`` or ``operation_id``)
so the Web UI can render cross-project Trek detail without coupling
``state.project`` (= e-2240 SPEC 設計方針 5).

The tests reuse the mock infrastructure pattern from
``test_trek_docs_and_lookup.py`` so the surface stays consistent.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402,F401
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402


# ---------------------------------------------------------------------------
# Shared mock store (mirrors test_trek_docs_and_lookup.py)
# ---------------------------------------------------------------------------

_treks: dict[str, dict] = {}
_projects: dict[str, dict] = {}


def _mock_get_trek(trek_id):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id, data):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    _treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}


def _mock_list_treks(actor_id=None, *, status=None, include_archived=False):
    out = []
    for t in _treks.values():
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        if actor_id:
            creator = (t.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id") for m in t.get("members") or []]
            if creator != actor_id and actor_id not in members:
                continue
        out.append(copy.deepcopy(t))
    return out


def _mock_get_project(project_id):
    return copy.deepcopy(_projects.get(project_id))


def _mock_save_project(project_id, data):
    _projects[project_id] = copy.deepcopy(data)


def _mock_no_op(*args, **kwargs):
    return None


def _rebind_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("get_project", _mock_get_project),
        ("save_project", _mock_save_project),
        ("find_user_by_email", lambda email: None),
        ("get_or_create_user", _mock_no_op),
        ("get_user", _mock_no_op),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    return prior


def _restore_db(prior):
    db_module = app_module.db
    for k, v in prior.items():
        if v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)


client = TestClient(app_module.app)


LEADER_UID = "uid-leader"
PROJECT_A = "proj-a"
PROJECT_B = "proj-b"


def _impersonate(uid):
    def _fake_auth():
        return {"sub": uid, "email": f"{uid}@x"}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


def _make_project(pid: str, *,
                  milestones: list[dict] | None = None,
                  operations: list[dict] | None = None):
    _projects[pid] = {
        "name": f"Project {pid}",
        "owner": LEADER_UID,
        "members": [
            {"user_id": LEADER_UID, "email": "leader@x", "role": "owner"},
        ],
        "milestones": milestones or [],
        "operations": operations or [],
    }


def _make_trek(trek_id: str, *, scope: list[dict],
               status: str = "active") -> None:
    _treks[trek_id] = {
        "trek_id": trek_id,
        "title": f"Trek {trek_id}",
        "type": "persistent",
        "status": status,
        "creator_actor": {"user_id": LEADER_UID, "email": "leader@x"},
        "leader_session_id": "sv-leader",
        "members": [{"user_id": LEADER_UID, "email": "leader@x",
                     "role": "leader",
                     "invited_at": "2026-06-23T00:00:00Z",
                     "joined_at": "2026-06-23T00:00:00Z"}],
        "scope": scope,
        "halt": None,
        "created_at": "2026-06-23T00:00:00Z",
        "updated_at": "2026-06-23T00:00:00Z",
        "archived_at": None,
    }


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _projects.clear()
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        _treks.clear()
        _projects.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior)


def _ms(ms_id: str, *, title: str = "", entries: list[dict] | None = None) -> dict:
    return {
        "id": ms_id,
        "title": title or f"MS {ms_id}",
        "status": "in_progress",
        "progress": 0,
        "target_date": "",
        "commits": [],
        "entries": entries or [],
    }


def _op(op_id: str, *, title: str = "", entries: list[dict] | None = None) -> dict:
    return {
        "id": op_id,
        "title": title or f"Op {op_id}",
        "status": "open",
        "entries": entries or [],
    }


def _task(entry_id: str, *, description: str = "") -> dict:
    return {
        "id": entry_id,
        "type": "task",
        "description": description or f"task {entry_id}",
        "status": "todo",
    }


# ---------------------------------------------------------------------------
# /api/treks/{trek_id}/milestones
# ---------------------------------------------------------------------------

class TestTrekMilestonesEndpoint:
    def test_project_wide_scope_returns_all_milestones(self):
        _make_project(PROJECT_A, milestones=[_ms("ms-1"), _ms("ms-2")])
        _make_trek("tk-wide", scope=[{"project": PROJECT_A}])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-wide/milestones")
        assert r.status_code == 200
        ids = sorted(m["id"] for m in r.json())
        assert ids == ["ms-1", "ms-2"]
        # source project_id surfaced for UI deep-link.
        assert all(m["project_id"] == PROJECT_A for m in r.json())

    def test_narrow_milestone_scope_returns_only_that_ms(self):
        _make_project(PROJECT_A, milestones=[_ms("ms-1"), _ms("ms-2")])
        _make_trek("tk-narrow", scope=[
            {"project": PROJECT_A, "milestone": "ms-2"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-narrow/milestones")
        assert r.status_code == 200
        ids = [m["id"] for m in r.json()]
        assert ids == ["ms-2"]

    def test_operation_only_scope_contributes_no_milestones(self):
        _make_project(PROJECT_A,
                      milestones=[_ms("ms-1")],
                      operations=[_op("op-1")])
        _make_trek("tk-op-only", scope=[
            {"project": PROJECT_A, "operation": "op-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-op-only/milestones")
        # Operation-narrowed scope contributes Ops, not MS.
        assert r.json() == []

    def test_task_only_scope_contributes_no_milestones(self):
        _make_project(PROJECT_A, milestones=[_ms("ms-1")])
        _make_trek("tk-task-only", scope=[
            {"project": PROJECT_A, "task": "e-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-task-only/milestones")
        assert r.json() == []

    def test_iterates_all_projects_in_scope(self):
        _make_project(PROJECT_A, milestones=[_ms("ms-a")])
        _make_project(PROJECT_B, milestones=[_ms("ms-b")])
        _make_trek("tk-multi", scope=[
            {"project": PROJECT_A},
            {"project": PROJECT_B},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-multi/milestones")
        pairs = sorted((m["project_id"], m["id"]) for m in r.json())
        assert pairs == [(PROJECT_A, "ms-a"), (PROJECT_B, "ms-b")]

    def test_dedupes_when_multiple_scope_entries_target_same_ms(self):
        _make_project(PROJECT_A, milestones=[_ms("ms-1")])
        _make_trek("tk-dup", scope=[
            {"project": PROJECT_A, "milestone": "ms-1"},
            {"project": PROJECT_A, "milestone": "ms-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-dup/milestones")
        assert len(r.json()) == 1

    def test_empty_scope_returns_empty_list(self):
        _make_trek("tk-empty", scope=[])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-empty/milestones")
        assert r.json() == []

    def test_stale_scope_entry_does_not_break_listing(self):
        # PROJECT_A exists, "proj-stale" doesn't. Should silently skip.
        _make_project(PROJECT_A, milestones=[_ms("ms-1")])
        _make_trek("tk-stale", scope=[
            {"project": "proj-stale"},
            {"project": PROJECT_A},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-stale/milestones")
        assert [m["id"] for m in r.json()] == ["ms-1"]

    def test_non_member_returns_403(self):
        _make_project(PROJECT_A, milestones=[_ms("ms-1")])
        _make_trek("tk-private", scope=[{"project": PROJECT_A}])
        _impersonate("uid-outsider")
        r = client.get("/api/treks/tk-private/milestones")
        assert r.status_code == 403

    def test_missing_trek_returns_404(self):
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-nonexistent/milestones")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# /api/treks/{trek_id}/operations
# ---------------------------------------------------------------------------

class TestTrekOperationsEndpoint:
    def test_project_wide_scope_returns_all_operations(self):
        _make_project(PROJECT_A, operations=[_op("op-1"), _op("op-2")])
        _make_trek("tk-wide", scope=[{"project": PROJECT_A}])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-wide/operations")
        assert r.status_code == 200
        ids = sorted(o["id"] for o in r.json())
        assert ids == ["op-1", "op-2"]
        assert all(o["project_id"] == PROJECT_A for o in r.json())

    def test_narrow_operation_scope(self):
        _make_project(PROJECT_A, operations=[_op("op-1"), _op("op-2")])
        _make_trek("tk-narrow", scope=[
            {"project": PROJECT_A, "operation": "op-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-narrow/operations")
        assert [o["id"] for o in r.json()] == ["op-1"]

    def test_milestone_scope_does_not_leak_into_operations(self):
        _make_project(PROJECT_A,
                      milestones=[_ms("ms-1")],
                      operations=[_op("op-1")])
        _make_trek("tk-ms-only", scope=[
            {"project": PROJECT_A, "milestone": "ms-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-ms-only/operations")
        assert r.json() == []

    def test_cross_project_aggregation(self):
        _make_project(PROJECT_A, operations=[_op("op-a")])
        _make_project(PROJECT_B, operations=[_op("op-b")])
        _make_trek("tk-multi", scope=[
            {"project": PROJECT_A},
            {"project": PROJECT_B},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-multi/operations")
        pairs = sorted((o["project_id"], o["id"]) for o in r.json())
        assert pairs == [(PROJECT_A, "op-a"), (PROJECT_B, "op-b")]


# ---------------------------------------------------------------------------
# /api/treks/{trek_id}/tasks
# ---------------------------------------------------------------------------

class TestTrekTasksEndpoint:
    def test_project_wide_scope_collects_all_tasks_from_ms_and_op(self):
        _make_project(PROJECT_A,
                      milestones=[_ms("ms-1", entries=[
                          _task("e-1"), _task("e-2"),
                      ])],
                      operations=[_op("op-1", entries=[_task("e-3")])])
        _make_trek("tk-wide", scope=[{"project": PROJECT_A}])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-wide/tasks")
        assert r.status_code == 200
        ids = sorted(t["id"] for t in r.json())
        assert ids == ["e-1", "e-2", "e-3"]
        # parent container reference attached.
        by_id = {t["id"]: t for t in r.json()}
        assert by_id["e-1"]["milestone_id"] == "ms-1"
        assert by_id["e-3"]["operation_id"] == "op-1"
        assert all(t["project_id"] == PROJECT_A for t in r.json())

    def test_milestone_narrow_collects_only_that_ms_tasks(self):
        _make_project(PROJECT_A, milestones=[
            _ms("ms-1", entries=[_task("e-1"), _task("e-2")]),
            _ms("ms-2", entries=[_task("e-9")]),
        ])
        _make_trek("tk-ms", scope=[
            {"project": PROJECT_A, "milestone": "ms-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-ms/tasks")
        ids = sorted(t["id"] for t in r.json())
        assert ids == ["e-1", "e-2"]

    def test_operation_narrow_collects_only_that_op_tasks(self):
        _make_project(PROJECT_A,
                      milestones=[_ms("ms-1", entries=[_task("e-x")])],
                      operations=[_op("op-1", entries=[_task("e-1")])])
        _make_trek("tk-op", scope=[
            {"project": PROJECT_A, "operation": "op-1"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-op/tasks")
        ids = [t["id"] for t in r.json()]
        assert ids == ["e-1"]

    def test_task_narrow_collects_only_that_task(self):
        _make_project(PROJECT_A, milestones=[
            _ms("ms-1", entries=[_task("e-1"), _task("e-2"), _task("e-3")]),
        ])
        _make_trek("tk-task", scope=[
            {"project": PROJECT_A, "task": "e-2"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-task/tasks")
        ids = [t["id"] for t in r.json()]
        assert ids == ["e-2"]

    def test_union_across_scope_entries(self):
        # Multiple narrowing kinds in one Trek scope: their contributions
        # union (= a task included by any entry shows up).
        _make_project(PROJECT_A, milestones=[
            _ms("ms-1", entries=[_task("e-1")]),
            _ms("ms-2", entries=[_task("e-2"), _task("e-9")]),
        ])
        _make_trek("tk-union", scope=[
            {"project": PROJECT_A, "milestone": "ms-1"},
            {"project": PROJECT_A, "task": "e-2"},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-union/tasks")
        ids = sorted(t["id"] for t in r.json())
        assert ids == ["e-1", "e-2"]

    def test_cross_project_dedup(self):
        _make_project(PROJECT_A, milestones=[
            _ms("ms-a", entries=[_task("e-1")]),
        ])
        _make_project(PROJECT_B, milestones=[
            _ms("ms-b", entries=[_task("e-99")]),
        ])
        _make_trek("tk-multi", scope=[
            {"project": PROJECT_A},
            {"project": PROJECT_B},
        ])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-multi/tasks")
        pairs = sorted((t["project_id"], t["id"]) for t in r.json())
        assert pairs == [(PROJECT_A, "e-1"), (PROJECT_B, "e-99")]

    def test_non_task_entries_skipped(self):
        # Commit / other entry types should not surface on the /tasks
        # endpoint — only `type == "task"`.
        _make_project(PROJECT_A, milestones=[
            _ms("ms-1", entries=[
                _task("e-1"),
                {"id": "c-1", "type": "commit", "description": "noise"},
            ]),
        ])
        _make_trek("tk-clean", scope=[{"project": PROJECT_A}])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-clean/tasks")
        ids = [t["id"] for t in r.json()]
        assert ids == ["e-1"]

    def test_empty_scope_returns_empty(self):
        _make_trek("tk-empty", scope=[])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-empty/tasks")
        assert r.json() == []
