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
# ms-97 / e-2650 — in-memory project pool used by mock get_project so tests
# can pre-populate task pool entries that the slot-done precondition
# verifies. Shape: { project_id: project_data_dict }. Each project_data
# follows the standard ``{"milestones": [...], "operations": [...]}`` shape
# that ``core.find_entry`` walks. Empty by default → mock returns None for
# every pid, which trips the precondition guard intentionally.
_project_pool: dict[str, dict] = {}


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


def _mock_get_project(project_id: str):
    """ms-97 / e-2650 — return ``_project_pool[project_id]`` or None.

    The slot-done precondition (= ``check_slot_done_precondition``) calls
    this via ``db.get_project`` to read the project pool task status.
    Tests that don't seed ``_project_pool`` get None back, which makes
    the precondition reject the done transition — the correct default
    for "the test setup didn't bother to model project pool truth".
    Tests that need done transitions to succeed populate
    ``_seed_pool_task(pid, ms_id, entry_id, status='done')``.
    """
    data = _project_pool.get(project_id)
    return copy.deepcopy(data) if data else None


def _seed_pool_task(project_id: str, ms_id: str, entry_id: str,
                    status: str = "done", entry_type: str = "task") -> None:
    """Pre-populate ``_project_pool`` so the slot-done precondition allows.

    Convenience helper for existing tests that flip Trek slots to done
    without otherwise caring about project pool state — they call this
    once to satisfy the e-2650 precondition.
    """
    proj = _project_pool.setdefault(
        project_id, {"milestones": [], "operations": []}
    )
    ms = None
    for existing in proj["milestones"]:
        if existing.get("id") == ms_id:
            ms = existing
            break
    if ms is None:
        ms = {"id": ms_id, "entries": []}
        proj["milestones"].append(ms)
    for child in ms["entries"]:
        if child.get("id") == entry_id:
            child["status"] = status
            child["type"] = entry_type
            return
    ms["entries"].append({"id": entry_id, "type": entry_type,
                          "status": status})


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
        ("get_project", _mock_get_project),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _bus_events_by_project.clear()
    _project_pool.clear()
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
        _project_pool.clear()
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

    def test_same_user_two_sessions_yield_two_members(self):
        """ms-97 / e-2636 (AC6, Done when #1+#4) — 同 user 2 session の連続
        join で members[] が 2 件になる。 旧 silent no-op を構造的に塞ぐ。

        dogfood で観測した 「PE session が join exit 0 / success message を
        出したのに server side members[] には反映されない」 病理の構造修正。
        join endpoint が ``X-Beacon-Session`` header で受け取った
        session_id 引数を accept_invitation に伝える結果、 pre-A trek は
        自動 phase A flip → session-grain expand に乗る。
        """
        trek_id = _create_seed_trek()
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r1 = client.post(
            f"/api/treks/{trek_id}/members/join",
            headers={"X-Beacon-Session": "sv-invited-a"},
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            f"/api/treks/{trek_id}/members/join",
            headers={"X-Beacon-Session": "sv-invited-b"},
        )
        assert r2.status_code == 200, r2.text
        members = r2.json()["members"]
        invited_entries = [
            m for m in members if m["user_id"] == INVITED_UID
        ]
        assert len(invited_entries) == 2, (
            f"expected 2 session-grain entries for INVITED_UID, got "
            f"{invited_entries}"
        )
        assert {m["session_id"] for m in invited_entries} == {
            "sv-invited-a", "sv-invited-b",
        }
        # Done when #2: exit 0 + success message は実 server state と一致。
        # ここでは response の members[] と GET /api/treks/{id} の members[]
        # が同期していることで確認 (= silent no-op 回避の構造証拠)。
        r3 = client.get(f"/api/treks/{trek_id}")
        assert r3.status_code == 200
        get_members = [
            m for m in r3.json()["members"] if m["user_id"] == INVITED_UID
        ]
        assert {m["session_id"] for m in get_members} == {
            "sv-invited-a", "sv-invited-b",
        }

    def _seed_scope_for_welcome_test(self, trek_id: str) -> None:
        """Add a milestone-narrow scope entry so welcome tick has a target
        bus to post into (= _fire_welcome_tick early-returns on empty
        scope). ms-97 / e-2637 helper."""
        _impersonate(LEADER_UID, LEADER_EMAIL)
        body = {"project": "beacon", "milestone": "ms-97"}
        r = client.put(f"/api/treks/{trek_id}/scope", json=body)
        assert r.status_code == 200, r.text
        pending_id = r.json()["pending_op"]["pending_id"]
        r2 = client.post(
            f"/api/treks/{trek_id}/scope/approve/{pending_id}",
        )
        assert r2.status_code == 200, r2.text

    def test_join_fires_welcome_tick_into_bus(self):
        """ms-97 / e-2637 — join endpoint fires welcome tick on first
        successful join. Done when #1+#2: tick lands in the joiner's
        bus with the AC28 manual doc_id + kickoff hint.
        """
        trek_id = _create_seed_trek()
        self._seed_scope_for_welcome_test(trek_id)
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/members/join",
            headers={"X-Beacon-Session": "sv-welcome-1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Response surfaces the event id so clients can correlate.
        assert body.get("_welcome_tick_event_id"), (
            "join response should include _welcome_tick_event_id when "
            "the welcome tick fires"
        )
        # Trek doc carries the idempotent stamp.
        r2 = client.get(f"/api/treks/{trek_id}")
        meta = (r2.json().get("meta") or {})
        fired_map = meta.get("welcome_tick_fired_at") or {}
        assert fired_map.get("sv-welcome-1"), (
            f"join must stamp welcome_tick_fired_at for sv-welcome-1, "
            f"got {fired_map}"
        )

    def test_join_welcome_tick_idempotent_on_replay(self):
        """ms-97 / e-2637 Done when #3 — replaying join from the same
        session_id does not re-fire welcome tick (= 1 stamp per
        session_id).
        """
        trek_id = _create_seed_trek()
        self._seed_scope_for_welcome_test(trek_id)
        _impersonate(INVITED_UID, INVITED_EMAIL)
        r1 = client.post(
            f"/api/treks/{trek_id}/members/join",
            headers={"X-Beacon-Session": "sv-welcome-2"},
        )
        assert r1.status_code == 200, r1.text
        first_event_id = r1.json().get("_welcome_tick_event_id")
        r2 = client.post(
            f"/api/treks/{trek_id}/members/join",
            headers={"X-Beacon-Session": "sv-welcome-2"},
        )
        assert r2.status_code == 200
        # Second join must NOT include a new welcome event id.
        assert not r2.json().get("_welcome_tick_event_id"), (
            "welcome tick must fire at most once per session_id; second "
            "join response should omit _welcome_tick_event_id"
        )
        assert first_event_id  # primary path landed

    def test_same_session_repeated_join_remains_idempotent(self):
        """ms-97 / e-2636 — 同 session_id の 2 度目 join は entry を増やさない。

        AC6 expand 経路の idempotent 保持。 1 user 1 session = 1 entry を
        維持する (= dogfood retry に強い)。
        """
        trek_id = _create_seed_trek()
        _impersonate(INVITED_UID, INVITED_EMAIL)
        for _ in range(2):
            r = client.post(
                f"/api/treks/{trek_id}/members/join",
                headers={"X-Beacon-Session": "sv-invited-once"},
            )
            assert r.status_code == 200, r.text
        members = r.json()["members"]
        invited_entries = [
            m for m in members if m["user_id"] == INVITED_UID
        ]
        assert len(invited_entries) == 1
        assert invited_entries[0]["session_id"] == "sv-invited-once"


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
        # ms-97 / e-2650 — slot-done precondition は project pool の task が
        # done であることを必須条件にする。 本 test は self-loop suppress の
        # 検証なので、 precondition を充たすために pool 側を done で seed する。
        _seed_pool_task("beacon-test", "ms-88", "e-x", status="done")
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
        # ms-128 方針5: done は user_review に migrate される (Trek 打ち止め)。
        assert suppressions[0]["state"] == "user_review"
        # State transition preserved (done → user_review に migrate 済)
        assert (t.get("task_states") or {}).get("e-x", {}).get("state") == "user_review"

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
        # ms-97 / e-2650 — slot-done precondition 充足のため pool seed。
        _seed_pool_task("beacon-test", "ms-88", "e-y", status="done")
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
        # ms-97 / e-2650 — slot-done precondition 充足のため pool seed。
        _seed_pool_task("beacon-test", "ms-97", "e-done-control",
                        status="done")
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
        # ms-128 方針5: done は user_review に migrate され、event payload も
        # effective_state (= user_review) を載せる。
        assert (review_events[0].get("payload") or {}).get("state") == "user_review"

    def test_member_stamp_user_review_still_emits_review_event(self):
        """control 2: user_review への遷移も依然 event 発火 (= 既存挙動)。"""
        trek_id = self._seed_with_member_working("e-user-rev-control")
        # ms-128 方針5 + option A: user_review が terminal になり、slot-done
        # precondition (= pool-done evidence) が user_review 書き込みにも適用される。
        # phantom-done を防ぐため pool 側を done で seed する。
        _seed_pool_task("beacon-test", "ms-97", "e-user-rev-control",
                        status="done")
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


# ---------------------------------------------------------------------------
# ms-95 / e-2640 — /api/treks/{trek_id}/scope-entries smoke pin
#
# The detailed cross-project / shape / RBAC cases live in
# ``test_trek_scope_aggregate_endpoints.py`` (= it already has the
# project-mocking scaffolding the new endpoint needs). This file's seed
# treks have empty scope so the test below only pins the auth gates +
# response envelope; cross-project body assertions live in the
# scope-aggregate suite.
# ---------------------------------------------------------------------------

class TestScopeEntriesEndpoint:
    def test_member_can_call_endpoint_returns_envelope(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}/scope-entries")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["trek_id"] == trek_id
        # Seed trek has empty scope → no MS rows; envelope still present.
        assert body["milestones"] == []

    def test_stranger_returns_403(self):
        trek_id = _create_seed_trek()
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.get(f"/api/treks/{trek_id}/scope-entries")
        assert r.status_code == 403

    def test_missing_trek_returns_404(self):
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.get("/api/treks/tk-nope/scope-entries")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# ms-97 / e-2650 — Trek slot done の task pool 真値源 precondition
# ---------------------------------------------------------------------------

class TestSlotDonePrecondition:
    """ms-97 / e-2650 — phantom done 構造防御 regression pin。

    Trek slot done への遷移は、 project pool 側の task が done である
    ことを必須条件とする (= 「view 側だけ done になる経路」 を server で
    構造的に reject)。 done 以外への遷移 (= todo / working / *_review) は
    従来通り通る。

    2026-06-28 dogfood で観測された 2 件の病理:
      (a) e-710 phantom done = PE 側で task が done と記録されているのに、
          該当 commit / ファイル変更が一切なかった
      (b) tk-7a3b88b9 = Trek 側 done、 task pool todo が固定化していた
    どちらも本 check の追加で原理的に消える。
    """

    def _seed_active_trek_with_scope(self, ms_id: str = "ms-97") -> str:
        trek_id = _create_seed_trek()
        _treks[trek_id]["scope"] = [
            {"project": "beacon-test", "milestone": ms_id}
        ]
        _treks[trek_id]["task_states"] = {}
        return trek_id

    def _seed_member_working(self, trek_id: str, task_id: str) -> None:
        _treks[trek_id]["task_states"][task_id] = {
            "state": "working",
            "updated_by_session_id": "sv-member",
            "updated_at": "2026-06-28T00:00:00.000000Z",
            "last_activity_at": "2026-06-28T00:00:00.000000Z",
            "note": "",
        }
        for k in list(_bus_events_by_project.keys()):
            _bus_events_by_project[k].clear()

    # ----- 1. task ref slot で task pool todo のまま slot done → 4xx -----

    def test_task_ref_slot_done_rejected_when_pool_todo(self):
        """task ref slot done を試みても project pool で todo なら 4xx。

        e-2650 done-when 1 (task ref) の核心 regression pin。 旧コード
        では set_task_state が無条件で通り、 phantom done を生んでいた。
        """
        trek_id = self._seed_active_trek_with_scope()
        self._seed_member_working(trek_id, "e-pool-todo")
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-97", "entries": [
                            {"id": "e-pool-todo", "type": "task",
                             "status": "todo"}
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.patch(
                f"/api/treks/{trek_id}/task-state",
                json={"task_id": "e-pool-todo", "state": "done",
                      "note": "executor が done に flip 試行"},
                headers={"X-Beacon-Session": "sv-member"},
            )
        assert r.status_code == 409, r.text
        body = r.json()
        # FastAPI HTTPException(detail=dict) は body["detail"] に dict を返す
        detail = body.get("detail") or {}
        assert detail.get("code") == "task_pool_not_done", detail
        assert "task pool" in (detail.get("message") or "")
        # state transition は適用されていないこと (= 真値源 ordering)
        assert (
            _treks[trek_id]["task_states"]["e-pool-todo"]["state"]
            == "working"
        )

    # ----- 2. MS ref slot で配下 task 一部 todo のまま slot done → 4xx -----

    def test_ms_ref_slot_done_rejected_when_children_partial_todo(self):
        """MS slot done を試みても配下 task が 1 件でも todo なら 4xx。

        e-2650 done-when 1 (MS ref) の核心 regression pin。 旧 reconcile
        flow に頼らず、 ordering を server で構造強制する。
        """
        trek_id = self._seed_active_trek_with_scope(ms_id="ms-97")
        self._seed_member_working(trek_id, "ms-97")
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-97", "entries": [
                            {"id": "e-done-1", "type": "task",
                             "status": "done"},
                            {"id": "e-still-todo", "type": "task",
                             "status": "todo"},
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.patch(
                f"/api/treks/{trek_id}/task-state",
                json={"task_id": "ms-97", "state": "done",
                      "note": "MS slot 一括 done flip 試行"},
                headers={"X-Beacon-Session": "sv-member"},
            )
        assert r.status_code == 409, r.text
        detail = r.json().get("detail") or {}
        assert detail.get("code") == "ms_children_not_all_done", detail
        # 1 件残っている旨が message に含まれること (= human-readable guide)
        assert "e-still-todo" in (detail.get("message") or "")

    # ----- 3. 全 done で slot done 通る (= happy path) -----

    def test_task_ref_slot_done_allowed_when_pool_done(self):
        """task ref slot: project pool が done なら slot done も通る。"""
        trek_id = self._seed_active_trek_with_scope()
        self._seed_member_working(trek_id, "e-pool-done")
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-97", "entries": [
                            {"id": "e-pool-done", "type": "task",
                             "status": "done"}
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.patch(
                f"/api/treks/{trek_id}/task-state",
                json={"task_id": "e-pool-done", "state": "done",
                      "note": "ok"},
                headers={"X-Beacon-Session": "sv-member"},
            )
        assert r.status_code == 200, r.text
        # ms-128 方針5: pool-done gate は通るが、保存状態は done → user_review に
        # migrate される (Trek は user_review で打ち止め)。
        assert (
            _treks[trek_id]["task_states"]["e-pool-done"]["state"] == "user_review"
        )

    def test_ms_ref_slot_done_allowed_when_all_children_done(self):
        """MS slot: 配下 task が全 done なら slot done も通る (= happy path)。"""
        trek_id = self._seed_active_trek_with_scope(ms_id="ms-97")
        self._seed_member_working(trek_id, "ms-97")
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-97", "entries": [
                            {"id": "e-c1", "type": "task", "status": "done"},
                            {"id": "e-c2", "type": "task", "status": "done"},
                        ]}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.patch(
                f"/api/treks/{trek_id}/task-state",
                json={"task_id": "ms-97", "state": "done",
                      "note": "全 done のため MS slot done"},
                headers={"X-Beacon-Session": "sv-member"},
            )
        assert r.status_code == 200, r.text

    # ----- 4. non-done 遷移は check 対象外 (= ordering 維持の確認) -----

    def test_non_done_transition_unaffected_by_precondition(self):
        """working → leader_review (= done 以外) は precondition check skip。

        e-2650 の防御は done 遷移にのみ働く。 他の遷移は従来通り 5-state
        machine のみで判定する (= overreach 防止 regression pin)。
        """
        trek_id = self._seed_active_trek_with_scope()
        self._seed_member_working(trek_id, "e-still-working")
        # get_project を呼ばないことを sentinel で確認
        call_count = {"n": 0}
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            call_count["n"] += 1
            return None

        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.patch(
                f"/api/treks/{trek_id}/task-state",
                json={"task_id": "e-still-working",
                      "state": "leader_review",
                      "note": "leader 判断要請"},
                headers={"X-Beacon-Session": "sv-member"},
            )
        assert r.status_code == 200, r.text
        assert call_count["n"] == 0, (
            "leader_review 遷移で get_project が呼ばれた "
            "(= check が done 以外にも overreach)"
        )

    # ----- 5. 未登録 id (= scope 上のどの project pool にも無い) は reject -----

    def test_task_ref_slot_done_rejected_when_id_unknown_to_pool(self):
        """scope project の task pool に存在しない id を done にしようとして
        も reject (= 「task 自体が無いのに slot だけ done」 phantom 経路の防御)。"""
        trek_id = self._seed_active_trek_with_scope()
        self._seed_member_working(trek_id, "e-ghost")
        from unittest.mock import patch

        def _fake_get_project(pid: str):
            if pid == "beacon-test":
                return {
                    "milestones": [
                        {"id": "ms-97", "entries": []}
                    ],
                    "operations": [],
                }
            return None

        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        with patch.object(app_module.db, "get_project", _fake_get_project):
            r = client.patch(
                f"/api/treks/{trek_id}/task-state",
                json={"task_id": "e-ghost", "state": "done", "note": ""},
                headers={"X-Beacon-Session": "sv-member"},
            )
        assert r.status_code == 409, r.text
        detail = r.json().get("detail") or {}
        assert detail.get("code") == "task_pool_lookup_failed"


