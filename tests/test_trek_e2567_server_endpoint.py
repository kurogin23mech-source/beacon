"""Integration + lib tests for ms-97 / e-2567 server endpoint changes.

Covers:
- AC13: leader-only state-machine transitions enforced server-side
- AC16/AC17: members-iterate cross-project fanout (= scope[0]-固定の脱却)
- AC23: scope-add approval flow (= POST /scope-add → pending →
  POST /scope-approve)
- AC32: halt skip — halted treks fire no tick events
- pending_scope lib helpers (= mint / add / list / approve / cancel)

These pin the SPEC AC contracts so Phase 1a dogfood can verify the
land in a deterministic way.
"""
from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
os.environ.setdefault("BEACON_SCHEDULER_INTERNAL_KEY", "test-scheduler-key")

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db

import trek as trek_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Lib helpers — pending scope additions (AC23)
# ---------------------------------------------------------------------------

def _fresh_trek(scope=None):
    """Build a minimal trek doc for lib-level tests."""
    return {
        "trek_id": "tk-test01",
        "title": "test",
        "status": "active",
        "scope": list(scope or []),
        "members": [],
        "halt": None,
        "meta": {},
        "creator_actor": {"user_id": "uid-a", "email": "a@b"},
    }


def test_mint_pending_scope_id_format():
    sid = trek_mod.mint_pending_scope_id()
    assert sid.startswith("sa-") and len(sid) == 11  # sa- + 8 hex


def test_add_pending_scope_basic():
    t = _fresh_trek()
    pid = trek_mod.add_pending_scope(
        t,
        entry={"project": "p1", "milestone": "ms-1"},
        requested_by_session_id="sv-1",
        requested_by_user_id="uid-1",
    )
    assert pid.startswith("sa-")
    pending = trek_mod.list_pending_scope(t)
    assert len(pending) == 1
    assert pending[0]["pending_id"] == pid
    assert pending[0]["entry"] == {"project": "p1", "milestone": "ms-1"}
    assert pending[0]["requested_by_session_id"] == "sv-1"
    assert pending[0]["requested_by_user_id"] == "uid-1"
    assert pending[0]["requested_at"]


def test_add_pending_scope_rejects_project_wide():
    """AC7/AC23: strict normalisation runs at request time."""
    t = _fresh_trek()
    with pytest.raises(ValueError, match="project-wide"):
        trek_mod.add_pending_scope(
            t,
            entry={"project": "p1"},  # no narrowing key
            requested_by_session_id="sv-1",
            requested_by_user_id="uid-1",
        )


def test_add_pending_scope_dedup_against_committed():
    t = _fresh_trek(scope=[{"project": "p1", "milestone": "ms-1"}])
    with pytest.raises(ValueError, match="already committed"):
        trek_mod.add_pending_scope(
            t,
            entry={"project": "p1", "milestone": "ms-1"},
            requested_by_session_id="sv-1",
            requested_by_user_id="uid-1",
        )


def test_add_pending_scope_dedup_against_pending():
    t = _fresh_trek()
    trek_mod.add_pending_scope(
        t, entry={"project": "p1", "milestone": "ms-1"},
        requested_by_session_id="sv-1", requested_by_user_id="uid-1",
    )
    with pytest.raises(ValueError, match="already pending"):
        trek_mod.add_pending_scope(
            t, entry={"project": "p1", "milestone": "ms-1"},
            requested_by_session_id="sv-2", requested_by_user_id="uid-2",
        )


def test_approve_pending_scope_commits_to_scope():
    t = _fresh_trek()
    pid = trek_mod.add_pending_scope(
        t, entry={"project": "p1", "milestone": "ms-1"},
        requested_by_session_id="sv-1", requested_by_user_id="uid-1",
    )
    committed = trek_mod.approve_pending_scope(
        t, pending_id=pid,
        approved_by_session_id="sv-2", approved_by_user_id="uid-2",
    )
    assert committed == {"project": "p1", "milestone": "ms-1"}
    assert t["scope"] == [{"project": "p1", "milestone": "ms-1"}]
    assert trek_mod.list_pending_scope(t) == []


def test_approve_pending_scope_unknown_id():
    t = _fresh_trek()
    with pytest.raises(ValueError, match="not found"):
        trek_mod.approve_pending_scope(
            t, pending_id="sa-deadbeef",
            approved_by_session_id="sv-1", approved_by_user_id="uid-1",
        )


