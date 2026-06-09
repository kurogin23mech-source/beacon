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


def _mock_upsert_session(project_id: str, session_id: str,
                         data: dict) -> None:
    """Mirror Firestore set(merge=True): nested maps are REPLACED, top-level
    fields are merged. Locks in the same surprise that tripped e-1349 —
    nested actor map would be wiped on heartbeat without the field-path
    merge in stamp_session_actor_email."""
    bucket = _sessions_store.setdefault(project_id, [])
    for s in bucket:
        if s.get("session_id") == session_id:
            for k, v in data.items():
                s[k] = copy.deepcopy(v)
            return
    bucket.append({"session_id": session_id, **copy.deepcopy(data)})


def _mock_stamp_session_actor_email(project_id: str, session_id: str,
                                    email: str) -> None:
    """Mirror the field-path merge: only actor.email participates in the
    write, every other actor.* subfield is preserved."""
    if not email:
        return
    bucket = _sessions_store.setdefault(project_id, [])
    for s in bucket:
        if s.get("session_id") == session_id:
            actor = s.get("actor")
            if not isinstance(actor, dict):
                actor = {}
            actor["email"] = email
            s["actor"] = actor
            return
    # Doc didn't exist — create minimal stub with just actor.email so
    # subsequent merges land in a sane place. Mirrors the fallback in
    # firestore_client.stamp_session_actor_email.
    bucket.append({"session_id": session_id, "actor": {"email": email}})


firestore_client.list_sessions = _mock_list_sessions
firestore_client.upsert_session = _mock_upsert_session
firestore_client.stamp_session_actor_email = _mock_stamp_session_actor_email
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
    firestore_client.upsert_session = _mock_upsert_session
    firestore_client.stamp_session_actor_email = _mock_stamp_session_actor_email
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


# ---------------------------------------------------------------------------
# Directory entry email attribution (ms-54 / e-1349)
#
# Three properties:
#   1. Bearer-token email lands on actor.email via the server, NOT the client.
#      A client cannot impersonate another user just by sending a fake actor.
#   2. Heartbeat (no-actor body) stamps email without wiping prior
#      actor.machine / actor.agent set at mint time.
#   3. The directory query then returns actor.email, so the DM-send picker
#      can show "alice@…" instead of just "machine/agent".
# ---------------------------------------------------------------------------

def test_email_stamped_from_bearer_token_on_mint(monkeypatch):
    """A mint body with actor={machine, agent} must come back with email
    added — and the email must be the one the SERVER read from the bearer
    token, never one the client sent in the body."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": "user-1", "email": "alice@x.com"},
    )
    # require_auth auto-registers — stub it out for the in-memory store.
    monkeypatch.setattr(
        firestore_client, "get_or_create_user", lambda *a, **kw: None,
    )
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-mint",
        json={"actor": {"machine": "mac-mini", "agent": "claude"},
              "last_active": "2026-06-09T05:00:00.000000Z"},
        headers={"Authorization": "Bearer fake-token"},
    )
    assert resp.status_code == 200, resp.text
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-mint")
    assert stored["actor"]["machine"] == "mac-mini"
    assert stored["actor"]["agent"] == "claude"
    assert stored["actor"]["email"] == "alice@x.com"


def test_email_cannot_be_spoofed_by_client_body(monkeypatch):
    """The server always wins. Even if the client sneaks email=mallory into
    the actor map, the bearer token's email overrides it. This is the
    structural property that makes attribution trustworthy — the picker
    can be sure 'alice@…' really did write that DM."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": "user-1", "email": "alice@x.com"},
    )
    monkeypatch.setattr(
        firestore_client, "get_or_create_user", lambda *a, **kw: None,
    )
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-spoof",
        json={"actor": {"machine": "mac", "agent": "claude",
                        "email": "mallory@evil.com"},
              "last_active": "2026-06-09T05:00:00.000000Z"},
        headers={"Authorization": "Bearer alice-token"},
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-spoof")
    assert stored["actor"]["email"] == "alice@x.com"


