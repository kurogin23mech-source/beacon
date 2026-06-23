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
# ms-88 / e-2109 — per-project live session registry stub for the fan-out
# filter integration test. Tests can populate this directly.
_sessions_by_project: dict[str, list[dict]] = {}


def _mock_list_sessions(project_id: str):
    return copy.deepcopy(_sessions_by_project.get(project_id, []))


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
        ("list_sessions", _mock_list_sessions),
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

    # Bus events landed: the cadence tick fires both trek-progress-check
    # (= executor surface) and trek-leader-digest (= leader surface,
    # added in ms-92 / e-2164). Pin the progress-check event shape here;
    # the digest shape has its own dedicated test below.
    events = _bus_events_by_project["beacon-test"]
    progress_events = [e for e in events
                       if e["channel"] == "trek-progress-check"]
    assert len(progress_events) == 1
    ev = progress_events[0]
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

# ---------------------------------------------------------------------------
# ms-92 / e-2164 — leader-digest fires alongside progress-check
# ---------------------------------------------------------------------------


def test_due_trek_fires_leader_digest_on_separate_channel():
    """The same scheduler tick that fires trek-progress-check must also
    fire one trek-leader-digest addressed to the leader's session.
    """
    _seed_trek(
        trek_id="tk-digest01",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-92"}],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-digest01"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["fired"]) == 1
    fired = body["fired"][0]
    assert fired["leader_session_id"] == "sv-leader"
    assert fired["leader_digest_event_id"], (
        "scheduler must record the leader-digest event id alongside the "
        "progress-check event ids"
    )

    # Two bus events landed: one progress-check + one leader-digest.
    events = _bus_events_by_project["beacon-test"]
    channels = [e["channel"] for e in events]
    assert "trek-progress-check" in channels
    assert "trek-leader-digest" in channels

    # Inspect the digest event shape.
    digest_event = next(e for e in events
                        if e["channel"] == "trek-leader-digest")
    assert digest_event["delivery"] == "auto-execute"
    assert digest_event["envelope"]["tier"] == "T1-system"
    assert digest_event["envelope"]["scope"] == "trek:tk-digest01"
    assert digest_event["envelope"]["actions_authorized"] == \
        ["trek.leader_digest"]
    # Payload addressed to the leader only.
    payload = digest_event["payload"]
    assert payload["kind"] == "trek-leader-digest"
    assert payload["recipient_session_id"] == "sv-leader"
    assert payload["trek_id"] == "tk-digest01"
    # Aggregate counts are present even when there are no pulse-acks yet
    # (= legitimate "fresh trek, no executors pulsed" snapshot).
    assert payload["summary"]["active"] == 0
    assert payload["sessions"] == []


def test_due_trek_without_leader_session_id_skips_digest():
    """A trek without a leader_session_id (= planning-era trek migrated
    badly) must skip the digest but still fire progress-check."""
    _seed_trek(
        trek_id="tk-noleader01",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-92"}],
    )
    # Manually drop leader_session_id.
    _treks["tk-noleader01"]["leader_session_id"] = ""

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-noleader01"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["fired"]) == 1
    fired = body["fired"][0]
    assert fired["leader_digest_event_id"] == ""

    # No leader-digest event in the bus.
    events = _bus_events_by_project["beacon-test"]
    channels = [e["channel"] for e in events]
    assert "trek-progress-check" in channels
    assert "trek-leader-digest" not in channels


def test_due_trek_stamps_last_leader_digest_at_when_fired():
    _seed_trek(
        trek_id="tk-stamp001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-92"}],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stamp001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    saved = _treks["tk-stamp001"]
    assert saved["meta"]["last_leader_digest_at"]


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
    # Only one cadence-fire landed. Each fire emits both a
    # trek-progress-check and a trek-leader-digest (= ms-92 / e-2164),
    # so 2 events total for one fire.
    events = _bus_events_by_project["beacon-test"]
    assert len(events) == 2
    channels = sorted(e["channel"] for e in events)
    assert channels == ["trek-leader-digest", "trek-progress-check"]