# ---------------------------------------------------------------------------
# ms-97 P5 (review Trek-H3) — leader-review self-approve gate
# ---------------------------------------------------------------------------

class TestLeaderReviewSelfApproveGate:
    """A task awaiting the leader's forced review must only be advanced by the
    leader — not self-approved by the executor, and not auto-mirrored to done
    by the pool sync."""

    def _seed_trek_with_task_state(self, task_id: str, state: str) -> str:
        trek_id = _create_seed_trek()
        _treks[trek_id]["scope"] = [
            {"project": "beacon-test", "milestone": "ms-97"}
        ]
        _treks[trek_id]["task_states"] = {
            task_id: {
                "state": state,
                "updated_by_session_id": "sv-member",
                "updated_at": "2026-06-28T00:00:00.000000Z",
                "last_activity_at": "2026-06-28T00:00:00.000000Z",
                "note": "",
            }
        }
        return trek_id

    def test_executor_cannot_self_approve_leader_review_to_done(self):
        """The executor (member session) attempting leader_review → done is
        rejected with 403 — the leader review gate fires before anything
        else (= not a 409 precondition, an auth refusal)."""
        trek_id = self._seed_trek_with_task_state("e-lr", "leader_review")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-lr", "state": "done",
                  "note": "self-approve attempt"},
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 403, r.text
        # The stamp must remain leader_review (transition never applied).
        assert _treks[trek_id]["task_states"]["e-lr"]["state"] == "leader_review"

    def test_executor_cannot_self_approve_user_review_to_done(self):
        """Same gate for user_review origin (= the User's call)."""
        trek_id = self._seed_trek_with_task_state("e-ur", "user_review")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-ur", "state": "done"},
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 403, r.text

    def test_leader_can_advance_leader_review(self):
        """The leader session CAN move leader_review → working (re-work) —
        the gate lets the legitimate reviewer through. Uses the working
        target so the slot-done precondition is not in play."""
        trek_id = self._seed_trek_with_task_state("e-lr2", "leader_review")
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-lr2", "state": "working",
                  "note": "leader sends back for re-work"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 200, r.text
        assert _treks[trek_id]["task_states"]["e-lr2"]["state"] == "working"

    def test_executor_working_to_terminal_still_open(self):
        """Executor origins stay open — a member can still stamp the work it
        performed (working → leader_review), which is NOT a self-approve."""
        trek_id = self._seed_trek_with_task_state("e-w", "working")
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.patch(
            f"/api/treks/{trek_id}/task-state",
            json={"task_id": "e-w", "state": "leader_review",
                  "note": "executor requests review"},
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 200, r.text

    def test_mirror_does_not_overwrite_leader_review(self):
        """The pool-done mirror must skip a leader_review stamp — otherwise a
        pool task going done would auto-approve past the leader review."""
        trek_id = self._seed_trek_with_task_state("e-mir", "leader_review")
        touched = app_module._mirror_task_done_to_treks("e-mir")
        assert trek_id not in touched
        assert _treks[trek_id]["task_states"]["e-mir"]["state"] == "leader_review"

    def test_mirror_still_unsticks_working(self):
        """Sanity: the mirror still does its job for a genuinely-stuck
        working stamp (= regression guard that the P5 skip is narrow)."""
        trek_id = self._seed_trek_with_task_state("e-mir2", "working")
        touched = app_module._mirror_task_done_to_treks("e-mir2")
        assert trek_id in touched
        assert _treks[trek_id]["task_states"]["e-mir2"]["state"] == "done"


# ---------------------------------------------------------------------------
# ms-97 / e-2650 — pure helper unit tests (lib/trek.check_slot_done_precondition)
# ---------------------------------------------------------------------------

class TestSlotDonePreconditionHelper:
    """``check_slot_done_precondition`` pure-helper coverage (no HTTP)。

    server endpoint test (= TestSlotDonePrecondition) は HTTP/auth 経路を
    pin する。 本クラスは helper の代表 branch を直接 verify し、 将来
    別 caller (= CLI / scheduler) から再利用された時に regression が
    検出できるようにする。
    """

    def _make_trek_doc(self, *, project: str = "p-x",
                       milestone: str = "ms-1") -> dict:
        return {
            "trek_id": "tk-helper",
            "scope": [{"project": project, "milestone": milestone}],
            "task_states": {},
        }

    def test_helper_allows_when_task_pool_done(self):
        import trek as trek_mod

        doc = self._make_trek_doc()

        def _gp(pid: str):
            return {
                "milestones": [
                    {"id": "ms-1", "entries": [
                        {"id": "e-1", "type": "task", "status": "done"}
                    ]}
                ],
                "operations": [],
            }

        allowed, code, msg = trek_mod.check_slot_done_precondition(
            doc, task_id="e-1", get_project=_gp,
        )
        assert allowed is True
        assert code == trek_mod.SLOT_DONE_ALLOWED
        assert msg == ""

    def test_helper_rejects_when_task_pool_not_done(self):
        import trek as trek_mod

        doc = self._make_trek_doc()

        def _gp(pid: str):
            return {
                "milestones": [
                    {"id": "ms-1", "entries": [
                        {"id": "e-1", "type": "task", "status": "working"}
                    ]}
                ],
                "operations": [],
            }

        allowed, code, msg = trek_mod.check_slot_done_precondition(
            doc, task_id="e-1", get_project=_gp,
        )
        assert allowed is False
        assert code == trek_mod.SLOT_DONE_REJECT_TASK_POOL_NOT_DONE
        assert "working" in msg

    def test_helper_rejects_empty_ms(self):
        import trek as trek_mod

        doc = self._make_trek_doc(milestone="ms-empty")

        def _gp(pid: str):
            return {
                "milestones": [{"id": "ms-empty", "entries": []}],
                "operations": [],
            }

        allowed, code, _msg = trek_mod.check_slot_done_precondition(
            doc, task_id="ms-empty", get_project=_gp,
        )
        assert allowed is False
        assert code == trek_mod.SLOT_DONE_REJECT_MS_CHILDREN_NOT_ALL_DONE

    def test_helper_rejects_partial_done_ms(self):
        import trek as trek_mod

        doc = self._make_trek_doc(milestone="ms-mix")

        def _gp(pid: str):
            return {
                "milestones": [{"id": "ms-mix", "entries": [
                    {"id": "e-a", "type": "task", "status": "done"},
                    {"id": "e-b", "type": "task", "status": "todo"},
                ]}],
                "operations": [],
            }

        allowed, code, _msg = trek_mod.check_slot_done_precondition(
            doc, task_id="ms-mix", get_project=_gp,
        )
        assert allowed is False
        assert code == trek_mod.SLOT_DONE_REJECT_MS_CHILDREN_NOT_ALL_DONE

    def test_helper_get_project_exception_does_not_propagate(self):
        """1 project が backend hiccup でも、 他 scope project で resolve できれば allow。"""
        import trek as trek_mod

        doc = {
            "trek_id": "tk-helper",
            "scope": [
                {"project": "p-flaky", "milestone": "ms-1"},
                {"project": "p-ok", "milestone": "ms-1"},
            ],
            "task_states": {},
        }

        def _gp(pid: str):
            if pid == "p-flaky":
                raise RuntimeError("transient backend error")
            if pid == "p-ok":
                return {
                    "milestones": [{"id": "ms-1", "entries": [
                        {"id": "e-x", "type": "task", "status": "done"},
                    ]}],
                    "operations": [],
                }
            return None

        allowed, code, _msg = trek_mod.check_slot_done_precondition(
            doc, task_id="e-x", get_project=_gp,
        )
        assert allowed is True
        assert code == trek_mod.SLOT_DONE_ALLOWED


class TestAddBlockerEndpoint:
    """ms-128 方針4 (e-4365) — POST /api/treks/{id}/blocker (leader-only)."""

    def test_leader_draws_blocker_and_target_blocks(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/blocker",
            json={"target_id": "ms-A", "blocker_target_id": "ms-B"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["task_states"]["ms-A"]["state"] == "block"
        assert doc["target_blockers"]["ms-A"] == ["ms-B"]

    def test_non_leader_member_forbidden(self):
        trek_id = _create_seed_trek()
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/blocker",
            json={"target_id": "ms-A", "blocker_target_id": "ms-B"},
            headers={"X-Beacon-Session": "sv-member"},
        )
        assert r.status_code == 403, r.text

    def test_self_block_rejected_400(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/blocker",
            json={"target_id": "ms-A", "blocker_target_id": "ms-A"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 400, r.text

    def test_cycle_rejected_409(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r1 = client.post(
            f"/api/treks/{trek_id}/blocker",
            json={"target_id": "ms-A", "blocker_target_id": "ms-B"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r1.status_code == 200, r1.text
        r2 = client.post(
            f"/api/treks/{trek_id}/blocker",
            json={"target_id": "ms-B", "blocker_target_id": "ms-A"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r2.status_code == 409, r2.text

    def test_missing_ids_rejected_400(self):
        trek_id = _create_seed_trek()
        _impersonate(LEADER_UID, LEADER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/blocker",
            json={"target_id": "", "blocker_target_id": "ms-B"},
            headers={"X-Beacon-Session": "sv-leader"},
        )
        assert r.status_code == 400, r.text