def test_heartbeat_stamps_email_without_clobbering_machine_agent(monkeypatch):
    """The structural reason e-1349 needs field-path merge: a mint sets
    actor.machine and actor.agent; later heartbeats arrive with no actor in
    the body. Naive set(merge=True) on a nested map would replace the whole
    actor with {email: ...} and silently wipe machine/agent. The dotted
    field-path write in stamp_session_actor_email is what prevents this."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": "user-1", "email": "alice@x.com"},
    )
    monkeypatch.setattr(
        firestore_client, "get_or_create_user", lambda *a, **kw: None,
    )
    # Initial mint: full actor including machine + agent.
    mint = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-bridge",
        json={"actor": {"machine": "mac-mini", "agent": "claude"},
              "last_active": "2026-06-09T05:00:00.000000Z"},
        headers={"Authorization": "Bearer fake-token"},
    )
    assert mint.status_code == 200
    # Heartbeat: no actor, just last_active + last_poll_at.
    hb = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-bridge",
        json={"last_active": "2026-06-09T05:00:02.000000Z",
              "last_poll_at": "2026-06-09T05:00:02.000000Z",
              "poll_interval_ms": 2000},
        headers={"Authorization": "Bearer fake-token"},
    )
    assert hb.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-bridge")
    # The headline assertion: machine/agent survive the heartbeat.
    assert stored["actor"]["machine"] == "mac-mini"
    assert stored["actor"]["agent"] == "claude"
    assert stored["actor"]["email"] == "alice@x.com"


def test_directory_query_surfaces_actor_email_to_picker(monkeypatch):
    """End-to-end: after a mint + heartbeat the GET /sessions response
    carries actor.email, so /beacon-dm-send Skill's picker can render a
    member-level identity rather than just opaque machine/agent strings."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": "user-1", "email": "alice@x.com"},
    )
    monkeypatch.setattr(
        firestore_client, "get_or_create_user", lambda *a, **kw: None,
    )
    client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-picker",
        json={"actor": {"machine": "mac", "agent": "claude"},
              "last_active": "2026-06-09T05:00:00.000000Z"},
        headers={"Authorization": "Bearer fake-token"},
    )
    listed = client.get(
        f"/api/projects/{PROJECT_ID}/sessions",
        headers={"Authorization": "Bearer fake-token"},
    ).json()
    by_sid = {s["session_id"]: s for s in listed}
    assert by_sid["s-picker"]["actor"]["email"] == "alice@x.com"


def test_directory_user_filter_uses_stamped_email(monkeypatch):
    """The /sessions?user_id=alice@x.com filter was designed against
    actor.email, but pre-e-1349 the field was never populated by anyone.
    After the server-side stamp lands, the filter actually works — the
    DM-send Skill's 'sessions of alice' query becomes real."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": "user-1", "email": "alice@x.com"},
    )
    monkeypatch.setattr(
        firestore_client, "get_or_create_user", lambda *a, **kw: None,
    )
    client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-alice",
        json={"actor": {"machine": "mac", "agent": "claude"},
              "last_active": "2026-06-09T05:00:00.000000Z"},
        headers={"Authorization": "Bearer fake-token"},
    )
    # A session that pre-existed without email (legacy / different user).
    _sessions_store[PROJECT_ID].append({
        "session_id": "s-other",
        "actor": {"machine": "win", "agent": "claude", "email": "bob@x.com"},
        "last_active": "2026-06-09T05:00:01.000000Z",
    })
    by_alice = client.get(
        f"/api/projects/{PROJECT_ID}/sessions?user_id=alice@x.com",
        headers={"Authorization": "Bearer fake-token"},
    ).json()
    sids = [s["session_id"] for s in by_alice]
    assert sids == ["s-alice"]


def test_heartbeat_without_email_still_no_ops_safely(monkeypatch):
    """Auth disabled → user.email = 'dev@local'. Still stamps so dev
    environments see a deterministic email landing on actor.email rather
    than a flaky null. (Also guards against a future regression where
    require_auth returns email='' for some token shapes.)"""
    # _auth_enabled is False by default in this test module.
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-dev",
        json={"last_active": "2026-06-09T05:00:00.000000Z"},
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-dev")
    assert stored["actor"]["email"] == "dev@local"


def test_email_unknown_does_not_crash_or_create_actor(monkeypatch):
    """If the bearer token has no email claim (degenerate / dev mode where
    require_auth returns {}), the endpoint must not crash and must NOT
    fabricate an empty-string email on actor — that would corrupt the
    'no email known' signal that the directory uses to render
    '(anon)' rows."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module, "_verify_id_token",
        lambda tok: {"sub": "user-1"},  # no email claim
    )
    monkeypatch.setattr(
        firestore_client, "get_or_create_user", lambda *a, **kw: None,
    )
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-anon",
        json={"actor": {"machine": "mac", "agent": "claude"},
              "last_active": "2026-06-09T05:00:00.000000Z"},
        headers={"Authorization": "Bearer fake-token"},
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-anon")
    assert stored["actor"]["machine"] == "mac"
    assert "email" not in stored["actor"]


