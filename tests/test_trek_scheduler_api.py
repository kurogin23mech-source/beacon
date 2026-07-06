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
# ms-95 / v0.49.2 (post e-2639) — project registry stub so the scheduler
# tick's ``_resolve_trek_scope_project_ids`` helper can expand a slug
# like "life-plan-simulator" to the canonical full id
# "life-plan-simulator-68c5df" before db.list_sessions / append_bus_event
# are called. Without this the slug-stored scope case (= LPS dogfood
# observation) cannot be exercised in tests.
_projects: dict[str, dict] = {}


def _mock_list_sessions(project_id: str):
    return copy.deepcopy(_sessions_by_project.get(project_id, []))


def _mock_get_project(project_id: str):
    return copy.deepcopy(_projects.get(project_id))


def _mock_list_projects(user_id: str = "", include_archived: bool = False):
    rows: list[dict] = []
    for pid, data in _projects.items():
        if user_id and data.get("owner") and data.get("owner") != user_id:
            continue
        rows.append({"project_id": pid, "name": data.get("name", "")})
    return rows


def _register_project(project_id: str, *, owner: str = "") -> None:
    """Register a fake project so ``_resolve_trek_scope_project_ids``
    can canonicalise scope slugs to ``<slug>-<hex6>``."""
    _projects[project_id] = {
        "name": project_id.rsplit("-", 1)[0] if "-" in project_id else project_id,
        "owner": owner,
    }


def _seed_pool_from_task_states(
    trek_id: str,
    *,
    project_id: str | None = None,
) -> None:
    """ms-99 / e-2833 — synthesize a minimal project pool so
    ``materialize_slots`` can attach each ``task_states`` entry to its MS
    slot's children. Phase 2 tick decisions read the slot inventory
    (= scope[] × pool) rather than the ``task_states`` cache alone; tests
    that only populate ``task_states`` need a pool mirror so the cache
    signal (state / owner) surfaces on materialize.

    Task-narrowed scope entries are already atomic and read cache-first,
    so we only seed MS entries. When ``project_id`` is None (default),
    all unique project_ids in the trek's scope are seeded — this covers
    cross-project treks whose scope entries fan across multiple pools.
    """
    trek = _treks.get(trek_id) or {}
    scope = trek.get("scope") or []
    task_states = trek.get("task_states") or {}
    scoped_task_ids = {s.get("task") for s in scope if s.get("task")}
    ms_task_entries = [
        {"id": eid, "type": "task", "status": "todo"}
        for eid in task_states.keys()
        if eid not in scoped_task_ids
    ]
    if project_id is None:
        # Default: seed each unique project in scope with only its own
        # milestones (= cross-project pools stay independent).
        target_pids = sorted({
            s.get("project") for s in scope if s.get("project")
        })
        for pid in target_pids:
            milestones = [
                {"id": s["milestone"], "entries": list(ms_task_entries)}
                for s in scope
                if s.get("milestone") and s.get("project") == pid
            ]
            existing = _projects.get(pid) or {"name": pid, "owner": ""}
            existing["milestones"] = milestones
            existing.setdefault("operations", [])
            _projects[pid] = existing
    else:
        # Explicit override: seed all scope-declared milestones under
        # this project id (= useful when the tick resolver canonicalises
        # a slug into a full project id and materialize will look up the
        # full id).
        milestones = [
            {"id": s["milestone"], "entries": list(ms_task_entries)}
            for s in scope if s.get("milestone")
        ]
        existing = _projects.get(project_id) or {
            "name": project_id, "owner": "",
        }
        existing["milestones"] = milestones
        existing.setdefault("operations", [])
        _projects[project_id] = existing


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
        # ms-95 / v0.49.2 — slug-resolving scheduler tick needs both
        # get_project (= fast path full-id check) and list_projects
        # (= slug expansion scan) to be deterministic in tests.
        ("get_project", _mock_get_project),
        ("list_projects", _mock_list_projects),
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


# ---------------------------------------------------------------------------
# Channel recognition helpers (ms-95 / e-2639)
#
# Trek scheduler tick was migrated from dedicated ``trek-progress-check`` /
# ``trek-leader-digest`` channels to the shared ``dm`` channel with a
# ``payload.origin_channel`` discriminator (= ms-97 SPEC 中心原則 6
# 「Wake 経路は DM と完全同一」). These helpers let tests assert behaviour
# without re-encoding the dm-transport wiring everywhere.
# ---------------------------------------------------------------------------

_TREK_PROGRESS_ORIGIN = "trek-progress-check"
_TREK_LEADER_DIGEST_ORIGIN = "trek-leader-digest"


def _is_trek_progress_check_event(e: dict) -> bool:
    """Return True iff ``e`` is a Trek progress-check dm event (e-2639).

    ms-97 / e-2637 — welcome tick も同じ origin_channel を使うが、
    sender_type=trek-welcome で区別される。 既存 AC16 / AC33 tests は
    regular tick だけを対象にしているので、 welcome 由来は除外する。
    welcome tick 専用の検査が必要な場合は payload.sender_type を直接
    フィルタすること。
    """
    if e.get("channel") != "dm":
        return False
    payload = e.get("payload") or {}
    if payload.get("origin_channel") != _TREK_PROGRESS_ORIGIN:
        return False
    return payload.get("sender_type") != "trek-welcome"


