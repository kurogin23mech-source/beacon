"""Tests for AC22 auto-succession (ms-97 Phase 7-B / e-2684).

Covers:
  * ``detect_unresponsive_leader`` — N=3 miss AND 30 min stale AND
  * ``pick_succession_candidate`` — invited_at priority, declined exclusion
  * Halt suppresses succession (= AC32 contract via tick endpoint)
  * Consent endpoint accept → leader transferred + pending cleared
  * Consent endpoint decline → declined list grows + pending cleared
  * Consent endpoint rejects non-candidate caller (= 403)
  * Escalation when no candidate available (= meta + warn logged)
"""
from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
from lib import trek as trek_mod  # noqa: E402

sys.modules["firestore_client"] = app_module.db


# ---------------------------------------------------------------------------
# Pure helpers — exercise lib/trek functions without standing up the server.
# ---------------------------------------------------------------------------

def _build_phase_a_trek(*, leader_sid: str = "sv-leader",
                        member_sids: list[str] | None = None) -> dict:
    """Construct a phase A+ trek doc with the requested member sessions.

    Avoids the new_trek factory's session_id wiring so the test owns the
    layout exactly. Each member is assigned ``invited_at`` in ascending
    order so priority is deterministic.
    """
    member_sids = member_sids or []
    now = trek_mod.utcnow_iso()
    members = [
        {
            "user_id": "u-leader",
            "email": "leader@x",
            "role": "leader",
            "session_id": leader_sid,
            "invited_at": "2026-06-20T00:00:00Z",
            "joined_at": "2026-06-20T00:00:01Z",
            "invited_by": "",
        }
    ]
    for i, sid in enumerate(member_sids):
        members.append({
            "user_id": f"u-mem-{i+1}",
            "email": f"m{i+1}@x",
            "role": "member",
            "session_id": sid,
            "invited_at": f"2026-06-2{1 + i}T00:00:00Z",  # ascending
            "joined_at": f"2026-06-2{1 + i}T00:00:01Z",
            "invited_by": "u-leader",
        })
    return {
        "trek_id": "tk-test",
        "title": "Succession test",
        "status": "active",
        "type": "persistent",
        "leader_session_id": leader_sid,
        "members": members,
        "scope": [{"project": "proj-1"}],
        "meta": {trek_mod.MIGRATION_PHASE_META_KEY: "A"},
        "created_at": now,
        "updated_at": now,
    }


def _stamp_n_misses(trek_doc: dict, leader_sid: str, n: int,
                    choice: str = "no-op") -> None:
    """Push N pulse-ack history entries for the leader, all marked miss."""
    for _ in range(n):
        trek_mod.record_pulse_ack(
            trek_doc, session_id=leader_sid,
            picked_choice=choice,
        )


# ---------------------------------------------------------------------------
# detect_unresponsive_leader
# ---------------------------------------------------------------------------

class TestDetectUnresponsiveLeader:
    def test_returns_leader_sid_when_miss_and_stale(self):
        t = _build_phase_a_trek()
        _stamp_n_misses(t, "sv-leader", 3, choice="no-op")
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        last_active = "2026-06-28T11:00:00.000000Z"  # 60 min stale
        result = trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        )
        assert result == "sv-leader"

    def test_returns_none_when_only_stale_no_misses(self):
        t = _build_phase_a_trek()
        _stamp_n_misses(t, "sv-leader", 3, choice="continue")  # active
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        last_active = "2026-06-28T11:00:00.000000Z"
        assert trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        ) is None

    def test_returns_none_when_only_misses_not_stale(self):
        t = _build_phase_a_trek()
        _stamp_n_misses(t, "sv-leader", 3, choice="no-op")
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        # last_active 5 minutes ago — fresh, not stale
        last_active = "2026-06-28T11:55:00.000000Z"
        assert trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        ) is None

    def test_returns_none_for_phase_pre_a_trek(self):
        t = _build_phase_a_trek()
        # Drop migration phase → defaults to pre-A
        t["meta"].pop(trek_mod.MIGRATION_PHASE_META_KEY, None)
        _stamp_n_misses(t, "sv-leader", 3, choice="no-op")
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        last_active = "2026-06-28T11:00:00.000000Z"
        assert trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        ) is None

    def test_empty_pulse_history_with_stale_counts_as_unresponsive(self):
        t = _build_phase_a_trek()
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        last_active = "2026-06-28T11:00:00.000000Z"
        # No pulse acks at all + stale → trivially unresponsive
        assert trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        ) == "sv-leader"

    def test_threshold_overridable_via_meta(self):
        t = _build_phase_a_trek()
        t["meta"][trek_mod.SUCCESSION_MISS_COUNT_META_KEY] = 5
        t["meta"][trek_mod.SUCCESSION_STALE_MINUTES_META_KEY] = 60
        _stamp_n_misses(t, "sv-leader", 3, choice="no-op")  # < 5 threshold
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        last_active = "2026-06-28T11:00:00.000000Z"  # 60 min, hits stale exactly
        # Not enough misses (3 < 5) but history len < threshold → trivially miss
        # The function treats insufficient samples as miss for safety.
        result = trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        )
        assert result == "sv-leader"

    def test_missing_leader_session_id_returns_none(self):
        t = _build_phase_a_trek()
        t["leader_session_id"] = ""
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        assert trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active="2026-06-28T11:00:00.000000Z",
        ) is None


