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
    # Re-apply firestore_client stubs every test in case another test module
    # (notably tests/test_api.py) has restored its own mocks at module import
    # time. Without this re-application, get_project would return whatever the
    # most recently imported module's mock dictates and the bus directory
    # tests would see surprise side effects (e.g. 404 instead of 200 because
    # get_project now returns None for our test project_id).
    firestore_client.list_sessions = _mock_list_sessions
    firestore_client.get_project = lambda pid: {"name": "test", "milestones": []}
    firestore_client.save_project = lambda pid, data: None
    firestore_client.list_projects = lambda: []
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


def test_session_id_is_surfaced_in_every_directory_row():
    """The directory query is only useful if each row identifies *which*
    session it is — otherwise a sender can't address the picked target via
    `bus send --sender <session_id>`. Catches the regression where
    firestore_client.list_sessions returned doc.to_dict() without merging
    doc.id (production hit this in the v0.14.0 deploy: rows appeared
    anonymous in the picker)."""
    _seed([
        {"session_id": "s-A", "actor": {"machine": "M1"},
         "last_active": "2026-06-07T01:00:00.000000Z"},
        {"session_id": "s-B", "actor": {"machine": "M2"},
         "last_active": "2026-06-07T01:00:01.000000Z"},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    assert {s.get("session_id") for s in body} == {"s-A", "s-B"}, body
    # No row should be unaddressable (None / "" / missing key).
    assert all(s.get("session_id") for s in body), body


# ---------------------------------------------------------------------------
# poll_health — Option C true-heartbeat (ms-54 / e-1318)
#
# These tests pin the structural inversion: stale last_poll_at MUST imply
# the bridge poll loop is dead. The previous last_active signal couldn't
# distinguish "bridge process alive but poll loop hung" from "session is
# fine" — today's dogfood case observed exactly that, with DMs piling up
# server-side while the receiving AI never woke.
# ---------------------------------------------------------------------------

def test_poll_health_present_on_every_row():
    """Every directory row carries a poll_health block, even when the
    session has never been touched by the new bridge. The /beacon-dm-send
    Skill consumes this unconditionally and must never have to crash on a
    missing key."""
    _seed([
        {"session_id": "s-old", "actor": {"machine": "M1"},
         "last_active": "2026-06-07T01:00:00.000000Z"},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    assert len(body) == 1
    ph = body[0].get("poll_health")
    assert isinstance(ph, dict), body
    # Legacy session with no last_poll_at → healthy=None (unknown).
    assert ph.get("healthy") is None
    assert ph.get("last_poll_at") == ""


def test_poll_health_marks_recent_poll_as_healthy():
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-fresh", "actor": {},
         "last_active": _iso(now),
         "last_poll_at": _iso(now - datetime.timedelta(seconds=1)),
         "poll_interval_ms": 2000},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    ph = body[0]["poll_health"]
    assert ph["healthy"] is True, body
    assert ph["age_seconds"] is not None
    assert ph["age_seconds"] < 5


def test_poll_health_marks_stale_poll_as_unhealthy():
    """The smoking gun from today's dogfood: bridge process alive,
    last_active fresh, but poll loop dead → stale last_poll_at.
    Directory must classify this as healthy=False."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-zombie", "actor": {},
         # last_active is fresh (the legacy heartbeat path is still firing)
         "last_active": _iso(now),
         # …but the poll loop hasn't stamped itself in 5 minutes.
         "last_poll_at": _iso(now - datetime.timedelta(minutes=5)),
         "poll_interval_ms": 2000},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    ph = body[0]["poll_health"]
    assert ph["healthy"] is False, body
    # The age field is what the picker uses to display "stale (300s)".
    assert ph["age_seconds"] is not None
    assert ph["age_seconds"] >= 200


def test_poll_health_threshold_scales_with_poll_interval():
    """Threshold = max(30s, 2 × poll_interval_ms). A bridge polling
    every 10s must still be healthy at age 15s, even though that exceeds
    the absolute 30s floor only marginally."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        # Slow bridge: 30s poll interval. At age 50s, 2×interval = 60s,
        # so still healthy (under the threshold).
        {"session_id": "s-slow", "actor": {},
         "last_poll_at": _iso(now - datetime.timedelta(seconds=50)),
         "poll_interval_ms": 30_000},
        # Same bridge config, age 70s → exceeds 60s threshold.
        {"session_id": "s-slow-dead", "actor": {},
         "last_poll_at": _iso(now - datetime.timedelta(seconds=70)),
         "poll_interval_ms": 30_000},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    h = {s["session_id"]: s["poll_health"]["healthy"] for s in body}
    assert h == {"s-slow": True, "s-slow-dead": False}, body


def test_poll_health_shutdown_flag_is_never_healthy():
    """A bridge that posted last_poll_at=now with shutdown=true is
    deliberately stopped. Directory must NOT advertise it as healthy
    even though the timestamp itself is fresh — otherwise senders would
    keep targeting a session that just hung up."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-shutdown", "actor": {},
         "last_poll_at": _iso(now),
         "poll_interval_ms": 2000,
         "shutdown": True},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    ph = body[0]["poll_health"]
    assert ph["healthy"] is False, body
    assert ph["shutdown"] is True


def test_healthy_only_filter_drops_stale_and_shutdown_and_unknown():
    """Only sessions with confirmed-healthy poll_health pass the filter."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-healthy", "actor": {},
         "last_poll_at": _iso(now), "poll_interval_ms": 2000},
        {"session_id": "s-stale", "actor": {},
         "last_poll_at": _iso(now - datetime.timedelta(minutes=5)),
         "poll_interval_ms": 2000},
        {"session_id": "s-shutdown", "actor": {},
         "last_poll_at": _iso(now),
         "poll_interval_ms": 2000, "shutdown": True},
        # Legacy session — no last_poll_at field at all.
        {"session_id": "s-legacy", "actor": {},
         "last_active": _iso(now)},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?healthy_only=true"
    )
    sids = [s["session_id"] for s in resp.json()]
    assert sids == ["s-healthy"], resp.json()


def test_healthy_only_composes_with_user_filter():
    """healthy_only must AND with other directory filters — the picker
    asks for "alice's healthy session", not "alice's session OR any
    healthy session"."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-alice-healthy", "actor": {"email": "alice@x"},
         "last_poll_at": _iso(now), "poll_interval_ms": 2000},
        {"session_id": "s-alice-zombie", "actor": {"email": "alice@x"},
         "last_poll_at": _iso(now - datetime.timedelta(minutes=10)),
         "poll_interval_ms": 2000},
        {"session_id": "s-bob-healthy", "actor": {"email": "bob@x"},
         "last_poll_at": _iso(now), "poll_interval_ms": 2000},
    ])
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/sessions"
        f"?user_id=alice@x&healthy_only=true"
    )
    sids = [s["session_id"] for s in resp.json()]
    assert sids == ["s-alice-healthy"]


def test_no_filter_call_still_includes_poll_health_for_legacy_rows():
    """Backward compat: callers that don't pass --healthy still get
    every row and the new poll_health field, with healthy=None for
    sessions the new bridge has never touched."""
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed([
        {"session_id": "s-modern", "actor": {},
         "last_poll_at": _iso(now), "poll_interval_ms": 2000},
        {"session_id": "s-legacy", "actor": {},
         "last_active": _iso(now)},
    ])
    body = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    assert len(body) == 2
    by_sid = {s["session_id"]: s for s in body}
    assert by_sid["s-modern"]["poll_health"]["healthy"] is True
    assert by_sid["s-legacy"]["poll_health"]["healthy"] is None


def test_session_upsert_accepts_new_heartbeat_fields():
    """The bridge writes last_poll_at / poll_interval_ms / shutdown via
    the existing session upsert endpoint. Pinning that the schema
    accepts these so a wire-mismatch (older server, newer bridge)
    surfaces here rather than as silently-dropped fields in prod."""
    # Capture writes the bridge would do.
    captured: dict = {}

    def _fake_upsert(project_id, session_id, data):  # noqa: ARG001
        captured.update(data)

    import firestore_client as fc
    original = fc.upsert_session
    fc.upsert_session = _fake_upsert
    try:
        now = "2026-06-09T01:00:00.000000Z"
        resp = client.put(
            f"/api/projects/{PROJECT_ID}/sessions/s-bridge",
            json={
                "last_active": now,
                "last_poll_at": now,
                "poll_interval_ms": 2000,
                "shutdown": False,
            },
        )
        assert resp.status_code == 200, resp.text
        assert captured.get("last_poll_at") == now
        assert captured.get("poll_interval_ms") == 2000
        assert captured.get("shutdown") is False
    finally:
        fc.upsert_session = original