def _is_trek_leader_digest_event(e: dict) -> bool:
    """Return True iff ``e`` is a Trek leader-digest dm event (e-2639)."""
    if e.get("channel") != "dm":
        return False
    payload = e.get("payload") or {}
    return payload.get("origin_channel") == _TREK_LEADER_DIGEST_ORIGIN


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

    # Bus events landed: the cadence tick fires both progress-check
    # (= executor surface) and leader-digest (= leader surface, added in
    # ms-92 / e-2164). Pre-e-2639 these were dedicated channels; post
    # ms-95 / e-2639 they share the ``dm`` channel with an
    # ``origin_channel`` discriminator (= ms-97 SPEC 中心原則 6).
    events = _bus_events_by_project["beacon-test"]
    progress_events = [e for e in events if _is_trek_progress_check_event(e)]
    assert len(progress_events) == 1
    ev = progress_events[0]
    assert ev["channel"] == "dm"
    assert ev["delivery"] == "auto-execute"
    assert ev["envelope"]["tier"] == "T1-system"
    assert ev["envelope"]["issuer"] == "beacon-system"
    assert ev["envelope"]["scope"] == "trek:tk-aaaa1111"
    assert ev["envelope"]["actions_authorized"] == ["trek.progress_check"]
    assert ev["payload"]["trek_id"] == "tk-aaaa1111"
    assert ev["payload"]["kind"] == "trek-progress-check"
    # Sender identity marker so receivers can filter Trek tick from
    # human DMs without parsing the envelope tier.
    assert ev["payload"]["sender_type"] == "trek-scheduler"
    assert ev["payload"]["origin_channel"] == "trek-progress-check"

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

    ms-97 / e-2613 (AC33) — leader-digest now requires lazy-start signal
    (= leader_review queue / todo float / completion imminent). Seed an
    unclaim todo so the digest gate opens.
    """
    _seed_trek(
        trek_id="tk-digest01",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-92"}],
    )
    # AC33: provide a todo float so the leader-digest gate opens.
    _treks["tk-digest01"]["task_states"] = {
        "e-todo1": {"state": "todo"},
    }
    _seed_pool_from_task_states("tk-digest01")
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
    # Both ride on the shared ``dm`` channel post e-2639 with
    # ``origin_channel`` markers distinguishing the two surfaces.
    events = _bus_events_by_project["beacon-test"]
    progress_events = [e for e in events if _is_trek_progress_check_event(e)]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(progress_events) >= 1
    assert len(digest_events) >= 1

    # Inspect the digest event shape.
    digest_event = digest_events[0]
    assert digest_event["channel"] == "dm"
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
    assert payload["sender_type"] == "trek-scheduler"
    assert payload["origin_channel"] == "trek-leader-digest"


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

    # No leader-digest event in the bus (= progress-check minimal tick
    # still fires as a broadcast fallback per AC33).
    events = _bus_events_by_project["beacon-test"]
    progress_events = [e for e in events if _is_trek_progress_check_event(e)]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(progress_events) >= 1
    assert digest_events == []


def test_due_trek_stamps_last_leader_digest_at_when_fired():
    _seed_trek(
        trek_id="tk-stamp001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-92"}],
    )
    # ms-97 / e-2613 (AC33) — leader-digest now lazy-gated; seed a todo
    # float so the gate opens and the stamp lands.
    _treks["tk-stamp001"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-stamp001")
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stamp001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    saved = _treks["tk-stamp001"]
    assert saved["meta"]["last_leader_digest_at"]


# ---------------------------------------------------------------------------
# ms-95 / e-2539 — leader-digest fan-out to all leader live sessions
#
# Background: cross-project Trek tk-7a3b88b9 dogfood (PE + LPS, 2026-06-27)
# observed that when the leader reconnects (= new bclaude session, fork,
# or machine switch), trek_doc.leader_session_id still points at the
# original (now dead) session_id. The pre-fix dispatch path was rigid
# single-sid append → bus event landed at the dead sid → leader's live
# bridge never picked it up → meta.last_leader_digest_at stamped without
# functional delivery.
#
# Fix: dispatch is now session-grain (= mirror what progress-check has
# done since e-2036). Every live session belonging to the leader user
# receives the digest. Backward compat: if no live leader session
# resolves, fall back to the stamped leader_session_id so dead-leader
# treks still get observable fires.
# ---------------------------------------------------------------------------


def test_leader_digest_targets_stamped_leader_session_only_when_live():
    """ms-95 / e-2645 — **narrow fanout**: stamped ``leader_session_id``
    が live なら **その 1 session のみ** に digest を送る。 旧実装は
    「leader user の全 live session に broadcast」 で、 same-user
    multi-session dogfood (= leader / executor を同 user の別 session が
    兼ねる) で executor session が leader-digest を受信する病理を生んだ
    (= dogfood findings § #16)。
    """
    _seed_trek(
        trek_id="tk-digestfan1",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-digestfan1"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-digestfan1")
    # Leader user has 3 live sessions: the original stamped one + 2
    # extras. With e-2645 narrow fanout, only the stamped sid gets the
    # digest because it IS live (= primary path triggers).
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader", "sv-leader-fork", "sv-leader-mac"],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-digestfan1"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    fired = body["fired"][0]
    # ms-95 / e-2645 — Only the stamped leader session gets the digest.
    assert fired["leader_digest_recipients"] == ["sv-leader"]
    # The stamped leader_session_id audit field is preserved untouched.
    assert fired["leader_session_id"] == "sv-leader"

    # One digest event lands (= narrow path).
    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 1
    assert digest_events[0]["payload"]["recipient_session_id"] == "sv-leader"


def test_leader_digest_falls_back_to_leader_user_sessions_when_stamped_stale():
    """ms-95 / e-2645 — **fallback path**: stamped ``leader_session_id``
    が stale (= live でない) の時のみ、 leader user の全 live session に
    fan out する (= ms-92 e-2164 multi-leader-session 互換、 reconnect /
    fork で sid が変わったときに digest が宙に消えない経路)。
    """
    _seed_trek(
        trek_id="tk-digestfan2",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-digestfan2"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-digestfan2")
    # Stamped is "sv-leader" but only "sv-leader-fork" + "sv-leader-mac"
    # are live → fallback to leader user's live sessions.
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader-fork", "sv-leader-mac"],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-digestfan2"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Fallback fan out reaches both live leader-user sessions.
    assert sorted(fired["leader_digest_recipients"]) == [
        "sv-leader-fork", "sv-leader-mac",
    ]
    # Stamped audit field preserved (= we did not silently re-stamp).
    assert fired["leader_session_id"] == "sv-leader"

    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 2
    recipients = sorted(
        e["payload"].get("recipient_session_id", "") for e in digest_events
    )
    assert recipients == ["sv-leader-fork", "sv-leader-mac"]


def test_leader_digest_delivers_to_live_session_when_stamped_is_stale():
    """The regression reproducer: stamped leader_session_id points at a
    DEAD session (= leader reconnected, original session gone), but the
    leader user has a different live session. The digest must reach the
    live one — not silently appended to the dead sid.

    ms-97 / e-2613 (AC33) — seed todo float so leader-digest gate opens.
    """
    _seed_trek(
        trek_id="tk-stamp-stale",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-stamp-stale"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-stamp-stale")
    # stamped leader is "sv-leader" but only "sv-leader-reconnect" is
    # live in the session directory.
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader-reconnect"],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-stamp-stale"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Digest went to the live sid, not the stale stamped one.
    assert fired["leader_digest_recipients"] == ["sv-leader-reconnect"]
    # Stamped audit field still reads the original (= we did not
    # silently mutate the trek doc; transfer is a separate command).
    assert fired["leader_session_id"] == "sv-leader"

    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 1
    assert digest_events[0]["payload"]["recipient_session_id"] == \
        "sv-leader-reconnect"


def test_leader_digest_falls_back_to_stamped_when_no_live_leader_session():
    """Backward compat: leader has no live session at all (= away from
    desk). The digest falls back to the stamped leader_session_id so
    behaviour stays compatible with the pre-fix single-sid path. The
    fired event lets observability surface (= dashboard) keep showing
    'last leader digest at X' without going dark.

    ms-97 / e-2613 (AC33) — seed todo float so leader-digest gate opens.
    """
    _seed_trek(
        trek_id="tk-leader-away",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-leader-away"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-leader-away")
    # No sessions for the leader user in the directory. The pre-fix
    # path relied on the stamped sid alone, so the fallback must
    # reproduce that.
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=[],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-leader-away"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Fallback recipient is the stamped leader_session_id.
    assert fired["leader_digest_recipients"] == ["sv-leader"]
    assert fired["leader_session_id"] == "sv-leader"


def test_leader_digest_ignores_non_leader_live_sessions():
    """Live sessions belonging to non-leader members must NOT receive
    the leader digest — they are progress-check material, not leader
    surface. Guards against accidentally cross-wiring the two
    channels.

    ms-97 / e-2613 (AC33) — seed todo float so leader-digest gate opens
    AND the executor sees the unclaim work.
    """
    # Add a second non-leader member to the trek so we can register a
    # live session for them.
    _seed_trek(
        trek_id="tk-nonleader",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-nonleader"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-nonleader")
    _treks["tk-nonleader"]["members"].append({
        "user_id": "uid-executor", "email": "ex@b.com",
        "role": "member", "invited_at": "2026-06-27T00:00:00.000000Z",
        "joined_at": "2026-06-27T00:00:00.000000Z",
        "invited_by": "uid-leader",
    })
    # Leader has 1 live session; executor (non-leader) has another.
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project["beacon-test"] = [
        {"session_id": "sv-leader-live", "user_id": "uid-leader",
         "last_active": now_iso},
        {"session_id": "sv-executor-live", "user_id": "uid-executor",
         "last_active": now_iso},
    ]

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-nonleader"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Only the leader's live session received the digest.
    assert fired["leader_digest_recipients"] == ["sv-leader-live"]

    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 1
    assert digest_events[0]["payload"]["recipient_session_id"] == \
        "sv-leader-live"
    # The executor live session DID receive a progress-check event
    # (= that channel is for them); just not the digest.
    progress_events = [
        e for e in events if _is_trek_progress_check_event(e)
    ]
    progress_recipients = {
        e["payload"].get("recipient_session_id", "") for e in progress_events
    }
    assert "sv-executor-live" in progress_recipients


# ---------------------------------------------------------------------------
# ms-95 / e-2723 — same-user multi-session leader-digest collapse fix
#
# Background: 2026-06-28 dogfood (tk-7495e1be) observed that leader-digest
# was being delivered to **every same-user session** when the primary
# stamped sid was stale (= reconnect/fork scenario). The old fallback
# path filtered solely on ``info["user_id"] == leader_user_id``, which
# collapses when leader + executor share a user_id (= dogfood multi-role
# self-test). Fix: phase A+ trek's fallback path narrows by
# ``members[]`` ``role=leader`` session_id, structurally excluding
# executor sessions regardless of user_id overlap. ms-92 e-2164 SPEC
# Done when #1 explicitly says "recipient = leader_session_id のみ".
# ---------------------------------------------------------------------------


def _mark_trek_session_id_keyed(trek_id: str) -> None:
    """Flip a seeded trek to phase A (= session_id keyed members[]) so
    the e-2723 fallback narrow exercises the new branch."""
    meta = _treks[trek_id].setdefault("meta", {})
    meta["migration_phase"] = "A"


def test_leader_digest_fallback_narrows_to_role_leader_session_same_user():
    """ms-95 / e-2723 — same-user multi-session collapse fix.

    Setup: leader + executor are the SAME user (= dogfood multi-role
    self-test on one machine, or Mac+Win parallel sessions of the same
    Beacon dev). Stamped leader_session_id is stale (= the original
    leader session has gone away). Both leader's fresh session AND the
    executor session are live under the same user_id.

    Pre-e-2723 behaviour: fallback path matched on user_id alone →
    both sessions received the leader digest (= cross-wired channels,
    executor saw leader noise).

    Post-e-2723 behaviour: phase A+ fallback walks members[] and picks
    only role=leader sessions. The executor session is structurally
    excluded.
    """
    _seed_trek(
        trek_id="tk-e2723-sameuser",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-e2723-sameuser"]["task_states"] = {
        "e-todo1": {"state": "todo"},
    }
    _seed_pool_from_task_states("tk-e2723-sameuser")
    # Phase A+ trek: members[] is session_id keyed AND both roles live
    # under the same user_id (= the dogfood collapse condition).
    _mark_trek_session_id_keyed("tk-e2723-sameuser")
    _treks["tk-e2723-sameuser"]["members"] = [
        {
            "user_id": "uid-leader", "email": "a@b.com",
            "session_id": "sv-leader-fresh",
            "role": "leader",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
        {
            "user_id": "uid-leader", "email": "a@b.com",
            "session_id": "sv-executor",
            "role": "executor",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
    ]
    # Stamped sid is the original (= now stale, not live).
    _treks["tk-e2723-sameuser"]["leader_session_id"] = "sv-leader-stale"
    # Live registry: only the fresh leader sid and the executor sid are
    # present. The stamped stale sid is NOT live → primary path falls
    # through to the fallback.
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader-fresh", "sv-executor"],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-e2723-sameuser"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Critical pin: digest reaches ONLY the leader's fresh session.
    # Pre-fix this list would include both "sv-leader-fresh" and
    # "sv-executor" because both share uid-leader.
    assert fired["leader_digest_recipients"] == ["sv-leader-fresh"]

    # Bus events: exactly one digest event, addressed to the leader.
    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 1
    assert digest_events[0]["payload"]["recipient_session_id"] == \
        "sv-leader-fresh"


def test_leader_digest_fallback_excludes_executor_when_cross_user():
    """ms-95 / e-2723 regression guard — cross-user trek (= different
    user_ids for leader and executor) must continue to narrow correctly
    on the fallback path. This pins the cross-user case so the
    same-user narrow doesn't accidentally regress the original
    behaviour.
    """
    _seed_trek(
        trek_id="tk-e2723-crossuser",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-e2723-crossuser"]["task_states"] = {
        "e-todo1": {"state": "todo"},
    }
    _seed_pool_from_task_states("tk-e2723-crossuser")
    _mark_trek_session_id_keyed("tk-e2723-crossuser")
    _treks["tk-e2723-crossuser"]["members"] = [
        {
            "user_id": "uid-leader", "email": "lead@b.com",
            "session_id": "sv-leader-fresh",
            "role": "leader",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
        {
            "user_id": "uid-executor", "email": "ex@b.com",
            "session_id": "sv-executor",
            "role": "executor",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
    ]
    # Stamped sid is stale → fallback path.
    _treks["tk-e2723-crossuser"]["leader_session_id"] = "sv-leader-stale"
    # Both live but under different user_ids.
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project["beacon-test"] = [
        {"session_id": "sv-leader-fresh", "user_id": "uid-leader",
         "last_active": now_iso},
        {"session_id": "sv-executor", "user_id": "uid-executor",
         "last_active": now_iso},
    ]

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-e2723-crossuser"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Cross-user: digest reaches the leader's fresh session only.
    assert fired["leader_digest_recipients"] == ["sv-leader-fresh"]

    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 1
    assert digest_events[0]["payload"]["recipient_session_id"] == \
        "sv-leader-fresh"


def test_leader_digest_fallback_skips_when_no_leader_member_session_live():
    """ms-95 / e-2723 — phase A+ trek fallback. If no role=leader member
    has a live session_id, the digest falls back to the stamped sid
    (= same observability surface as 'leader fully offline').
    """
    _seed_trek(
        trek_id="tk-e2723-leader-down",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-e2723-leader-down"]["task_states"] = {
        "e-todo1": {"state": "todo"},
    }
    _mark_trek_session_id_keyed("tk-e2723-leader-down")
    _treks["tk-e2723-leader-down"]["members"] = [
        {
            "user_id": "uid-leader", "email": "lead@b.com",
            "session_id": "sv-leader-offline",  # not in live registry
            "role": "leader",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
        {
            "user_id": "uid-leader", "email": "lead@b.com",
            "session_id": "sv-executor",
            "role": "executor",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
    ]
    _treks["tk-e2723-leader-down"]["leader_session_id"] = "sv-leader-stale"
    # Only the executor session is live; no leader session is live at
    # all.
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-executor"],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-e2723-leader-down"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Fallback observability surface: stamped sid is used so the
    # dashboard's last_leader_digest_at keeps stamping.
    assert fired["leader_digest_recipients"] == ["sv-leader-stale"]
    # Critical pin: executor (= same user) must NOT receive the digest
    # even though their session is live.
    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    digest_recipients = {
        e["payload"].get("recipient_session_id", "") for e in digest_events
    }
    assert "sv-executor" not in digest_recipients


# ---------------------------------------------------------------------------
# ms-95 / v0.49.2 — slug ↔ canonical project_id resolution in scheduler tick
#
# Background: PR #271 fixed the task-add endpoint to canonicalise
# ``scope[].project`` from slug to full id, but the scheduler tick path
# was not touched. v0.49.1 deploy still left LPS without leader-digest
# delivery because ``scope[0]['project']`` is "life-plan-simulator" (=
# the slug users type at ``beacon trek plan --add-scope <slug>:<ms>``)
# but ``db.list_sessions`` / ``db.append_bus_event`` need the full id
# "life-plan-simulator-68c5df" to find the leader session and write to
# the right bus. The scheduler-side helper (now
# ``_resolve_trek_scope_project_ids`` post e-2639) closes this gap for
# the progress-check and idle-escalation loops.
# ---------------------------------------------------------------------------


def test_slug_stored_scope_resolves_to_canonical_full_project_id():
    """LPS dogfood reproducer (2026-06-27): scope is stored as slug,
    leader's live session is registered under the full id project.

    Pre-fix: ``db.list_sessions("life-plan-simulator")`` returns nothing
    because that slug isn't a real project id → ``leader_live_sids`` is
    empty → fallback to stamped sid → digest event is appended to the
    "life-plan-simulator" *slug* bus, which no real bridge listens to.

    Post-fix: helper canonicalises to "life-plan-simulator-68c5df", so
    list_sessions finds the leader's live sid AND the bus event lands
    in the real project's bus where the leader actually subscribes.
    """
    slug = "life-plan-simulator"
    full_id = "life-plan-simulator-68c5df"
    _register_project(full_id, owner="uid-leader")
    _seed_trek(
        trek_id="tk-slug-resolve",
        status="active",
        cadence=10,
        scope=[{"project": slug, "milestone": "ms-22"}],
    )
    # ms-97 / e-2613 (AC33) — seed todo float so leader-digest fires.
    _treks["tk-slug-resolve"]["task_states"] = {"e-todo1": {"state": "todo"}}
    # ms-99 / e-2833 — seed the pool under the canonicalised full id.
    # The scheduler's slug resolver rewrites scope in place before the
    # tick fires, so materialize_slots looks the pool up under full_id.
    _seed_pool_from_task_states("tk-slug-resolve", project_id=full_id)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    # Leader's live session lives in the **full id** project (= mirrors
    # what a real bclaude bridge does: it registers under the cwd
    # project's full id, never under a bare slug).
    _sessions_by_project[full_id] = [
        {"session_id": "sv-leader-live", "user_id": "uid-leader",
         "last_active": now_iso},
    ]

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-slug-resolve"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    # Audit field reports the canonical full id (= proves the helper
    # ran and downstream code saw the right project).
    assert fired["project_id"] == full_id
    # Leader's actual live session was reached, not the fallback
    # stamped sid (= the LPS dogfood failure mode would have produced
    # ["sv-leader"] from the trek_doc.leader_session_id fallback).
    assert fired["leader_digest_recipients"] == ["sv-leader-live"]

    # Bus events landed in the full-id project's bus, not the slug
    # bus. The slug bucket must remain empty.
    assert slug not in _bus_events_by_project
    full_events = _bus_events_by_project[full_id]
    digest_events = [
        e for e in full_events if _is_trek_leader_digest_event(e)
    ]
    assert len(digest_events) == 1
    assert digest_events[0]["payload"]["recipient_session_id"] == \
        "sv-leader-live"


def test_full_id_stored_scope_passes_through_unchanged():
    """When scope is already stored as the canonical full id, the
    helper's fast path returns it as-is — no slug expansion, no
    spurious project lookup. Guards the helper against accidentally
    re-resolving an already-canonical id."""
    full_id = "life-plan-simulator-68c5df"
    _register_project(full_id, owner="uid-leader")
    _seed_trek(
        trek_id="tk-already-full",
        status="active",
        cadence=10,
        scope=[{"project": full_id, "milestone": "ms-22"}],
    )
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project[full_id] = [
        {"session_id": "sv-leader-live", "user_id": "uid-leader",
         "last_active": now_iso},
    ]

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-already-full"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"][0]
    assert fired["project_id"] == full_id
    assert fired["leader_digest_recipients"] == ["sv-leader-live"]


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
    # One progress-check dm (cadence due, e-2639 transport) + one
    # notify (idle, still on the legacy notify channel because it's
    # user-facing not AI-wake).
    progress_events = [e for e in events if _is_trek_progress_check_event(e)]
    notify_events = [e for e in events if e["channel"] == "notify"]
    assert len(progress_events) >= 1
    assert len(notify_events) >= 1
    notify_event = notify_events[0]
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
    # ms-97 / e-2613 (AC33) — seed todo float so both progress-check and
    # leader-digest fire (= original 2-event expectation preserved). The
    # cadence-decision test is about temporal gating, not lazy-start.
    _treks["tk-cccc3333"]["task_states"] = {"e-todo1": {"state": "todo"}}
    _seed_pool_from_task_states("tk-cccc3333")
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
    # Only one cadence-fire landed. Each fire emits both a progress-
    # check and a leader-digest (= ms-92 / e-2164). Post e-2639 both
    # ride on the ``dm`` channel with ``origin_channel`` markers, so
    # the two events live in the ``dm`` channel slice of the bus.
    events = _bus_events_by_project["beacon-test"]
    assert len(events) == 2
    progress_events = [e for e in events if _is_trek_progress_check_event(e)]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(progress_events) == 1
    assert len(digest_events) == 1


# ---------------------------------------------------------------------------
# Auto-stall pass (ms-75 / e-2067) — HTTP integration
# ---------------------------------------------------------------------------

def test_auto_stall_transitions_stalled_task_to_leader_review():
    """ms-88 / e-2107 + ms-95 / e-2646: auto-stall 罰則先が `leader_review`。
    Default TTL は 24h に再緩和されたので、 test は per-trek
    ``stall_threshold_minutes=12`` を inject して 13 min silence で
    mechanism が発火することを verify。"""
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
    # ms-95 / e-2646 — inject 12-min threshold so mechanism stays testable
    # without waiting 24h.
    _treks["tk-stall0001"]["meta"]["stall_threshold_minutes"] = 12
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
    assert entry["ttl_minutes"] == 12  # ms-95 / e-2646 — injected, not default

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
    leader_review。 ms-95 / e-2646: TTL default 24h なので test は短い
    threshold を inject して 13 min stale で発火させる。"""
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
    # ms-95 / e-2646 — inject 12-min threshold.
    _treks["tk-stall0005"]["meta"]["stall_threshold_minutes"] = 12
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
    """ms-97 / e-2815 (2026-07-03): lazy-start gate 撤廃後の invariant。

    旧 ms-88 e-2109 は 「terminal-claim session (= done / leader_review /
    user_review だけを持つ session) は tick を受け取らない」 を pin して
    いた。 dogfood で 「Trek scope は MS-level bind、 実装は project pool
    側で走る」 主流ケースで LPS 等の executor が silent-skip されて
    scheduler → executor 経路が事実上停止する事故が発生 (user 指摘
    「executor に行かないと意味ないじゃん」)。 fix (e-2815): lazy-start
    gate を撤廃、 全 non-leader member を無条件 fanout する。

    本テストはその新 invariant を pin する: sv-active / sv-finished /
    sv-fresh の全 3 executor が progress-check を受信する (claim 状態や
    unclaim todo 有無に関わらず)。
    """
    _seed_trek(
        trek_id="tk-fan0001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-88"}],
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-active", "sv-finished", "sv-fresh"],
    )
    _treks["tk-fan0001"]["task_states"] = {
        "e-w": {"state": "working", "updated_by_session_id": "sv-active"},
        "e-d": {"state": "done", "updated_by_session_id": "sv-finished"},
    }
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-fan0001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    progress_events = [
        e for e in events if _is_trek_progress_check_event(e)
    ]
    recipients = sorted(
        e["payload"].get("recipient_session_id", "") for e in progress_events
    )
    # e-2815: 全 3 executor が recipient に出現する (= 無条件 fanout)。
    assert "sv-active" in recipients, (
        "e-2815: active-claim executor must receive fire (unchanged)."
    )
    assert "sv-finished" in recipients, (
        "e-2815: terminal-only-claim executor must ALSO receive fire "
        "(this is the invariant flip vs the pre-e-2815 gate)."
    )
    assert "sv-fresh" in recipients, (
        "e-2815: fresh (no-claim) executor must ALSO receive fire."
    )