# ---------------------------------------------------------------------------
# Auto-stall pass (ms-75 / e-2067) — HTTP integration
# ---------------------------------------------------------------------------

def test_auto_stall_transitions_stalled_task_to_leader_review():
    """ms-88 / e-2107: auto-stall 罰則先が `waiting-review` → `leader_review` に
    変更され、 TTL default が 30 → 12 min 短縮された。 13 min 沈黙の working
    task で safety net が発火、 leader_review に遷移 + trek-task-review DM 発火。"""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = (now - datetime.timedelta(minutes=13)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_trek(
        trek_id="tk-stall0001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-75"}],
        last_at=stale,  # prevent cadence-fire from also running
    )
    _treks["tk-stall0001"]["task_states"] = {
        "e-1": {
            "state": "working",
            "updated_at": stale,
            "last_activity_at": stale,
            "updated_by_session_id": "sv-exec",
            "note": "",
        },
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stall0001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_stalled" in body
    assert len(body["auto_stalled"]) == 1
    entry = body["auto_stalled"][0]
    assert entry["trek_id"] == "tk-stall0001"
    assert entry["task_id"] == "e-1"
    assert entry["silence_minutes"] >= 13
    assert entry["ttl_minutes"] == 12  # ms-88 / e-2107: 30 → 12 min 短縮

    # Trek doc has the new leader_review state (= ms-88 / e-2107)。
    saved = _treks["tk-stall0001"]
    assert saved["task_states"]["e-1"]["state"] == "leader_review"
    assert "auto-stalled" in saved["task_states"]["e-1"]["note"]

    # Leader received a trek-task-review DM with auto_stalled flag.
    events = _bus_events_by_project["beacon-test"]
    review_events = [
        e for e in events if e["channel"] == "trek-task-review"
    ]
    assert len(review_events) == 1
    rev = review_events[0]
    assert rev["delivery"] == "auto-execute"
    assert rev["payload"]["trek_id"] == "tk-stall0001"
    assert rev["payload"]["task_id"] == "e-1"
    # ms-88 / e-2107: state も leader_review に統一
    assert rev["payload"]["state"] == "leader_review"
    assert rev["payload"]["auto_stalled"] is True
    assert rev["payload"]["silence_minutes"] >= 13
    assert rev["payload"]["recipient_session_id"] == "sv-leader"


def test_auto_stall_does_not_fire_for_task_under_ttl():
    """Working task with 5 min silence + default 30 min TTL stays in working."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = (now - datetime.timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_trek(
        trek_id="tk-stall0002",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-75"}],
        last_at=recent,
    )
    _treks["tk-stall0002"]["task_states"] = {
        "e-1": {
            "state": "working",
            "updated_at": recent,
            "last_activity_at": recent,
        },
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stall0002"]},
        headers=HEADERS_OK,
    )
    assert resp.json()["auto_stalled"] == []
    assert _treks["tk-stall0002"]["task_states"]["e-1"]["state"] == "working"


def test_auto_stall_honors_per_trek_ttl_override():
    """meta.working_ttl_minutes=5: a task 6 min silent triggers the safety
    net even though the default TTL is 30 min (= AC 6)."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = (now - datetime.timedelta(minutes=6)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_trek(
        trek_id="tk-stall0003",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-75"}],
        last_at=stale,
    )
    _treks["tk-stall0003"]["meta"]["working_ttl_minutes"] = 5
    _treks["tk-stall0003"]["task_states"] = {
        "e-1": {
            "state": "working",
            "updated_at": stale,
            "last_activity_at": stale,
        },
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stall0003"]},
        headers=HEADERS_OK,
    )
    body = resp.json()
    assert len(body["auto_stalled"]) == 1
    assert body["auto_stalled"][0]["ttl_minutes"] == 5


