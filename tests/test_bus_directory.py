"""Directory query for DM target selection (ms-54 / e-1134).

GET /api/projects/{id}/sessions has been the "who is alive" lookup since
ms-57. e-1134 extends it with optional filters so a sender can resolve a DM
target ("the user 'kurogin' on machine X") without knowing the exact
session_id out-of-band.

These tests lock in three things:
  * the no-arg call still returns everything (ms-57 rescue + Web UI rely on it),
  * each filter narrows correctly,
  * live_only uses a heartbeat threshold (sessions that crashed without
    session-end go stale and disappear from "live" once their heartbeat
    expires — they don't haunt the picker forever).
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

import firestore_client

# Per-project in-memory mirror of the sessions subcollection.
_sessions_store: dict[str, list[dict]] = {}


def _mock_list_sessions(project_id: str) -> list[dict]:
    items = list(_sessions_store.get(project_id, []))
    items.sort(key=lambda s: s.get("last_active", ""), reverse=True)
    return copy.deepcopy(items)


firestore_client.list_sessions = _mock_list_sessions
firestore_client.get_project = lambda pid: {"name": "test", "milestones": []}
firestore_client.save_project = lambda pid, data: None
firestore_client.list_projects = lambda: []


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False
app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)

PROJECT_ID = "dir-test"


@pytest.fixture(autouse=True)
def reset_store():
    _sessions_store.clear()
    yield
    _sessions_store.clear()


def _seed(sessions: list[dict]) -> None:
    _sessions_store[PROJECT_ID] = list(sessions)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# No-arg behavior (ms-57 backwards compat)
# ---------------------------------------------------------------------------

def test_no_filters_returns_all_sessions():
    _seed([
        {"session_id": "s-1", "actor": {"email": "a@x", "machine": "M1"},
         "last_active": "2026-06-07T00:00:01.000000Z"},
        {"session_id": "s-2", "actor": {"email": "b@x", "machine": "M2"},
         "last_active": "2026-06-07T00:00:02.000000Z"},
    ])
    resp = client.get(f"/api/projects/{PROJECT_ID}/sessions")
    assert resp.status_code == 200
    sids = [s["session_id"] for s in resp.json()]
    assert sorted(sids) == ["s-1", "s-2"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_user_id_filter_matches_actor_email():
    _seed([
        {"session_id": "s-1", "actor": {"email": "alice@x"},
         "last_active": "2026-06-07T00:00:01.000000Z"},
        {"session_id": "s-2", "actor": {"email": "bob@x"},
         "last_active": "2026-06-07T00:00:02.000000Z"},
    ])
    resp = client.get(f"/api/projects/{PROJECT_ID}/sessions?user_id=alice@x")
    assert [s["session_id"] for s in resp.json()] == ["s-1"]


def test_machine_filter():
    _seed([
        {"session_id": "s-1", "actor": {"machine": "mac"},
         "last_active": "2026-06-07T00:00:01.000000Z"},
        {"session_id": "s-2", "actor": {"machine": "win"},
         "last_active": "2026-06-07T00:00:02.000000Z"},
    ])
    resp = client.get(f"/api/projects/{PROJECT_ID}/sessions?machine=mac")
    assert [s["session_id"] for s in resp.json()] == ["s-1"]


def test_agent_filter():
    _seed([
        {"session_id": "s-1", "actor": {"agent": "claude"},
         "last_active": "2026-06-07T00:00:01.000000Z"},
        {"session_id": "s-2", "actor": {"agent": "subagent-1"},
         "last_active": "2026-06-07T00:00:02.000000Z"},
    ])
    resp = client.get(f"/api/projects/{PROJECT_ID}/sessions?agent=subagent-1")
    assert [s["session_id"] for s in resp.json()] == ["s-2"]


def test_filters_compose_with_AND_semantics():
    """Multiple filters must AND, not OR — otherwise a target picker would
    show false positives (alice@mac matches when you ask for bob@mac)."""
    _seed([
        {"session_id": "s-1", "actor": {"email": "alice@x", "machine": "mac"},
         "last_active": "2026-06-07T00:00:01.000000Z"},
        {"session_id": "s-2", "actor": {"email": "bob@x", "machine": "mac"},
         "last_active": "2026-06-07T00:00:02.000000Z"},
        {"session_id": "s-3", "actor": {"email": "alice@x", "machine": "win"},
         "last_active": "2026-06-07T00:00:03.000000Z"},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?user_id=alice@x&machine=mac"
    )
    assert [s["session_id"] for s in resp.json()] == ["s-1"]


# ---------------------------------------------------------------------------
# live_only — heartbeat-based liveness
# ---------------------------------------------------------------------------

def test_live_only_drops_sessions_older_than_threshold():
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-live", "actor": {},
         "last_active": _iso(now - datetime.timedelta(minutes=1))},
        {"session_id": "s-stale", "actor": {},
         "last_active": _iso(now - datetime.timedelta(minutes=30))},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?live_only=true&since_minutes=5"
    )
    assert [s["session_id"] for s in resp.json()] == ["s-live"]


def test_live_only_threshold_configurable_via_since_minutes():
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-recent", "actor": {},
         "last_active": _iso(now - datetime.timedelta(minutes=1))},
        {"session_id": "s-medium", "actor": {},
         "last_active": _iso(now - datetime.timedelta(minutes=30))},
        {"session_id": "s-ancient", "actor": {},
         "last_active": _iso(now - datetime.timedelta(hours=3))},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?live_only=true&since_minutes=60"
    )
    sids = sorted(s["session_id"] for s in resp.json())
    assert sids == ["s-medium", "s-recent"]


def test_live_only_compose_with_user_filter():
    """The point of e-1134 — "the LIVE session of user X" — must compose
    cleanly. If live_only ignores other filters, a picker shows wrong rows."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-alice-live", "actor": {"email": "alice@x"},
         "last_active": _iso(now - datetime.timedelta(minutes=1))},
        {"session_id": "s-alice-stale", "actor": {"email": "alice@x"},
         "last_active": _iso(now - datetime.timedelta(hours=2))},
        {"session_id": "s-bob-live", "actor": {"email": "bob@x"},
         "last_active": _iso(now - datetime.timedelta(minutes=1))},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?user_id=alice@x&live_only=true"
    )
    assert [s["session_id"] for s in resp.json()] == ["s-alice-live"]


def test_session_with_missing_last_active_is_never_live():
    """An incompletely-mounted session (no last_active yet) must not pretend
    to be live, otherwise it would mask a real session under the same user."""
    _seed([
        {"session_id": "s-no-heartbeat", "actor": {"email": "x@x"}},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?live_only=true"
    )
    assert resp.json() == []
