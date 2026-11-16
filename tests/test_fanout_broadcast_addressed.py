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


# ms-185 e-7070: broadcast fallback は Trek の定期集約 (``_run_trek_tick``) が
# 唯一の発生源だったので、それを駆動していたテストを落とした。
#
# **残した 2 つは Trek 非依存**: ``_bus_event_addressed_to`` が「DM + recipient が
# stamp 済」を受け、「DM + recipient 未設定」を落とすこと = DM 配送の宛先判定
# そのもので、すべての DM 経路が通る。Trek が無くなっても守る価値は変わらない。
