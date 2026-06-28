"""ms-97 / Phase 7-A / AC21 — POST /api/treks/{id}/summary-sent endpoint.

Endpoint stamps ``meta.summary_sent_at`` after the leader sends the user
summary DM. Leader-only (= ``_require_trek_leader_session`` hard-check
on phase A+ trek). The stamp combined with ``meta.completion_notified_at``
makes the leader-digest tick fire stop in the scheduler endpoint
(= covered separately by ``tests/test_trek_scheduler_api.py``-style
integration; here we focus on the endpoint contract and AC13 auth).

Three test cases pin:

  1. Happy path — leader's stamped session stamps the field, response
     mirrors the updated trek doc.
  2. AC13 hard-check — non-leader user is rejected (403).
  3. AC13 hard-check — leader user but wrong session_id is rejected
     (403) on a phase A+ trek.

Mirrors the in-memory db harness from ``tests/test_trek_api.py``.
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


def _mock_get_trek(trek_id: str):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id: str, data: dict):
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


def _mock_find_user_by_email(email: str):
    return _users_by_email.get(email)


def _mock_get_or_create_user(user_id: str, email: str):
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
        ("find_user_by_email", _mock_find_user_by_email),
        ("get_or_create_user", _mock_get_or_create_user),
        ("get_user", _mock_get_user),
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
MEMBER_UID = "uid-member"
LEADER_EMAIL = "leader@x"
MEMBER_EMAIL = "member@x"


def _impersonate(uid: str, email: str = "") -> None:
    actual_email = email or f"{uid}@x"

    def _fake_auth():
        return {"sub": uid, "email": actual_email}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _users_by_email.clear()
    for uid, email in (
        (LEADER_UID, LEADER_EMAIL),
        (MEMBER_UID, MEMBER_EMAIL),
    ):
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


def _seed_phase_a_trek() -> str:
    """Trek with migration_phase=A, leader_session_id=sv-leader, member sv-member."""
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post("/api/treks", json={
        "title": "Phase 7-A summary-sent test",
        "creator_session_id": "sv-leader",
    })
    assert r.status_code == 200, r.text
    trek_id = r.json()["trek_id"]
    # Invite + join MEMBER so we have a non-leader role for the 403 test.
    r = client.post(f"/api/treks/{trek_id}/members",
                    json={"email": MEMBER_EMAIL})
    assert r.status_code == 200, r.text
    _impersonate(MEMBER_UID, MEMBER_EMAIL)
    r = client.post(f"/api/treks/{trek_id}/members/join")
    assert r.status_code == 200, r.text
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post(f"/api/treks/{trek_id}/start")
    assert r.status_code == 200, r.text
    # Stamp phase A + session_id on existing members[] entries so the
    # session-grain leader hard-check resolves correctly.
    t = _treks[trek_id]
    meta = t.setdefault("meta", {})
    meta["migration_phase"] = "A"
    for m in t.get("members") or []:
        if m.get("user_id") == LEADER_UID:
            m["session_id"] = "sv-leader"
        elif m.get("user_id") == MEMBER_UID:
            m["session_id"] = "sv-member"
    return trek_id


def test_summary_sent_stamps_meta_field():
    """Happy path: leader's stamped session stamps ``meta.summary_sent_at``."""
    trek_id = _seed_phase_a_trek()
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post(
        f"/api/treks/{trek_id}/summary-sent",
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trek_id"] == trek_id
    stamped = (body.get("meta") or {}).get("summary_sent_at")
    assert stamped, "summary_sent_at must be a non-empty ISO timestamp"
    # Persistence check — the stamp survives a re-read.
    persisted = (_treks[trek_id].get("meta") or {}).get("summary_sent_at")
    assert persisted == stamped


def test_summary_sent_rejects_non_leader_user():
    """AC13 — a member who is NOT the leader is rejected (403)."""
    trek_id = _seed_phase_a_trek()
    _impersonate(MEMBER_UID, MEMBER_EMAIL)
    r = client.post(
        f"/api/treks/{trek_id}/summary-sent",
        headers={"X-Beacon-Session": "sv-member"},
    )
    assert r.status_code == 403


def test_summary_sent_rejects_leader_user_wrong_session():
    """AC13 hard-check — leader user but wrong session_id → 403."""
    trek_id = _seed_phase_a_trek()
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r = client.post(
        f"/api/treks/{trek_id}/summary-sent",
        headers={"X-Beacon-Session": "sv-leader-second-terminal"},
    )
    assert r.status_code == 403


def test_summary_sent_idempotent_re_stamps_latest_timestamp():
    """Calling twice re-stamps the field (= idempotent intent; the
    leader-tick stop condition only cares about non-null)."""
    trek_id = _seed_phase_a_trek()
    _impersonate(LEADER_UID, LEADER_EMAIL)
    r1 = client.post(
        f"/api/treks/{trek_id}/summary-sent",
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r1.status_code == 200
    first = (r1.json().get("meta") or {}).get("summary_sent_at")
    assert first

    r2 = client.post(
        f"/api/treks/{trek_id}/summary-sent",
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert r2.status_code == 200
    second = (r2.json().get("meta") or {}).get("summary_sent_at")
    assert second
    # Second call re-stamps; both stamps are well-formed.
    assert second >= first
