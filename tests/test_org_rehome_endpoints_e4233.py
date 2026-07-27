"""Project re-home endpoint tests (ms-118 / e-4233).

Pins the contract of:
  * POST /api/projects/{project_id}/rehome — org 所属リンクだけ張り替える

Headline invariants:
  - **owner-only**: only the project owner can re-home (editor → 403).
  - **target-org membership**: caller must belong to the destination org, and a
    non-member / unknown org is 404 (= 存在を漏らさない).
  - **identity + history preserved**: the saved project keeps project_id and
    milestones; only ``org_id`` changes (= disclosure re-evaluates live off the
    new org, no re-creation).
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
_project_saves: dict[str, dict] = {}


def _mock_get_org(org_id):
    data = _orgs.get(org_id)
    return copy.deepcopy(data) if data else None


def _mock_save_org(org_id, data):
    payload = {k: v for k, v in data.items() if k != "org_id"}
    _orgs[org_id] = {**copy.deepcopy(payload), "org_id": org_id}


def _mock_load_meta_only(project_id):
    data = _projects.get(project_id)
    if data is None:
        raise LookupError(project_id)
    return copy.deepcopy(data)


def _mock_save_project(project_id, data):
    _project_saves[project_id] = copy.deepcopy(data)
    _projects[project_id] = copy.deepcopy(data)


@pytest.fixture(autouse=True)
def _bind_mock_db():
    db_module = app_module.db
    ops_module = app_module.operations
    prior = {}
    for name, mock in [
        ("get_org", _mock_get_org),
        ("save_org", _mock_save_org),
        ("save_project", _mock_save_project),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    prior_meta = ops_module.load_project_meta_only
    ops_module.load_project_meta_only = _mock_load_meta_only
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    _orgs.clear()
    _projects.clear()
    _project_saves.clear()
    yield
    ops_module.load_project_meta_only = prior_meta
    app_module._auth_enabled = prior_auth
    for k, v in prior.items():
        if v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)


client = TestClient(app_module.app)


def _impersonate(uid, email=""):
    def _fake_auth():
        return {"sub": uid, "email": email or f"{uid}@x"}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


def _seed_org(owner="u1"):
    _impersonate(owner)
    r = client.post("/api/orgs", json={"name": "Acme"})
    assert r.status_code == 200, r.text
    return r.json()["org_id"]


def _seed_project(project_id="p1", owner="u1", milestones=None):
    _projects[project_id] = {
        "project_id": project_id,
        "name": "Proj",
        "owner": owner,
        "members": [],
        "milestones": milestones if milestones is not None else [],
    }


def test_rehome_by_owner_and_org_member_ok():
    oid = _seed_org("u1")
    _seed_project("p1", owner="u1", milestones=[{"id": "ms-1", "title": "keep"}])
    _impersonate("u1")
    r = client.post("/api/projects/p1/rehome", json={"org_id": oid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_id"] == oid
    assert body["previous_org_id"]  # personal org 導出値
    # ★ identity + history preserved in the saved doc; only org_id changed.
    saved = _project_saves["p1"]
    assert saved["project_id"] == "p1"
    assert saved["milestones"] == [{"id": "ms-1", "title": "keep"}]
    assert saved["org_id"] == oid


def test_rehome_by_non_owner_is_403():
    oid = _seed_org("u1")
    # bob is an editor member of the project, and also a member of the org…
    _seed_project("p1", owner="u1")
    _projects["p1"]["members"] = [{"user_id": "bob", "role": "editor"}]
    _orgs[oid]["members"].append(
        {"user_id": "bob", "email": "bob@x", "role": "member"})
    _impersonate("bob")
    r = client.post("/api/projects/p1/rehome", json={"org_id": oid})
    assert r.status_code == 403  # editor では re-home できない (owner-only)


def test_rehome_into_org_youre_not_member_of_is_404():
    oid = _seed_org("u1")  # owned by u1, bob not a member
    _seed_project("p1", owner="bob")  # bob owns the project…
    _impersonate("bob")
    r = client.post("/api/projects/p1/rehome", json={"org_id": oid})
    assert r.status_code == 404  # 所属していない org は存在を漏らさない
    assert "p1" not in _project_saves  # 拒否時は project を書かない


def test_rehome_unknown_org_is_404():
    _seed_project("p1", owner="u1")
    _impersonate("u1")
    r = client.post("/api/projects/p1/rehome",
                    json={"org_id": "org-t-ghost0000"})
    assert r.status_code == 404
    assert "p1" not in _project_saves


def test_rehome_missing_org_id_is_400():
    _seed_project("p1", owner="u1")
    _impersonate("u1")
    r = client.post("/api/projects/p1/rehome", json={"org_id": "  "})
    assert r.status_code == 400


def test_rehome_unknown_project_is_404():
    oid = _seed_org("u1")
    _impersonate("u1")
    r = client.post("/api/projects/ghost/rehome", json={"org_id": oid})
    assert r.status_code == 404
