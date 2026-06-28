"""ms-97 / e-2660 — broadcast-fallback DM delivery (Bug B).

旧 broadcast fallback は ``channel="dm"`` で post しつつ payload に
``recipient_session_id`` を stamp せず、 _bus_event_addressed_to が
「DM + recipient 未設定」を drop する穴があった (= bus storage には
書かれるが bridge へ届かない、 「fired at minimum once」 SPEC hole)。
fix: stamped leader_session_id を recipient に stamp して配送可能化。
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


_treks: dict[str, dict] = {}
_bus_events_by_project: dict[str, list[dict]] = {}
_sessions_by_project: dict[str, list[dict]] = {}
_projects: dict[str, dict] = {}


@pytest.fixture(autouse=True)
def _rebind_db():
    db_module = app_module.db
    prior: dict = {}
    binds = [
        ("get_trek", lambda tid: copy.deepcopy(_treks.get(tid))
         if tid in _treks else None),
        ("save_trek", lambda tid, data: _treks.update(
            {tid: {**copy.deepcopy({k: v for k, v in data.items()
                                    if k != "trek_id"}),
                   "trek_id": tid}})),
        ("list_treks", lambda actor_id=None, *, status=None,
         include_archived=False: [
            copy.deepcopy(t) for t in _treks.values()
            if (include_archived or t.get("status") != "archived")
            and (not status or t.get("status") == status)
         ]),
        ("append_bus_event", lambda pid, data: (
            lambda eid: (
                _bus_events_by_project.setdefault(pid, []).append(
                    {"event_id": eid, **copy.deepcopy(data)}
                ) or eid
            )
        )(f"ev-{pid}-{len(_bus_events_by_project.get(pid, []))}")),
        ("list_sessions", lambda pid: copy.deepcopy(
            _sessions_by_project.get(pid, [])
        )),
        ("get_project", lambda pid: copy.deepcopy(_projects.get(pid))),
        ("list_projects", lambda user_id="", include_archived=False: [
            {"project_id": pid, "name": data.get("name", "")}
            for pid, data in _projects.items()
        ]),
    ]
    for name, mock in binds:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _bus_events_by_project.clear()
    _sessions_by_project.clear()
    _projects.clear()
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)
HEADERS_OK = {"X-Beacon-Scheduler-Key": "test-scheduler-key"}


def _is_progress_check(e: dict) -> bool:
    if e.get("channel") != "dm":
        return False
    payload = e.get("payload") or {}
    return payload.get("origin_channel") == "trek-progress-check"


def test_broadcast_fallback_sets_recipient_session_id_to_leader():
    """Broadcast fallback DM must carry recipient_session_id=leader_sid.

    Without this stamp, _bus_event_addressed_to drops the event silently
    (= channel="dm" + no recipient → not deliverable). With the fix the
    broadcast fallback lands as a leader-addressed DM.
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _projects["proj-leader"] = {"name": "proj-leader"}
    # Phase A+ trek with only a leader member (= no executor target
    # forces the broadcast fallback path).
    _treks["tk-bcast-1"] = {
        "trek_id": "tk-bcast-1",
        "title": "broadcast fallback test",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": "uid-leader", "email": ""},
        "leader_session_id": "sv-leader-bcast",
        "members": [
            {"user_id": "uid-leader", "session_id": "sv-leader-bcast",
             "role": "leader", "joined_at": now_iso},
        ],
        "scope": [{"project": "proj-leader", "milestone": "ms-x"}],
        "halt": None,
        "goal_state": "",
        "meta": {"cadence_minutes": 10, "migration_phase": "A"},
        "task_states": {"e-todo1": {"state": "todo"}},
        "created_at": now_iso,
        "updated_at": now_iso,
        "archived_at": None,
    }
    _sessions_by_project["proj-leader"] = [
        {"session_id": "sv-leader-bcast", "user_id": "uid-leader",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-bcast-1"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    bus = _bus_events_by_project.get("proj-leader", [])
    progress = [e for e in bus if _is_progress_check(e)]
    assert len(progress) == 1, (
        f"expected 1 broadcast fallback event, got {len(progress)}: {bus}"
    )
    payload = progress[0]["payload"]
    assert payload.get("recipient_session_id") == "sv-leader-bcast", (
        "broadcast fallback must stamp recipient_session_id=leader_sid"
    )


def test_bus_event_addressed_to_accepts_fixed_broadcast_event():
    """_bus_event_addressed_to(event, leader_sid) returns True for fix."""
    leader_sid = "sv-leader-deliv"
    event = {
        "channel": "dm",
        "sender_session_id": "",
        "payload": {
            "origin_channel": "trek-progress-check",
            "sender_type": "trek-scheduler",
            "recipient_session_id": leader_sid,
        },
    }
    assert app_module._bus_event_addressed_to(event, leader_sid) is True


def test_bus_event_addressed_to_drops_old_unstamped_broadcast_event():
    """Regression: pre-fix shape (no recipient_session_id) still drops.

    Documents the structural reason Bug B existed. If this assertion
    flips, the routing filter has changed and broadcasts need re-review.
    """
    event = {
        "channel": "dm",
        "sender_session_id": "",
        "payload": {
            "origin_channel": "trek-progress-check",
            "sender_type": "trek-scheduler",
        },
    }
    assert app_module._bus_event_addressed_to(event, "sv-any") is False
