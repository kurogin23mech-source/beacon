"""Cross-project session directory endpoint (ms-54 / e-1587).

GET /api/me/sessions answers "what bclaude sessions of mine are alive across
ALL my projects right now" — the cross-project counterpart of the project-
scoped /api/projects/{pid}/sessions endpoint. /beacon-dm-send and op-6
watchdog rely on this to skip the cd-into-each-project dance.

These tests lock in:
  * sessions from multiple projects are aggregated and tagged with project_id
    + project_name, so the dm picker can route bus send --project <pid>
  * projects the user is neither owner nor member of are excluded
  * archived projects are excluded
  * live_only / healthy_only / machine / agent filters compose across projects
  * sort order is last_active desc across the aggregate (not per-project)
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

# Per-project in-memory mirror of the sessions subcollection. Same shape as
# test_bus_directory's mock so the seeded rows go through the same code path
# inside the endpoint (list_sessions → _compute_poll_health → filter).
_sessions_store: dict[str, list[dict]] = {}

# In-memory project registry. Each entry: project_id → {name, owner, members,
# archived}. list_projects(user_id=...) below applies the same membership rule
# as firestore_client.list_projects.
_projects_store: dict[str, dict] = {}


def _mock_list_sessions(project_id: str) -> list[dict]:
    items = list(_sessions_store.get(project_id, []))
    items.sort(key=lambda s: s.get("last_active", ""), reverse=True)
    return copy.deepcopy(items)


def _mock_list_projects(user_id: str | None = None,
                       include_archived: bool = False) -> list[dict]:
    result = []
    for pid, data in _projects_store.items():
        if not include_archived and data.get("archived"):
            continue
        if user_id:
            owner = data.get("owner")
            if owner:
                members = [m.get("user_id") for m in data.get("members", [])]
                if owner != user_id and user_id not in members:
                    continue
        result.append({
            "project_id": pid,
            "name": data.get("name", ""),
        })
    return result


firestore_client.list_sessions = _mock_list_sessions
firestore_client.list_projects = _mock_list_projects
firestore_client.get_project = lambda pid: _projects_store.get(pid) or {
    "name": "test", "milestones": []
}
firestore_client.save_project = lambda pid, data: None


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

app_module._auth_enabled = False
app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset_store():
    firestore_client.list_sessions = _mock_list_sessions
    firestore_client.list_projects = _mock_list_projects
    firestore_client.get_project = lambda pid: _projects_store.get(pid) or {
        "name": "test", "milestones": []
    }
    _sessions_store.clear()
    _projects_store.clear()
    yield
    _sessions_store.clear()
    _projects_store.clear()


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed_project(pid: str, name: str, *, owner: str = "dev",
                  members: list | None = None, archived: bool = False) -> None:
    _projects_store[pid] = {
        "name": name,
        "owner": owner,
        "members": members or [],
        "archived": archived,
    }


def _seed_session(pid: str, sid: str, *, last_active: str,
                  machine: str = "", agent: str = "",
                  email: str = "", last_poll_at: str = "",
                  poll_interval_ms: int = 2000, shutdown: bool = False,
                  cwd: str = "", agent_kind: str = "") -> None:
    sess = {
        "session_id": sid,
        "actor": {"email": email, "machine": machine, "agent": agent},
        "last_active": last_active,
        "poll_interval_ms": poll_interval_ms,
        "shutdown": shutdown,
    }
    if last_poll_at:
        sess["last_poll_at"] = last_poll_at
    # e-2520 stable identity fields: cwd + the structural agent.kind.
    if cwd:
        sess["cwd"] = cwd
    if agent_kind:
        sess["agent"] = {"kind": agent_kind}
    _sessions_store.setdefault(pid, []).append(sess)


# In dev/non-auth mode require_auth returns {"sub": "dev"}; seed projects
# owned by "dev" so the membership filter accepts them.
DEV_UID = "dev"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_aggregates_sessions_from_multiple_projects():
    _seed_project("p-alpha", "Alpha", owner=DEV_UID)
    _seed_project("p-beta", "Beta", owner=DEV_UID)
    _seed_session("p-alpha", "s-1", last_active="2026-06-12T00:00:01.000000Z")
    _seed_session("p-beta", "s-2", last_active="2026-06-12T00:00:02.000000Z")

    resp = client.get("/api/me/sessions")
    assert resp.status_code == 200
    rows = resp.json()
    sids = sorted(r["session_id"] for r in rows)
    assert sids == ["s-1", "s-2"]


def test_each_row_carries_project_id_and_name():
    _seed_project("p-alpha", "Alpha Project", owner=DEV_UID)
    _seed_session("p-alpha", "s-1", last_active="2026-06-12T00:00:01.000000Z")

    resp = client.get("/api/me/sessions")
    row = resp.json()[0]
    assert row["project_id"] == "p-alpha"
    assert row["project_name"] == "Alpha Project"


def test_sorted_by_last_active_desc_across_projects():
    _seed_project("p-alpha", "Alpha", owner=DEV_UID)
    _seed_project("p-beta", "Beta", owner=DEV_UID)
    # Interleaved on purpose: alpha-old, beta-newest, alpha-newer.
    _seed_session("p-alpha", "alpha-old",
                  last_active="2026-06-12T00:00:01.000000Z")
    _seed_session("p-beta", "beta-newest",
                  last_active="2026-06-12T00:00:09.000000Z")
    _seed_session("p-alpha", "alpha-newer",
                  last_active="2026-06-12T00:00:05.000000Z")

    rows = client.get("/api/me/sessions").json()
    assert [r["session_id"] for r in rows] == [
        "beta-newest", "alpha-newer", "alpha-old",
    ]


# ---------------------------------------------------------------------------
# Membership boundary
# ---------------------------------------------------------------------------

def test_archived_projects_are_excluded():
    _seed_project("p-active", "Active", owner=DEV_UID)
    _seed_project("p-arch", "Archived", owner=DEV_UID, archived=True)
    _seed_session("p-active", "s-active",
                  last_active="2026-06-12T00:00:01.000000Z")
    _seed_session("p-arch", "s-arch",
                  last_active="2026-06-12T00:00:02.000000Z")

    sids = [r["session_id"] for r in client.get("/api/me/sessions").json()]
    assert sids == ["s-active"]


def test_non_member_projects_are_excluded():
    _seed_project("p-mine", "Mine", owner=DEV_UID)
    _seed_project("p-foreign", "Foreign", owner="someone-else", members=[])
    _seed_session("p-mine", "s-mine",
                  last_active="2026-06-12T00:00:01.000000Z")
    _seed_session("p-foreign", "s-foreign",
                  last_active="2026-06-12T00:00:02.000000Z")

    sids = [r["session_id"] for r in client.get("/api/me/sessions").json()]
    assert sids == ["s-mine"]


# ---------------------------------------------------------------------------
# Filters (compose across projects)
# ---------------------------------------------------------------------------

def test_live_only_filter_applies_across_projects():
    _seed_project("p-a", "A", owner=DEV_UID)
    _seed_project("p-b", "B", owner=DEV_UID)
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = now - datetime.timedelta(seconds=30)
    stale = now - datetime.timedelta(minutes=20)
    _seed_session("p-a", "fresh-a", last_active=_iso(fresh))
    _seed_session("p-b", "stale-b", last_active=_iso(stale))

    rows = client.get("/api/me/sessions?live_only=true&since_minutes=5").json()
    assert [r["session_id"] for r in rows] == ["fresh-a"]


def test_healthy_only_filter_applies_across_projects():
    _seed_project("p-a", "A", owner=DEV_UID)
    _seed_project("p-b", "B", owner=DEV_UID)
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = _iso(now - datetime.timedelta(seconds=10))
    stale = _iso(now - datetime.timedelta(minutes=10))
    # Project A: heartbeating bridge.
    _seed_session("p-a", "healthy-a",
                  last_active=fresh, last_poll_at=fresh)
    # Project B: bridge poll loop is stale.
    _seed_session("p-b", "stale-b",
                  last_active=fresh, last_poll_at=stale)

    rows = client.get("/api/me/sessions?healthy_only=true").json()
    assert [r["session_id"] for r in rows] == ["healthy-a"]


def test_machine_filter_applies_across_projects():
    _seed_project("p-a", "A", owner=DEV_UID)
    _seed_project("p-b", "B", owner=DEV_UID)
    _seed_session("p-a", "mac-row", machine="mac-mini",
                  last_active="2026-06-12T00:00:01.000000Z")
    _seed_session("p-b", "win-row", machine="WORKMACHINE",
                  last_active="2026-06-12T00:00:02.000000Z")

    rows = client.get("/api/me/sessions?machine=mac-mini").json()
    assert [r["session_id"] for r in rows] == ["mac-row"]


# ---------------------------------------------------------------------------
# poll_health stamping
# ---------------------------------------------------------------------------

def test_poll_health_is_attached_to_every_row():
    _seed_project("p-a", "A", owner=DEV_UID)
    _seed_session("p-a", "s-1", last_active="2026-06-12T00:00:01.000000Z")

    row = client.get("/api/me/sessions").json()[0]
    assert "poll_health" in row
    assert "bridge" in row


# ---------------------------------------------------------------------------
# e-2520 stable recipient identity resolve (cwd + agent_kind coarse key)
# ---------------------------------------------------------------------------

def test_cwd_and_agent_kind_filter_resolve_stable_identity():
    """(machine, cwd, agent_kind) is the sid-independent identity key.

    A sender targets "the codex in /work/beacon on mac1" and gets the live
    session(s) for that tuple regardless of the ephemeral sid, so a bridge
    restart / daemon re-mint no longer strands the sender on a dead sid.
    """
    _seed_project("p-id", "IdProj", owner=DEV_UID)
    _seed_session("p-id", "codex-A1", machine="mac1", cwd="/work/beacon",
                  agent_kind="codex", last_active="2026-07-07T00:00:01.000000Z")
    _seed_session("p-id", "codex-A2-remint", machine="mac1", cwd="/work/beacon",
                  agent_kind="codex", last_active="2026-07-07T00:00:09.000000Z")
    _seed_session("p-id", "claude-B", machine="mac1", cwd="/work/beacon",
                  agent_kind="claude-code", last_active="2026-07-07T00:00:05.000000Z")
    _seed_session("p-id", "codex-C-other-cwd", machine="mac1", cwd="/other",
                  agent_kind="codex", last_active="2026-07-07T00:00:03.000000Z")

    rows = client.get(
        "/api/projects/p-id/sessions?cwd=/work/beacon&agent_kind=codex"
    ).json()
    sids = {r["session_id"] for r in rows}
    # only the two codex sessions in /work/beacon — not the claude in the same
    # cwd (different agent_kind), not the codex in a different cwd.
    assert sids == {"codex-A1", "codex-A2-remint"}


def test_cwd_filter_alone_spans_agent_kinds_in_that_dir():
    _seed_project("p-id", "IdProj", owner=DEV_UID)
    _seed_session("p-id", "codex-A1", machine="mac1", cwd="/work/beacon",
                  agent_kind="codex", last_active="2026-07-07T00:00:01.000000Z")
    _seed_session("p-id", "claude-B", machine="mac1", cwd="/work/beacon",
                  agent_kind="claude-code", last_active="2026-07-07T00:00:05.000000Z")
    _seed_session("p-id", "codex-C-other", machine="mac1", cwd="/other",
                  agent_kind="codex", last_active="2026-07-07T00:00:03.000000Z")

    rows = client.get("/api/projects/p-id/sessions?cwd=/work/beacon").json()
    assert {r["session_id"] for r in rows} == {"codex-A1", "claude-B"}


def test_no_identity_filters_still_returns_everything():
    """Backward compat: the coarse-key params are opt-in; the no-arg call is
    unchanged (ms-57 rescue / Web UI 'who is active')."""
    _seed_project("p-id", "IdProj", owner=DEV_UID)
    _seed_session("p-id", "codex-A1", machine="mac1", cwd="/work/beacon",
                  agent_kind="codex", last_active="2026-07-07T00:00:01.000000Z")
    _seed_session("p-id", "codex-C", machine="mac1", cwd="/other",
                  agent_kind="codex", last_active="2026-07-07T00:00:03.000000Z")

    rows = client.get("/api/projects/p-id/sessions").json()
    assert {r["session_id"] for r in rows} == {"codex-A1", "codex-C"}
