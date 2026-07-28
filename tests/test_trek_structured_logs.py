"""ms-97 / Phase 7-C / AC26 + AC27 — structured logs subcollection (e-2603).

AC26 — Tick payload + pulse-ack response + outcome triple are persisted
under the Firestore subcollection ``treks/{trek_id}/logs/{log_id}`` with
indefinite retention (= MVP).

AC27 — Log read RBAC follows the existing Trek member check, and a bulk
NDJSON export endpoint ``GET /api/treks/{trek_id}/logs.jsonl`` streams
the rows ordered by ``created_at``.

This file pins the firestore-shaped store helpers + the three write
points (tick / pulse-ack / task-state terminal) + the NDJSON endpoint.
Same harness as test_trek_scope_pending_approval.py (= mocked store
methods, FastAPI TestClient with auth off).
"""

from __future__ import annotations

import copy
import json
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
# Shared mock store (logs in memory, keyed by trek_id).
# ---------------------------------------------------------------------------

_treks: dict[str, dict] = {}
_logs: dict[str, list[dict]] = {}
_bus_events: dict[str, list[dict]] = {}
# ms-97 / e-2650 — project pool mock so slot-done precondition can verify
# that task pool truth backs Trek slot done transitions. Tests that exercise
# done transitions seed this via ``_seed_pool_task``.
_project_pool: dict[str, dict] = {}


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


def _mock_append_trek_log(trek_id: str, entry: dict) -> str:
    payload = copy.deepcopy(entry)
    payload["trek_id"] = trek_id
    log_id = payload.get("log_id") or f"log-mock-{len(_logs.get(trek_id, []))}"
    payload["log_id"] = log_id
    _logs.setdefault(trek_id, []).append(payload)
    return log_id


def _mock_list_trek_logs(trek_id: str, *, limit=100, since=""):
    rows = list(_logs.get(trek_id) or [])
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
    rows.sort(key=lambda r: (r.get("created_at", ""), r.get("log_id", "")))
    return rows[: limit if limit and limit > 0 else None]


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _bus_events.setdefault(project_id, []).append(copy.deepcopy(data))
    return f"evt-{project_id}-{len(_bus_events[project_id]) - 1}"


def _mock_list_sessions(project_id: str):
    return []


def _mock_get_project(project_id: str):
    data = _project_pool.get(project_id)
    return copy.deepcopy(data) if data else None


