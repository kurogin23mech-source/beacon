"""Org REST endpoint tests (ms-118 / e-4231).

Pins the contract of the three endpoints that let the client `beacon org`
CLI reach the ms-113 org store (server side):

  * POST /api/orgs           — create a team org, caller becomes owner
  * GET  /api/orgs           — list orgs the caller is a member of
  * GET  /api/orgs/{org_id}  — show one org; non-member gets 404 (存在を漏らさない)

Mirrors the mock-store + dependency-override pattern from
test_trek_scope_aggregate_endpoints.py so the surface stays consistent.
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


def _mock_get_org(org_id):
    data = _orgs.get(org_id)
    return copy.deepcopy(data) if data else None


def _mock_save_org(org_id, data):
    payload = {k: v for k, v in data.items() if k != "org_id"}
    _orgs[org_id] = {**copy.deepcopy(payload), "org_id": org_id}


def _mock_list_orgs_for_user(user_id=None):
    out = []
    for o in _orgs.values():
        if user_id:
            members = [m.get("user_id") for m in o.get("members") or []]
            if user_id not in members:
                continue
        out.append(copy.deepcopy(o))
    return out


@pytest.fixture(autouse=True)
def _bind_mock_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_org", _mock_get_org),
        ("save_org", _mock_save_org),
        ("list_orgs_for_user", _mock_list_orgs_for_user),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    # Pin auth ON so the membership filters (list) and member-only 404 (show)
    # are actually exercised — other test modules flip _auth_enabled=False and
    # may not restore it, which would silently disable those gates here.
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    _orgs.clear()
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


def test_create_org_makes_caller_owner():
    _impersonate("u1", "u1@x")
    r = client.post("/api/orgs", json={"name": "Acme 法人"})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["name"] == "Acme 法人"
    assert doc["personal"] is False
    assert doc["org_id"].startswith("org-t-")
    assert doc["members"] == [
        {"user_id": "u1", "email": "u1@x", "role": "owner",
         "added_at": doc["members"][0]["added_at"], "added_by": "u1"}
    ]


def test_create_org_rejects_empty_name():
    _impersonate("u1")
    assert client.post("/api/orgs", json={"name": "   "}).status_code == 400


def test_list_orgs_filters_by_membership():
    _impersonate("u1")
    client.post("/api/orgs", json={"name": "A"})
    _impersonate("u2")
    client.post("/api/orgs", json={"name": "B"})

    _impersonate("u1")
    names = {o["name"] for o in client.get("/api/orgs").json()}
    assert names == {"A"}  # u1 only sees the org it owns


def test_get_org_member_ok_nonmember_404():
    _impersonate("u1")
    org_id = client.post("/api/orgs", json={"name": "Acme"}).json()["org_id"]

    # owner (member) can read it
    _impersonate("u1")
    assert client.get(f"/api/orgs/{org_id}").status_code == 200

    # non-member gets 404 (= 存在を漏らさない), not 403
    _impersonate("stranger")
    r = client.get(f"/api/orgs/{org_id}")
    assert r.status_code == 404

    # unknown id is also 404
    _impersonate("u1")
    assert client.get("/api/orgs/org-t-nope").status_code == 404