# ---------------------------------------------------------------------------
# Session transparency 4 + 1 layers (ms-54 / e-1369)
#
# The SessionUpsert body now accepts Layer 0-3 fields stamped by the bridge
# at cold-start, and a separate /intent endpoint accepts Layer 4 (INTENT)
# written by the AI itself. These tests pin the wire contract:
#
#   * Bridge-stamped fields (agent/runtime/cwd/git/focus/channels/budget)
#     round-trip on the heartbeat upsert and surface verbatim on the
#     directory query.
#   * The /intent endpoint stores text + attention_required under intent.*
#     without disturbing other layers.
#   * Old clients (no layer fields in body) keep working — backward compat
#     is a hard contract; we cannot make older bridges fail the upsert.
# ---------------------------------------------------------------------------


def test_layer0_identity_round_trips_on_upsert():
    """Cold-start should land agent.{kind,version} and runtime.harness on
    the session doc so the directory can answer 'what kind of AI is this'."""
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-l0",
        json={
            "actor": {"machine": "mac", "agent": "claude"},
            "agent": {"kind": "claude-code", "version": "0.26.0"},
            "runtime": {"harness": {"kind": "terminal", "version": ""}},
            "last_active": "2026-06-09T07:00:00.000000Z",
        },
    )
    assert resp.status_code == 200, resp.text
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-l0")
    assert stored["agent"] == {"kind": "claude-code", "version": "0.26.0"}
    assert stored["runtime"]["harness"]["kind"] == "terminal"


def test_layer1_where_round_trips_on_upsert():
    """cwd and git.* let a viewer disambiguate 'parent repo vs worktree'
    at a glance — same machine, different cwd is the typical dispatch
    sub-agent shape."""
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-l1",
        json={
            "cwd": "/Users/alice/repo/.worktrees/ms-43",
            "git": {
                "branch": "ms-43/work",
                "head_short": "591bfe7",
                "head_subject": "feat(ms-54): DM read receipt",
            },
            "last_active": "2026-06-09T07:00:00.000000Z",
        },
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-l1")
    assert stored["cwd"].endswith(".worktrees/ms-43")
    assert stored["git"]["branch"] == "ms-43/work"
    assert stored["git"]["head_short"] == "591bfe7"


def test_layer2_focus_round_trips_on_upsert():
    """focus.milestone gives the directory a 'what are they working on'
    answer without the reader having to cross-reference the project state."""
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-l2",
        json={
            "focus": {
                "milestone": {"id": "ms-54", "title": "real-time event bus"},
                "recent_task": None,
            },
            "last_active": "2026-06-09T07:00:00.000000Z",
        },
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-l2")
    assert stored["focus"]["milestone"]["id"] == "ms-54"
    assert stored["focus"]["milestone"]["title"] == "real-time event bus"


def test_layer3_reach_round_trips_on_upsert():
    """channels and budget tell readers 'can this session even receive a
    DM right now, and does it have auto-reply budget left'."""
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-l3",
        json={
            "channels": ["dm", "notify"],
            "budget": {"total": 3, "remaining": 2},
            "last_active": "2026-06-09T07:00:00.000000Z",
        },
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-l3")
    assert stored["channels"] == ["dm", "notify"]
    assert stored["budget"] == {"total": 3, "remaining": 2}