def test_approve_pending_scope_race_with_direct_add():
    t = _fresh_trek()
    pid = trek_mod.add_pending_scope(
        t, entry={"project": "p1", "milestone": "ms-1"},
        requested_by_session_id="sv-1", requested_by_user_id="uid-1",
    )
    # Simulate someone calling PUT /scope between request and approve.
    trek_mod.add_scope_entry(t, entry={"project": "p1", "milestone": "ms-1"})
    with pytest.raises(ValueError, match="already committed"):
        trek_mod.approve_pending_scope(
            t, pending_id=pid,
            approved_by_session_id="sv-2", approved_by_user_id="uid-2",
        )


def test_cancel_pending_scope():
    t = _fresh_trek()
    pid = trek_mod.add_pending_scope(
        t, entry={"project": "p1", "milestone": "ms-1"},
        requested_by_session_id="sv-1", requested_by_user_id="uid-1",
    )
    assert trek_mod.cancel_pending_scope(t, pending_id=pid) is True
    assert trek_mod.list_pending_scope(t) == []
    # Idempotent: second cancel is a no-op.
    assert trek_mod.cancel_pending_scope(t, pending_id=pid) is False


# ---------------------------------------------------------------------------
# HTTP integration tests — in-memory store mocks
# ---------------------------------------------------------------------------

_treks: dict[str, dict] = {}
_bus_events_by_project: dict[str, list[dict]] = {}
_sessions_by_project: dict[str, list[dict]] = {}


def _mock_list_sessions(project_id: str):
    return copy.deepcopy(_sessions_by_project.get(project_id, []))


def _mock_list_projects():
    return list(_sessions_by_project.keys())


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


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    bus = _bus_events_by_project.setdefault(project_id, [])
    event_id = f"ev-{project_id}-{len(bus)}"
    bus.append({"event_id": event_id, **copy.deepcopy(data)})
    return event_id


@pytest.fixture(autouse=True)
def _rebind_db():
    db_module = app_module.db
    prior = {}
    binds = [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("append_bus_event", _mock_append_bus_event),
        ("list_sessions", _mock_list_sessions),
        ("list_projects", _mock_list_projects),
    ]
    for name, mock in binds:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _bus_events_by_project.clear()
    _sessions_by_project.clear()
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)
HEADERS_SCHED = {"X-Beacon-Scheduler-Key": "test-scheduler-key"}


def _seed_trek(**overrides):
    # _auth_enabled=False makes require_auth return user.sub="dev"; the
    # membership check still runs, so the seed includes "dev" as the
    # default leader/member. Tests that need a non-dev caller use the
    # X-Beacon-Session header (= session_id grain) for the leader hard-
    # check; user grain stays uniformly "dev".
    base = {
        "trek_id": "tk-e2567a",
        "title": "e2567 test",
        "type": "persistent",
        "status": "active",
        "scope": [{"project": "p-home", "milestone": "ms-1"}],
        "members": [{
            "user_id": "dev", "email": "dev@local",
            "role": "leader", "joined_at": "2026-06-27T00:00:00.000000Z",
            "invited_at": "2026-06-27T00:00:00.000000Z",
            "invited_by": "dev",
        }],
        "leader_session_id": "sv-leader",
        "creator_actor": {"user_id": "dev", "email": "dev@local"},
        "halt": None,
        "goal_state": "",
        "meta": {"cadence_minutes": 10},
        "task_states": {},
        "session_history": [],
        "created_at": "2026-06-27T00:00:00.000000Z",
        "updated_at": "2026-06-27T00:00:00.000000Z",
        "archived_at": None,
    }
    base.update(overrides)
    _treks[base["trek_id"]] = copy.deepcopy(base)
    return _treks[base["trek_id"]]


def _seed_live_session(project_id: str, session_id: str, user_id: str):
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project.setdefault(project_id, []).append(
        {"session_id": session_id, "user_id": user_id, "last_active": now_iso}
    )


# ---------------------------------------------------------------------------
# AC32 — halt skip
# ---------------------------------------------------------------------------

def test_halted_trek_skips_all_tick_phases():
    """AC32: trek.halt non-null → cadence-fire / idle-escalation / auto-stall
    が全停止する (= candidate_treks レベルで切る)。"""
    _seed_trek(
        trek_id="tk-halt001",
        halt={
            "issued_at": "2026-06-27T01:00:00.000000Z",
            "issued_by_session_id": "sv-leader",
            "reason": "test halt",
        },
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-halt001"]},
        headers=HEADERS_SCHED,
    )
    assert resp.status_code == 200
    body = resp.json()
    # candidate_treks 自体から除外されるので candidates=0
    assert body["candidates"] == 0
    assert body["fired"] == []
    assert body["escalations"] == []
    assert body["auto_stalled"] == []
    # No bus event of any kind landed for this trek's project.
    assert _bus_events_by_project.get("p-home", []) == []