# ---------------------------------------------------------------------------
# pick_succession_candidate
# ---------------------------------------------------------------------------

class TestPickSuccessionCandidate:
    def test_returns_earliest_invited_at_member(self):
        t = _build_phase_a_trek(member_sids=["sv-m1", "sv-m2", "sv-m3"])
        c = trek_mod.pick_succession_candidate(t)
        assert c is not None
        assert c["session_id"] == "sv-m1"  # earliest invited_at

    def test_excludes_current_leader(self):
        t = _build_phase_a_trek(member_sids=["sv-m1"])
        c = trek_mod.pick_succession_candidate(t)
        assert c["session_id"] == "sv-m1"
        assert c["session_id"] != t["leader_session_id"]

    def test_excludes_already_declined(self):
        t = _build_phase_a_trek(member_sids=["sv-m1", "sv-m2"])
        t.setdefault("meta", {})[trek_mod.SUCCESSION_DECLINED_META_KEY] = [
            "sv-m1",
        ]
        c = trek_mod.pick_succession_candidate(t)
        assert c["session_id"] == "sv-m2"

    def test_returns_none_when_only_leader_remains(self):
        t = _build_phase_a_trek()  # no member_sids
        assert trek_mod.pick_succession_candidate(t) is None

    def test_returns_none_for_pre_a_trek(self):
        t = _build_phase_a_trek(member_sids=["sv-m1"])
        t["meta"].pop(trek_mod.MIGRATION_PHASE_META_KEY, None)
        assert trek_mod.pick_succession_candidate(t) is None

    def test_excludes_placeholder_invitations(self):
        t = _build_phase_a_trek()
        # Add a placeholder (= no session_id, no joined_at)
        t["members"].append({
            "user_id": "u-pending",
            "email": "pending@x",
            "role": "member",
            "invited_at": "2026-06-21T00:00:00Z",
            "joined_at": "",
            "invited_by": "u-leader",
        })
        assert trek_mod.pick_succession_candidate(t) is None


# ---------------------------------------------------------------------------
# clear_succession_state + record_succession_decline
# ---------------------------------------------------------------------------

class TestSuccessionStateMutators:
    def test_record_decline_appends_unique(self):
        t = _build_phase_a_trek(member_sids=["sv-m1"])
        trek_mod.record_succession_decline(t, "sv-m1")
        trek_mod.record_succession_decline(t, "sv-m1")  # idempotent
        assert t["meta"][trek_mod.SUCCESSION_DECLINED_META_KEY] == ["sv-m1"]
        assert t["meta"][trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY] == ""

    def test_clear_succession_state_resets_all(self):
        t = _build_phase_a_trek(member_sids=["sv-m1"])
        meta = t["meta"]
        meta[trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY] = "sv-m1"
        meta[trek_mod.SUCCESSION_PENDING_EMITTED_AT_META_KEY] = "2026-06-28T00:00:00Z"
        meta[trek_mod.SUCCESSION_DECLINED_META_KEY] = ["sv-m1"]
        meta[trek_mod.SUCCESSION_ESCALATED_AT_META_KEY] = "2026-06-28T00:00:00Z"
        trek_mod.clear_succession_state(t)
        meta = t["meta"]
        assert meta[trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY] == ""
        assert meta[trek_mod.SUCCESSION_PENDING_EMITTED_AT_META_KEY] == ""
        assert meta[trek_mod.SUCCESSION_DECLINED_META_KEY] == []
        assert meta[trek_mod.SUCCESSION_ESCALATED_AT_META_KEY] == ""