def _seed_pool_task(project_id: str, ms_id: str, entry_id: str,
                    status: str = "done") -> None:
    """ms-97 / e-2650 — seed _project_pool so the slot-done precondition allows.

    Tests in this file flip Trek slots to done as part of verifying
    structured-log writes; they need the project pool to mirror that
    truth (= true to ms-97 / e-2650 ordering: pool is the source of
    truth, Trek slot is the derived view).
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
            return
    ms["entries"].append({"id": entry_id, "type": "task", "status": status})


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
    _project_pool.clear()
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)


def _seed_trek(*, leader_sid: str = "sv-leader") -> dict:
    t = {
        "trek_id": "tk-log000001",
        "title": "structured-logs test",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": "dev", "email": "dev@local"},
        "leader_session_id": leader_sid,
        "members": [{
            "user_id": "dev", "email": "dev@local",
            "role": "leader", "invited_at": "2026-06-18T00:00:00.000000Z",
            "joined_at": "2026-06-18T00:00:00.000000Z",
            "invited_by": "dev",
            "session_id": leader_sid,
        }],
        "scope": [{"project": "p1", "milestone": "ms-1"}],
        "halt": None,
        "goal_state": "",
        "meta": {},
        "pending_scope_ops": [],
        "kickoff_status": {
            # Pre-fill so pulse-ack does not 400 with kickoff_required.
            leader_sid: {
                "session_id": leader_sid,
                "user_id": "dev",
                "pending": False,
                "sent_at": "2026-06-18T00:00:00.000000Z",
                "kickoff_dm_event_id": "evt-mock-kickoff",
            },
        },
        "task_states": {},
        "created_at": "2026-06-18T00:00:00.000000Z",
        "updated_at": "2026-06-18T00:00:00.000000Z",
        "archived_at": None,
    }
    _treks[t["trek_id"]] = copy.deepcopy(t)
    return t


# ---------------------------------------------------------------------------
# Store helpers (= AC26 schema invariants)
# ---------------------------------------------------------------------------

class TestStoreHelpers:
    def test_append_trek_log_writes_entry_with_log_id(self):
        log_id = _mock_append_trek_log("tk-log000001", {
            "kind": "tick",
            "session_id": "",
            "payload": {"recipients": ["sv-x"]},
            "created_at": "2026-06-28T00:00:00.000000Z",
        })
        assert log_id  # truthy id assigned
        rows = _mock_list_trek_logs("tk-log000001")
        assert len(rows) == 1
        assert rows[0]["kind"] == "tick"
        assert rows[0]["trek_id"] == "tk-log000001"
        assert rows[0]["log_id"] == log_id

    def test_list_trek_logs_returns_chronological(self):
        _mock_append_trek_log("tk-log000001", {
            "kind": "tick", "session_id": "", "payload": {},
            "created_at": "2026-06-28T00:00:02.000000Z",
        })
        _mock_append_trek_log("tk-log000001", {
            "kind": "outcome", "session_id": "sv-x", "payload": {},
            "created_at": "2026-06-28T00:00:01.000000Z",
        })
        rows = _mock_list_trek_logs("tk-log000001")
        assert [r["kind"] for r in rows] == ["outcome", "tick"]


# ---------------------------------------------------------------------------
# Write paths (= tick / pulse-ack / outcome)
# ---------------------------------------------------------------------------

class TestPulseAckWritesLog:
    def test_pulse_ack_endpoint_writes_pulse_ack_log(self):
        _seed_trek()
        resp = client.post(
            "/api/treks/tk-log000001/pulse-ack",
            json={
                "session_id": "sv-leader",
                "picked_choice": "continue",
                "note": "ok",
            },
        )
        assert resp.status_code == 200, resp.text
        rows = _logs.get("tk-log000001") or []
        kinds = [r["kind"] for r in rows]
        assert "pulse-ack" in kinds
        # Payload carries the structured fields.
        rec = next(r for r in rows if r["kind"] == "pulse-ack")
        assert rec["session_id"] == "sv-leader"
        assert rec["payload"]["picked_choice"] == "continue"


class TestTaskStateWritesOutcomeLog:
    def test_terminal_transition_writes_outcome_log(self):
        _seed_trek()
        # ms-97 / e-2650 — pool 真値源 precondition 充足のため pool seed。
        _seed_pool_task("p1", "ms-1", "e-1", status="done")
        # First move todo → working (= state machine requirement).
        r1 = client.patch(
            "/api/treks/tk-log000001/task-state",
            json={"task_id": "e-1", "state": "working"},
        )
        assert r1.status_code == 200, r1.text
        # Then working → done (= terminal).
        resp = client.patch(
            "/api/treks/tk-log000001/task-state",
            json={"task_id": "e-1", "state": "done", "note": "shipped"},
        )
        assert resp.status_code == 200, resp.text
        rows = _logs.get("tk-log000001") or []
        outcomes = [r for r in rows if r["kind"] == "outcome"]
        assert len(outcomes) == 1
        assert outcomes[0]["payload"]["task_id"] == "e-1"
        # ms-128 方針5: done は user_review に migrate され、outcome log も
        # effective_state (= user_review) を記録する。
        assert outcomes[0]["payload"]["state"] == "user_review"

    def test_non_terminal_transition_does_not_write_outcome(self):
        _seed_trek()
        resp = client.patch(
            "/api/treks/tk-log000001/task-state",
            json={"task_id": "e-1", "state": "working"},
        )
        assert resp.status_code == 200, resp.text
        rows = _logs.get("tk-log000001") or []
        outcomes = [r for r in rows if r["kind"] == "outcome"]
        assert outcomes == []


class TestTickWritesTickLog:
    def test_scheduler_tick_writes_tick_log(self):
        _seed_trek()
        # Tick must read X-Beacon-Scheduler-Key auth.
        resp = client.post(
            "/api/system/trek-scheduler/tick",
            headers={"X-Beacon-Scheduler-Key": "test-scheduler-key"},
            json={"trek_ids": ["tk-log000001"]},
        )
        assert resp.status_code == 200, resp.text
        rows = _logs.get("tk-log000001") or []
        ticks = [r for r in rows if r["kind"] == "tick"]
        # 1 tick row per fired trek (= aggregate).
        assert len(ticks) == 1
        # Tick payload carries fanout breadth.
        assert "recipients" in ticks[0]["payload"]


# ---------------------------------------------------------------------------
# AC27 — NDJSON export endpoint
# ---------------------------------------------------------------------------

class TestLogsJsonlEndpoint:
    def test_logs_jsonl_streams_ndjson(self):
        _seed_trek()
        _mock_append_trek_log("tk-log000001", {
            "kind": "tick",
            "session_id": "",
            "payload": {"x": 1},
            "created_at": "2026-06-28T00:00:01.000000Z",
        })
        _mock_append_trek_log("tk-log000001", {
            "kind": "outcome",
            "session_id": "sv-x",
            "payload": {"task_id": "e-1", "state": "done"},
            "created_at": "2026-06-28T00:00:02.000000Z",
        })
        resp = client.get("/api/treks/tk-log000001/logs.jsonl")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith(
            "application/x-ndjson"
        )
        lines = [ln for ln in resp.text.strip().split("\n") if ln]
        assert len(lines) == 2
        parsed = [json.loads(ln) for ln in lines]
        assert [p["kind"] for p in parsed] == ["tick", "outcome"]

    def test_logs_jsonl_filters_since(self):
        _seed_trek()
        _mock_append_trek_log("tk-log000001", {
            "kind": "tick", "session_id": "", "payload": {},
            "created_at": "2026-06-28T00:00:01.000000Z",
        })
        _mock_append_trek_log("tk-log000001", {
            "kind": "outcome", "session_id": "sv-x", "payload": {},
            "created_at": "2026-06-28T00:00:05.000000Z",
        })
        resp = client.get(
            "/api/treks/tk-log000001/logs.jsonl"
            "?since=2026-06-28T00:00:03.000000Z",
        )
        assert resp.status_code == 200, resp.text
        lines = [ln for ln in resp.text.strip().split("\n") if ln]
        assert len(lines) == 1
        assert json.loads(lines[0])["kind"] == "outcome"

    def test_logs_jsonl_non_member_403(self):
        """RBAC: a non-member user is rejected by ``_load_trek_for_read``."""
        _seed_trek()
        prior = app_module._auth_enabled
        app_module._auth_enabled = True
        try:
            app_module.app.dependency_overrides[app_module.require_auth] = (
                lambda: {"sub": "uid-outsider", "email": "out@b.com"}
            )
            try:
                resp = client.get("/api/treks/tk-log000001/logs.jsonl")
                # _load_trek_for_read raises 403 for non-members.
                assert resp.status_code == 403, resp.text
            finally:
                app_module.app.dependency_overrides.pop(
                    app_module.require_auth, None,
                )
        finally:
            app_module._auth_enabled = prior
