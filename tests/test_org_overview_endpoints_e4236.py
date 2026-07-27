"""Org overview endpoint tests (ms-118 / e-4236).

Pins the contract of:
  * GET /api/orgs/{org_id}/overview — read-only 俯瞰: この org の project と member

Invariants:
  - only projects **homed to this org** are listed (project_org_id == org_id);
  - only projects the caller **participates in** are listed (via list_projects);
  - each member row carries role + external_guest (org 非所属 = 外部ゲスト, e-4235);
  - non-member of the org → 404 (存在を漏らさない); the endpoint never writes.
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


_orgs: dict[str, dict] = {}
_projects: dict[str, dict] = {}
_visible: dict[str, list] = {}  # uid -> [project summaries]
_project_saves: list = []


def _mock_get_org(org_id):
    d = _orgs.get(org_id)
    return copy.deepcopy(d) if d else None


def _mock_save_org(org_id, data):
    _orgs[org_id] = {**copy.deepcopy(data), "org_id": org_id}


def _mock_list_projects(uid=None):
    return copy.deepcopy(_visible.get(uid, []))


def _mock_get_project(pid):
    d = _projects.get(pid)
    return copy.deepcopy(d) if d else None


def _mock_save_project(pid, data):
    _project_saves.append(pid)


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    db = app_module.db
    prior = {}
    for name, mock in [
        ("get_org", _mock_get_org),
        ("save_org", _mock_save_org),
        ("list_projects", _mock_list_projects),
        ("get_project", _mock_get_project),
        ("save_project", _mock_save_project),
    ]:
        prior[name] = getattr(db, name, None)
        setattr(db, name, mock)
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    _orgs.clear(); _projects.clear(); _visible.clear(); _project_saves.clear()
    yield
    app_module._auth_enabled = prior_auth
    for k, v in prior.items():
        if v is None:
            hasattr(db, k) and delattr(db, k)
        else:
            setattr(db, k, v)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)


client = TestClient(app_module.app)


def _impersonate(uid):
    app_module.app.dependency_overrides[app_module.require_auth] = \
        lambda: {"sub": uid, "email": f"{uid}@x"}


def _seed_org(oid, owner, members):
    _orgs[oid] = {"org_id": oid, "name": "Acme", "personal": False,
                  "members": [{"user_id": u, "role": r} for u, r in members]}


def test_overview_lists_org_projects_with_guest_flags():
    _seed_org("org-t-a", "u1", [("u1", "owner"), ("emp", "member")])
    # p1 belongs to org-t-a, has owner u1 + emp + an external guest.
    _projects["p1"] = {"project_id": "p1", "name": "Proj1", "owner": "u1",
                       "org_id": "org-t-a",
                       "members": [{"user_id": "emp", "role": "editor"},
                                   {"user_id": "guest", "role": "viewer"}]}
    # p2 belongs to a DIFFERENT org → excluded.
    _projects["p2"] = {"project_id": "p2", "name": "Other", "owner": "u1",
                       "org_id": "org-t-zzzz", "members": []}
    _visible["u1"] = [{"project_id": "p1"}, {"project_id": "p2"}]
    _impersonate("u1")

    r = client.get("/api/orgs/org-t-a/overview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_id"] == "org-t-a"
    assert [p["project_id"] for p in body["projects"]] == ["p1"]  # p2 excluded
    members = {m["user_id"]: m for m in body["projects"][0]["members"]}
    assert members["u1"]["role"] == "owner"
    assert members["u1"]["external_guest"] is False
    assert members["emp"]["external_guest"] is False   # org member
    assert members["guest"]["external_guest"] is True  # not in org
    assert _project_saves == []  # read-only


def test_overview_excludes_non_participating_projects():
    _seed_org("org-t-a", "u1", [("u1", "owner")])
    _projects["p1"] = {"project_id": "p1", "name": "P1", "owner": "u1",
                       "org_id": "org-t-a", "members": []}
    # caller participates in nothing → empty overview even though p1 is in the org.
    _visible["u1"] = []
    _impersonate("u1")
    r = client.get("/api/orgs/org-t-a/overview")
    assert r.status_code == 200
    assert r.json()["projects"] == []


def test_overview_non_member_is_404():
    _seed_org("org-t-a", "u1", [("u1", "owner")])
    _impersonate("stranger")
    r = client.get("/api/orgs/org-t-a/overview")
    assert r.status_code == 404


def test_overview_unknown_org_is_404():
    _impersonate("u1")
    r = client.get("/api/orgs/org-t-ghost/overview")
    assert r.status_code == 404
