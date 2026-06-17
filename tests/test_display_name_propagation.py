"""display_name end-to-end propagation tests (ms-78 / e-1909).

Pins the four UC11-F5 follow-up acceptance criteria:

  AC 1. Server-side actor resolution stamps ``meta.author`` with
        ``{user_id, email, display_name}`` on commits, task creation,
        task done, and task update.
  AC 2. ``GET /api/me/profile`` reads + ``PATCH /api/me/profile`` writes
        the caller's own display_name through the symmetric
        Firestore / DynamoDB store router contract.
  AC 3. Existing users without a display_name can be retroactively
        prompted, and the prompt's dismissal contract is supported by
        the profile endpoint's empty-string-clears semantics.
  AC 4. ``core._clean_author`` keeps the persisted shape tight
        (allowlist + empty drop), so the Web UI's renderName fallback
        stays well-defined.

The pure ``core`` paths are exercised against in-memory dicts (no I/O).
The server endpoints are exercised through FastAPI's TestClient against
an in-memory store_router mock — same pattern as
``tests/test_invitation_api.py`` so we don't depend on Firestore /
DynamoDB connectivity.
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route apply_operation through the in-memory mock so we don't need Firestore.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import core  # noqa: E402
import firestore_client  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import store_router  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory mocks (mirrors test_invitation_api.py for isolation)
# ---------------------------------------------------------------------------

_store: dict[str, dict] = {}
_users: dict[str, dict] = {}


def mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def mock_get_user(user_id: str):
    return copy.deepcopy(_users.get(user_id)) if user_id in _users else None


def mock_update_user(user_id: str, updates: dict) -> bool:
    if user_id not in _users:
        # /api/me/profile mints the user record up front; in tests where
        # the user is pre-seeded the existing dict is updated in place.
        _users[user_id] = {"user_id": user_id}
    _users[user_id].update(updates)
    return True


def mock_get_or_create_user(user_id: str, email: str) -> dict:
    if user_id not in _users:
        _users[user_id] = {"user_id": user_id, "email": email, "role": "user"}
    return copy.deepcopy(_users[user_id])


app_module._auth_enabled = False  # mirrors test_invitation_api.py


def _mock_load(project_id: str, user=None):
    data = mock_get_project(project_id)
    if data is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="not found")
    return data


_PRIOR_LOAD = app_module._load


def setup_module(_module):
    app_module._load = _mock_load


def teardown_module(_module):
    app_module._load = _PRIOR_LOAD


client = TestClient(app_module.app)

PROJECT_ID = "ms78-e1909-test"


def _seed():
    _store.clear()
    _users.clear()
    # In auth-disabled mode require_auth returns sub="dev", email="dev@local".
    # Pre-seed "dev" with a display_name so commits / tasks stamped under that
    # identity carry the display_name forward.
    _users["dev"] = {
        "user_id": "dev",
        "email": "dev@local",
        "display_name": "Dev Person",
    }
    _store[PROJECT_ID] = {
        "name": "Test Project",
        "owner": "dev",
        "members": [],
        "milestones": [
            {
                "id": "ms-1",
                "title": "Test MS",
                "status": "in_progress",
                "entries": [],
                "progress": 0,
            }
        ],
        "schema_version": 2,
    }


@pytest.fixture(autouse=True)
def reset_state():
    # Resolve the *current* module objects via sys.modules — earlier tests
    # (e.g. test_log_commit_actor's fixture) `sys.modules.pop` the
    # firestore_client module, so the reference we captured at module
    # import time may be a stale duplicate. Always resolve fresh per
    # fixture invocation so our monkeypatches land on the module
    # ``apply_operation._apply_mock`` will see when it does
    # ``import firestore_client``.
    import importlib
    fc_mod = sys.modules.get("firestore_client") or importlib.import_module(
        "firestore_client"
    )
    sr_mod = sys.modules.get("store_router") or importlib.import_module(
        "store_router"
    )
    # Also make sure ``app`` (the FastAPI module) sees the same store_router
    # — re-bind ``db`` if it points at a stale module.
    app_db = getattr(app_module, "db", None)
    if app_db is not None and app_db is not sr_mod:
        app_module.db = sr_mod

    targets = [
        "get_project", "save_project", "get_user", "update_user",
        "get_or_create_user",
    ]
    _orig: dict[str, object] = {}
    for m_name, m in (("fc", fc_mod), ("sr", sr_mod)):
        for attr in targets:
            _orig[f"{m_name}_{attr}"] = getattr(m, attr, None)
    overrides = {
        "get_project": mock_get_project,
        "save_project": mock_save_project,
        "get_user": mock_get_user,
        "update_user": mock_update_user,
        "get_or_create_user": mock_get_or_create_user,
    }
    for attr, fn in overrides.items():
        setattr(fc_mod, attr, fn)
        setattr(sr_mod, attr, fn)
    _seed()
    yield
    _store.clear()
    _users.clear()
    for k, v in _orig.items():
        if v is None:
            continue
        m = fc_mod if k.startswith("fc_") else sr_mod
        setattr(m, k[3:], v)
    if app_db is not None:
        app_module.db = app_db


# ===========================================================================
# AC 4 / underlying helper: _clean_author shape contract
# ===========================================================================

def test_clean_author_strips_unknown_and_empty_keys():
    """``_clean_author`` keeps only {user_id, email, display_name} and drops empties."""
    out = core._clean_author({
        "user_id": "u1",
        "email": "a@example.com",
        "display_name": "Alice",
        "extra": "should be dropped",
        "machine": "ignored-here",
    })
    assert out == {
        "user_id": "u1",
        "email": "a@example.com",
        "display_name": "Alice",
    }


def test_clean_author_drops_whitespace_only_values():
    out = core._clean_author({
        "user_id": "u1",
        "email": "  ",
        "display_name": "",
    })
    assert out == {"user_id": "u1"}


def test_clean_author_returns_empty_for_non_dict():
    assert core._clean_author(None) == {}
    assert core._clean_author("alice") == {}


# ===========================================================================
# AC 1 (core layer): log_commit / task_add / task_done / task_update stamp
# meta.author when given a server-resolved author dict.
# ===========================================================================

def _empty_project():
    return {
        "name": "t",
        "milestones": [
            {"id": "ms-1", "title": "T", "status": "in_progress",
             "entries": [], "progress": 0}
        ],
        "summary": "",
    }


def test_log_commit_stamps_meta_author():
    data = _empty_project()
    core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234",
        message="m", date="2026-06-18T00:00:00Z",
        author={
            "user_id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
        },
    )
    entry = data["milestones"][0]["entries"][-1]
    assert entry["meta"]["author"] == {
        "user_id": "u-alice",
        "email": "alice@example.com",
        "display_name": "Alice",
    }


def test_log_commit_without_author_omits_field():
    data = _empty_project()
    core.log_commit(
        data, ms_id="ms-1", commit_hash="def4567",
        message="m", date="2026-06-18T00:00:00Z",
    )
    entry = data["milestones"][0]["entries"][-1]
    assert "author" not in entry["meta"]


def test_task_add_stamps_meta_author():
    data = _empty_project()
    eid = core.task_add(
        data, "ms-1", "do work",
        author={
            "user_id": "u-bob",
            "email": "bob@example.com",
            "display_name": "Bob",
        },
    )
    entry = next(
        e for e in data["milestones"][0]["entries"] if e["id"] == eid
    )
    assert entry["meta"]["author"] == {
        "user_id": "u-bob",
        "email": "bob@example.com",
        "display_name": "Bob",
    }


def test_task_done_stamps_done_by_user():
    data = _empty_project()
    eid = core.task_add(data, "ms-1", "do work")
    core.task_done(
        data, eid,
        author={
            "user_id": "u-carol",
            "email": "carol@example.com",
            "display_name": "Carol",
        },
    )
    entry = next(
        e for e in data["milestones"][0]["entries"] if e["id"] == eid
    )
    assert entry["meta"]["done_by_user"] == {
        "user_id": "u-carol",
        "email": "carol@example.com",
        "display_name": "Carol",
    }
    # And the legacy local-actor field still gets stamped.
    assert "done_by" in entry["meta"]


def test_task_update_stamps_updated_by_user_only_when_changed():
    data = _empty_project()
    eid = core.task_add(data, "ms-1", "do work")
    # No-op call: nothing changes → updated_by_user must NOT be stamped.
    core.task_update(
        data, eid,
        author={
            "user_id": "u-dave",
            "email": "dave@example.com",
            "display_name": "Dave",
        },
    )
    entry = next(
        e for e in data["milestones"][0]["entries"] if e["id"] == eid
    )
    assert "updated_by_user" not in entry.get("meta", {})

    # Real change: description edit → updated_by_user gets stamped.
    core.task_update(
        data, eid, description="do something different",
        author={
            "user_id": "u-dave",
            "email": "dave@example.com",
            "display_name": "Dave",
        },
    )
    entry = next(
        e for e in data["milestones"][0]["entries"] if e["id"] == eid
    )
    assert entry["meta"]["updated_by_user"] == {
        "user_id": "u-dave",
        "email": "dave@example.com",
        "display_name": "Dave",
    }


# ===========================================================================
# AC 1 (API layer): server endpoints resolve display_name from the user
# record and thread it through to meta.author.
# ===========================================================================

def test_api_log_commit_threads_display_name_to_meta_author():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/log",
        json={
            "hash": "deadbee",
            "message": "feat: hello",
            "date": "2026-06-18T00:00:00Z",
            "ms_id": "ms-1",
        },
    )
    assert r.status_code == 200, r.text
    project = _store[PROJECT_ID]
    entries = project["milestones"][0]["entries"]
    assert len(entries) >= 1
    commit = entries[-1]
    author = commit["meta"]["author"]
    assert author["user_id"] == "dev"
    assert author["email"] == "dev@local"
    # display_name picked up from the pre-seeded user record (= the
    # invite-accept persisted name).
    assert author["display_name"] == "Dev Person"


def test_api_create_entry_threads_display_name_to_meta_author():
    r = client.post(
        f"/api/projects/{PROJECT_ID}/milestones/ms-1/entries",
        json={"description": "do thing", "type": "task"},
    )
    assert r.status_code == 200, r.text
    project = _store[PROJECT_ID]
    entries = project["milestones"][0]["entries"]
    task = entries[-1]
    assert task["meta"]["author"]["display_name"] == "Dev Person"


def test_api_log_commit_omits_display_name_when_user_has_none():
    # Wipe display_name to simulate a legacy / unprompted user.
    _users["dev"]["display_name"] = ""
    r = client.post(
        f"/api/projects/{PROJECT_ID}/log",
        json={
            "hash": "feedfac",
            "message": "feat: no-name",
            "date": "2026-06-18T00:00:00Z",
            "ms_id": "ms-1",
        },
    )
    assert r.status_code == 200, r.text
    commit = _store[PROJECT_ID]["milestones"][0]["entries"][-1]
    author = commit["meta"]["author"]
    assert author["user_id"] == "dev"
    # display_name absent (= falsy fallback path for renderName).
    assert "display_name" not in author


# ===========================================================================
# AC 2: /api/me/profile GET + PATCH symmetry
# ===========================================================================

def test_me_get_profile_returns_persisted_display_name():
    r = client.get("/api/me/profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "dev"
    assert body["email"] == "dev@local"
    assert body["display_name"] == "Dev Person"


def test_me_patch_profile_persists_display_name():
    r = client.patch(
        "/api/me/profile", json={"display_name": "Renamed Person"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Renamed Person"
    # Persisted on the user record (= visible to subsequent commits / GET).
    assert _users["dev"]["display_name"] == "Renamed Person"
    follow = client.get("/api/me/profile").json()
    assert follow["display_name"] == "Renamed Person"


# ===========================================================================
# AC 3: empty-string-clears semantics (= retroactive prompt's dismiss path)
# ===========================================================================

def test_me_patch_profile_empty_clears_display_name():
    # Pre-condition: caller has a display_name set.
    assert _users["dev"]["display_name"] == "Dev Person"
    r = client.patch("/api/me/profile", json={"display_name": "  "})
    assert r.status_code == 200
    assert _users["dev"]["display_name"] == ""
    # GET now reflects the cleared state — UI falls back to email.
    body = client.get("/api/me/profile").json()
    assert body["display_name"] == ""
    assert body["email"] == "dev@local"


def test_me_patch_profile_mints_user_record_when_absent():
    # Drop the pre-seeded "dev" record to simulate a freshly authenticated
    # caller whose users/{id} doc has not been created yet.
    _users.clear()
    r = client.patch("/api/me/profile", json={"display_name": "Lazy Mint"})
    assert r.status_code == 200, r.text
    assert _users["dev"]["display_name"] == "Lazy Mint"


# ===========================================================================
# AC 1 + AC 4 together: full end-to-end loop through PATCH → next commit
# carries the updated display_name. This is the user-visible path that
# answers the e-1909 promise — "I change my name in Settings, and the next
# commit's author column shows the new label".
# ===========================================================================

def test_display_name_update_propagates_into_next_commit_meta_author():
    # 1. User updates their display_name.
    r1 = client.patch("/api/me/profile", json={"display_name": "Updated Dev"})
    assert r1.status_code == 200
    # 2. They make a commit.
    r2 = client.post(
        f"/api/projects/{PROJECT_ID}/log",
        json={
            "hash": "cafebab",
            "message": "feat: under new name",
            "date": "2026-06-18T01:00:00Z",
            "ms_id": "ms-1",
        },
    )
    assert r2.status_code == 200, r2.text
    # 3. Commit entry's meta.author reflects the **new** display_name.
    commit = _store[PROJECT_ID]["milestones"][0]["entries"][-1]
    assert commit["meta"]["author"]["display_name"] == "Updated Dev"
