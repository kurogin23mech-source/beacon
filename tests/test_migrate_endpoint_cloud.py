"""ms-97 / e-2658 — POST /api/treks/{id}/migrate-members-session-keyed endpoint.

Pure mutator (= ``lib/trek.migrate_members_to_session_keyed``) is shared
with ``scripts/migrate_members_session_keyed.py``. Four tests pin:

  1. Happy path — leader migrates a pre-A trek, phase advances to A.
  2. Leader-only — non-leader caller is rejected (403).
  3. dry_run — returns the migrated doc without persisting.
  4. Idempotency — second call returns 409.

Mirrors the in-memory db harness from
``tests/test_trek_summary_sent_endpoint.py``.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db

_treks: dict[str, dict] = {}
_users_by_email: dict[str, tuple[str, dict]] = {}


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


def _mock_find_user_by_email(email):
    return _users_by_email.get(email)


def _mock_no_op(*args, **kwargs):
    return None


def _rebind_db():
    db = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek), ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("find_user_by_email", _mock_find_user_by_email),
        ("get_or_create_user", _mock_no_op), ("get_user", _mock_no_op),
    ]:
        prior[name] = getattr(db, name, None)
        setattr(db, name, mock)
    return prior


def _restore_db(prior):
    db = app_module.db
    for k, v in prior.items():
        if v is None and hasattr(db, k):
            delattr(db, k)
        elif v is not None:
            setattr(db, k, v)


client = TestClient(app_module.app)
LEADER_UID, MEMBER_UID = "uid-leader", "uid-member"
LEADER_EMAIL, MEMBER_EMAIL = "leader@x", "member@x"
ENDPOINT = "/api/treks/{tid}/migrate-members-session-keyed"


def _impersonate(uid, email=""):
    actual = email or f"{uid}@x"

    def _fake_auth():
        return {"sub": uid, "email": actual}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _users_by_email.clear()
    for uid, email in ((LEADER_UID, LEADER_EMAIL), (MEMBER_UID, MEMBER_EMAIL)):
        _users_by_email[email] = (uid, {"sub": uid, "email": email})
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


def _seed_pre_a_trek() -> str:
    """Trek with leader + 1 joined member, default pre-A phase."""
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post("/api/treks", json={
        "title": "migrate endpoint pre-A",
        "creator_session_id": "sv-leader",
    })
    assert r.status_code == 200, r.text
    trek_id = r.json()["trek_id"]
    assert client.post(
        f"/api/treks/{trek_id}/members",
        json={"email": MEMBER_EMAIL}).status_code == 200
    _impersonate(MEMBER_UID, MEMBER_EMAIL)
    assert client.post(
        f"/api/treks/{trek_id}/members/join",
        headers={"X-Beacon-Session": "sv-member"}).status_code == 200
    _impersonate(LEADER_UID, LEADER_EMAIL)
    return trek_id


def test_migrate_endpoint_happy_path_leader_flips_phase_to_A():
    trek_id = _seed_pre_a_trek()
    r = client.post(
        ENDPOINT.format(tid=trek_id),
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body.get("meta") or {}).get("migration_phase") == "A"
    for m in body.get("members") or []:
        assert m.get("session_id"), f"member missing session_id: {m}"
    assert body.get("members_legacy_backup"), "rollback snapshot must exist"
    assert (_treks[trek_id].get("meta") or {}).get("migration_phase") == "A"


def test_migrate_endpoint_rejects_non_leader_user():
    trek_id = _seed_pre_a_trek()
    _impersonate(MEMBER_UID, MEMBER_EMAIL)
    r = client.post(
        ENDPOINT.format(tid=trek_id),
        headers={"X-Beacon-Session": "sv-member"},
    )
    assert r.status_code == 403
    assert (_treks[trek_id].get("meta") or {}).get("migration_phase") in (
        None, "pre-A",
    )


def test_migrate_endpoint_dry_run_returns_doc_without_persisting():
    trek_id = _seed_pre_a_trek()
    r = client.post(
        ENDPOINT.format(tid=trek_id) + "?dry_run=true",
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r.status_code == 200, r.text
    assert (r.json().get("meta") or {}).get("migration_phase") == "A"
    persisted = (_treks[trek_id].get("meta") or {}).get("migration_phase")
    assert persisted in (None, "pre-A"), (
        f"dry_run must not persist; persisted_phase={persisted!r}"
    )


def test_migrate_endpoint_double_call_returns_409():
    trek_id = _seed_pre_a_trek()
    r1 = client.post(
        ENDPOINT.format(tid=trek_id),
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        ENDPOINT.format(tid=trek_id),
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r2.status_code == 409, r2.text
    detail = r2.json().get("detail", "").lower()
    assert "phase" in detail or "migrat" in detail
