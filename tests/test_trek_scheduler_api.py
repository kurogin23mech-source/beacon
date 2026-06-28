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
    """Return True iff ``e`` is a Trek progress-check dm event (e-2639)."""
    if e.get("channel") != "dm":
        return False
    payload = e.get("payload") or {}
    return payload.get("origin_channel") == _TREK_PROGRESS_ORIGIN


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


def test_leader_digest_fans_out_to_all_live_leader_sessions():
    """Multi-session leader (= same user logged in from N bclaude) gets
    the digest delivered to each live session, not just the originally
    stamped one. This is the core fix for the LPS dogfood observation.

    ms-97 / e-2613 (AC33) — seed task_states so the leader-digest gate
    opens (= todo float). The fan-out behaviour stays under test.
    """
    _seed_trek(
        trek_id="tk-digestfan1",
        status="active",
        cadence=10,
        scope=[{"project": "beacon-test", "milestone": "ms-95"}],
    )
    _treks["tk-digestfan1"]["task_states"] = {"e-todo1": {"state": "todo"}}
    # Leader user has 3 live sessions: the original stamped one + 2
    # extras (= reconnect / fork scenarios). All three are within the
    # 10-minute live cutoff so they all qualify.
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
    # All three live sessions are recorded as digest recipients.
    assert sorted(fired["leader_digest_recipients"]) == [
        "sv-leader", "sv-leader-fork", "sv-leader-mac",
    ]
    # The stamped leader_session_id audit field is preserved untouched
    # (= "did not silently re-stamp the doc just because we fanned out").
    assert fired["leader_session_id"] == "sv-leader"

    # Three digest events landed, one per session, all addressed.
    events = _bus_events_by_project["beacon-test"]
    digest_events = [e for e in events if _is_trek_leader_digest_event(e)]
    assert len(digest_events) == 3
    recipients = sorted(
        e["payload"].get("recipient_session_id", "") for e in digest_events
    )
    assert recipients == ["sv-leader", "sv-leader-fork", "sv-leader-mac"]


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
    届く」 問題の構造解)。

    ms-97 / e-2613 (AC33) — extended: terminal-claim session に対しても
    unclaim todo float が存在すれば fire する (= 「次の仕事が残ってるなら
    pick up しに来い」)。 unclaim todo が無い場合のみ silent。

    本テストは unclaim todo 無し状態を維持して original e-2109 silent
    semantics を pin する。 sv-fresh は AC33 lazy-start gate に該当
    しないので broadcast fallback (= recipient="") 経路に degrade する。
    """
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
    # sv-active が working な claim を持つ、 sv-finished は done のみ。
    # AC33 silent semantics を保つため unclaim todo は seed しない。
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
    # sv-active のみが lazy-start で fire、 sv-finished と sv-fresh は
    # AC33 silent (= 仕事無し)、 broadcast fallback で空 recipient が
    # 1 件混ざる場合あり。
    assert "sv-active" in recipients
    assert "sv-finished" not in recipients
    assert "sv-fresh" not in recipients


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
        e for e in events if _is_trek_progress_check_event(e)
    ]
    # broadcast fallback (= recipient_session_id 未設定 or "") 1 件のみ
    assert len(progress_events) == 1
    assert progress_events[0]["payload"].get("recipient_session_id", "") == ""


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
    # 全 live session が leader (= 除外) → broadcast fallback (空 recipient) のみ
    assert len(progress_events) == 1
    assert progress_events[0]["payload"].get("recipient_session_id", "") == ""


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
    # Minimal tick: broadcast fallback (= recipient "") の 1 件のみ。
    assert len(progress_events) == 1
    assert progress_events[0]["payload"].get("recipient_session_id", "") == ""
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
