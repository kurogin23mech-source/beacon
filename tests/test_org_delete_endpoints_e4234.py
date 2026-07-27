"""Org delete + owner-only destructive endpoint tests (ms-118 / e-4234).

Pins the contract of:
  * DELETE /api/orgs/{org_id}                    — delete org (owner only)
  * DELETE /api/orgs/{org_id}/members/{target}   — remove member, now owner ONLY
                                                   (e-4232 の owner/admin から厳格化)

Headline invariants:
  - **owner-only destructive**: admin can add members but cannot remove them or
    delete the org (both 403). member/non-member likewise rejected.
  - **personal org は削除不可** (400).
  - delete touches ONLY the org store, never a project (participation-only).
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
_deleted: list = []
_project_saves: list = []


def _mock_get_org(org_id):
    data = _orgs.get(org_id)
    return copy.deepcopy(data) if data else None


def _mock_save_org(org_id, data):
    payload = {k: v for k, v in data.items() if k != "org_id"}
    _orgs[org_id] = {**copy.deepcopy(payload), "org_id": org_id}


def _mock_delete_org(org_id):
    existed = org_id in _orgs
    _orgs.pop(org_id, None)
    _deleted.append(org_id)
    return existed


def _mock_find_user_by_email(email):
    return _users.get(email)


def _mock_save_project(project_id, data):
    _project_saves.append(project_id)


@pytest.fixture(autouse=True)
def _bind_mock_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_org", _mock_get_org),
        ("save_org", _mock_save_org),
        ("delete_org", _mock_delete_org),
        ("find_user_by_email", _mock_find_user_by_email),
        ("save_project", _mock_save_project),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    _orgs.clear()
    _users.clear()
    _deleted.clear()
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
    _impersonate(owner)
    r = client.post("/api/orgs", json={"name": "Acme"})
    assert r.status_code == 200, r.text
    return r.json()["org_id"]


def _add_member(oid, uid, email, role="member"):
    _users[email] = (uid, {"email": email})
    _impersonate("u1")  # owner adds
    r = client.post(f"/api/orgs/{oid}/members", json={"email": email, "role": role})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# DELETE /api/orgs/{org_id}
# --------------------------------------------------------------------------

def test_delete_org_by_owner_ok():
    oid = _seed_org("u1")
    _impersonate("u1")
    r = client.delete(f"/api/orgs/{oid}")
    assert r.status_code == 200, r.text
    assert r.json() == {"org_id": oid, "deleted": True}
    assert oid in _deleted
    assert _project_saves == []  # project は触らない


def test_delete_org_by_admin_is_403():
    oid = _seed_org("u1")
    _add_member(oid, "adm", "adm@x", role="admin")
    _impersonate("adm")
    r = client.delete(f"/api/orgs/{oid}")
    assert r.status_code == 403  # admin でも org 削除は不可 (owner only)
    assert oid not in _deleted


def test_delete_org_by_member_is_403():
    oid = _seed_org("u1")
    _add_member(oid, "bob", "bob@x")
    _impersonate("bob")
    r = client.delete(f"/api/orgs/{oid}")
    assert r.status_code == 403


def test_delete_org_by_non_member_is_404():
    oid = _seed_org("u1")
    _impersonate("stranger")
    r = client.delete(f"/api/orgs/{oid}")
    assert r.status_code == 404  # 非 member には存在を漏らさない


def test_delete_personal_org_is_400():
    import org as org_mod
    pid = org_mod.personal_org_id("u1")
    _orgs[pid] = org_mod.build_personal_org("u1", "u1@x",
                                            now="2026-07-27T00:00:00+00:00")
    _impersonate("u1")
    r = client.delete(f"/api/orgs/{pid}")
    assert r.status_code == 400  # personal org は削除不可
    assert pid not in _deleted


# --------------------------------------------------------------------------
# DELETE member — tightened to owner-only (e-4232 は owner/admin だった)
# --------------------------------------------------------------------------

def test_remove_member_by_admin_now_403():
    oid = _seed_org("u1")
    _add_member(oid, "adm", "adm@x", role="admin")
    _add_member(oid, "bob", "bob@x")
    _impersonate("adm")
    r = client.delete(f"/api/orgs/{oid}/members/bob")
    assert r.status_code == 403  # e-4234: admin による member 削除は不可に厳格化


def test_remove_member_by_owner_still_ok():
    oid = _seed_org("u1")
    _add_member(oid, "bob", "bob@x")
    _impersonate("u1")
    r = client.delete(f"/api/orgs/{oid}/members/bob")
    assert r.status_code == 200, r.text
    assert all(m["user_id"] != "bob" for m in r.json()["members"])
