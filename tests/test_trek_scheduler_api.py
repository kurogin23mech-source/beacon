"""Integration tests for the Trek scheduler HTTP endpoint (ms-83 / e-1997).

The pure cadence logic is unit-tested in test_trek_scheduler.py; this
file exercises the HTTP wiring on /api/system/trek-scheduler/tick:

  (1) wrong / missing scheduler key → 403
  (2) tick with no active treks → empty fired list
  (3) due trek + scope → fires, writes bus event with T1-system
      envelope on trek-progress-check channel, stamps
      meta.last_progress_check_at on the trek doc
  (4) trek without scope → recorded in errors, not fired
  (5) immediate second tick → not refire (= cadence still elapsing)
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
# Use a stable scheduler key so the test predates production secret rotation.
os.environ["BEACON_SCHEDULER_INTERNAL_KEY"] = "test-scheduler-key"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


_treks: dict[str, dict] = {}
_bus_events_by_project: dict[str, list[dict]] = {}


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
    event_id = f"ev-{len(bus)}"
    bus.append({"event_id": event_id, **copy.deepcopy(data)})
    return event_id


@pytest.fixture(autouse=True)
def _rebind_db():
    """Bind in-memory mocks before each test, restore after."""
    db_module = app_module.db
    prior = {}
    binds = [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("append_bus_event", _mock_append_bus_event),
    ]
    for name, mock in binds:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _bus_events_by_project.clear()
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_trek(*,
               trek_id: str,
               status: str = "active",
               cadence: int = 10,
               last_at: str = "",
               scope: list[dict] | None = None) -> dict:
    """Insert a trek into the in-memory store and return it."""
    t = {
        "trek_id": trek_id,
        "title": "test trek",
        "description": "",
        "type": "persistent",
        "status": status,
        "creator_actor": {"user_id": "uid-leader", "email": "a@b.com"},
        "leader_session_id": "sv-leader",
        "members": [{
            "user_id": "uid-leader", "email": "a@b.com",
            "role": "leader", "invited_at": "2026-06-18T00:00:00.000000Z",
            "joined_at": "2026-06-18T00:00:00.000000Z", "invited_by": "uid-leader",
        }],
        "scope": scope or [],
        "halt": None,
        "goal_state": "",
        "meta": {"cadence_minutes": cadence},
        "created_at": "2026-06-18T00:00:00.000000Z",
        "updated_at": "2026-06-18T00:00:00.000000Z",
        "archived_at": None,
    }
    if last_at:
        t["meta"]["last_progress_check_at"] = last_at
    _treks[trek_id] = copy.deepcopy(t)
    return t


# ---------------------------------------------------------------------------
# (1) Auth gate
# ---------------------------------------------------------------------------

def test_tick_missing_scheduler_key_403():
    resp = client.post("/api/system/trek-scheduler/tick", json={})
    assert resp.status_code == 403


def test_tick_wrong_scheduler_key_403():
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={},
        headers={"X-Beacon-Scheduler-Key": "wrong"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# (2) Empty backend
# ---------------------------------------------------------------------------

def test_tick_no_active_treks_returns_empty_fired():
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == 0
    assert body["fired"] == []
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# (3) Due trek fires; envelope + bus event + last_progress_check_at stamped
# ---------------------------------------------------------------------------

def test_due_trek_fires_t1_system_envelope_on_progress_check_channel():
    _seed_trek(
        trek_id="tk-aaaa1111",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-83"}],
        # No last_at → never fired → due.
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-aaaa1111"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates"] == 1
    assert body["due"] == 1
    assert len(body["fired"]) == 1
    assert body["fired"][0]["trek_id"] == "tk-aaaa1111"
    assert body["fired"][0]["project_id"] == "beacon-test"

    # Bus event landed with the right channel + envelope tier + delivery.
    events = _bus_events_by_project["beacon-test"]
    assert len(events) == 1
    ev = events[0]
    assert ev["channel"] == "trek-progress-check"
    assert ev["delivery"] == "auto-execute"
    assert ev["envelope"]["tier"] == "T1-system"
    assert ev["envelope"]["issuer"] == "beacon-system"
    assert ev["envelope"]["scope"] == "trek:tk-aaaa1111"
    assert ev["envelope"]["actions_authorized"] == ["trek.progress_check"]
    assert ev["payload"]["trek_id"] == "tk-aaaa1111"
    assert ev["payload"]["kind"] == "trek-progress-check"

    # Trek's last_progress_check_at was stamped.
    saved = _treks["tk-aaaa1111"]
    assert saved["meta"]["last_progress_check_at"]


# ---------------------------------------------------------------------------
# (4) Trek without scope → recorded in errors, not fired
# ---------------------------------------------------------------------------

def test_empty_scope_trek_lands_in_errors_not_fired():
    _seed_trek(
        trek_id="tk-bbbb2222",
        status="active",
        scope=[],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-bbbb2222"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fired"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["trek_id"] == "tk-bbbb2222"


# ---------------------------------------------------------------------------
# (5) 10-min cadence: tick at t=0 fires, immediate tick at t≈0 does NOT
# ---------------------------------------------------------------------------

def test_idle_trek_fires_escalation_on_notify_channel():
    """Trek that hasn't responded for cadence × 3 → escalation DM lands
    on notify channel with kind=trek-idle-escalation."""
    _seed_trek(
        trek_id="tk-idle0000",
        status="active",
        cadence=10,
        # last_progress_check fired 60 min ago — well past 30-min idle gate.
        last_at="2026-06-18T00:00:00.000000Z",
        scope=[{"project": "beacon-test", "milestone": "ms-83"}],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-idle0000"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    body = resp.json()
    # Trek is due AND idle on this tick. The tick fires both progress
    # check (because cadence elapsed) and escalation (because idle).
    assert len(body["escalations"]) == 1
    esc = body["escalations"][0]
    assert esc["trek_id"] == "tk-idle0000"

    events = _bus_events_by_project["beacon-test"]
    # One trek-progress-check (cadence due) + one notify (idle).
    channels = [e["channel"] for e in events]
    assert "trek-progress-check" in channels
    assert "notify" in channels
    notify_event = [e for e in events if e["channel"] == "notify"][0]
    assert notify_event["payload"]["kind"] == "trek-idle-escalation"
    assert notify_event["delivery"] == "notify-user-only"


def test_session_heartbeat_stamps_last_response_at():
    """Heartbeat endpoint records the latest activity timestamp."""
    _seed_trek(
        trek_id="tk-hb000000",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-83"}],
    )
    resp = client.post(
        "/api/treks/tk-hb000000/session-heartbeat",
        json={"session_id": "sv-leader"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trek_id"] == "tk-hb000000"
    assert body["last_session_response_at"]
    # Trek doc has been stamped.
    saved = _treks["tk-hb000000"]
    assert saved["meta"]["last_session_response_at"]
    assert saved["meta"]["last_session_response_session_id"] == "sv-leader"


def test_idle_escalation_cooldown_prevents_refire_within_30min():
    """Idle trek already escalated 5 minutes ago → no refire this tick."""
    _seed_trek(
        trek_id="tk-cooldown0",
        status="active",
        cadence=10,
        last_at="2026-06-18T00:00:00.000000Z",
        scope=[{"project": "beacon-test", "milestone": "ms-83"}],
    )
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = (now - datetime.timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _treks["tk-cooldown0"]["meta"]["last_idle_escalation_at"] = recent
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-cooldown0"]},
        headers=HEADERS_OK,
    )
    body = resp.json()
    assert body["escalations"] == []


def test_10min_cadence_does_not_refire_immediately():
    _seed_trek(
        trek_id="tk-cccc3333",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-83"}],
    )
    # First tick — fires.
    r1 = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-cccc3333"]},
        headers=HEADERS_OK,
    )
    assert r1.json()["due"] == 1
    # Second tick (= same wall-clock minute) — does NOT refire because
    # last_progress_check_at is now ~0 sec ago, cadence=10 min.
    r2 = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-cccc3333"]},
        headers=HEADERS_OK,
    )
    assert r2.status_code == 200
    assert r2.json()["due"] == 0
    # Only one bus event landed.
    events = _bus_events_by_project["beacon-test"]
    assert len(events) == 1
