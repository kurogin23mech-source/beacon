"""Integration tests for the token-based invitation API (ms-78 e-1803/e-1804).

Covers:
  * POST /api/projects/{pid}/invitations          (owner issues token)
  * GET  /api/projects/{pid}/invitations          (list pending)
  * DELETE /api/projects/{pid}/invitations/{id}   (cancel)
  * GET  /api/invitations/{token}                 (public preview)
  * POST /api/invitations/{token}/accept          (consume + add member)
  * GET  /join/{token}                            (static landing page route)
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route apply_operation through the in-memory mock so we don't need Firestore.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client

_store: dict[str, dict] = {}
_users: dict[str, dict] = {}


def mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_list_projects(*args, **kwargs):
    return [
        {"project_id": pid, "name": data.get("name", ""), "objective": data.get("objective", "")}
        for pid, data in _store.items()
    ]


def mock_list_all_projects():
    return [
        {"project_id": pid, "name": data.get("name", ""), "owner": data.get("owner", "")}
        for pid, data in _store.items()
    ]


def mock_get_user(user_id: str):
    return copy.deepcopy(_users.get(user_id)) if user_id in _users else None


def mock_update_user(user_id: str, updates: dict) -> bool:
    if user_id not in _users:
        _users[user_id] = {"user_id": user_id}
    _users[user_id].update(updates)
    return True


# We deliberately do NOT module-level monkeypatch firestore_client /
# store_router here — that would leak into adjacent test modules (= test_api.py)
# whose collection order interleaves with ours and whose own fixtures only
# restore firestore_client. All monkeypatching happens inside the
# `reset_state` fixture and is fully reverted on yield.

from fastapi.testclient import TestClient
import app as app_module
import store_router  # noqa: E402

# After the SHARED region of server/app.py was refactored to embed the
# project_id in the token (e-1804 follow-up), invitation endpoints no longer
# need to know about every project in the system. We only need to mock the
# direct project / user reads on store_router so that `db.get_project(pid)`,
# `db.get_user(uid)`, and `db.update_user(uid, ...)` flow through the
# in-memory dictionaries.


# Disable auth (= mirrors test_api.py's regime) so we don't depend on JWT
# plumbing. The owner / non-owner positive / negative paths inside the
# invitation endpoints are covered by lib/invitations.py unit tests
# (= tests/test_invitations.py); here we focus on integration shape:
# data round-trips, status codes, replay protection, and the static /join
# landing route.
app_module._auth_enabled = False


# `_load` (= app.py) goes through `operations.load_project_consistent`, which
# tries to hit real Firestore subcollections. Patch it to use our in-memory
# store so list_invitations / accept can read project state without network.
def _mock_load(project_id: str, user=None):
    data = mock_get_project(project_id)
    if data is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="not found")
    return data


_PRIOR_LOAD = app_module._load


def setup_module(_module):
    app_module._load = _mock_load


def teardown_module(_module):
    app_module._load = _PRIOR_LOAD


client = TestClient(app_module.app)

PROJECT_ID = "ms78-test-proj"


def _seed():
    _store.clear()
    _users.clear()
    # In auth-disabled mode require_auth returns sub="dev", email="dev@local".
    # Make `dev` the owner so the implicit owner-check in invitation endpoints
    # works the same way it would under real auth.
    _users["dev"] = {"user_id": "dev", "email": "dev@local"}
    _users["alice-uid"] = {"user_id": "alice-uid", "email": "alice@example.com"}
    _store[PROJECT_ID] = {
        "name": "Test Project",
        "owner": "dev",
        "members": [],
        "milestones": [],
        "schema_version": 2,
    }


@pytest.fixture(autouse=True)
def reset_state():
    # Snapshot the originals so we can restore them after each test, keeping
    # adjacent test modules (= test_api.py) blissfully unaware that we ever
    # touched the storage layer.
    targets = ["get_project", "save_project", "get_user", "update_user"]
    _orig = {}
    for m_name, m in (("fc", firestore_client), ("sr", store_router)):
        for attr in targets:
            _orig[f"{m_name}_{attr}"] = getattr(m, attr, None)
    overrides = {
        "get_project": mock_get_project,
        "save_project": mock_save_project,
        "get_user": mock_get_user,
        "update_user": mock_update_user,
    }
    for attr, fn in overrides.items():
        setattr(firestore_client, attr, fn)
        setattr(store_router, attr, fn)
    _seed()
    yield
    _store.clear()
    _users.clear()
    for k, v in _orig.items():
        if v is None:
            continue
        m = firestore_client if k.startswith("fc_") else store_router
        setattr(m, k[3:], v)


# ---------------------------------------------------------------------------
# Create / list / cancel
# ---------------------------------------------------------------------------

def test_owner_creates_invitation_returns_token_and_url():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "alice@example.com", "role": "editor"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]  # plaintext returned ONCE
    assert body["url"].endswith(f"/join/{body['token']}")
    assert body["invitation"]["email"] == "alice@example.com"
    assert body["invitation"]["role"] == "editor"
    # token plaintext is NOT in the stored invitation
    stored = _store[PROJECT_ID]["invitations"]
    assert len(stored) == 1
    assert "token" not in stored[0]
    assert stored[0]["token_hash"]  # only hash persisted


def test_invalid_role_rejected():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "alice@example.com", "role": "superadmin"},
    )
    assert r.status_code == 400


def test_list_pending_invitations():
    client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "a@example.com", "role": "viewer"},
    )
    client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "b@example.com", "role": "editor"},
    )
    r = client.get(f"/api/projects/{PROJECT_ID}/invitations")
    assert r.status_code == 200
    items = r.json()["invitations"]
    assert len(items) == 2
    # Public view strips token_hash
    for it in items:
        assert "token_hash" not in it


def test_cancel_invitation():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "a@example.com", "role": "viewer"},
    )
    inv_id = r.json()["invitation"]["id"]
    r2 = client.delete(f"/api/projects/{PROJECT_ID}/invitations/{inv_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"
    assert _store[PROJECT_ID]["invitations"] == []


def test_cancel_unknown_invitation_404():
    r = client.delete(f"/api/projects/{PROJECT_ID}/invitations/bogus-id")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Public preview
# ---------------------------------------------------------------------------

def test_preview_invitation_returns_project_metadata():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "alice@example.com", "role": "editor"},
    )
    token = r.json()["token"]
    # Preview endpoint should work even before sign-in. The endpoint does
    # not consult `user`, so the auth-disabled dev identity is fine.
    r2 = client.get(f"/api/invitations/{token}")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["project_id"] == PROJECT_ID
    assert body["project_name"] == "Test Project"
    assert body["role"] == "editor"
    assert body["invited_email"] == "alice@example.com"
    # In auth-disabled mode the inviter is `dev@local` (= require_auth's
    # canned identity), so that's what gets stored as invited_by_email.
    assert body["inviter_email"] == "dev@local"


def test_preview_unknown_token_404():
    r = client.get("/api/invitations/totally-bogus-token")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------

def test_accept_consumes_invitation_and_persists_display_name():
    # Invite the dev identity itself so the email-match guard passes in
    # auth-disabled mode (= dev@local). The owner-self edge case is exercised
    # here on purpose — it confirms accept is a no-op for membership when the
    # caller is the owner but still consumes the invite and stores display_name.
    r = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "dev@local", "role": "viewer"},
    )
    # invitation_create rejects "email already a member" but the owner sits
    # in the `owner` field, not members[]; the create path treats them as
    # separate, so this should succeed.
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r2 = client.post(
        f"/api/invitations/{token}/accept",
        json={"display_name": "Dev"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "accepted"
    assert body["project_id"] == PROJECT_ID
    # Invitation consumed (= replay impossible)
    assert _store[PROJECT_ID]["invitations"] == []
    # display_name persisted (= UC11-F5 / e-1807)
    assert _users["dev"].get("display_name") == "Dev"


def test_accept_twice_rejected_replay_protection():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/invitations",
        json={"email": "dev@local", "role": "viewer"},
    )
    token = r.json()["token"]
    r2 = client.post(f"/api/invitations/{token}/accept", json={"display_name": "D"})
    assert r2.status_code == 200
    r3 = client.post(f"/api/invitations/{token}/accept", json={"display_name": "D"})
    assert r3.status_code == 404


def test_accept_unknown_token_404():
    r = client.post(
        "/api/invitations/bogus-token/accept",
        json={"display_name": "X"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /join/<token> landing route
# ---------------------------------------------------------------------------

def test_join_landing_serves_html_for_any_token():
    # The landing page is static — it just needs to be reachable. The token
    # is parsed client-side, so even a bogus token resolves to the HTML.
    r = client.get("/join/anything-here")
    assert r.status_code == 200
    assert b"Beacon" in r.content
    assert b"<title>" in r.content


# ---------------------------------------------------------------------------
# display_name enrichment in list_members (ms-78 e-1807)
# ---------------------------------------------------------------------------

def test_list_members_includes_display_name_from_user_record():
    """`GET /api/projects/{pid}/members` should join the user record so
    callers see display_name, not just the raw email."""
    # Seed an extra member + a display_name on the user record
    _users["alice-uid"] = {
        "user_id": "alice-uid",
        "email": "alice@example.com",
        "display_name": "Alice A.",
    }
    _store[PROJECT_ID]["members"] = [
        {"user_id": "alice-uid", "email": "alice@example.com", "role": "editor"}
    ]
    r = client.get(f"/api/projects/{PROJECT_ID}/members")
    assert r.status_code == 200, r.text
    body = r.json()
    members = body["members"]
    assert any(
        m.get("display_name") == "Alice A." and m.get("email") == "alice@example.com"
        for m in members
    ), members


def test_list_members_returns_empty_display_name_when_user_has_none():
    _users["alice-uid"] = {"user_id": "alice-uid", "email": "alice@example.com"}
    _store[PROJECT_ID]["members"] = [
        {"user_id": "alice-uid", "email": "alice@example.com", "role": "viewer"}
    ]
    r = client.get(f"/api/projects/{PROJECT_ID}/members")
    assert r.status_code == 200
    members = r.json()["members"]
    for m in members:
        if m.get("email") == "alice@example.com":
            # display_name is either absent or empty — both are acceptable
            assert not (m.get("display_name") or "")
            break
    else:
        pytest.fail("alice not found in members list")
