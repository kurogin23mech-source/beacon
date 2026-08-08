"""Server-side tests for owner-only purge endpoints (ms-14 e-1030).

The purge verbs (milestone / entry / operation) physically remove records
from the project document — the only safety net is the audit trail in the
changelog. Before e-1030 the cloud purge path went through the generic
project-write endpoint, which means any *editor* could hard-delete a record
that an owner would never have approved. These tests pin the new behavior:

  • owner role  → purge succeeds (200)
  • editor role → 403 ``Owner access required``
  • viewer role → 403
  • non-member  → 403 (``Access denied`` from ``_load``)
  • empty reason → 400
  • non-existent target → 400 (``core.*_purge`` ``ValueError`` mapped)
  • duplicate IDs without --index → 400 (ambiguous purge target)

The tests mirror the in-memory mock pattern from ``test_trash_sweep.py`` /
``test_api.py``: ``firestore_client.get_project`` / ``save_project`` are
swapped for an in-memory dict, ``BEACON_OPERATIONS_BACKEND=mock`` routes
``operations.apply_operation`` through the same store, and
``app.dependency_overrides[require_auth]`` lets each test impersonate a
specific role without going through real Google token verification.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Route operations.apply_operation through the in-memory mock. Must be set
# BEFORE importing operations / app.
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402

_store: dict[str, dict] = {}


def _mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


def _mock_list_projects():
    return [{"project_id": pid} for pid in _store]


# Each test rebinds ``firestore_client`` and toggles ``_auth_enabled`` in its
# autouse fixture rather than at module-import time. Module-level patches
# leak across the whole pytest session: with two test files both monkey-
# patching ``firestore_client.get_project`` to their own in-memory store,
# whichever was imported *last* wins — silently breaking the other file.
# (e-1030)

client = TestClient(app_module.app)


def _rebind_db():
    """Point ``app.db`` (and the sys.modules cache) at our mocks for this test.

    Restored to whatever the prior binding was on teardown so we don't leak
    these patches into test files that pytest runs after us.
    """
    db_module = app_module.db
    prior = {
        "get_project": getattr(db_module, "get_project", None),
        "save_project": getattr(db_module, "save_project", None),
        "list_projects": getattr(db_module, "list_projects", None),
    }
    db_module.get_project = _mock_get_project
    db_module.save_project = _mock_save_project
    db_module.list_projects = _mock_list_projects
    prior_module = sys.modules.get("firestore_client")
    sys.modules["firestore_client"] = db_module
    return prior, prior_module


def _restore_db(prior, prior_module):
    db_module = app_module.db
    for k, v in prior.items():
        if v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)
    if prior_module is not None:
        sys.modules["firestore_client"] = prior_module

PROJECT_ID = "test-project"

# uids referenced in the seed project's owner / members table.
OWNER_UID = "uid-owner"
EDITOR_UID = "uid-editor"
VIEWER_UID = "uid-viewer"
OUTSIDER_UID = "uid-stranger"


def _seed_project() -> dict:
    """Project with one of each role + records that can be purged.

    The shapes mirror what ``core.validate_project`` accepts: stable ms-N /
    e-N / op-N ids, no extra-strict status values, plain entry list. No
    duplicate IDs here — tests that exercise the dup-purge path call
    ``_inject_dup_ms`` to add an extra ms-99 record on top of the clean seed.
    """
    return {
        "name": "Purge fixture",
        "owner": OWNER_UID,
        "members": [
            {"user_id": OWNER_UID, "email": "owner@x", "role": "owner"},
            {"user_id": EDITOR_UID, "email": "editor@x", "role": "editor"},
            {"user_id": VIEWER_UID, "email": "viewer@x", "role": "viewer"},
        ],
        "milestones": [
            {
                "id": "ms-1", "title": "Solo", "status": "in_progress",
                "progress": 0, "target_date": "", "commits": [],
                "entries": [
                    {
                        "id": "e-1", "type": "task", "description": "Solo task",
                        "status": "todo", "meta": {},
                    },
                ],
            },
        ],
        "operations": [
            {"id": "op-1", "status": "open", "title": "Solo op", "entries": []},
        ],
    }


def _inject_dup_ms(project_id: str) -> None:
    """Append two copies of ms-99 to the stored project.

    The duplicates intentionally break ``core.validate_project`` — purging
    one of them with --index=2 restores the invariant, so the apply_operation
    post-validate step passes. Tests that don't exercise the dup path keep
    the clean seed (validate would refuse to save mid-purge data otherwise).
    """
    _store[project_id]["milestones"].extend([
        {"id": "ms-99", "title": "First", "status": "todo",
         "progress": 0, "target_date": "", "commits": [], "entries": []},
        {"id": "ms-99", "title": "Second", "status": "todo",
         "progress": 0, "target_date": "", "commits": [], "entries": []},
    ])


@pytest.fixture(autouse=True)
def reset_store():
    """Per-test isolation: fresh project + clear any dependency override.

    e-1344: the purge endpoints now also require a verified envelope via
    ``require_envelope_for_action`` (X-Beacon-Envelope header). That gate
    has its own coverage in ``test_server_high_risk_endpoint_enforce.py``;
    here we want to keep testing the *role* enforcement layer
    (owner-only / editor-forbidden / etc) without minting envelopes on
    every call. So we install a wide-open envelope override that mimics
    "envelope already verified" — same trick we use for ``require_auth``.
    """
    prior_db, prior_module = _rebind_db()
    _store.clear()
    _store[PROJECT_ID] = _seed_project()
    # Default to no override — each test calls _impersonate() explicitly.
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    # Bypass the envelope gate for these role-focused tests. The gate's own
    # contract is exercised by tests/test_server_high_risk_endpoint_enforce.py.
    _disable_envelope_gate()
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        _store.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior_db, prior_module)


def _iter_api_routes(routes):
    """Yield every APIRoute reachable from ``routes``, descending into routers
    mounted via ``include_router``.

    ms-127 e-4871 (PR1): the envelope-gated endpoints (purge / archive /
    migrate) moved from top-level ``@app.post`` into the ``/api/projects/*``
    router, which app.py mounts with ``include_router``. Newer FastAPI
    (>=0.140-ish, as CI installs) no longer *flattens* included routes into
    ``app.routes`` — it keeps them nested inside an ``_IncludedRouter`` wrapper
    (exposed via ``.include_context.included_router.routes``). The old flat
    walk therefore missed the moved deps entirely on CI (Python 3.11 +
    newer FastAPI), so the gate was never overridden and every purge returned
    403 ``envelope_required``. This recursive walk finds the deps in both the
    flattened (older FastAPI) and nested (newer FastAPI) representations, and
    also descends into any ``Mount``/sub-router with a ``.routes`` list.
    """
    for route in routes:
        if hasattr(route, "dependant"):
            yield route
        ctx = getattr(route, "include_context", None)
        included = getattr(ctx, "included_router", None) if ctx else None
        if included is not None and getattr(included, "routes", None):
            yield from _iter_api_routes(included.routes)
        elif getattr(route, "routes", None):
            yield from _iter_api_routes(route.routes)


def _disable_envelope_gate() -> None:
    """Install a dependency override that lets every action through.

    The factory ``require_envelope_for_action`` returns a fresh closure per
    endpoint, so FastAPI registers each as a distinct dependency. Walking the
    routes (recursively, via :func:`_iter_api_routes`, so include_router'd
    routes are covered) and overriding each closure keeps this file decoupled
    from the list of high-risk endpoints (= no need to mirror that list here).
    """
    for route in _iter_api_routes(app_module.app.routes):
        for dep in getattr(getattr(route, "dependant", None), "dependencies",
                           []) or []:
            call = dep.call
            # The closure was created inside ``require_envelope_for_action``;
            # its enclosing function exposes ``__qualname__`` like
            # ``require_envelope_for_action.<locals>.dep``.
            if (callable(call)
                    and getattr(call, "__qualname__", "")
                    .startswith("require_envelope_for_action")):
                app_module.app.dependency_overrides[call] = (
                    lambda: {"envelope": None, "verify_result": None}
                )


def _impersonate(uid: str) -> None:
    """Override require_auth so subsequent requests use the given user id."""
    def _fake_auth():
        return {"sub": uid, "email": f"{uid}@x"}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


# ---------------------------------------------------------------------------
# Milestone purge
# ---------------------------------------------------------------------------

class TestMilestonePurge:
    def test_owner_can_purge_single(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge",
            json={"reason": "test recovery"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "ms-1"
        assert body["purged"] is True
        # Side-effect: ms-1 is gone from the stored project.
        assert all(m["id"] != "ms-1" for m in _store[PROJECT_ID]["milestones"])

    def test_owner_can_purge_duplicate_with_index(self):
        _inject_dup_ms(PROJECT_ID)
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-99/purge",
            json={"reason": "duplicate id recovery", "index": 2},
        )
        assert r.status_code == 200, r.text
        # After purging index 2, one ms-99 record remains.
        remaining = [m for m in _store[PROJECT_ID]["milestones"]
                     if m["id"] == "ms-99"]
        assert len(remaining) == 1
        # And it's the *first* one (title "First").
        assert remaining[0]["title"] == "First"

    def test_editor_is_forbidden(self):
        _impersonate(EDITOR_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge",
            json={"reason": "editor attempting purge"},
        )
        assert r.status_code == 403
        assert "Owner" in r.json()["detail"]
        # And ms-1 is still there — the 403 happened before any mutation.
        assert any(m["id"] == "ms-1"
                   for m in _store[PROJECT_ID]["milestones"])

    def test_viewer_is_forbidden(self):
        _impersonate(VIEWER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge",
            json={"reason": "viewer attempting purge"},
        )
        assert r.status_code == 403
        assert "Owner" in r.json()["detail"]

    def test_outsider_is_forbidden_with_access_denied(self):
        _impersonate(OUTSIDER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge",
            json={"reason": "stranger attempting purge"},
        )
        # Non-members hit _load's general Access denied path. Either way the
        # request must NOT mutate the project — the exact wording is less
        # important than the 403 + side-effect-free guarantee.
        assert r.status_code == 403
        assert any(m["id"] == "ms-1"
                   for m in _store[PROJECT_ID]["milestones"])

    def test_empty_reason_is_400(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge",
            json={"reason": ""},
        )
        assert r.status_code == 400
        assert "reason" in r.json()["detail"].lower()

    def test_missing_reason_field_is_422(self):
        _impersonate(OWNER_UID)
        # Pydantic schema rejects a missing required field at request parsing.
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-1/purge",
            json={},
        )
        assert r.status_code == 422

    def test_unknown_milestone_is_400(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-9999/purge",
            json={"reason": "ghost"},
        )
        assert r.status_code == 400
        assert "not found" in r.json()["detail"].lower()

    def test_duplicate_without_index_is_400(self):
        _inject_dup_ms(PROJECT_ID)
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/milestones/ms-99/purge",
            json={"reason": "ambiguous"},
        )
        assert r.status_code == 400
        # core.milestone_purge surfaces "--index" in its error message.
        assert "--index" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Entry purge
# ---------------------------------------------------------------------------

class TestEntryPurge:
    def test_owner_can_purge_entry(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/entries/e-1/purge",
            json={"reason": "test recovery"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["entry_id"] == "e-1"
        assert body["purged"] is True
        ms1 = next(m for m in _store[PROJECT_ID]["milestones"]
                   if m["id"] == "ms-1")
        assert all(e["id"] != "e-1" for e in ms1["entries"])

    def test_editor_is_forbidden(self):
        _impersonate(EDITOR_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/entries/e-1/purge",
            json={"reason": "editor attempting"},
        )
        assert r.status_code == 403
        assert "Owner" in r.json()["detail"]

    def test_viewer_is_forbidden(self):
        _impersonate(VIEWER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/entries/e-1/purge",
            json={"reason": "viewer attempting"},
        )
        assert r.status_code == 403

    def test_empty_reason_is_400(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/entries/e-1/purge",
            json={"reason": ""},
        )
        assert r.status_code == 400

    def test_unknown_entry_is_400(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/entries/e-9999/purge",
            json={"reason": "ghost"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Operation purge
# ---------------------------------------------------------------------------

class TestOperationPurge:
    def test_owner_can_purge_operation(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/operations/op-1/purge",
            json={"reason": "test recovery"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == "op-1"
        assert body["purged"] is True
        assert all(o["id"] != "op-1"
                   for o in _store[PROJECT_ID].get("operations", []))

    def test_editor_is_forbidden(self):
        _impersonate(EDITOR_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/operations/op-1/purge",
            json={"reason": "editor attempting"},
        )
        assert r.status_code == 403
        assert "Owner" in r.json()["detail"]

    def test_viewer_is_forbidden(self):
        _impersonate(VIEWER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/operations/op-1/purge",
            json={"reason": "viewer attempting"},
        )
        assert r.status_code == 403

    def test_empty_reason_is_400(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/operations/op-1/purge",
            json={"reason": ""},
        )
        assert r.status_code == 400

    def test_unknown_operation_is_400(self):
        _impersonate(OWNER_UID)
        r = client.post(
            f"/api/projects/{PROJECT_ID}/operations/op-9999/purge",
            json={"reason": "ghost"},
        )
        assert r.status_code == 400
