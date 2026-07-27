"""Org membership endpoint tests (ms-118 / e-4232).

Pins the contract of:
  * POST   /api/orgs/{org_id}/members          — invite (所属のみ、participation-only)
  * DELETE /api/orgs/{org_id}/members/{target}  — remove (owner/admin only)

The headline regression is **participation-not-granted**: inviting a member
must touch ONLY the org doc and never grant project access (= never write any
project). We assert that by spying on db.save_project.
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
_users: dict[str, tuple] = {}
_project_saves: list = []


def _mock_get_org(org_id):
    data = _orgs.get(org_id)
    return copy.deepcopy(data) if data else None


def _mock_save_org(org_id, data):
    payload = {k: v for k, v in data.items() if k != "org_id"}
    _orgs[org_id] = {**copy.deepcopy(payload), "org_id": org_id}


def _mock_find_user_by_email(email):
    return _users.get(email)


def _mock_save_project(project_id, data):
    # participation-only regression の番人: invite/remove がここを呼んだら失格。
    _project_saves.append(project_id)


@pytest.fixture(autouse=True)
def _bind_mock_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_org", _mock_get_org),
        ("save_org", _mock_save_org),
        ("find_user_by_email", _mock_find_user_by_email),
        ("save_project", _mock_save_project),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    _orgs.clear()
    _users.clear()
    _project_saves.clear()
    yield
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
    """Create an org owned by `owner` and return its id."""
    _impersonate(owner)
    # reuse the create endpoint so the owner membership shape is authentic
    r = client.post("/api/orgs", json={"name": "Acme"})
    assert r.status_code == 200, r.text
    return r.json()["org_id"]


def test_invite_grants_membership_only_not_project_access():
    _users["bob@x"] = ("uid-bob", {"email": "bob@x"})
    oid = _seed_org("u1")
    _impersonate("u1")
    r = client.post(f"/api/orgs/{oid}/members", json={"email": "bob@x"})
    assert r.status_code == 200, r.text
    members = {m["user_id"]: m["role"] for m in r.json()["members"]}
    assert members == {"u1": "owner", "uid-bob": "member"}
    # ★ participation-only: no project was written by the invite.
    assert _project_saves == []


def test_invite_by_non_member_is_404():
    _users["bob@x"] = ("uid-bob", {"email": "bob@x"})
    oid = _seed_org("u1")
    _impersonate("stranger")
    r = client.post(f"/api/orgs/{oid}/members", json={"email": "bob@x"})
    assert r.status_code == 404  # org は非 member に存在を漏らさない


def test_invite_unknown_email_is_404():
    oid = _seed_org("u1")
    _impersonate("u1")
    r = client.post(f"/api/orgs/{oid}/members", json={"email": "ghost@x"})
    assert r.status_code == 404


def test_invite_invalid_role_is_400():
    _users["bob@x"] = ("uid-bob", {"email": "bob@x"})
    oid = _seed_org("u1")
    _impersonate("u1")
    r = client.post(f"/api/orgs/{oid}/members",
                    json={"email": "bob@x", "role": "owner"})
    assert r.status_code == 400


def test_remove_member_by_owner_ok():
    _users["bob@x"] = ("uid-bob", {"email": "bob@x"})
    oid = _seed_org("u1")
    _impersonate("u1")
    client.post(f"/api/orgs/{oid}/members", json={"email": "bob@x"})
    r = client.delete(f"/api/orgs/{oid}/members/uid-bob")
    assert r.status_code == 200, r.text
    assert all(m["user_id"] != "uid-bob" for m in r.json()["members"])


def test_remove_member_by_plain_member_is_403():
    _users["bob@x"] = ("uid-bob", {"email": "bob@x"})
    oid = _seed_org("u1")
    _impersonate("u1")
    client.post(f"/api/orgs/{oid}/members", json={"email": "bob@x"})
    # bob (plain member) tries to remove the owner
    _impersonate("uid-bob")
    r = client.delete(f"/api/orgs/{oid}/members/u1")
    assert r.status_code == 403


def test_remove_last_owner_is_400():
    oid = _seed_org("u1")
    _impersonate("u1")
    r = client.delete(f"/api/orgs/{oid}/members/u1")
    assert r.status_code == 400  # org を owner 不在にしない