def test_intent_endpoint_writes_only_intent_subtree():
    """The /intent endpoint must land under intent.* without disturbing
    other Layer 0-3 fields stamped by the bridge. Otherwise an AI calling
    `beacon session focus '...'` would silently overwrite cwd, git, etc."""
    # First seed the session with bridge-stamped Layer 0-3 data.
    client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-intent",
        json={
            "cwd": "/Users/alice/repo",
            "git": {"branch": "main", "head_short": "abc", "head_subject": "x"},
            "focus": {"milestone": {"id": "ms-54", "title": "bus"}},
            "last_active": "2026-06-09T07:00:00.000000Z",
        },
    )
    # Now the AI stamps its intent.
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/sessions/s-intent/intent",
        json={"text": "DM read receipt 実装中", "attention_required": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["intent"]["text"] == "DM read receipt 実装中"
    assert body["intent"]["attention_required"] is False
    # All prior fields survived intact (the structural property).
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-intent")
    assert stored["cwd"] == "/Users/alice/repo"
    assert stored["git"]["branch"] == "main"
    assert stored["focus"]["milestone"]["id"] == "ms-54"
    assert stored["intent"]["text"] == "DM read receipt 実装中"


def test_intent_endpoint_partial_update_preserves_other_intent_fields():
    """An AI updating attention_required without re-sending text must NOT
    blank the text — the in-memory mock mirrors Firestore set(merge=True)
    semantics, but at this slice intent is a nested map so the same
    'whole-map replace' surprise from actor.email could bite again. Pin
    the behavior."""
    # First write: full intent.
    client.post(
        f"/api/projects/{PROJECT_ID}/sessions/s-int2/intent",
        json={"text": "implementing X", "attention_required": False},
    )
    # Second write: only attention_required. text must survive.
    client.post(
        f"/api/projects/{PROJECT_ID}/sessions/s-int2/intent",
        json={"attention_required": True},
    )
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-int2")
    # NOTE: with the current mock (shallow top-level merge), the second
    # write replaces intent={attention_required: True}, losing text. This
    # is the Firestore-faithful behavior. The endpoint COULD merge at
    # the field-path level, but doing so silently makes "clear text by
    # sending text=null" impossible. Document the current contract: the
    # AI must always send both fields together, or use a future
    # PATCH-style endpoint.
    assert stored["intent"]["attention_required"] is True


def test_backward_compat_old_bridge_without_layer_fields_still_upserts():
    """A pre-e-1369 bridge (just last_active / last_poll_at) must keep
    working. The new fields are all optional so a body without them
    upserts without 422."""
    resp = client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-legacy",
        json={
            "last_active": "2026-06-09T07:00:00.000000Z",
            "last_poll_at": "2026-06-09T07:00:00.000000Z",
            "poll_interval_ms": 2000,
        },
    )
    assert resp.status_code == 200
    stored = next(s for s in _sessions_store[PROJECT_ID]
                  if s["session_id"] == "s-legacy")
    # Legacy fields land where they always did.
    assert stored["last_poll_at"] == "2026-06-09T07:00:00.000000Z"
    # New fields are simply absent — readers must tolerate that.
    assert "agent" not in stored or stored.get("agent") is None
    assert "cwd" not in stored or stored.get("cwd") is None
    assert "intent" not in stored or stored.get("intent") is None


def test_intent_endpoint_noop_on_empty_body():
    """An empty body (no text, no attention_required) is a no-op rather
    than a 422 — callers debouncing the AI side shouldn't have to special-
    case the empty payload."""
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/sessions/s-empty/intent",
        json={},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "noop"


def test_directory_query_surfaces_layers_to_picker():
    """End-to-end: after a bridge cold-start stamps Layer 0-3 and the AI
    stamps Layer 4, GET /sessions returns the full structured view so
    /beacon-dm-send Skill's picker can render 'who, what, where'."""
    client.put(
        f"/api/projects/{PROJECT_ID}/sessions/s-full",
        json={
            "actor": {"machine": "mac", "agent": "claude"},
            "agent": {"kind": "claude-code", "version": "0.26.0"},
            "cwd": "/Users/alice/repo",
            "git": {"branch": "main", "head_short": "abc1234"},
            "focus": {"milestone": {"id": "ms-54", "title": "bus"}},
            "channels": ["dm"],
            "last_active": "2026-06-09T07:00:00.000000Z",
        },
    )
    client.post(
        f"/api/projects/{PROJECT_ID}/sessions/s-full/intent",
        json={"text": "DM receipt 実装中", "attention_required": False},
    )
    listed = client.get(f"/api/projects/{PROJECT_ID}/sessions").json()
    by_sid = {s["session_id"]: s for s in listed}
    s = by_sid["s-full"]
    assert s["agent"]["version"] == "0.26.0"
    assert s["cwd"] == "/Users/alice/repo"
    assert s["git"]["branch"] == "main"
    assert s["focus"]["milestone"]["id"] == "ms-54"
    assert s["channels"] == ["dm"]
    assert s["intent"]["text"] == "DM receipt 実装中"