def test_auto_stall_skips_halted_trek():
    """halt set = leader paused; safety net stays silent (= AC: not duplicate
    signal to a leader who already engaged the cord)."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = (now - datetime.timedelta(minutes=60)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_trek(
        trek_id="tk-stall0004",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-75"}],
        last_at=stale,
    )
    _treks["tk-stall0004"]["halt"] = {
        "issued_at": stale,
        "issued_by_session_id": "sv-leader",
        "reason": "manual stop",
    }
    _treks["tk-stall0004"]["task_states"] = {
        "e-1": {"state": "working", "last_activity_at": stale},
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stall0004"]},
        headers=HEADERS_OK,
    )
    assert resp.json()["auto_stalled"] == []


def test_auto_stall_leader_can_recover_via_re_stamp_working():
    """AC 5: false-positive recovery path. ms-88 / e-2107: 罰則先が
    leader_review に変更されたので leader は leader_review → working で復帰。
    TTL も 12 min 短縮なので 13 min stale で発火する。"""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = (now - datetime.timedelta(minutes=13)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_trek(
        trek_id="tk-stall0005",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-75"}],
        last_at=stale,
    )
    _treks["tk-stall0005"]["task_states"] = {
        "e-1": {
            "state": "working",
            "updated_at": stale,
            "last_activity_at": stale,
        },
    }
    # Auto-stall fires → leader_review (ms-88 / e-2107)。
    r1 = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stall0005"]},
        headers=HEADERS_OK,
    )
    assert len(r1.json()["auto_stalled"]) == 1
    assert _treks["tk-stall0005"]["task_states"]["e-1"]["state"] == "leader_review"

    # Leader simulates re-stamping working (= the lib operation behind the
    # PATCH endpoint). leader_review → working は新 5-state machine で許可。
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
    import trek as trek_mod
    trek_mod.set_task_state(
        _treks["tk-stall0005"],
        task_id="e-1",
        state="working",
        updated_by_session_id="sv-leader",
        note="false positive — executor was busy committing",
    )
    assert _treks["tk-stall0005"]["task_states"]["e-1"]["state"] == "working"
    # last_activity_at refreshed → next tick does NOT auto-stall.
    r2 = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stall0005"]},
        headers=HEADERS_OK,
    )
    assert r2.json()["auto_stalled"] == []


# ---------------------------------------------------------------------------
# Per-session scheduler fanout filter (ms-88 / e-2109)
# ---------------------------------------------------------------------------

def _seed_live_sessions_for_trek(project_id: str, *,
                                 user_id: str,
                                 session_ids: list[str]) -> None:
    """Register a set of live sessions for a project (= each one is fresh,
    last_active = now). ms-88 / e-2109 helper."""
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project[project_id] = [
        {"session_id": sid, "user_id": user_id, "last_active": now_iso}
        for sid in session_ids
    ]


def test_fanout_skips_session_with_only_terminal_claims():
    """ms-88 / e-2109: session whose claims are all in leader_review /
    user_review / done が tick を受け取らない (= 「work_review 後も scheduler
    届く」 問題の構造解)。"""
    _seed_trek(
        trek_id="tk-fan0001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-88"}],
    )
    # 2 sessions for leader user: 1 active claim + 1 all-terminal
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-active", "sv-finished", "sv-fresh"],
    )
    # sv-active が working な claim を持つ
    _treks["tk-fan0001"]["task_states"] = {
        "e-w": {"state": "working", "updated_by_session_id": "sv-active"},
        "e-d": {"state": "done", "updated_by_session_id": "sv-finished"},
        # sv-fresh は claim 無し → fallback で tick もらう
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-fan0001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    progress_events = [
        e for e in events if e["channel"] == "trek-progress-check"
    ]
    recipients = sorted(
        e["payload"].get("recipient_session_id", "") for e in progress_events
    )
    # sv-active と sv-fresh は届く、 sv-finished は届かない
    assert "sv-active" in recipients
    assert "sv-fresh" in recipients
    assert "sv-finished" not in recipients


def test_fanout_skips_all_sessions_when_all_have_only_terminal_claims():
    """全 live session の claim が terminal-ish → fanout は broadcast fallback
    のみ (= 全 session が黙る方向 = 「誰にも届かない」 状態が emerge)。"""
    _seed_trek(
        trek_id="tk-fan0002",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-88"}],
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-a", "sv-b"],
    )
    # 両 session ともに done claim のみ
    _treks["tk-fan0002"]["task_states"] = {
        "e-1": {"state": "done", "updated_by_session_id": "sv-a"},
        "e-2": {"state": "leader_review", "updated_by_session_id": "sv-b"},
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-fan0002"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project.get("beacon-test", [])
    progress_events = [
        e for e in events if e["channel"] == "trek-progress-check"
    ]
    # broadcast fallback (= recipient_session_id 未設定 or "") 1 件のみ
    assert len(progress_events) == 1
    assert progress_events[0]["payload"].get("recipient_session_id", "") == ""


def test_fanout_fresh_session_with_no_claims_still_receives_tick():
    """fresh session (= 何も claim していない) は fallback 経路で tick を貰う
    (= todo task を pick up する経路)。"""
    _seed_trek(
        trek_id="tk-fan0003",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-88"}],
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-fresh"],
    )
    # task_states 空 = どの session も claim していない
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-fan0003"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    progress_events = [
        e for e in events if e["channel"] == "trek-progress-check"
    ]
    recipients = [
        e["payload"].get("recipient_session_id", "") for e in progress_events
    ]
    assert "sv-fresh" in recipients


# ---------------------------------------------------------------------------
# ms-88 / e-2109 second pass — leader 除外を fanout filter に追加 (2026-06-20)
# ---------------------------------------------------------------------------

def test_fanout_excludes_leader_session_from_progress_check():
    """ms-88 / e-2109 (補完): leader_session_id は progress-check の宛先から
    除外される。 leader は executor の進捗を促す立場ではない (= 役割が違う、
    CORE doc trek-leader-stance / e-2166 と整合)、 progress-check は executor
    だけに届く。"""
    _seed_trek(
        trek_id="tk-leader-excl",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-88"}],
    )
    # leader (sv-leader) + executor (sv-exec) の 2 session
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader", "sv-exec"],
    )
    # sv-exec が working な claim
    _treks["tk-leader-excl"]["task_states"] = {
        "e-1": {"state": "working", "updated_by_session_id": "sv-exec"},
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-leader-excl"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    progress_events = [
        e for e in events if e["channel"] == "trek-progress-check"
    ]
    recipients = [
        e["payload"].get("recipient_session_id", "") for e in progress_events
    ]
    # leader (sv-leader) は受け取らない、 executor (sv-exec) だけ
    assert "sv-leader" not in recipients
    assert "sv-exec" in recipients


def test_fanout_leader_excluded_even_with_no_claims():
    """leader_session_id は claim 0 件 (= fresh session 扱い) でも除外される。
    e-2109 の補完 (fresh fallback) は executor のみに適用、 leader には適用
    しない。 そうしないと leader が「fresh session、 todo 取りに行ってもらう
    ため tick」 扱いされて 永久 tick の温床になる (= 2026-06-19 tk-40b0b27c
    観測の根本原因)。"""
    _seed_trek(
        trek_id="tk-leader-no-claim",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-88"}],
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader"],
    )
    # leader だけ、 claim も無し
    _treks["tk-leader-no-claim"]["task_states"] = {}
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-leader-no-claim"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project.get("beacon-test", [])
    progress_events = [
        e for e in events if e["channel"] == "trek-progress-check"
    ]
    # 全 live session が leader (= 除外) → broadcast fallback (空 recipient) のみ
    assert len(progress_events) == 1
    assert progress_events[0]["payload"].get("recipient_session_id", "") == ""
