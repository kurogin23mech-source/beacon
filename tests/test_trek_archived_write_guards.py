"""ms-95 / e-2875 — Server-side 410 Gone guards on archived Trek writes.

Pins the defense-in-depth server layer that closes the residual
peer-DM noise pathway observed on 2026-07-03 (tk-29a11d2f archive
dogfood: pulse-ack + peer DM traffic continued for 5-10 minutes
after the Trek was archived because the endpoints only checked
membership, not archive status).

The client-side inbox-hook drops archived-trek events before Skill
invocation (= layer 3-A). This server guard is layer 3-B: even a
pre-fix bridge that still surfaces the event and drives a Skill
invocation will be stopped at the API boundary.

Endpoints covered here are the ones the executor / leader call
during normal Trek operation:

  * POST   /api/treks/{trek_id}/pulse-ack       — /beacon-trek-pulse
  * PATCH  /api/treks/{trek_id}/task-state      — executor commit ack
  * POST   /api/treks/{trek_id}/task-add        — cross-project task add
  * POST   /api/treks/{trek_id}/kickoff         — kickoff DM completion
  * POST   /api/treks/{trek_id}/extend-ttl      — dispatch TTL push
  * POST   /api/treks/{trek_id}/session-heartbeat — session liveness

Read endpoints and the archive transition itself are exempt (=
covered by transition validator, not this guard).
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"
os.environ["BEACON_SCHEDULER_INTERNAL_KEY"] = "test-scheduler-key"

import firestore_client  # noqa: E402,F401
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


# ---------------------------------------------------------------------------
# Shared in-memory mock store (kept intentionally minimal — this test file
# only needs get_trek / save_trek + no-op append helpers so the endpoints
# reach the guard before touching any real backend).
# ---------------------------------------------------------------------------

_treks: dict[str, dict] = {}
_logs: dict[str, list[dict]] = {}
_bus_events: dict[str, list[dict]] = {}


def _mock_get_trek(trek_id: str):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id: str, data: dict):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    _treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}


def _mock_append_trek_log(trek_id: str, entry: dict) -> str:
    _logs.setdefault(trek_id, []).append(copy.deepcopy(entry))
    return f"log-{len(_logs[trek_id]) - 1}"


def _mock_list_trek_logs(trek_id: str, *, limit=100, since=""):
    return list(_logs.get(trek_id) or [])


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _bus_events.setdefault(project_id, []).append(copy.deepcopy(data))
    return f"evt-{project_id}-{len(_bus_events[project_id]) - 1}"


def _mock_list_sessions(project_id: str):
    return []


def _mock_get_project(project_id: str):
    return {"milestones": [], "operations": []}


def _mock_list_treks(actor_id=None, *, status=None, include_archived=False):
    out = []
    for t in _treks.values():
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        out.append(copy.deepcopy(t))
    return out


@pytest.fixture(autouse=True)
def _rebind_db():
    db_module = app_module.db
    prior = {}
    binds = [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("append_trek_log", _mock_append_trek_log),
        ("list_trek_logs", _mock_list_trek_logs),
        ("append_bus_event", _mock_append_bus_event),
        ("list_sessions", _mock_list_sessions),
        ("get_project", _mock_get_project),
    ]
    for name, mock in binds:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _logs.clear()
    _bus_events.clear()
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)


def _seed_trek(*, status: str = "archived",
               leader_sid: str = "sv-leader") -> dict:
    t = {
        "trek_id": "tk-archived01",
        "title": "archived-guard test",
        "description": "",
        "type": "persistent",
        "status": status,
        "creator_actor": {"user_id": "dev", "email": "dev@local"},
        "leader_session_id": leader_sid,
        "members": [{
            "user_id": "dev", "email": "dev@local",
            "role": "leader", "invited_at": "2026-07-03T00:00:00.000000Z",
            "joined_at": "2026-07-03T00:00:00.000000Z",
            "invited_by": "dev",
            "session_id": leader_sid,
        }],
        "scope": [{"project": "p1", "milestone": "ms-1"}],
        "halt": None,
        "goal_state": "",
        "meta": {},
        "pending_scope_ops": [],
        # Pre-fill so pulse-ack doesn't trip on kickoff_required first.
        "kickoff_status": {
            leader_sid: {
                "session_id": leader_sid,
                "user_id": "dev",
                "pending": False,
                "sent_at": "2026-07-03T00:00:00.000000Z",
                "kickoff_dm_event_id": "evt-mock-kickoff",
            },
        },
        "task_states": {},
        "created_at": "2026-07-03T00:00:00.000000Z",
        "updated_at": "2026-07-03T00:00:00.000000Z",
        "archived_at": (
            "2026-07-03T22:55:00.000000Z" if status == "archived" else None
        ),
    }
    _treks[t["trek_id"]] = copy.deepcopy(t)
    return t


# ---------------------------------------------------------------------------
# 410 Gone on archived Trek — one test per guarded endpoint.
# ---------------------------------------------------------------------------

class TestArchivedTrekReturns410:
    def test_pulse_ack_on_archived_trek_returns_410(self):
        _seed_trek(status="archived")
        resp = client.post(
            "/api/treks/tk-archived01/pulse-ack",
            json={"session_id": "sv-leader", "picked_choice": "continue"},
        )
        assert resp.status_code == 410, resp.text
        assert "archived" in resp.json()["detail"].lower()
        # Guard fires BEFORE any state mutation → no log row written.
        assert _logs.get("tk-archived01") in (None, [])

    def test_task_state_on_archived_trek_returns_410(self):
        _seed_trek(status="archived")
        resp = client.patch(
            "/api/treks/tk-archived01/task-state",
            json={"task_id": "e-1", "state": "working"},
        )
        assert resp.status_code == 410, resp.text
        assert "archived" in resp.json()["detail"].lower()

    def test_task_add_on_archived_trek_returns_410(self):
        _seed_trek(status="archived")
        resp = client.post(
            "/api/treks/tk-archived01/task-add",
            json={
                "target_project": "p1",
                "target_milestone": "ms-1",
                "description": "should not land",
            },
        )
        assert resp.status_code == 410, resp.text
        assert "archived" in resp.json()["detail"].lower()

    def test_kickoff_on_archived_trek_returns_410(self):
        _seed_trek(status="archived")
        resp = client.post(
            "/api/treks/tk-archived01/kickoff",
            json={"session_id": "sv-leader",
                  "kickoff_dm_event_id": "evt-x"},
        )
        assert resp.status_code == 410, resp.text
        assert "archived" in resp.json()["detail"].lower()

    def test_extend_ttl_on_archived_trek_returns_410(self):
        _seed_trek(status="archived")
        resp = client.post(
            "/api/treks/tk-archived01/extend-ttl",
            json={"task_id": "e-1", "minutes": 10},
        )
        assert resp.status_code == 410, resp.text
        assert "archived" in resp.json()["detail"].lower()

    def test_session_heartbeat_on_archived_trek_returns_410(self):
        _seed_trek(status="archived")
        resp = client.post(
            "/api/treks/tk-archived01/session-heartbeat",
            json={"session_id": "sv-leader"},
        )
        assert resp.status_code == 410, resp.text
        assert "archived" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Regression: active Trek writes must still succeed after the guard.
# ---------------------------------------------------------------------------

class TestActiveTrekStillWorks:
    def test_pulse_ack_on_active_trek_succeeds(self):
        _seed_trek(status="active")
        resp = client.post(
            "/api/treks/tk-archived01/pulse-ack",
            json={"session_id": "sv-leader", "picked_choice": "continue"},
        )
        assert resp.status_code == 200, resp.text

    def test_session_heartbeat_on_active_trek_succeeds(self):
        _seed_trek(status="active")
        resp = client.post(
            "/api/treks/tk-archived01/session-heartbeat",
            json={"session_id": "sv-leader"},
        )
        assert resp.status_code == 200, resp.text
