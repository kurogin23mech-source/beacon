"""ms-97 / e-2659 Phase 3 — AC16 / AC17 cross-project fanout tests.

AC16: 1 trek の scope が 2+ projects にまたがる時、 各 member の home
project bus に直接 progress-check DM が届く (= scope walk の中継経路に
依存せず、 members[] iteration で完結する構造)。

AC17: 同 tick で複数 project bus に append された event_id は project
間で衝突しない (= 1 DM event = 1 project bus への 1 append、 cross-
project event id collision は構造的に不可)。

これらは server-level integration tests として、 in-memory db stub と
TestClient で `/api/system/trek-scheduler/tick` を叩いて検証する。

Phase A+ trek (= session_id keyed) に絞ったテストである点に注意:
pre-A trek の cross-project fanout は legacy scope walk path (= 既存
`test_trek_scheduler_api.py::test_cross_project_trek_fans_dm_to_each_member_home_project`)
で従来通り regression テスト済。 本ファイルは Phase A+ 専用の新経路
(= `_build_executor_targets_session_grain`) を pin する。
"""
from __future__ import annotations

import copy
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


def _mock_list_sessions(project_id: str):
    return copy.deepcopy(_sessions_by_project.get(project_id, []))


def _mock_get_project(project_id: str):
    return copy.deepcopy(_projects.get(project_id))


def _mock_list_projects(user_id: str = "", include_archived: bool = False):
    return [
        {"project_id": pid, "name": data.get("name", "")}
        for pid, data in _projects.items()
    ]


def _register_project(pid: str) -> None:
    _projects[pid] = {"name": pid.rsplit("-", 1)[0] if "-" in pid else pid}


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
    # AC17: event id encodes project_id so cross-project collision is
    # structurally impossible. The production backend uses uuid4 but the
    # in-memory stub uses ``ev-<project>-<i>`` so test assertions can
    # observe both the count + per-project naming.
    event_id = f"ev-{project_id}-{len(bus)}"
    bus.append({"event_id": event_id, **copy.deepcopy(data)})
    return event_id


