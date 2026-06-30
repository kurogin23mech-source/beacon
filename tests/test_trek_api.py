"""Server-side tests for trek HTTP API (ms-69 / e-1656).

The trek endpoints live at top-level ``/api/treks`` (not under
``/api/projects/{id}``) because treks are cross-project by nature. These
tests exercise:

  * CRUD: create / get / list / patch / archive
  * lifecycle: start (planning → active), archive terminal-ness
  * members: invite (by email) / join (caller accepts own invitation) /
    leave (caller removes self, leader-blocked, last-member-blocked)
  * scope: add / remove (with duplicate detection)
  * halt: set / clear (Andon cord, any joined member can pull)
  * transfer-leader: dual session + user grain check
  * summary endpoint

Authorization rules pinned (per AC #e-1656):

  * un-authenticated (no bearer token, auth enabled) → 401
  * non-invited caller calling ``POST /members/join`` → 403
  * non-leader caller calling ``DELETE /{id}`` (archive) → 403
  * non-member caller calling ``GET /{id}`` → 403

Storage is mocked in-memory (mirrors ``tests/test_purge_api.py``). We do not
exercise Firestore / DynamoDB integration here — that lives in the dual-
backend trek schema tests + the e-1658 4-project lifecycle smoke.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route operations.apply_operation through the in-memory mock for safety;
# trek endpoints don't go through apply_operation but other endpoints that
# share the process might if pytest imported them earlier.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

# Module-load defense (mirrors tests/test_purge_api.py / tests/test_api.py
# interaction): store_router captures function references at `from
# firestore_client import ...` time, so any later mutation to
# ``firestore_client.get_project = mock`` made by *other* test files is
# silently lost — they hit real Firestore and get PermissionDenied.
#
# By aliasing ``sys.modules["firestore_client"] = app_module.db`` (=
# store_router), we make subsequent ``import firestore_client; firestore_client.X
# = mock`` operations rebind ON store_router. That keeps test_api.py's
# rebinds effective regardless of which test file pytest collects first.
sys.modules["firestore_client"] = app_module.db


# In-memory trek storage + user-by-email index.
_treks: dict[str, dict] = {}
_users_by_email: dict[str, tuple[str, dict]] = {}
# ms-88 / e-2168 — bus events recorded by mock append_bus_event so tests can
# assert on review event mint / suppression.
_bus_events_by_project: dict[str, list[dict]] = {}


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    import copy
    bus = _bus_events_by_project.setdefault(project_id, [])
    event_id = f"ev-{len(bus)}"
    bus.append({"event_id": event_id, **copy.deepcopy(data)})
    return event_id


def _mock_get_trek(trek_id: str):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id: str, data: dict):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    # Mirror firestore_client: store without trek_id field, re-add on read.
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
    out.sort(key=lambda t: (t.get("created_at", ""), t.get("trek_id", "")),
             reverse=True)
    return out


def _mock_delete_trek(trek_id: str) -> bool:
    return _treks.pop(trek_id, None) is not None


def _mock_find_user_by_email(email: str):
    return _users_by_email.get(email)


def _mock_get_or_create_user(user_id: str, email: str):
    # auto-register hook in require_auth — make it a no-op for tests.
    return None


def _mock_get_user(user_id: str):
    for uid, (_, data) in _users_by_email.items():
        if data.get("sub") == user_id or uid == user_id:
            return data
    return None


def _rebind_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("delete_trek", _mock_delete_trek),
        ("find_user_by_email", _mock_find_user_by_email),
        ("get_or_create_user", _mock_get_or_create_user),
        ("get_user", _mock_get_user),
        ("append_bus_event", _mock_append_bus_event),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _bus_events_by_project.clear()
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


# uids referenced in the seed trek's creator / members.
LEADER_UID = "uid-leader"
MEMBER_UID = "uid-member"
INVITED_UID = "uid-invited"  # invited but not yet joined
STRANGER_UID = "uid-stranger"
ADMIN_UID = "uid-admin"

LEADER_EMAIL = "leader@x"
MEMBER_EMAIL = "member@x"
INVITED_EMAIL = "invited@x"
STRANGER_EMAIL = "stranger@x"
ADMIN_EMAIL = "admin@x"


def _impersonate(uid: str, email: str = "") -> None:
    """Override require_auth so subsequent requests use the given identity."""
    actual_email = email or f"{uid}@x"

    def _fake_auth():
        return {"sub": uid, "email": actual_email}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _users_by_email.clear()
    # Seed the user-by-email index so invite/lookup tests can resolve users.
    for uid, email in (
        (LEADER_UID, LEADER_EMAIL),
        (MEMBER_UID, MEMBER_EMAIL),
        (INVITED_UID, INVITED_EMAIL),
        (STRANGER_UID, STRANGER_EMAIL),
        (ADMIN_UID, ADMIN_EMAIL),
    ):
        _users_by_email[email] = (uid, {"sub": uid, "email": email})
    # Admin role on ADMIN_UID for the all_actors=true list path.
    _users_by_email[ADMIN_EMAIL] = (ADMIN_UID,
                                    {"sub": ADMIN_UID, "email": ADMIN_EMAIL,
                                     "role": "admin"})
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        _treks.clear()
        _users_by_email.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior)


def _create_seed_trek(*, status: str = "active") -> str:
    """Create a trek with LEADER as leader + MEMBER joined + INVITED pending.

    Returns the trek_id. Status defaults to ``active`` so members can mutate.
    """
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post("/api/treks", json={
        "title": "Seed trek",
        "description": "for tests",
        "type": "persistent",
        "creator_session_id": "sv-leader",
    })
    assert r.status_code == 200, r.text
    trek_id = r.json()["trek_id"]

    # Invite MEMBER and have them join.
    r = client.post(f"/api/treks/{trek_id}/members",
                    json={"email": MEMBER_EMAIL})
    assert r.status_code == 200, r.text
    _impersonate(MEMBER_UID, MEMBER_EMAIL)
    r = client.post(f"/api/treks/{trek_id}/members/join")
    assert r.status_code == 200, r.text

    # Invite INVITED but don't join.
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post(f"/api/treks/{trek_id}/members",
                    json={"email": INVITED_EMAIL})
    assert r.status_code == 200, r.text

    if status == "active":
        # Caller is leader (LEADER_UID).
        r = client.post(f"/api/treks/{trek_id}/start")
        assert r.status_code == 200, r.text
    return trek_id


# ---------------------------------------------------------------------------
# Auth gate (AC: unauthenticated → 401)
# ---------------------------------------------------------------------------

class TestAuth:
    def test_unauthenticated_list_returns_401(self):
        # No dependency override → real require_auth runs, finds no Bearer.
        app_module.app.dependency_overrides.clear()
        r = client.get("/api/treks")
        assert r.status_code == 401

    def test_unauthenticated_create_returns_401(self):
        app_module.app.dependency_overrides.clear()
        r = client.post("/api/treks", json={
            "title": "x", "creator_session_id": "sv-x",
        })
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# CRUD happy paths
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_minimal(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "Hello", "creator_session_id": "sv-leader",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["title"] == "Hello"
        assert body["status"] == "planning"
        assert body["type"] == "persistent"
        assert body["leader_session_id"] == "sv-leader"
        assert body["creator_actor"]["user_id"] == LEADER_UID
        # Caller is auto-added as leader-role member.
        assert any(m["user_id"] == LEADER_UID and m["role"] == "leader"
                   for m in body["members"])

    def test_create_temporary_type(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "T", "type": "temporary",
            "creator_session_id": "sv-leader",
        })
        assert r.status_code == 200
        assert r.json()["type"] == "temporary"

    def test_create_rejects_blank_title(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "   ", "creator_session_id": "sv-leader",
        })
        assert r.status_code == 400

    def test_create_rejects_missing_session(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "T", "creator_session_id": "",
        })
        assert r.status_code == 400

    def test_create_rejects_invalid_type(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "T", "type": "bogus",
            "creator_session_id": "sv-leader",
        })
        assert r.status_code == 400


class TestGetList:
    def test_get_as_creator(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}")
        assert r.status_code == 200
        assert r.json()["trek_id"] == trek_id

    def test_get_as_joined_member(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}")
        assert r.status_code == 200

    def test_get_as_invited_not_joined(self):
        """Invited (but not joined) still counts as a member entry, so the
        visibility check passes (they need to see the trek to call /join)."""
        trek_id = _create_seed_trek()
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r = client.get(f"/api/treks/{trek_id}")
        assert r.status_code == 200

    def test_get_as_stranger_returns_403(self):
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}")
        assert r.status_code == 403

    def test_get_missing_returns_404(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.get("/api/treks/tk-doesnotexist")
        assert r.status_code == 404

    def test_list_filters_to_caller_treks(self):
        trek_id = _create_seed_trek()
        # Another user creates a private trek — should NOT appear in
        # LEADER's listing.
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "Stranger trek", "creator_session_id": "sv-s",
        })
        assert r.status_code == 200
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.get("/api/treks")
        assert r.status_code == 200
        ids = {t["trek_id"] for t in r.json()}
        assert trek_id in ids
        assert all(t["title"] != "Stranger trek" for t in r.json())

    def test_list_status_filter(self):
        # One active + one planning.
        trek_id = _create_seed_trek(status="planning")
        _impersonate(LEADER_UID, LEADER_EMAIL)
        # Start an additional trek to active.
        r = client.post("/api/treks", json={
            "title": "Other", "creator_session_id": "sv-l",
        })
        other_id = r.json()["trek_id"]
        r = client.post(f"/api/treks/{other_id}/start")
        assert r.status_code == 200
        r = client.get("/api/treks", params={"status": "active"})
        assert r.status_code == 200
        ids = {t["trek_id"] for t in r.json()}
        assert other_id in ids
        assert trek_id not in ids


class TestUpdate:
    def test_leader_can_update(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.patch(f"/api/treks/{trek_id}", json={
            "title": "Renamed", "description": "new desc",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "Renamed"
        assert body["description"] == "new desc"

    def test_member_cannot_update(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(f"/api/treks/{trek_id}", json={"title": "x"})
        assert r.status_code == 403

    def test_stranger_cannot_update(self):
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.patch(f"/api/treks/{trek_id}", json={"title": "x"})
        assert r.status_code == 403  # blocked by _load_trek_for_read


class TestArchive:
    def test_leader_can_archive(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.delete(f"/api/treks/{trek_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "archived"
        assert body["archived_at"]

    def test_non_leader_member_cannot_archive(self):
        """AC: 非 owner の archive は 403."""
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.delete(f"/api/treks/{trek_id}")
        assert r.status_code == 403

    def test_stranger_cannot_archive(self):
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.delete(f"/api/treks/{trek_id}")
        assert r.status_code == 403

    def test_archive_is_terminal(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        client.delete(f"/api/treks/{trek_id}")
        # Cannot start again.
        r = client.post(f"/api/treks/{trek_id}/start")
        assert r.status_code == 400  # ALLOWED_TRANSITIONS rejects archived → *


class TestStart:
    def test_leader_starts_planning_to_active(self):
        trek_id = _create_seed_trek(status="planning")
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/start")
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_member_cannot_start(self):
        trek_id = _create_seed_trek(status="planning")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/start")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

class TestInvite:
    def test_member_can_invite_known_user(self):
        trek_id = _create_seed_trek()
        # MEMBER (already joined) invites STRANGER.
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members",
                        json={"email": STRANGER_EMAIL})
        assert r.status_code == 200
        members = r.json()["members"]
        assert any(m["email"] == STRANGER_EMAIL and not m["joined_at"]
                   for m in members)

    def test_invite_unknown_email_returns_404(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members",
                        json={"email": "ghost@nowhere.com"})
        assert r.status_code == 404

    def test_invite_already_member_returns_409(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members",
                        json={"email": MEMBER_EMAIL})
        assert r.status_code == 409

    def test_invited_not_joined_cannot_invite_others(self):
        """The invited-but-not-joined user is not yet a 'joined member', so
        cannot perform writes including invite. AC #e-1656."""
        trek_id = _create_seed_trek()
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members",
                        json={"email": STRANGER_EMAIL})
        assert r.status_code == 403