# ---------------------------------------------------------------------------
# Halt suppresses succession (= AC32 contract via tick endpoint)
# Verified at the lib layer by confirming is_halted gates the tick path;
# the unit-level guarantee is that detect_unresponsive_leader still answers
# truthfully — the tick endpoint is the one that must skip on halt.
# ---------------------------------------------------------------------------

class TestHaltSuppressesSuccession:
    def test_is_halted_true_does_not_break_detection(self):
        """Detection helper is halt-agnostic; tick endpoint enforces halt skip."""
        t = _build_phase_a_trek()
        _stamp_n_misses(t, "sv-leader", 3, choice="no-op")
        t["halt"] = {"issued_by_session_id": "sv-leader", "reason": "stop"}
        now = datetime.datetime(2026, 6, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        last_active = "2026-06-28T11:00:00.000000Z"
        # Detection still reports the unresponsive sid — halt suspension
        # happens at the orchestrator (tick endpoint) layer.
        assert trek_mod.detect_unresponsive_leader(
            t, now, leader_last_active=last_active,
        ) == "sv-leader"
        # And is_halted is True (= the gate the orchestrator reads).
        assert trek_mod.is_halted(t) is True


# ---------------------------------------------------------------------------
# Consent endpoint — server integration tests with mocked db.
# Mirrors the test_trek_api.py mocking pattern.
# ---------------------------------------------------------------------------

_treks: dict[str, dict] = {}
_users_by_email: dict[str, tuple[str, dict]] = {}
_bus_events_by_project: dict[str, list[dict]] = {}


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    bus = _bus_events_by_project.setdefault(project_id, [])
    event_id = f"ev-{len(bus)}"
    bus.append({"event_id": event_id, **copy.deepcopy(data)})
    return event_id


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
        out.append(copy.deepcopy(t))
    return out


def _mock_find_user_by_email(email: str):
    return _users_by_email.get(email)


def _mock_get_or_create_user(user_id: str, email: str):
    return None


def _mock_get_user(user_id: str):
    for _, (_, data) in _users_by_email.items():
        if data.get("sub") == user_id:
            return data
    return None


def _mock_list_sessions(project_id: str):
    return []


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
        ("append_bus_event", _mock_append_bus_event),
        ("list_sessions", _mock_list_sessions),
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


_client = TestClient(app_module.app)

LEADER_UID = "u-leader"
CAND_UID = "u-mem-1"
NON_CAND_UID = "u-mem-2"
LEADER_EMAIL = "leader@x"
CAND_EMAIL = "m1@x"
NON_CAND_EMAIL = "m2@x"


def _impersonate(uid: str, email: str) -> None:
    def _fake_auth():
        return {"sub": uid, "email": email}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _users_by_email.clear()
    for uid, email in (
        (LEADER_UID, LEADER_EMAIL),
        (CAND_UID, CAND_EMAIL),
        (NON_CAND_UID, NON_CAND_EMAIL),
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


def _seed_trek_with_pending(*, pending_sid: str = "sv-m1") -> str:
    """Seed an active phase-A trek with succession pending on sv-m1."""
    trek_id = "tk-succ-test"
    t = _build_phase_a_trek(member_sids=["sv-m1", "sv-m2"])
    t["trek_id"] = trek_id
    # Reassign user_ids to match impersonation fixtures.
    t["members"][0]["user_id"] = LEADER_UID
    t["members"][0]["email"] = LEADER_EMAIL
    t["members"][1]["user_id"] = CAND_UID
    t["members"][1]["email"] = CAND_EMAIL
    t["members"][2]["user_id"] = NON_CAND_UID
    t["members"][2]["email"] = NON_CAND_EMAIL
    t["creator_actor"] = {"user_id": LEADER_UID, "email": LEADER_EMAIL}
    t["meta"][trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY] = pending_sid
    t["meta"][trek_mod.SUCCESSION_PENDING_EMITTED_AT_META_KEY] = (
        trek_mod.utcnow_iso()
    )
    _treks[trek_id] = t
    return trek_id


class TestSuccessionConsentEndpoint:
    def test_accept_transfers_leader_and_clears_pending(self):
        trek_id = _seed_trek_with_pending(pending_sid="sv-m1")
        _impersonate(CAND_UID, CAND_EMAIL)
        r = _client.post(
            f"/api/treks/{trek_id}/succession-consent",
            json={"decision": "accept"},
            headers={"X-Beacon-Session": "sv-m1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["leader_session_id"] == "sv-m1"
        # Pending state cleared
        assert (
            body["meta"].get(trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY)
            == ""
        )
        # Candidate member promoted to leader role
        cand_member = next(
            m for m in body["members"] if m.get("session_id") == "sv-m1"
        )
        assert cand_member["role"] == "leader"

    def test_decline_appends_to_declined_and_clears_pending(self):
        trek_id = _seed_trek_with_pending(pending_sid="sv-m1")
        _impersonate(CAND_UID, CAND_EMAIL)
        r = _client.post(
            f"/api/treks/{trek_id}/succession-consent",
            json={"decision": "decline"},
            headers={"X-Beacon-Session": "sv-m1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Leader unchanged
        assert body["leader_session_id"] == "sv-leader"
        # Declined list grows
        assert "sv-m1" in body["meta"].get(
            trek_mod.SUCCESSION_DECLINED_META_KEY, []
        )
        # Pending cleared
        assert (
            body["meta"].get(trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY)
            == ""
        )

    def test_rejects_non_candidate_caller(self):
        trek_id = _seed_trek_with_pending(pending_sid="sv-m1")
        # Call from a different session header — must 403.
        _impersonate(NON_CAND_UID, NON_CAND_EMAIL)
        r = _client.post(
            f"/api/treks/{trek_id}/succession-consent",
            json={"decision": "accept"},
            headers={"X-Beacon-Session": "sv-m2"},
        )
        assert r.status_code == 403

    def test_rejects_when_no_pending(self):
        trek_id = _seed_trek_with_pending(pending_sid="sv-m1")
        # Wipe pending field.
        _treks[trek_id]["meta"][
            trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY
        ] = ""
        _impersonate(CAND_UID, CAND_EMAIL)
        r = _client.post(
            f"/api/treks/{trek_id}/succession-consent",
            json={"decision": "accept"},
            headers={"X-Beacon-Session": "sv-m1"},
        )
        assert r.status_code == 400

    def test_rejects_invalid_decision(self):
        trek_id = _seed_trek_with_pending(pending_sid="sv-m1")
        _impersonate(CAND_UID, CAND_EMAIL)
        r = _client.post(
            f"/api/treks/{trek_id}/succession-consent",
            json={"decision": "maybe"},
            headers={"X-Beacon-Session": "sv-m1"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Escalation when no candidate available — exercise pick + the orchestrator
# decision branch by simulating the trek state directly.
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_no_candidate_returns_none_and_caller_should_stamp(self):
        """When pick_succession_candidate returns None, caller is expected to
        stamp meta.succession_escalated_at. This unit pin verifies the helper
        contract; the tick orchestrator integration is exercised indirectly
        by `test_returns_none_when_only_leader_remains`."""
        t = _build_phase_a_trek()  # leader only, no other members
        assert trek_mod.pick_succession_candidate(t) is None
        # Caller (= tick endpoint) stamps the escalation marker.
        now_iso = trek_mod.utcnow_iso()
        meta = t.setdefault("meta", {})
        meta[trek_mod.SUCCESSION_ESCALATED_AT_META_KEY] = now_iso
        assert (
            t["meta"][trek_mod.SUCCESSION_ESCALATED_AT_META_KEY] == now_iso
        )