@pytest.fixture(autouse=True)
def _rebind_db():
    db_module = app_module.db
    prior = {}
    binds = [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("append_bus_event", _mock_append_bus_event),
        ("list_sessions", _mock_list_sessions),
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
# Channel discriminator (same shape as test_trek_scheduler_api.py)
# ---------------------------------------------------------------------------

def _is_progress_check(e: dict) -> bool:
    if e.get("channel") != "dm":
        return False
    payload = e.get("payload") or {}
    return payload.get("origin_channel") == "trek-progress-check"


# ---------------------------------------------------------------------------
# Phase A+ trek seed helper
# ---------------------------------------------------------------------------

def _seed_phase_a_trek(*,
                       trek_id: str,
                       scope: list[dict],
                       members: list[dict],
                       leader_session_id: str = "",
                       cadence: int = 10) -> dict:
    """Insert a phase A+ trek (meta.migration_phase='A') into the store."""
    t = {
        "trek_id": trek_id,
        "title": "phase-a fanout test trek",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {
            "user_id": members[0]["user_id"] if members else "",
            "email": "",
        },
        "leader_session_id": leader_session_id,
        "members": members,
        "scope": scope,
        "halt": None,
        "goal_state": "",
        "meta": {
            "cadence_minutes": cadence,
            "migration_phase": "A",
        },
        "task_states": {
            # Unclaim todo so the lazy-start gate opens for executors.
            "e-todo1": {"state": "todo"},
            "e-todo2": {"state": "todo"},
        },
        "created_at": "2026-06-28T00:00:00.000000Z",
        "updated_at": "2026-06-28T00:00:00.000000Z",
        "archived_at": None,
    }
    _treks[trek_id] = copy.deepcopy(t)
    return t


# ---------------------------------------------------------------------------
# AC16 positive: 2-project fanout — each project gets exactly one dm
# ---------------------------------------------------------------------------

def test_ac16_two_project_phase_a_fanout_yields_one_dm_per_project():
    """2 project にまたがる phase A+ trek が members[] iterate で fanout する。"""
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    p1 = "proj-leader"
    p2 = "proj-exec"
    _register_project(p1)
    _register_project(p2)
    _seed_phase_a_trek(
        trek_id="tk-ac16-2p",
        scope=[
            {"project": p1, "milestone": "ms-x"},
            {"project": p2, "milestone": "ms-x"},
        ],
        leader_session_id="sv-leader",
        members=[
            {"user_id": "uid-leader", "session_id": "sv-leader",
             "role": "leader", "joined_at": now_iso},
            {"user_id": "uid-exec", "session_id": "sv-exec",
             "role": "member", "joined_at": now_iso},
        ],
    )
    _sessions_by_project[p1] = [
        {"session_id": "sv-leader", "user_id": "uid-leader",
         "last_active": now_iso},
    ]
    _sessions_by_project[p2] = [
        {"session_id": "sv-exec", "user_id": "uid-exec",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ac16-2p"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    fired = resp.json()["fired"]
    assert len(fired) == 1

    # AC16: executor's dm landed in p2 (= their home project), NOT in p1.
    p2_progress = [
        e for e in _bus_events_by_project.get(p2, [])
        if _is_progress_check(e)
    ]
    assert len(p2_progress) == 1
    assert p2_progress[0]["payload"]["recipient_session_id"] == "sv-exec"

    # Leader is structurally excluded from progress-check.
    p1_progress = [
        e for e in _bus_events_by_project.get(p1, [])
        if _is_progress_check(e)
    ]
    assert len(p1_progress) == 0


def test_ac16_three_project_phase_a_fanout_each_executor_in_own_project():
    """3-project phase A+ fanout: 2 executors, each in different scope project."""
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    p1, p2, p3 = "proj-leader", "proj-exec-a", "proj-exec-b"
    for pid in (p1, p2, p3):
        _register_project(pid)
    _seed_phase_a_trek(
        trek_id="tk-ac16-3p",
        scope=[
            {"project": p1, "milestone": "ms-y"},
            {"project": p2, "milestone": "ms-y"},
            {"project": p3, "milestone": "ms-y"},
        ],
        leader_session_id="sv-leader",
        members=[
            {"user_id": "uid-leader", "session_id": "sv-leader",
             "role": "leader", "joined_at": now_iso},
            {"user_id": "uid-a", "session_id": "sv-exec-a",
             "role": "member", "joined_at": now_iso},
            {"user_id": "uid-b", "session_id": "sv-exec-b",
             "role": "member", "joined_at": now_iso},
        ],
    )
    _sessions_by_project[p1] = [
        {"session_id": "sv-leader", "user_id": "uid-leader",
         "last_active": now_iso},
    ]
    _sessions_by_project[p2] = [
        {"session_id": "sv-exec-a", "user_id": "uid-a",
         "last_active": now_iso},
    ]
    _sessions_by_project[p3] = [
        {"session_id": "sv-exec-b", "user_id": "uid-b",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ac16-3p"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    audit = resp.json()["fired"][0]
    assert sorted(audit["recipient_home_project_ids"]) == sorted([p2, p3])
    # Each executor's dm only lands in their own project's bus.
    assert len([
        e for e in _bus_events_by_project.get(p2, [])
        if _is_progress_check(e)
    ]) == 1
    assert len([
        e for e in _bus_events_by_project.get(p3, [])
        if _is_progress_check(e)
    ]) == 1


def test_ac16_session_outside_scope_is_skipped_with_warning():
    """AC16 ghost-member path: member.session_id not in any scope project →
    skipped (= no dm sent), tick still succeeds.
    """
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    p1 = "proj-only"
    _register_project(p1)
    _seed_phase_a_trek(
        trek_id="tk-ghost",
        scope=[{"project": p1, "milestone": "ms-z"}],
        leader_session_id="sv-leader",
        members=[
            {"user_id": "uid-leader", "session_id": "sv-leader",
             "role": "leader", "joined_at": now_iso},
            # This member's session_id does NOT appear in any scope
            # project's session registry → ghost member.
            {"user_id": "uid-ghost", "session_id": "sv-ghost",
             "role": "member", "joined_at": now_iso},
        ],
    )
    _sessions_by_project[p1] = [
        {"session_id": "sv-leader", "user_id": "uid-leader",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ghost"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    audit = resp.json()["fired"][0]
    # No real executor target landed (the ghost was skipped).
    assert audit["recipient_home_project_ids"] == []


# ---------------------------------------------------------------------------
# AC17: per-project event id uniqueness
# ---------------------------------------------------------------------------

def test_ac17_event_ids_unique_across_projects():
    """同 tick で 2 project bus に append された event_id が衝突しない。"""
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    p1, p2 = "proj-l", "proj-e"
    _register_project(p1)
    _register_project(p2)
    _seed_phase_a_trek(
        trek_id="tk-ac17",
        scope=[
            {"project": p1, "milestone": "ms"},
            {"project": p2, "milestone": "ms"},
        ],
        leader_session_id="sv-leader",
        members=[
            {"user_id": "uid-l", "session_id": "sv-leader",
             "role": "leader", "joined_at": now_iso},
            {"user_id": "uid-e", "session_id": "sv-exec",
             "role": "member", "joined_at": now_iso},
        ],
    )
    _sessions_by_project[p1] = [
        {"session_id": "sv-leader", "user_id": "uid-l",
         "last_active": now_iso},
    ]
    _sessions_by_project[p2] = [
        {"session_id": "sv-exec", "user_id": "uid-e",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-ac17"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    # Collect all event_ids minted this tick across both buses.
    all_ids: list[str] = []
    for pid in (p1, p2):
        for e in _bus_events_by_project.get(pid, []):
            all_ids.append(e["event_id"])
    assert len(all_ids) >= 1
    # No duplicates across projects.
    assert len(all_ids) == len(set(all_ids))


# ---------------------------------------------------------------------------
# Negative: pre-A trek does NOT take the new members iterate path
# ---------------------------------------------------------------------------

def test_pre_a_trek_uses_legacy_user_grain_scope_walk():
    """Pre-A trek (= migration_phase 未設定) は legacy scope walk path を
    走り、 session_id が members[] に居なくても user_id 一致で fanout する。

    Regression-guard: phase A+ 専用の新経路が pre-A trek を巻き込まない
    ことを構造保証する。
    """
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    p1, p2 = "proj-l2", "proj-e2"
    _register_project(p1)
    _register_project(p2)
    # Pre-A trek: members[] has no session_id field.
    t = {
        "trek_id": "tk-prea",
        "title": "pre-a regression-guard",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": "uid-l2", "email": ""},
        "leader_session_id": "sv-leader-pre",
        "members": [
            {"user_id": "uid-l2", "role": "leader",
             "joined_at": now_iso},
            {"user_id": "uid-e2", "role": "member",
             "joined_at": now_iso},
        ],
        "scope": [
            {"project": p1, "milestone": "ms"},
            {"project": p2, "milestone": "ms"},
        ],
        "halt": None,
        "goal_state": "",
        "meta": {"cadence_minutes": 10},  # NOTE: no migration_phase
        "task_states": {"e-todo1": {"state": "todo"}},
        "created_at": now_iso,
        "updated_at": now_iso,
        "archived_at": None,
    }
    _treks["tk-prea"] = copy.deepcopy(t)
    _sessions_by_project[p1] = [
        {"session_id": "sv-leader-pre", "user_id": "uid-l2",
         "last_active": now_iso},
    ]
    _sessions_by_project[p2] = [
        # Fresh session_id (= not in trek.members because pre-A doesn't
        # track session_id), but user_id matches → legacy path should
        # accept it.
        {"session_id": "sv-fresh-exec", "user_id": "uid-e2",
         "last_active": now_iso},
    ]
    resp = client.post(
        "/api/system/trek-scheduler/tick",
        json={"trek_ids": ["tk-prea"]},
        headers=HEADERS_OK,
    )
    assert resp.status_code == 200, resp.text
    audit = resp.json()["fired"][0]
    # Pre-A path resolves uid-e2 via user_id grain → dm lands in p2.
    assert p2 in audit["recipient_home_project_ids"]