# ---------------------------------------------------------------------------
# AC13 — leader hard-check on state-machine API
# ---------------------------------------------------------------------------

def test_non_leader_cannot_transition_from_leader_review():
    """AC13: leader_review → done を non-leader session が試すと 403 reject。"""
    t = _seed_trek(
        trek_id="tk-stm001",
        task_states={
            "e-1": {
                "state": "leader_review",
                "updated_by_session_id": "sv-exec",
                "updated_at": "2026-06-27T01:00:00.000000Z",
            },
        },
    )
    # Call as a non-leader session (X-Beacon-Session: sv-exec ≠ leader sv-leader)
    resp = client.patch(
        f"/api/treks/{t['trek_id']}/task-state",
        json={"task_id": "e-1", "state": "done", "note": "executor self-approve"},
        headers={"X-Beacon-Session": "sv-exec"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "leader_review" in detail
    assert "trek.leader_session_id" in detail


def test_leader_session_can_transition_from_leader_review():
    """AC13: same transition by the leader session is allowed (= 200)."""
    t = _seed_trek(
        trek_id="tk-stm002",
        task_states={
            "e-1": {
                "state": "leader_review",
                "updated_by_session_id": "sv-exec",
                "updated_at": "2026-06-27T01:00:00.000000Z",
            },
        },
    )
    resp = client.patch(
        f"/api/treks/{t['trek_id']}/task-state",
        json={"task_id": "e-1", "state": "done", "note": "leader approve"},
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert resp.status_code == 200, resp.text
    assert _treks["tk-stm002"]["task_states"]["e-1"]["state"] == "done"


def test_working_to_leader_review_executor_self_declaration_allowed():
    """Non-leader transitions (e.g. working → leader_review) stay unrestricted
    — the leader hard-check only fires on transitions FROM leader_review /
    user_review. Executor self-declaration is the canonical happy path."""
    t = _seed_trek(
        trek_id="tk-stm003",
        task_states={
            "e-1": {
                "state": "working",
                "updated_by_session_id": "sv-exec",
                "updated_at": "2026-06-27T01:00:00.000000Z",
            },
        },
    )
    resp = client.patch(
        f"/api/treks/{t['trek_id']}/task-state",
        json={
            "task_id": "e-1",
            "state": "leader_review",
            "note": "executor needs review",
        },
        headers={"X-Beacon-Session": "sv-exec"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# AC23 — scope-add approval flow endpoints
# ---------------------------------------------------------------------------

def test_scope_add_endpoint_enqueues_pending():
    t = _seed_trek(trek_id="tk-scope01")
    resp = client.post(
        f"/api/treks/{t['trek_id']}/scope-add",
        json={
            "project": "p-home",
            "milestone": "ms-2",
            "requested_by_session_id": "sv-exec",
        },
        headers={"X-Beacon-Session": "sv-exec"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_id"].startswith("sa-")
    # scope[] not yet committed.
    assert _treks["tk-scope01"]["scope"] == [
        {"project": "p-home", "milestone": "ms-1"},
    ]
    pending = _treks["tk-scope01"]["pending_scope_additions"]
    assert len(pending) == 1
    assert pending[0]["pending_id"] == body["pending_id"]


def test_scope_add_endpoint_rejects_project_wide():
    """AC7+AC23: strict reject at request time."""
    t = _seed_trek(trek_id="tk-scope02")
    resp = client.post(
        f"/api/treks/{t['trek_id']}/scope-add",
        json={"project": "p-home", "requested_by_session_id": "sv-exec"},
        headers={"X-Beacon-Session": "sv-exec"},
    )
    assert resp.status_code == 400
    assert "project-wide" in resp.json()["detail"]


def test_scope_pending_list_endpoint():
    t = _seed_trek(trek_id="tk-scope03")
    client.post(
        f"/api/treks/{t['trek_id']}/scope-add",
        json={
            "project": "p-home", "milestone": "ms-2",
            "requested_by_session_id": "sv-exec",
        },
        headers={"X-Beacon-Session": "sv-exec"},
    )
    resp = client.get(f"/api/treks/{t['trek_id']}/scope-pending")
    assert resp.status_code == 200
    pending = resp.json()
    assert isinstance(pending, list)
    assert len(pending) == 1
    assert pending[0]["entry"] == {"project": "p-home", "milestone": "ms-2"}


def test_scope_approve_endpoint_commits_pending():
    t = _seed_trek(trek_id="tk-scope04")
    add_resp = client.post(
        f"/api/treks/{t['trek_id']}/scope-add",
        json={
            "project": "p-home", "milestone": "ms-2",
            "requested_by_session_id": "sv-exec",
        },
        headers={"X-Beacon-Session": "sv-exec"},
    )
    pending_id = add_resp.json()["pending_id"]
    resp = client.post(
        f"/api/treks/{t['trek_id']}/scope-approve",
        json={"pending_id": pending_id},
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert resp.status_code == 200, resp.text
    trek_after = resp.json()
    scopes = trek_after["scope"]
    assert {"project": "p-home", "milestone": "ms-2"} in scopes
    assert trek_after.get("pending_scope_additions", []) == []


def test_scope_approve_unknown_pending_id_404():
    t = _seed_trek(trek_id="tk-scope05")
    resp = client.post(
        f"/api/treks/{t['trek_id']}/scope-approve",
        json={"pending_id": "sa-deadbeef"},
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert resp.status_code == 404


def test_scope_pending_cancel_endpoint():
    t = _seed_trek(trek_id="tk-scope06")
    add_resp = client.post(
        f"/api/treks/{t['trek_id']}/scope-add",
        json={
            "project": "p-home", "milestone": "ms-2",
            "requested_by_session_id": "sv-exec",
        },
        headers={"X-Beacon-Session": "sv-exec"},
    )
    pending_id = add_resp.json()["pending_id"]
    resp = client.delete(
        f"/api/treks/{t['trek_id']}/scope-pending/{pending_id}",
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert resp.status_code == 200
    trek_after = resp.json()
    assert trek_after.get("pending_scope_additions", []) == []
    assert trek_after["scope"] == [
        {"project": "p-home", "milestone": "ms-1"},
    ]


def test_legacy_put_scope_still_immediate_commit():
    """PUT /scope は legacy 直書き経路として維持される (= leader 専用 shortcut)。
    新規 scope-add 経路は default で pending を経由するが、 既存 CLI は
    壊さない。"""
    t = _seed_trek(trek_id="tk-scope07")
    resp = client.put(
        f"/api/treks/{t['trek_id']}/scope",
        json={"project": "p-home", "milestone": "ms-9"},
    )
    assert resp.status_code == 200, resp.text
    assert any(
        s.get("milestone") == "ms-9" for s in _treks["tk-scope07"]["scope"]
    )


# ---------------------------------------------------------------------------
# AC16/AC17 — cross-project fanout via members iterate
# ---------------------------------------------------------------------------

def test_cross_project_fanout_reaches_member_in_scope1():
    """AC16/AC17: trek scope spans 2 projects; member session lives in
    scope[1] project. Pre-fix the fanout dropped it; new fanout reaches it
    via the cross-project session index."""
    # Trek scope: [p-home (ms-1), p-side (ms-2)] — leader in p-home,
    # executor in p-side.
    _seed_trek(
        trek_id="tk-crossfan",
        scope=[
            {"project": "p-home", "milestone": "ms-1"},
            {"project": "p-side", "milestone": "ms-2"},
        ],
        members=[
            {
                "user_id": "uid-leader", "session_id": "sv-leader",
                "email": "a@b", "role": "leader",
                "joined_at": "2026-06-27T00:00:00.000000Z",
                "invited_at": "2026-06-27T00:00:00.000000Z",
                "invited_by": "uid-leader",
            },
            {
                "user_id": "uid-exec", "session_id": "sv-exec-side",
                "email": "c@d", "role": "member",
                "joined_at": "2026-06-27T00:00:00.000000Z",
                "invited_at": "2026-06-27T00:00:00.000000Z",
                "invited_by": "uid-leader",
            },
        ],
        leader_session_id="sv-leader",
    )
    _seed_live_session("p-home", "sv-leader", "uid-leader")
    _seed_live_session("p-side", "sv-exec-side", "uid-exec")
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-crossfan"]},
        headers=HEADERS_SCHED,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["fired"]) == 1
    fired = body["fired"][0]
    # progress-check should land in p-side (= executor's home project),
    # not p-home (= old scope[0] behaviour).
    assert "p-side" in fired["recipient_projects"]
    # leader-digest goes into leader's home project (p-home).
    p_home_events = _bus_events_by_project.get("p-home", [])
    digest_channels = [e["channel"] for e in p_home_events]
    assert "trek-leader-digest" in digest_channels
    # Executor sees the progress-check in their own project's bus.
    p_side_events = _bus_events_by_project.get("p-side", [])
    side_channels = [e["channel"] for e in p_side_events]
    assert "trek-progress-check" in side_channels
    # The progress-check is addressed to the executor's session_id.
    progress_events = [
        e for e in p_side_events if e["channel"] == "trek-progress-check"
    ]
    sids = [e["payload"].get("recipient_session_id") for e in progress_events]
    assert "sv-exec-side" in sids