def test_fanout_skips_all_sessions_when_all_have_only_terminal_claims():
    """ms-97 / e-2815 — 全 executor が terminal claim だけ持つ状態でも
    無条件 fanout する (旧 「全員 skip → broadcast fallback だけ」 pin を
    invariant flip)。 broadcast fallback 経路は今も残るが、 executor が
    home-resolvable + live である限りは executor 経路優先で fire する。
    """
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
    # 両 session ともに terminal-only claim
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
        e for e in events if _is_trek_progress_check_event(e)
    ]
    recipients = sorted(
        e["payload"].get("recipient_session_id", "") for e in progress_events
    )
    # e-2815: 両 executor が recipient に立つ (broadcast fallback は不要)。
    assert "sv-a" in recipients, "e-2815: sv-a must be targeted."
    assert "sv-b" in recipients, "e-2815: sv-b must be targeted."


def test_fanout_fresh_session_with_no_claims_still_receives_tick():
    """fresh session (= 何も claim していない) は fallback 経路で tick を貰う
    (= todo task を pick up する経路)。

    ms-97 / e-2613 (AC33) — fresh session の lazy-start には unclaim todo が
    必要。 seed しないと「 nothing to do here」 と判断され broadcast
    fallback に degrade する。 unclaim todo を seed して legacy fresh
    pick-up 経路を維持する。
    """
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
    # AC33: seed unclaim todo so sv-fresh gets ticked directly.
    _treks["tk-fan0003"]["task_states"] = {"e-todo1": {"state": "todo"}}
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-fan0003"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    progress_events = [
        e for e in events if _is_trek_progress_check_event(e)
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
        e for e in events if _is_trek_progress_check_event(e)
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
        e for e in events if _is_trek_progress_check_event(e)
    ]
    # ms-97 / e-2660 — 全 live session が leader (= 除外) → broadcast
    # fallback (recipient=stamped leader_session_id) のみ。 旧実装は
    # recipient_session_id="" だったが、 DM routing filter が drop する
    # ので leader sid を stamp する経路へ変更 (= 配送可能化 + leader が
    # 「fanout が executor 不在で fallback した」 audit signal を受信)。
    assert len(progress_events) == 1
    assert (
        progress_events[0]["payload"].get("recipient_session_id", "")
        == "sv-leader"
    )