class TestJoin:
    def test_invited_can_join(self):
        trek_id = _create_seed_trek()
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members/join")
        assert r.status_code == 200
        member = next(m for m in r.json()["members"]
                      if m["user_id"] == INVITED_UID)
        assert member["joined_at"]

    def test_non_invited_join_returns_403(self):
        """AC: 非 invite actor の join は 403."""
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members/join")
        # Stranger isn't even a member row → _load_trek_for_read denies first.
        assert r.status_code == 403

    def test_join_is_idempotent(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/members/join")
        assert r.status_code == 200


class TestLeave:
    def test_member_can_leave(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.delete(f"/api/treks/{trek_id}/members/me")
        assert r.status_code == 200
        assert all(m["user_id"] != MEMBER_UID for m in r.json()["members"])

    def test_leader_cannot_leave_directly(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.delete(f"/api/treks/{trek_id}/members/me")
        # remove_member helper rejects leader removal → 400.
        assert r.status_code == 400

    def test_last_member_cannot_leave(self):
        # Leader removes MEMBER + INVITED + transfers; then this is brittle —
        # just keep it simple by creating a solo trek.
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post("/api/treks", json={
            "title": "Solo", "creator_session_id": "sv-l",
        })
        trek_id = r.json()["trek_id"]
        # Leader is the only member, and they cannot leave (=archive instead).
        r = client.delete(f"/api/treks/{trek_id}/members/me")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestScope:
    def test_member_can_add_scope(self):
        """ms-97 / e-2626 AC23 — scope-add now stages a pending op.

        Pre-e-2626 the PUT was immediate; AC23 requires the user to
        explicitly approve each addition. The PUT call stages, then
        scope-approve flushes the pending entry into ``scope[]``.
        """
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        body = {"project": "beacon", "milestone": "ms-69"}
        # Stage: PUT returns 200 + ``pending_op`` but scope is unchanged.
        r = client.put(f"/api/treks/{trek_id}/scope", json=body)
        assert r.status_code == 200
        payload = r.json()
        assert body not in payload["scope"], (
            "AC23: scope-add stages a pending op; scope[] must NOT "
            "mutate until approve."
        )
        pending_id = payload["pending_op"]["pending_id"]
        # Approve: flush the pending op into scope[].
        r2 = client.post(
            f"/api/treks/{trek_id}/scope/approve/{pending_id}",
        )
        assert r2.status_code == 200
        assert body in r2.json()["scope"]

    def test_add_scope_project_only_rejected(self):
        """ms-97 / e-2659 (AC7): project-wide adds are now 400.

        Pre-AC7 the endpoint accepted ``{"project": "beacon"}`` and staged
        a pending op for a project-wide row. AC7 closes that hole — the
        narrowing key (= milestone / operation / task) is mandatory on
        the server validation layer and the response carries a clear
        ``narrowing key`` hint.
        """
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        body = {"project": "beacon"}
        r = client.put(f"/api/treks/{trek_id}/scope", json=body)
        assert r.status_code == 400, r.text
        assert "narrowing key" in r.text

    def test_add_duplicate_scope_returns_409(self):
        """Duplicate-add still 409s, but now at stage-time (e-2626).

        First add: stage + approve (= scope[] now contains the entry).
        Second add: stage fails with 409 because the entry is already
        present. Without the symmetric check in
        ``add_pending_scope_op``, the duplicate would silently pile up
        as a second pending record and 409 only at approve-time.
        """
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        body = {"project": "beacon", "milestone": "ms-69"}
        r1 = client.put(f"/api/treks/{trek_id}/scope", json=body)
        assert r1.status_code == 200
        pid = r1.json()["pending_op"]["pending_id"]
        client.post(f"/api/treks/{trek_id}/scope/approve/{pid}")
        r2 = client.put(f"/api/treks/{trek_id}/scope", json=body)
        assert r2.status_code == 409

    def test_remove_scope(self):
        """ms-97 / e-2611 AC25 — scope-remove now stages a pending op.

        Pre-e-2611 the DELETE was immediate; AC25 requires the user to
        explicitly approve each removal. The DELETE call stages, then
        scope-approve flushes the pending entry into ``scope[]``.

        ms-97 / e-2626 — scope-add also stages now; the setup steps
        below chain through approve to actually grow ``scope[]`` before
        the remove-side flow under test.
        """
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        body = {"project": "beacon", "milestone": "ms-69"}
        # Setup: stage + approve add so scope[] has the entry to remove.
        r0 = client.put(f"/api/treks/{trek_id}/scope", json=body)
        pid0 = r0.json()["pending_op"]["pending_id"]
        client.post(f"/api/treks/{trek_id}/scope/approve/{pid0}")
        # Stage remove: DELETE returns 200 + ``pending_op`` but scope is
        # unchanged.
        r = client.request("DELETE", f"/api/treks/{trek_id}/scope", json=body)
        assert r.status_code == 200
        payload = r.json()
        assert body in payload["scope"], (
            "AC25: scope-remove stages a pending op; scope[] must NOT "
            "mutate until approve."
        )
        pending_id = payload["pending_op"]["pending_id"]
        # Approve: flush the pending op into scope[].
        r2 = client.post(
            f"/api/treks/{trek_id}/scope/approve/{pending_id}",
        )
        assert r2.status_code == 200
        assert body not in r2.json()["scope"]

    def test_remove_nonexistent_scope_returns_404(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.request("DELETE", f"/api/treks/{trek_id}/scope", json={
            "project": "not-in-scope",
        })
        assert r.status_code == 404

    def test_invited_not_joined_cannot_edit_scope(self):
        # ms-97 / e-2659 (AC7): use a narrowing-keyed body so the test
        # actually exercises the auth gate instead of the AC7 strict
        # validation (= which would 400 before auth ever fires).
        trek_id = _create_seed_trek()
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r = client.put(f"/api/treks/{trek_id}/scope",
                       json={"project": "beacon", "milestone": "ms-1"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Halt
# ---------------------------------------------------------------------------

class TestHalt:
    def test_member_can_set_halt(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.put(f"/api/treks/{trek_id}/halt", json={
            "issued_by_session_id": "sv-member",
            "reason": "found a bug",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["halt"]["reason"] == "found a bug"
        assert body["status"] == "active"  # halt is metadata, not a status

    def test_halt_on_planning_trek_returns_400(self):
        trek_id = _create_seed_trek(status="planning")
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.put(f"/api/treks/{trek_id}/halt", json={
            "issued_by_session_id": "sv-l",
        })
        assert r.status_code == 400  # can only halt active treks

    def test_clear_halt(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        client.put(f"/api/treks/{trek_id}/halt", json={
            "issued_by_session_id": "sv-m",
        })
        r = client.delete(f"/api/treks/{trek_id}/halt")
        assert r.status_code == 200
        assert r.json()["halt"] is None

    def test_clear_halt_idempotent_when_not_halted(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.delete(f"/api/treks/{trek_id}/halt")
        assert r.status_code == 200

    def test_stranger_cannot_halt(self):
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.put(f"/api/treks/{trek_id}/halt", json={
            "issued_by_session_id": "sv-s",
        })
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Transfer leader
# ---------------------------------------------------------------------------

class TestTransferLeader:
    def test_leader_can_transfer_with_matching_session(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/transfer-leader", json={
            "from_session_id": "sv-leader",
            "to_session_id": "sv-leader-other-terminal",
        })
        assert r.status_code == 200
        assert r.json()["leader_session_id"] == "sv-leader-other-terminal"

    def test_wrong_from_session_returns_403(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/transfer-leader", json={
            "from_session_id": "sv-not-leader",
            "to_session_id": "sv-elsewhere",
        })
        assert r.status_code == 403

    def test_non_leader_cannot_transfer(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(f"/api/treks/{trek_id}/transfer-leader", json={
            "from_session_id": "sv-leader",
            "to_session_id": "sv-member",
        })
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_returns_counts(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["trek_id"] == trek_id
        assert body["status"] == "active"
        assert body["halted"] is False
        # Leader + Member joined; INVITED is invited but not joined.
        assert body["member_count"] == 3
        assert body["joined_member_count"] == 2
        assert body["scope_count"] == 0

    def test_summary_reflects_halt(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        client.put(f"/api/treks/{trek_id}/halt", json={
            "issued_by_session_id": "sv-l",
            "reason": "pause",
        })
        r = client.get(f"/api/treks/{trek_id}/summary")
        assert r.status_code == 200
        body = r.json()
        assert body["halted"] is True
        assert body["halt"]["reason"] == "pause"

    def test_summary_stranger_returns_403(self):
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}/summary")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# ms-88 / e-2168 — leader 自己循環 suppress
# ---------------------------------------------------------------------------

class TestLeaderSelfLoopSuppress:
    """leader 自身が stamp した task-state transition で trek-task-review event を
    leader 宛に mint しない (= 「自分が判断したものを自分に review 依頼」 ループの
    構造解消、 2026-06-20 議論)。"""

    def test_leader_self_stamp_suppresses_review_event(self):
        # Active trek with leader_session_id = sv-leader (= seed default).
        trek_id = _create_seed_trek()
        _treks[trek_id]["scope"] = [
            {"project": "beacon-test", "milestone": "ms-88"}
        ]
        # Pre-populate task_states[e-x] in working state owned by leader sid
        _treks[trek_id]["task_states"] = {
            "e-x": {
                "state": "working",
                "updated_by_session_id": "sv-leader",
                "updated_at": "2026-06-19T00:00:00.000000Z",
                "last_activity_at": "2026-06-19T00:00:00.000000Z",
                "note": "",
            }
        }
        # Clear any prior bus events
        for k in list(_bus_events_by_project.keys()):
            _bus_events_by_project[k].clear()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        # Caller session header == leader_session_id
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-x", "state": "done", "note": "leader done"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 200, r.text
        # No trek-task-review event should be appended
        bus = _bus_events_by_project.get("beacon-test", [])
        review_events = [
            e for e in bus if e.get("channel") == "trek-task-review"
        ]
        assert review_events == [], (
            f"Expected no trek-task-review event for self-judgment, "
            f"got {review_events}"
        )
        # Suppression recorded in meta
        t = _treks[trek_id]
        suppressions = (t.get("meta") or {}).get("review_suppressions") or []
        assert len(suppressions) == 1
        assert suppressions[0]["suppression_reason"] == "self_judgment"
        assert suppressions[0]["task_id"] == "e-x"
        assert suppressions[0]["state"] == "done"
        # State transition preserved
        assert (t.get("task_states") or {}).get("e-x", {}).get("state") == "done"

    def test_non_leader_member_stamp_emits_review_event(self):
        """control: non-leader member が stamp → review event 発火 (= 既存挙動)。"""
        trek_id = _create_seed_trek()
        _treks[trek_id]["scope"] = [
            {"project": "beacon-test", "milestone": "ms-88"}
        ]
        _treks[trek_id]["task_states"] = {
            "e-y": {
                "state": "working",
                "updated_by_session_id": "sv-member",
                "updated_at": "2026-06-19T00:00:00.000000Z",
                "last_activity_at": "2026-06-19T00:00:00.000000Z",
                "note": "",
            }
        }
        for k in list(_bus_events_by_project.keys()):
            _bus_events_by_project[k].clear()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-y", "state": "done", "note": "member done"},
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 200, r.text
        bus = _bus_events_by_project.get("beacon-test", [])
        review_events = [
            e for e in bus if e.get("channel") == "trek-task-review"
        ]
        assert len(review_events) == 1
        # No suppression for non-leader caller
        t = _treks[trek_id]
        suppressions = (t.get("meta") or {}).get("review_suppressions") or []
        assert suppressions == []


# ---------------------------------------------------------------------------
# ms-97 / e-2706 — leader_review 遷移時の trek-task-review event 発火 (AC1)
# ---------------------------------------------------------------------------

class TestReviewTriggerStatesEmit:
    """ms-97 / e-2706 — REVIEW_TRIGGER_STATES (= done / user_review / leader_review)
    の **どれに遷移しても** trek-task-review event を発火する regression pin。

    旧コードは TERMINAL_TASK_STATES (= done / user_review) のみで判定しており、
    `leader_review` (= ms-88 e-2107 の 5-state 移行で新設) への遷移時に
    leader へ notify event が発火しない構造 bug を持っていた。 dogfood
    (2026-06-28) で LPS exec が e-373 を `working → leader_review` に stamp
    したが leader に届かず、 10 min+ の遷移見落としを生んだ。
    """

    def _seed_with_member_working(self, task_id: str) -> str:
        trek_id = _create_seed_trek()
        _treks[trek_id]["scope"] = [
            {"project": "beacon-test", "milestone": "ms-97"}
        ]
        _treks[trek_id]["task_states"] = {
            task_id: {
                "state": "working",
                "updated_by_session_id": "sv-member",
                "updated_at": "2026-06-28T00:00:00.000000Z",
                "last_activity_at": "2026-06-28T00:00:00.000000Z",
                "note": "",
            }
        }
        for k in list(_bus_events_by_project.keys()):
            _bus_events_by_project[k].clear()
        return trek_id

    def test_member_stamp_leader_review_emits_review_event(self):
        """e-2706 の核心: working → leader_review 遷移で event 発火。

        旧コードでは event が発火せず、 bus は空のままだった。 本テストが
        regression pin として残る限り、 leader_review notify drift は再発しない。
        """
        trek_id = self._seed_with_member_working("e-2706-target")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={
                "task_id": "e-2706-target",
                "state": "leader_review",
                "note": "leader 判断要請",
            },
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 200, r.text
        bus = _bus_events_by_project.get("beacon-test", [])
        review_events = [
            e for e in bus if e.get("channel") == "trek-task-review"
        ]
        assert len(review_events) == 1, (
            f"Expected leader_review transition to emit trek-task-review event, "
            f"got {review_events}"
        )
        payload = review_events[0].get("payload") or {}
        assert payload.get("state") == "leader_review"
        assert payload.get("task_id") == "e-2706-target"
        assert payload.get("recipient_session_id") == "sv-leader"
        # delivery=auto-execute (= Level 3 invoke 経路) を維持
        assert review_events[0].get("delivery") == "auto-execute"
        # NOTE: outcome log row (= AC26 audit trail) は Firestore 側に書かれる
        # 経路で、 本 in-memory mock store では検証しない (= 既存
        # TestLeaderSelfLoopSuppress も同様に skip)。 critical assertion は
        # bus event の発火と payload 整合性。

    def test_member_stamp_done_still_emits_review_event(self):
        """control 1: done への遷移は依然 event 発火 (= 既存挙動 regression なし)。"""
        trek_id = self._seed_with_member_working("e-done-control")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-done-control", "state": "done", "note": "done"},
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 200, r.text
        bus = _bus_events_by_project.get("beacon-test", [])
        review_events = [
            e for e in bus if e.get("channel") == "trek-task-review"
        ]
        assert len(review_events) == 1
        assert (review_events[0].get("payload") or {}).get("state") == "done"

    def test_member_stamp_user_review_still_emits_review_event(self):
        """control 2: user_review への遷移も依然 event 発火 (= 既存挙動)。"""
        trek_id = self._seed_with_member_working("e-user-rev-control")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={
                "task_id": "e-user-rev-control",
                "state": "user_review",
                "note": "forward to user",
            },
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 200, r.text
        bus = _bus_events_by_project.get("beacon-test", [])
        review_events = [
            e for e in bus if e.get("channel") == "trek-task-review"
        ]
        assert len(review_events) == 1
        assert (
            (review_events[0].get("payload") or {}).get("state") == "user_review"
        )

    def test_leader_self_stamp_leader_review_still_suppresses(self):
        """ms-88 e-2168 の self-loop suppress は leader_review にも効く (= 二重防御)。

        e-2706 で trigger 集合を拡張しても、 leader 自身が leader_review を
        stamp した時に「自分に review 依頼」 する循環は依然 mint しないこと。
        """
        trek_id = self._seed_with_member_working("e-leader-self")
        # task_states の updater を leader に書き換え (= 既存 stamp も leader)
        _treks[trek_id]["task_states"]["e-leader-self"]["updated_by_session_id"] = "sv-leader"
        for k in list(_bus_events_by_project.keys()):
            _bus_events_by_project[k].clear()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={
                "task_id": "e-leader-self",
                "state": "leader_review",
                "note": "leader が自分で leader_review に置く稀ケース",
            },
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 200, r.text
        bus = _bus_events_by_project.get("beacon-test", [])
        review_events = [
            e for e in bus if e.get("channel") == "trek-task-review"
        ]
        assert review_events == [], (
            f"leader self-stamp should suppress review event even at "
            f"leader_review, got {review_events}"
        )
        # Suppression recorded
        t = _treks[trek_id]
        suppressions = (t.get("meta") or {}).get("review_suppressions") or []
        assert len(suppressions) == 1
        assert suppressions[0]["state"] == "leader_review"
        assert suppressions[0]["suppression_reason"] == "self_judgment"


# ---------------------------------------------------------------------------
# ms-88 / e-2167 — Trek task_states ↔ task pool reconcile endpoint
# ---------------------------------------------------------------------------

class TestTrekReconcileEndpoint:
    """task pool で done だが Trek stamp が non-terminal で stuck の状態を
    一括検知 + 修復する reconcile endpoint。 default dry-run、 apply=true で
    mirror 適用。"""

    def _seed_active_trek_with_scope(self) -> str:
        trek_id = _create_seed_trek()
        _treks[trek_id]["scope"] = [
            {"project": "beacon-test", "milestone": "ms-88"}
        ]
        return trek_id

    def test_reconcile_dry_run_returns_diff_no_changes(self):
        trek_id = self._seed_active_trek_with_scope()
        _treks[trek_id]["task_states"] = {
            "e-stuck": {
                "state": "leader_review",
                "updated_by_session_id": "sv-x",
                "updated_at": "2026-06-19T00:00:00.000000Z",
                "last_activity_at": "2026-06-19T00:00:00.000000Z",
                "note": "stuck",
            }
        }
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-88", "entries": [
                            {"id": "e-stuck", "type": "task", "status": "done"}
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(LEADER_UID, LEADER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.post(
                f"/api/treks/{trek_id}/reconcile",
                json={"apply": False},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["trek_id"] == trek_id
        assert body["applied"] is False
        assert len(body["diff"]) == 1
        assert body["diff"][0]["entry_id"] == "e-stuck"
        assert body["diff"][0]["trek_state"] == "leader_review"
        assert body["diff"][0]["would_change_to"] == "done"
        # Unchanged
        assert (
            _treks[trek_id]["task_states"]["e-stuck"]["state"]
            == "leader_review"
        )

    def test_reconcile_apply_mirrors_to_done(self):
        trek_id = self._seed_active_trek_with_scope()
        _treks[trek_id]["task_states"] = {
            "e-stuck": {
                "state": "waiting-review",  # legacy token → leader_review
                "updated_by_session_id": "sv-x",
                "updated_at": "2026-06-19T00:00:00.000000Z",
                "last_activity_at": "2026-06-19T00:00:00.000000Z",
                "note": "legacy stuck",
            }
        }
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-88", "entries": [
                            {"id": "e-stuck", "type": "task", "status": "done"}
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(LEADER_UID, LEADER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.post(
                f"/api/treks/{trek_id}/reconcile",
                json={"apply": True},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is True
        assert body["applied_entry_ids"] == ["e-stuck"]
        new_state = _treks[trek_id]["task_states"]["e-stuck"]
        assert new_state["state"] == "done"
        assert new_state["updated_by_session_id"] == "task-pool-mirror"
        assert "mirror 同期" in new_state["note"]

    def test_reconcile_no_diff_when_aligned(self):
        trek_id = self._seed_active_trek_with_scope()
        _treks[trek_id]["task_states"] = {
            "e-aligned": {
                "state": "done",
                "updated_by_session_id": "sv-x",
                "updated_at": "2026-06-19T00:00:00.000000Z",
                "last_activity_at": "2026-06-19T00:00:00.000000Z",
                "note": "",
            }
        }
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-88", "entries": [
                            {"id": "e-aligned", "type": "task", "status": "done"}
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(LEADER_UID, LEADER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.post(
                f"/api/treks/{trek_id}/reconcile",
                json={"apply": False},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["diff"] == []

    def test_reconcile_non_member_returns_403(self):
        trek_id = self._seed_active_trek_with_scope()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/reconcile",
            json={"apply": False},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# ms-97 Phase 4 / AC13 — leader hard-check (session_id grain)
# ---------------------------------------------------------------------------

class TestLeaderSessionHardCheck:
    """AC13: leader-only endpoints require BOTH role and session_id grain.

    pre-Phase-4 the role check passed for any session of the leader
    user; that left "second terminal of the leader user" able to
    mutate trek-level state without being the live leader session.
    Phase 4 closes the gap by hard-checking
    ``X-Beacon-Session == trek.leader_session_id`` on phase A+ trek.

    Pre-A invariance: the check degrades to role-only.
    """

    def _seed_phase_a_trek(self) -> str:
        """Trek with migration_phase=A, leader_session_id=sv-leader, member sv-member."""
        trek_id = _create_seed_trek()
        t = _treks[trek_id]
        # Stamp phase A so is_session_id_keyed returns True.
        meta = t.setdefault("meta", {})
        meta["migration_phase"] = "A"
        # Attach session_id to existing members[] entries so the
        # session-grain role check resolves correctly.
        for m in t.get("members") or []:
            if m.get("user_id") == LEADER_UID:
                m["session_id"] = "sv-leader"
            elif m.get("user_id") == MEMBER_UID:
                m["session_id"] = "sv-member"
        return trek_id

    def test_leader_session_can_update(self):
        """AC13 positive path — leader's stamped session can patch."""
        trek_id = self._seed_phase_a_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}", json={"title": "Renamed"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 200, r.text

    def test_other_session_of_leader_user_blocked_on_update(self):
        """AC13 hard-check: leader user but wrong session_id → 403."""
        trek_id = self._seed_phase_a_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}", json={"title": "x"},
            headers={"X-Beacon-Session": "sv-leader-second-terminal"},
        )
        # Role check passes (= leader user), session check rejects.
        # NOTE: the session-grain role check also resolves to "" for
        # an unknown session_id under phase A+, so this can come back
        # as "Trek leader role required" rather than the AC13 detail.
        # Either way it's a 403.
        assert r.status_code == 403

    def test_leader_session_can_archive(self):
        """Archive endpoint also gated by AC13 hard-check."""
        trek_id = self._seed_phase_a_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.delete(
            f"/api/treks/{trek_id}",
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 200, r.text

    def test_pre_a_trek_invariance_no_session_header(self):
        """Pre-A trek + no session header → degrades to role-only check (legacy)."""
        # Default seed is pre-A (no migration_phase stamp).
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        # No X-Beacon-Session header → role-only check applies.
        r = client.patch(f"/api/treks/{trek_id}", json={"title": "Renamed"})
        assert r.status_code == 200, r.text