# ---------------------------------------------------------------------------
# ms-97 / e-2612 (AC32) — Halt 中の tick fire 全停止
# ---------------------------------------------------------------------------


def _seed_halted_trek(trek_id: str, *,
                      scope: list[dict] | None = None) -> dict:
    """Helper: a due trek with halt set."""
    t = _seed_trek(
        trek_id=trek_id,
        status="active",
        cadence=10,
        scope=scope or [{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _treks[trek_id]["halt"] = {
        "issued_at": "2026-06-28T00:00:00.000000Z",
        "issued_by_session_id": "sv-leader",
        "reason": "AC32 dogfood",
    }
    return t


def test_halt_skips_executor_progress_check_fire():
    """AC32: halt 中の trek は executor tick (= trek-progress-check) を
    打たない。 candidate / due には残るが、 fired / events は空。
    """
    _seed_halted_trek("tk-halt-exec")
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-halt-exec"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # candidate / due には残る (= halt は status を変えない)、 fired 0、
    # halted リストに 1 件。
    assert body["candidates"] == 1
    assert body["fired"] == []
    assert any(h["trek_id"] == "tk-halt-exec" for h in body["halted"])
    # Bus events も書かれない。
    events = _bus_events_by_project.get("beacon-test", [])
    progress_events = [
        e for e in events if _is_trek_progress_check_event(e)
    ]
    assert progress_events == []


def test_halt_skips_leader_digest_fanout():
    """AC32: halt 中は leader-digest channel も書かれない。"""
    _seed_halted_trek("tk-halt-digest")
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader"],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-halt-digest"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    events = _bus_events_by_project.get("beacon-test", [])
    digest_events = [
        e for e in events if _is_trek_leader_digest_event(e)
    ]
    assert digest_events == [], (
        "AC32: leader-digest must not fire while halt is set"
    )


def test_halt_skips_idle_escalation():
    """AC32: halt 中は idle escalation (= notify channel) も発火しない。
    Halt は autonomous activity 全体の pause なので、 idle 検出による
    user 通知も leader が cord を引いた間は止まる。 Resume 後に再度
    idle 判定 → escalate という流れに戻す。
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    # Idle 判定が走るように last_session_response_at を古く設定。
    very_stale = (now - datetime.timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_halted_trek("tk-halt-idle")
    _treks["tk-halt-idle"]["meta"]["last_session_response_at"] = very_stale
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-halt-idle"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["escalations"] == [], (
        "AC32: idle escalation must not fire while halt is set"
    )
    events = _bus_events_by_project.get("beacon-test", [])
    notify_events = [e for e in events if e["channel"] == "notify"]
    assert notify_events == []


# ---------------------------------------------------------------------------
# ms-97 / e-2613 (AC33) — Tick fire lazy start (per-executor / per-leader)
# ---------------------------------------------------------------------------


def test_lazy_start_no_signal_yields_broadcast_fallback_only():
    """AC33 edge case: 全 lazy-start gate が closed (= claim 無し /
    unclaim todo 無し / leader_review 無し / todo float 無し /
    completion not imminent) でも、 minimal tick 1 件は fire する
    (= no complete silence)。 broadcast fallback (= recipient_session_id "")
    がその minimal tick の正体。 leader-digest は AC33 で完全 silent。
    """
    _seed_trek(
        trek_id="tk-lazy-silent",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader"],
    )
    # task_states 空 = どの session も claim していない、 unclaim todo
    # 無し、 leader_review 無し、 todo float 無し → 全 gate close。
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-lazy-silent"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    events = _bus_events_by_project.get("beacon-test", [])
    progress_events = [
        e for e in events if _is_trek_progress_check_event(e)
    ]
    digest_events = [
        e for e in events if _is_trek_leader_digest_event(e)
    ]
    # ms-97 / e-2660 — Minimal tick: broadcast fallback (= recipient =
    # stamped leader_session_id) の 1 件のみ。 旧実装は recipient_session_id=""
    # で post していたが _bus_event_addressed_to が drop する穴があり、
    # 配送可能な leader sid stamp に変更。
    assert len(progress_events) == 1
    assert (
        progress_events[0]["payload"].get("recipient_session_id", "")
        == "sv-leader"
    )
    # Leader-digest は AC33 で silent。
    assert digest_events == []


def test_lazy_start_leader_digest_fires_on_leader_review_queue():
    """AC33: leader_review queue が non-empty なら leader-digest 発火。"""
    _seed_trek(
        trek_id="tk-lazy-lreview",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _treks["tk-lazy-lreview"]["task_states"] = {
        "e-1": {"state": "leader_review",
                "updated_by_session_id": "sv-exec"},
    }
    _seed_pool_from_task_states("tk-lazy-lreview")
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader"],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-lazy-lreview"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    digest_events = [
        e for e in events if _is_trek_leader_digest_event(e)
    ]
    assert len(digest_events) >= 1


def test_lazy_start_leader_digest_silent_when_all_working():
    """AC33: working-only trek (= 多数 slot が non-terminal) は leader
    action 不要として digest silent。 progress-check は executor へ届く。
    """
    _seed_trek(
        trek_id="tk-lazy-working",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    # 4 working tasks under sv-exec — leader action は不要 (= not
    # imminent, not leader_review, not todo float)。
    _treks["tk-lazy-working"]["task_states"] = {
        "e-1": {"state": "working", "updated_by_session_id": "sv-exec"},
        "e-2": {"state": "working", "updated_by_session_id": "sv-exec"},
        "e-3": {"state": "working", "updated_by_session_id": "sv-exec"},
        "e-4": {"state": "working", "updated_by_session_id": "sv-exec"},
    }
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader", "sv-exec"],
    )
    # Add sv-exec to members so the live filter matches.
    _treks["tk-lazy-working"]["members"].append({
        "user_id": "uid-leader", "email": "a@b.com", "role": "member",
        "invited_at": "2026-06-28T00:00:00.000000Z",
        "joined_at": "2026-06-28T00:00:00.000000Z",
        "invited_by": "uid-leader",
    })
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-lazy-working"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    digest_events = [
        e for e in events if _is_trek_leader_digest_event(e)
    ]
    # AC33: working-only → leader silent.
    assert digest_events == []


def test_lazy_start_executor_silent_with_only_terminal_claims_and_no_todo():
    """AC33: executor whose claims are all terminal AND no unclaim todo
    → silent (= stop condition)。 broadcast fallback だけが minimal tick。
    """
    _seed_trek(
        trek_id="tk-lazy-stop",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _treks["tk-lazy-stop"]["task_states"] = {
        "e-1": {"state": "done", "updated_by_session_id": "sv-exec"},
    }
    _seed_pool_from_task_states("tk-lazy-stop")
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-exec"],
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-lazy-stop"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    body = resp.json()
    # is_trek_task_aggregate_terminal もこの状態 (= 全 done) を quiesce
    # に分類する。 fired 0 / quiesced 1 を観察する経路。
    assert body["fired"] == []
    assert len(body["quiesced"]) == 1


# ---------------------------------------------------------------------------
# ms-99 / e-2834 — Quiesce observability trio (diag log + meta stamp + DM)
# ---------------------------------------------------------------------------


def _is_trek_quiesce_dm(e: dict) -> bool:
    payload = e.get("payload") or {}
    return payload.get("kind") == "trek-quiesced"


def _seed_quiesced_trek(trek_id: str) -> None:
    """Shared fixture: a trek whose materialised slots all read terminal."""
    _seed_trek(
        trek_id=trek_id,
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-99"}],
    )
    _treks[trek_id]["task_states"] = {
        "e-1": {"state": "done", "updated_by_session_id": "sv-exec"},
    }
    _seed_pool_from_task_states(trek_id)
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-leader"],
    )


def test_quiesce_stamps_meta_quiesced_at_and_reason():
    """e-2834 AC3: quiesce hit で meta.quiesced_at と quiesce_reason が
    stamp される。 CLI (= beacon trek show --json) から観測できるように
    trek doc に永続する。"""
    _seed_quiesced_trek("tk-quiesce-stamp")
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-quiesce-stamp"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    saved = _treks["tk-quiesce-stamp"]
    meta = saved.get("meta") or {}
    assert meta.get("quiesced_at"), (
        "quiesce path must stamp meta.quiesced_at so /beacon-trek-review "
        "and beacon trek show can render the quiesce transition."
    )
    assert meta.get("quiesce_reason") == "task_state_aggregate_terminal"


def test_quiesce_emits_leader_dm_once():
    """e-2834 AC2: quiesce hit で leader session に 1 回だけ DM が届く。
    payload.kind='trek-quiesced' + recipient_session_id=leader_sid で
    /beacon-trek-review Skill が完遂遷移を検知できる。"""
    _seed_quiesced_trek("tk-quiesce-dm")
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-quiesce-dm"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    quiesce_dms = [e for e in events if _is_trek_quiesce_dm(e)]
    assert len(quiesce_dms) == 1, (
        f"expected exactly 1 trek-quiesced DM, got {len(quiesce_dms)}"
    )
    dm = quiesce_dms[0]
    assert dm["channel"] == "dm"
    assert dm["delivery"] == "auto-execute"
    payload = dm["payload"]
    assert payload["kind"] == "trek-quiesced"
    assert payload["recipient_session_id"] == "sv-leader"
    assert payload["trek_id"] == "tk-quiesce-dm"
    assert payload["sender_type"] == "trek-scheduler"
    assert payload["reason"] == "task_state_aggregate_terminal"


def test_quiesce_stamps_completion_notified_at_and_marks_dm():
    """ms-97 P3 (review H1 / C2 decision): completion_ready (AC20/21) was
    structurally unreachable — every aggregate-terminal trek is caught by the
    quiesce branch and ``continue``s before the completion_ready block. The
    fix evaluates completion_ready inside the quiesce branch, so a terminal,
    non-Op trek now (a) stamps ``meta.completion_notified_at`` (the missing
    half of the AC21 stop condition) and (b) marks the quiesce DM with
    ``completion_ready=True``. This is the tick-integration pin the pure
    ``is_completion_ready`` tests could not provide."""
    _seed_quiesced_trek("tk-p3-completion")
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-p3-completion"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    saved = _treks["tk-p3-completion"]
    assert saved["meta"].get("completion_notified_at"), (
        "quiesce branch must stamp completion_notified_at so the AC21 stop "
        "condition (summary_sent_at AND completion_notified_at) is reachable"
    )
    events = _bus_events_by_project["beacon-test"]
    quiesce_dms = [e for e in events if _is_trek_quiesce_dm(e)]
    assert len(quiesce_dms) == 1
    assert quiesce_dms[0]["payload"].get("completion_ready") is True, (
        "the quiesce DM must carry completion_ready=True so the leader-side "
        "renders a completion (AC20) rather than a plain quiesce notice"
    )


def test_completion_notified_at_does_not_refire_on_second_quiesce_tick():
    """ms-97 P3 — one-shot: once completion_notified_at is stamped,
    ``is_completion_ready`` returns False, so a second due tick in the same
    terminal state does NOT re-stamp or re-mark. Guards against a
    completion-signal loop."""
    _seed_quiesced_trek("tk-p3-oneshot")
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-p3-oneshot"]},
        headers=HEADERS_OK,
    )
    first_stamp = _treks["tk-p3-oneshot"]["meta"].get("completion_notified_at")
    assert first_stamp
    # Make the trek due again (cadence elapsed) without leaving terminal.
    _treks["tk-p3-oneshot"]["meta"]["last_progress_check_at"] = ""
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-p3-oneshot"]},
        headers=HEADERS_OK,
    )
    # Stamp unchanged (idempotent), no second completion-marked DM.
    assert _treks["tk-p3-oneshot"]["meta"]["completion_notified_at"] == first_stamp
    events = _bus_events_by_project["beacon-test"]
    marked = [e for e in events
              if (e.get("payload") or {}).get("completion_ready") is True]
    assert len(marked) == 1, (
        "completion_ready must be marked on exactly one DM across both ticks"
    )


def test_quiesce_dm_dedupes_on_second_tick():
    """e-2834 AC2: 同 quiesce lifecycle 内では 2 回目の tick で DM を
    再送しない (meta.quiesce_notified_at で dedup)。 leader が同一 quiesce
    に対して重複通知を受けない forcing function。"""
    _seed_quiesced_trek("tk-quiesce-dedup")
    # First tick emits the DM.
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-quiesce-dedup"]},
        headers=HEADERS_OK,
    )
    saved_after_first = _treks["tk-quiesce-dedup"]
    assert saved_after_first["meta"].get("quiesce_notified_at"), (
        "first tick must stamp quiesce_notified_at"
    )
    # Reset last_progress_check_at so the second tick is due (= cadence
    # elapsed). Otherwise ``is_trek_due`` short-circuits before we reach
    # the quiesce branch.
    saved_after_first["meta"]["last_progress_check_at"] = ""
    # Second tick: same quiesce state, but quiesce_notified_at already
    # stamped. No new DM should fire.
    events_before = list(_bus_events_by_project["beacon-test"])
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-quiesce-dedup"]},
        headers=HEADERS_OK,
    )
    events_after = _bus_events_by_project["beacon-test"]
    new_quiesce_dms = [
        e for e in events_after[len(events_before):]
        if _is_trek_quiesce_dm(e)
    ]
    assert new_quiesce_dms == [], (
        f"second tick must NOT re-emit quiesce DM; got {len(new_quiesce_dms)}"
    )


def test_quiesce_marks_cleared_when_task_transitions_out_of_terminal():
    """e-2834 AC2 / AC3 補完: task が terminal 外へ遷移すると quiesce
    marks (= quiesced_at / quiesce_notified_at / quiesce_reason) が
    クリアされ、 次の quiesce lifecycle で新規 DM が発火可能になる。"""
    _seed_quiesced_trek("tk-quiesce-reset")
    trek = _treks["tk-quiesce-reset"]
    # Align member user_id with the test client's default "dev" identity
    # so the PATCH endpoint's member check passes.
    trek["members"][0]["user_id"] = "dev"
    trek["members"][0]["email"] = "dev@local"
    trek["creator_actor"]["user_id"] = "dev"
    trek["meta"]["quiesced_at"] = "2026-07-04T00:00:00.000000Z"
    trek["meta"]["quiesce_notified_at"] = "2026-07-04T00:00:00.000000Z"
    trek["meta"]["quiesce_reason"] = "task_state_aggregate_terminal"
    # Rewind the task to a non-terminal state via the PATCH endpoint.
    resp = client.patch(
        "/api/treks/tk-quiesce-reset/task-state",
        json={"task_id": "e-1", "state": "working"},
        headers={"X-Beacon-Session": "sv-leader"},
    )
    assert resp.status_code == 200, resp.text
    saved = _treks["tk-quiesce-reset"]
    meta = saved.get("meta") or {}
    assert meta.get("quiesced_at") is None, (
        "task-state PATCH to non-terminal must clear quiesced_at so a "
        "future quiesce lifecycle fires a fresh DM"
    )
    assert meta.get("quiesce_notified_at") is None
    assert meta.get("quiesce_reason") is None


def test_quiesce_emits_diag_log_line_to_stderr(capfd):
    """e-2834 AC1: quiesce hit で stderr に diag[ms-99 e-2834] プレフィックス
    の log 行が emit される (Cloud Run stderr → GCP Logs で grep 可能)。"""
    _seed_quiesced_trek("tk-quiesce-diag")
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-quiesce-diag"]},
        headers=HEADERS_OK,
    )
    _, err = capfd.readouterr()
    assert "diag[ms-99 e-2834]" in err, (
        f"expected diag[ms-99 e-2834] prefix in stderr, got: {err[:400]}"
    )
    assert "trek=tk-quiesce-diag" in err
    assert "quiesced" in err
    assert "reason=task_state_aggregate_terminal" in err


def test_resume_clears_halt_and_tick_fires_again():
    """AC32 補完: halt クリア後 (= clear_halt) は次 tick で発火復帰する。
    Resume 後に「再度 trek-progress-check が動く」 ことを確認する。
    """
    _seed_halted_trek("tk-halt-resume")
    # First tick: halted, no fire.
    r1 = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-halt-resume"]},
        headers=HEADERS_OK,
    )
    assert r1.status_code == 200
    assert r1.json()["fired"] == []
    # Clear halt (= simulate beacon trek resume).
    _treks["tk-halt-resume"]["halt"] = None
    # Second tick should fire normally.
    r2 = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-halt-resume"]},
        headers=HEADERS_OK,
    )
    assert r2.status_code == 200
    body = r2.json()
    assert len(body["fired"]) == 1
    assert body["fired"][0]["trek_id"] == "tk-halt-resume"
    assert body["halted"] == []


# ---------------------------------------------------------------------------
# ms-95 / e-2639 — dm transport per-member fanout cross-project verification
#
# Background: Pre-e-2639 the tick wrote to ``scope[0]['project']`` only.
# A cross-project Trek (= scope[0] = project P1, scope[1] = project P2,
# scope[2] = project P3) with members living in different home projects
# left scope[1..N] members permanently deaf because the bridge only
# subscribes to its own project's bus. The 2026-06-28 dogfood
# (tk-LPS cross-project Trek) surfaced this as opened ✗ for every
# member except scope[0] residents.
#
# Post-e-2639 fix: walk each scope project's session registry, stitch a
# (user_id, session_id, home_project_id) tuple per live member session,
# then post one dm event per session into that session's home project
# bus. SPEC AC17: "Cross-project Trek で scope[1..N] の各 project に
# 住む member session が tick (= progress-check / leader-digest) を
# 受信できることを実機 dogfood で確認" — this test pins the structural
# guarantee in unit-test form so the regression cannot silently come
# back.
# ---------------------------------------------------------------------------


def test_cross_project_trek_fans_dm_to_each_member_home_project():
    """3-member cross-project Trek (= the e-2639 reproducer in test form).

    Setup:
      * Trek scope spans 3 projects P1 / P2 / P3.
      * Leader lives in P1 (= scope[0]).
      * Executor A lives in P2 (= scope[1]).
      * Executor B lives in P3 (= scope[2]).
      * All three executors have unclaim todo so the lazy-start gate
        opens and the dm fires.

    Expected behaviour:
      * 1 dm per executor session lands in *that executor's home
        project bus* (= AC17 cross-project delivery guarantee).
      * 1 dm for the leader-digest lands in the leader's home project
        bus.
      * No event lands in a project where the recipient does not live.
    """
    p1 = "proj-leader"
    p2 = "proj-exec-a"
    p3 = "proj-exec-b"

    # Trek scope spans all three projects. Leader sits in scope[0].
    _seed_trek(
        trek_id="tk-cross3p",
        status="active",
        cadence=10,
        scope=[
            {"project": p1, "milestone": "ms-95"},
            {"project": p2, "milestone": "ms-95"},
            {"project": p3, "milestone": "ms-95"},
        ],
    )
    # Add executor members so the dm fanout sees more than the leader.
    _treks["tk-cross3p"]["members"].extend([
        {
            "user_id": "uid-exec-a", "email": "a@b.com", "role": "member",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
        {
            "user_id": "uid-exec-b", "email": "b@b.com", "role": "member",
            "invited_at": "2026-06-28T00:00:00.000000Z",
            "joined_at": "2026-06-28T00:00:00.000000Z",
            "invited_by": "uid-leader",
        },
    ])
    # Seed unclaim todo so the executor lazy-start gate (AC33) opens.
    _treks["tk-cross3p"]["task_states"] = {
        "e-todo1": {"state": "todo"},
        "e-todo2": {"state": "todo"},
    }
    _seed_pool_from_task_states("tk-cross3p")
    # Each member lives in a different project's session registry —
    # exactly the cross-project topology that broke pre-e-2639.
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project[p1] = [
        {"session_id": "sv-leader", "user_id": "uid-leader",
         "last_active": now_iso},
    ]
    _sessions_by_project[p2] = [
        {"session_id": "sv-exec-a", "user_id": "uid-exec-a",
         "last_active": now_iso},
    ]
    _sessions_by_project[p3] = [
        {"session_id": "sv-exec-b", "user_id": "uid-exec-b",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-cross3p"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"]
    assert len(fired) == 1
    audit = fired[0]
    # Audit surface: per-recipient home project_ids are recorded so
    # observers can verify cross-project delivery happened.
    assert sorted(audit["recipient_home_project_ids"]) == sorted([p2, p3])
    # Leader-digest landed in the leader's home project (= p1, scope[0]).
    assert audit["leader_digest_target_project_ids"] == [p1]

    # Executor A's dm landed ONLY in P2's bus (= their home project),
    # not in P1 or P3 (= we explicitly check the cross-project routing).
    p2_events = _bus_events_by_project.get(p2, [])
    p2_progress = [e for e in p2_events if _is_trek_progress_check_event(e)]
    assert len(p2_progress) == 1
    assert p2_progress[0]["payload"]["recipient_session_id"] == "sv-exec-a"

    # Executor B's dm landed ONLY in P3's bus.
    p3_events = _bus_events_by_project.get(p3, [])
    p3_progress = [e for e in p3_events if _is_trek_progress_check_event(e)]
    assert len(p3_progress) == 1
    assert p3_progress[0]["payload"]["recipient_session_id"] == "sv-exec-b"

    # Leader's digest landed in P1's bus. The leader is structurally
    # excluded from progress-check (= role filter), so the only Trek
    # tick event in P1 should be the leader-digest, addressed to the
    # leader's session.
    p1_events = _bus_events_by_project.get(p1, [])
    p1_progress = [e for e in p1_events if _is_trek_progress_check_event(e)]
    p1_digest = [e for e in p1_events if _is_trek_leader_digest_event(e)]
    assert p1_progress == [], (
        "leader's home project must not receive a progress-check dm — "
        "the leader is structurally excluded from the executor surface"
    )
    assert len(p1_digest) == 1
    assert p1_digest[0]["payload"]["recipient_session_id"] == "sv-leader"

    # No project should have received an event addressed to a session
    # that doesn't live in it (= the pre-e-2639 regression mode).
    for pid, events in _bus_events_by_project.items():
        for ev in events:
            sid = ev.get("payload", {}).get("recipient_session_id") or ""
            if not sid:
                continue
            # The session must live in this project (i.e., its home
            # project for the bridge to subscribe).
            session_homes = {
                s["session_id"]: pid_inner
                for pid_inner, sessions in _sessions_by_project.items()
                for s in sessions
            }
            assert session_homes.get(sid) == pid, (
                f"event addressed to {sid} landed in {pid} but its "
                f"home project is {session_homes.get(sid)} — cross-"
                f"project mis-routing regression"
            )


def test_cross_project_trek_executor_in_scope1_receives_dm_in_own_bus():
    """Minimal AC17 reproducer: scope[1] executor receives the dm in
    their OWN home project bus, not in scope[0].

    Pre-e-2639 the dm would have gone to scope[0]['project'] only, so
    this test would have asserted on an empty bus. Post-fix the
    executor's home project bus carries the event.
    """
    scope0 = "proj-home-of-leader"
    scope1 = "proj-home-of-exec"
    _seed_trek(
        trek_id="tk-ac17",
        status="active",
        cadence=10,
        scope=[
            {"project": scope0, "milestone": "ms-97"},
            {"project": scope1, "milestone": "ms-97"},
        ],
    )
    _treks["tk-ac17"]["members"].append({
        "user_id": "uid-exec", "email": "ex@b.com", "role": "member",
        "invited_at": "2026-06-28T00:00:00.000000Z",
        "joined_at": "2026-06-28T00:00:00.000000Z",
        "invited_by": "uid-leader",
    })
    _treks["tk-ac17"]["task_states"] = {"e-todo": {"state": "todo"}}
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _sessions_by_project[scope0] = [
        {"session_id": "sv-leader", "user_id": "uid-leader",
         "last_active": now_iso},
    ]
    _sessions_by_project[scope1] = [
        {"session_id": "sv-exec", "user_id": "uid-exec",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ac17"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text

    # The executor's dm landed in scope1 — their home project.
    s1_progress = [
        e for e in _bus_events_by_project.get(scope1, [])
        if _is_trek_progress_check_event(e)
    ]
    assert len(s1_progress) == 1
    assert s1_progress[0]["payload"]["recipient_session_id"] == "sv-exec"
    # And specifically NOT in scope0 (= the pre-e-2639 regression mode
    # would have written the executor's tick there).
    s0_progress = [
        e for e in _bus_events_by_project.get(scope0, [])
        if _is_trek_progress_check_event(e)
        and e["payload"].get("recipient_session_id") == "sv-exec"
    ]
    assert s0_progress == [], (
        "pre-e-2639 regression: executor dm leaked into scope[0] bus"
    )


def test_progress_check_dm_carries_system_sender_marker():
    """e-2639 AC3: sender identity is system-scheduler (= pseudo sid /
    marker) so receivers can filter Trek tick from human DMs without
    parsing the envelope tier.

    The discriminator is ``payload.sender_type == "trek-scheduler"`` +
    ``payload.origin_channel ∈ {trek-progress-check, trek-leader-digest}``;
    ``sender_session_id`` stays empty (= server-issued, no human-typed
    confusion). This pins the contract so downstream consumers can rely
    on the marker without inspecting envelope internals.
    """
    _seed_trek(
        trek_id="tk-marker001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-marker001"]["task_states"] = {"e-todo": {"state": "todo"}}
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-leader",
        session_ids=["sv-exec"],  # not the stamped leader sid → executor
    )
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-marker001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200
    events = _bus_events_by_project["beacon-test"]
    progress = [e for e in events if _is_trek_progress_check_event(e)]
    assert len(progress) >= 1
    ev = progress[0]
    # Channel is dm — the unified wake rail.
    assert ev["channel"] == "dm"
    # sender_session_id stays empty (= server-issued); receivers must
    # NOT mis-attribute the dm to a human sender.
    assert ev["sender_session_id"] == ""
    # System-scheduler marker pair.
    assert ev["payload"]["sender_type"] == "trek-scheduler"
    assert ev["payload"]["origin_channel"] == "trek-progress-check"
    # Delivery stays auto-execute (= AI autonomous progression).
    assert ev["delivery"] == "auto-execute"


# ---------------------------------------------------------------------------
# ms-95 / e-2644 — fanout snapshot strategy
# ---------------------------------------------------------------------------

def test_executor_with_active_claim_receives_progress_check_even_if_stall_flips_in_same_tick():
    """ms-95 / e-2644 — **snapshot strategy** regression reproducer.

    2026-06-28 dogfood で「executor が active claim を保持していたのに
    progress-check fanout から漏れた」 病理を観察 (= dogfood findings § #19)。
    root cause: 同 tick 内で stall transition が走ると task_states[*].
    updated_by_session_id が "" にリセットされ、 fanout 評価 (=
    session_has_active_claim) で claim 主と session_id が一致せず False。

    本テストは:
      * stall threshold を 12 min に inject (= 同 tick で stall が確実に
        発火する条件を作る)
      * executor session が working claim を 13 min 前に stamp 済み
        (= stall 対象だが、 tick 開始時点では active claim 持ち)

    Expected: snapshot strategy により、 fanout は tick 開始時の
    task_states を参照する → executor は progress-check を受信する。
    stall transition は同 transaction 内で fanout の後に走るので、 次 tick
    以降は leader_review に降格された state が反映される。
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    stale = (now - datetime.timedelta(minutes=13)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    _seed_trek(
        trek_id="tk-snap0001",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
        # last_at が古いと cadence-fire も走る (= due treks に入る)
        last_at=stale,
    )
    # ms-95 / e-2646 — inject short threshold so stall fires this tick.
    _treks["tk-snap0001"]["meta"]["stall_threshold_minutes"] = 12
    _treks["tk-snap0001"]["task_states"] = {
        "e-373": {
            "state": "working",
            "updated_at": stale,
            "last_activity_at": stale,
            "updated_by_session_id": "sv-lps-exec",
            "note": "",
        },
    }
    # Add the executor user as member + live session.
    _treks["tk-snap0001"]["members"].append({
        "user_id": "uid-lps-exec", "email": "lps@b.com",
        "role": "member", "invited_at": "2026-06-27T00:00:00.000000Z",
        "joined_at": "2026-06-27T00:00:00.000000Z",
        "invited_by": "uid-leader",
    })
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _sessions_by_project["beacon-test"] = [
        {"session_id": "sv-leader", "user_id": "uid-leader",
         "last_active": now_iso},
        {"session_id": "sv-lps-exec", "user_id": "uid-lps-exec",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-snap0001"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The stall pass DID fire (= late-tick, after fanout).
    assert len(body["auto_stalled"]) == 1
    assert body["auto_stalled"][0]["task_id"] == "e-373"

    # CRITICAL: the executor still got the progress-check this tick (=
    # snapshot strategy preserved claim attribution during fanout).
    events = _bus_events_by_project["beacon-test"]
    progress_events = [
        e for e in events if _is_trek_progress_check_event(e)
    ]
    recipients = [
        e["payload"].get("recipient_session_id", "") for e in progress_events
    ]
    assert "sv-lps-exec" in recipients, (
        "e-2644 regression: executor with active claim was excluded from "
        "fanout because stall transition ran before/concurrent with the "
        "fanout decision in the same tick."
    )

    # And the trek doc reflects the stall transition (= next-tick state
    # is consistent with the auto_stalled report).
    saved = _treks["tk-snap0001"]
    assert saved["task_states"]["e-373"]["state"] == "leader_review"


# ---------------------------------------------------------------------------
# ms-97 / e-2637 — welcome tick bootstrap (= fresh joiner wake-up event)
# ---------------------------------------------------------------------------

def _seed_phase_A_member(trek_id: str, *, session_id: str,
                         user_id: str, email: str = "exec@x") -> None:
    """Append a session-grain member entry and flip the trek to phase A.

    e-2637 welcome tick is only fired for phase A+ members (= entries
    that carry a session_id field), so the fixture must seed those
    fields explicitly. Also stamps ``leader_session_id`` on the trek if
    not already set, so leader exclusion logic for the regular tick
    keeps working.
    """
    t = _treks[trek_id]
    t.setdefault("members", []).append({
        "user_id": user_id,
        "email": email,
        "role": "member",
        "invited_at": "2026-06-18T00:00:00.000000Z",
        "joined_at": "2026-06-28T00:00:00.000000Z",
        "invited_by": "uid-leader",
        "session_id": session_id,
    })
    t.setdefault("meta", {})["migration_phase"] = "A"


def test_welcome_tick_fires_for_phase_A_member_without_stamp():
    """ms-97 / e-2637 Done when #1+#2 — server tick fires a welcome tick
    for any phase A+ member whose ``meta.welcome_tick_fired_at`` stamp
    is missing. payload carries the AC28 manual doc_id + kickoff hint.
    """
    _seed_trek(
        trek_id="tk-welcome01",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _seed_phase_A_member(
        "tk-welcome01", session_id="sv-fresh-joiner",
        user_id="uid-fresh",
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-fresh",
        session_ids=["sv-fresh-joiner"],
    )

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-welcome01"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200

    events = _bus_events_by_project.get("beacon-test") or []
    welcome_events = [
        e for e in events
        if (e.get("payload") or {}).get("sender_type") == "trek-welcome"
    ]
    assert len(welcome_events) == 1, (
        f"expected 1 welcome tick, got events={events}"
    )
    ev = welcome_events[0]
    payload = ev["payload"]
    # Done when #2: payload carries AC28 manual doc_id + hint copy.
    assert payload["recipient_session_id"] == "sv-fresh-joiner"
    assert payload["manual_doc_id"] == "yfOufm7d2zkAhcm5QWES"
    assert "/beacon-trek-execute" in payload["body"]
    assert "kickoff" in payload["body"].lower()
    # DM channel + auto-execute delivery = standard wake path.
    assert ev["channel"] == "dm"
    assert ev["delivery"] == "auto-execute"
    # T1-system envelope authorises trek.welcome.
    assert ev["envelope"]["tier"] == "T1-system"
    assert "trek.welcome" in ev["envelope"]["actions_authorized"]
    # Done when #3: stamp recorded.
    saved = _treks["tk-welcome01"]
    fired_map = (saved.get("meta") or {}).get("welcome_tick_fired_at") or {}
    assert fired_map.get("sv-fresh-joiner"), (
        f"welcome_tick_fired_at must stamp sv-fresh-joiner, got {fired_map}"
    )


def test_welcome_tick_fires_at_most_once_per_session():
    """ms-97 / e-2637 Done when #3 — idempotent: 2 度目の tick で welcome
    event は新規に発火しない (= meta.welcome_tick_fired_at が gate)。
    """
    _seed_trek(
        trek_id="tk-welcome02",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _seed_phase_A_member(
        "tk-welcome02", session_id="sv-once",
        user_id="uid-once",
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-once",
        session_ids=["sv-once"],
    )

    # First tick fires the welcome.
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-welcome02"]},
        headers=HEADERS_OK,
    )
    events1 = list(_bus_events_by_project.get("beacon-test") or [])
    first_welcome_count = sum(
        1 for e in events1
        if (e.get("payload") or {}).get("sender_type") == "trek-welcome"
    )
    assert first_welcome_count == 1

    # Refresh cadence (= clear last_progress_check_at so next tick is due).
    _treks["tk-welcome02"]["meta"].pop("last_progress_check_at", None)

    # Second tick on the same trek/member: welcome must NOT re-fire.
    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-welcome02"]},
        headers=HEADERS_OK,
    )
    events2 = list(_bus_events_by_project.get("beacon-test") or [])
    welcome_count = sum(
        1 for e in events2
        if (e.get("payload") or {}).get("sender_type") == "trek-welcome"
    )
    assert welcome_count == 1, (
        f"welcome tick must fire at most once per session_id, got "
        f"{welcome_count} events: {events2}"
    )


def test_welcome_tick_independent_of_ac33_lazy_start_gate():
    """ms-97 / e-2637 Done when #4 — welcome tick fires even when AC33
    per-executor lazy start gate is closed (= no claim and no unclaim
    todo). The welcome tick is the path that primes the kickoff ritual
    BEFORE AC33 has any signal.
    """
    _seed_trek(
        trek_id="tk-welcome03",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _seed_phase_A_member(
        "tk-welcome03", session_id="sv-idle",
        user_id="uid-idle",
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-idle",
        session_ids=["sv-idle"],
    )
    # Deliberately seed NO task_states → has_unclaim_todo = False.
    _treks["tk-welcome03"]["task_states"] = {}

    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-welcome03"]},
        headers=HEADERS_OK,
    )
    events = _bus_events_by_project.get("beacon-test") or []
    welcome_events = [
        e for e in events
        if (e.get("payload") or {}).get("sender_type") == "trek-welcome"
    ]
    assert len(welcome_events) == 1, (
        "welcome tick must fire even when AC33 lazy-start gate yields no "
        f"executor target; got events={events}"
    )


def test_ac33_fresh_joiner_both_sessions_receive_tick_when_unclaim_todo_present():
    """ms-97 / e-2638 Done when #1+#2 — e-2636 + e-2637 land 後、 同 user
    別 session を 2 つ join させると両方が AC33 per-executor lazy tick
    の対象になる (= fanout target に含まれる)。

    実機 dogfood の前提条件を unit 範囲で pin する。 fresh joiner は
    claim ゼロ だが ``has_unclaim_todo`` が True なら
    ``should_fire_executor_tick`` は True を返す。
    """
    _seed_trek(
        trek_id="tk-ac33-fresh01",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    # 同 user (= uid-exec) の 2 session を phase A member として登録。
    _seed_phase_A_member(
        "tk-ac33-fresh01", session_id="sv-exec-a",
        user_id="uid-exec",
    )
    _seed_phase_A_member(
        "tk-ac33-fresh01", session_id="sv-exec-b",
        user_id="uid-exec",
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-exec",
        session_ids=["sv-exec-a", "sv-exec-b"],
    )
    # AC33: unclaim todo 1 件を seed して fresh joiner 経路を開く。
    _treks["tk-ac33-fresh01"]["task_states"] = {
        "e-todo-shared": {"state": "todo"},
    }
    # 旧 welcome tick が両 session 分配信される前提で 2 度 tick を
    # 回しても fanout 評価は AC33 に揃って通る。 ここでは tick 1 回で
    # progress-check 経路に 2 session 揃うことを確認する。
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ac33-fresh01"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    events = _bus_events_by_project.get("beacon-test", [])
    # AC33 はあくまで regular tick (= sender_type=trek-scheduler) が対象。
    # welcome tick (= e-2637 bootstrap) は同じ origin_channel を共有する
    # ので、 「両 session が AC33 で wake する」 ことを純粋に pin する
    # ため welcome 由来の event は除外する。
    ac33_events = [
        e for e in events
        if _is_trek_progress_check_event(e)
        and (e.get("payload") or {}).get("sender_type") == "trek-scheduler"
    ]
    recipients = {
        e["payload"].get("recipient_session_id", "") for e in ac33_events
    }
    assert "sv-exec-a" in recipients, (
        f"AC33 must include sv-exec-a in fanout (= fresh joiner #1), "
        f"got {recipients}"
    )
    assert "sv-exec-b" in recipients, (
        f"AC33 must include sv-exec-b in fanout (= fresh joiner #2), "
        f"got {recipients}"
    )


def test_ac33_pure_observer_fresh_joiner_included_after_e2815():
    """ms-97 / e-2815 (2026-07-03) — AC33 の旧 「pure observer は silent」
    仕様 invariant flip 後の pin。 fresh joiner (= no claim, no todo)
    でも member として join した以上、 regular tick を受け取る。

    以前は AC33 lazy-start gate で pure observer が silent skip されて
    いたが、 dogfood で「executor に届かない = Trek scheduler の目的
    そのものが破綻」 と判明したため撤廃 (e-2815)。 本テストは 「observer
    session は sv-observer 宛 progress-check を必ず受信する」 を pin。
    """
    _seed_trek(
        trek_id="tk-ac33-observer",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    _seed_phase_A_member(
        "tk-ac33-observer", session_id="sv-observer",
        user_id="uid-observer",
    )
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-observer",
        session_ids=["sv-observer"],
    )
    # No task_states — 以前は silent 経路、 e-2815 後は fire される。
    _treks["tk-ac33-observer"]["task_states"] = {}

    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ac33-observer"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    events = _bus_events_by_project.get("beacon-test", [])
    # welcome tick は e-2637 bootstrap で無条件 fire するため regular
    # tick と分離して判定する (= sender_type=trek-scheduler のみ)。
    progress_events = [
        e for e in events
        if _is_trek_progress_check_event(e)
        and (e.get("payload") or {}).get("sender_type") != "trek-welcome"
    ]
    observer_targeted = [
        e for e in progress_events
        if e["payload"].get("recipient_session_id") == "sv-observer"
    ]
    assert len(observer_targeted) >= 1, (
        "e-2815: pure observer (= no todo, no claim) MUST receive a "
        "regular tick after the lazy-start gate was removed. "
        f"got events={progress_events}"
    )


def test_welcome_tick_skips_pre_A_member_without_session_id():
    """ms-97 / e-2637 — pre-A members (= no session_id field) are not
    eligible for welcome tick. Phase A migration is the precondition
    (= e-2636 supplies the migration path).
    """
    _seed_trek(
        trek_id="tk-welcome04",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-97"}],
    )
    # Legacy pre-A member: user_id only, no session_id field.
    _treks["tk-welcome04"]["members"].append({
        "user_id": "uid-legacy",
        "email": "legacy@x",
        "role": "member",
        "invited_at": "2026-06-18T00:00:00.000000Z",
        "joined_at": "2026-06-18T00:00:00.000000Z",
        "invited_by": "uid-leader",
    })
    # No migration_phase stamp → pre-A.
    _seed_live_sessions_for_trek(
        "beacon-test",
        user_id="uid-legacy",
        session_ids=["sv-legacy"],
    )

    client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-welcome04"]},
        headers=HEADERS_OK,
    )
    events = _bus_events_by_project.get("beacon-test") or []
    welcome_events = [
        e for e in events
        if (e.get("payload") or {}).get("sender_type") == "trek-welcome"
    ]
    assert welcome_events == [], (
        f"welcome tick must be skipped for pre-A members, got {welcome_events}"
    )
