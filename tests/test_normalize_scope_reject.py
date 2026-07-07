"""AC7 / AC8 enforcement tests for ``normalize_scope_entry`` — ms-97 / e-2659.

AC7: ``normalize_scope_entry`` in strict mode (= default) MUST reject
scope entries that have no narrowing key (= ``milestone`` / ``operation``
/ ``task``). Project-wide scope is no longer accepted on the new write
path so a Trek's autonomous権限 cannot accidentally cover an entire
project.

AC8: legacy data (= already-persisted project-wide rows) must continue
to be readable. We model that by passing ``strict=False`` and assert
the entry round-trips through normalisation untouched.

The tests also cover the 3-layer reject (= lib / server / CLI):

* lib: ``normalize_scope_entry`` strict=True raises
* lib: ``add_scope_entry`` / ``parse_scope_arg`` propagate the strict reject
* CLI: ``parse_scope_arg`` (= what ``cmd_trek_plan`` calls) raises
* server: ``server.app.add_trek_scope_endpoint`` returns HTTP 400

The server-side assertion uses the existing in-memory mock fixtures
from ``test_trek_task_add_endpoint`` style — we re-implement a small
client here to avoid coupling the AC7 unit tests to the full task-add
test scaffolding.
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# Lib layer — normalize_scope_entry strict mode
# ---------------------------------------------------------------------------


class TestNormalizeStrict:
    def test_milestone_narrowing_passes(self):
        """Milestone narrowing satisfies AC7 (= narrowing key present)."""
        out = trek.normalize_scope_entry({
            "project": "p-1",
            "milestone": "ms-3",
        })
        assert out == {"project": "p-1", "milestone": "ms-3"}

    def test_operation_narrowing_passes(self):
        out = trek.normalize_scope_entry({
            "project": "p-1",
            "operation": "op-2",
        })
        assert out == {"project": "p-1", "operation": "op-2"}

    def test_task_narrowing_passes(self):
        out = trek.normalize_scope_entry({
            "project": "p-1",
            "task": "e-1234",
        })
        assert out == {"project": "p-1", "task": "e-1234"}

    def test_project_only_strict_raises(self):
        """AC7: project-wide entry under strict mode raises ValueError."""
        with pytest.raises(ValueError, match="narrowing key"):
            trek.normalize_scope_entry({"project": "p-1"})

    def test_explicit_strict_true_raises(self):
        with pytest.raises(ValueError, match="narrowing key"):
            trek.normalize_scope_entry({"project": "p-1"}, strict=True)

    def test_missing_project_still_raises_in_strict(self):
        """Sanity: missing-project guard fires before the narrowing guard."""
        with pytest.raises(ValueError, match="project"):
            trek.normalize_scope_entry({"milestone": "ms-3"})

    def test_missing_project_still_raises_in_non_strict(self):
        with pytest.raises(ValueError, match="project"):
            trek.normalize_scope_entry({"milestone": "ms-3"}, strict=False)


class TestNormalizeNonStrict:
    """AC8 — legacy reads must keep working."""

    def test_project_only_non_strict_accepts(self):
        """Legacy on-disk data must round-trip via strict=False."""
        out = trek.normalize_scope_entry(
            {"project": "p-1"}, strict=False,
        )
        assert out == {"project": "p-1"}

    def test_unknown_keys_dropped_in_non_strict(self):
        out = trek.normalize_scope_entry(
            {"project": "p-1", "spurious": "value"}, strict=False,
        )
        assert out == {"project": "p-1"}
        assert "spurious" not in out


# ---------------------------------------------------------------------------
# Lib layer — add_scope_entry / parse_scope_arg also enforce strict
# ---------------------------------------------------------------------------


class TestLibStrictPropagation:
    """The lib's higher-level helpers must surface the same rejection."""

    def test_add_scope_entry_rejects_project_only(self):
        doc = {"scope": []}
        with pytest.raises(ValueError, match="narrowing key"):
            trek.add_scope_entry(doc, entry={"project": "p-1"})
        assert doc["scope"] == []

    def test_parse_scope_arg_rejects_project_only_string(self):
        """CLI input ``"beacon-1"`` (= no `:`) → project-wide → reject."""
        with pytest.raises(ValueError, match="narrowing key"):
            trek.parse_scope_arg("beacon-1")

    def test_parse_scope_arg_rejects_trailing_colon(self):
        """CLI input ``"beacon-1:"`` → project-wide (trailing colon) → reject."""
        with pytest.raises(ValueError, match="narrowing key"):
            trek.parse_scope_arg("beacon-1:")

    def test_parse_scope_arg_accepts_milestone(self):
        out = trek.parse_scope_arg("beacon-1:ms-64")
        assert out == {"project": "beacon-1", "milestone": "ms-64"}

    def test_parse_scope_arg_non_strict_accepts_project_only(self):
        """``strict=False`` keeps the legacy parse path open for migrations."""
        out = trek.parse_scope_arg("beacon-1", strict=False)
        assert out == {"project": "beacon-1"}

    def test_pending_add_op_rejects_project_only(self):
        """Server staging path (= add_pending_scope_op) also enforces strict."""
        doc = {"scope": []}
        with pytest.raises(ValueError, match="narrowing key"):
            trek.add_pending_scope_op(
                doc,
                action=trek.PENDING_SCOPE_ACTION_ADD,
                entry={"project": "p-1"},
            )

    def test_pending_remove_op_accepts_project_only_legacy(self):
        """AC8: removing a legacy project-wide row must still work."""
        doc = {"scope": [{"project": "p-1"}]}  # grandfathered row
        rec = trek.add_pending_scope_op(
            doc,
            action=trek.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p-1"},
        )
        assert rec["action"] == "scope_remove"
        assert rec["entry"] == {"project": "p-1"}


# ---------------------------------------------------------------------------
# Server layer — /api/treks/{id}/scope returns HTTP 400 on project-wide
# ---------------------------------------------------------------------------


@pytest.fixture
def server_client():
    """Spin up the FastAPI TestClient with an in-memory trek store.

    We reuse the same monkey-patch surface as test_trek_task_add_endpoint
    but keep it scoped to this file so AC7 server-layer assertions stay
    independent of the cross-project task-add test scaffold.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
    os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

    import firestore_client  # noqa: F401
    from fastapi.testclient import TestClient
    import app as app_module

    treks: dict[str, dict] = {}

    def _mock_get_trek(trek_id):
        data = treks.get(trek_id)
        return copy.deepcopy(data) if data else None

    def _mock_save_trek(trek_id, data):
        payload = {k: v for k, v in data.items() if k != "trek_id"}
        treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}

    def _restore(prior):
        for name, fn in prior.items():
            setattr(app_module.db, name, fn)

    prior = {
        "get_trek": app_module.db.get_trek,
        "save_trek": app_module.db.save_trek,
    }
    app_module.db.get_trek = _mock_get_trek
    app_module.db.save_trek = _mock_save_trek

    USER_UID = "uid-leader"
    USER_EMAIL = "leader@x"

    def _fake_auth():
        return {"sub": USER_UID, "email": USER_EMAIL}

    prior_auth = getattr(app_module, "_auth_enabled", True)
    app_module._auth_enabled = False
    from fastapi import Request  # noqa: F401
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth

    # Seed one trek for the test to scope-add into.
    treks["tk-ac7"] = {
        "trek_id": "tk-ac7",
        "title": "ac7-test",
        "status": "active",
        "type": "persistent",
        "creator_actor": {"user_id": USER_UID, "email": USER_EMAIL},
        "members": [{
            "user_id": USER_UID, "email": USER_EMAIL, "role": "leader",
            "invited_by": USER_UID, "invited_at": "2026-06-28T00:00:00Z",
            "joined_at": "2026-06-28T00:00:00Z",
        }],
        "scope": [],
        "leader_session_id": "sv-leader",
        "halt": None,
        "task_states": {},
        "created_at": "2026-06-28T00:00:00Z",
        "updated_at": "2026-06-28T00:00:00Z",
    }

    client = TestClient(app_module.app)
    yield client

    app_module._auth_enabled = prior_auth
    app_module.app.dependency_overrides.clear()
    _restore(prior)


class TestServerEndpointRejects:
    def test_add_trek_scope_project_wide_returns_400(self, server_client):
        """PUT /api/treks/{id}/scope with no narrowing key → 400."""
        r = server_client.put(
            "/api/treks/tk-ac7/scope",
            json={"project": "p-1"},
        )
        assert r.status_code == 400, r.text
        assert "narrowing key" in r.text

    def test_add_trek_scope_with_milestone_succeeds(self, server_client):
        """Milestone narrowing satisfies AC7 → 200 with pending_op staged."""
        r = server_client.put(
            "/api/treks/tk-ac7/scope",
            json={"project": "p-1", "milestone": "ms-3"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("pending_op", {}).get("action") == "scope_add"
        entry = body["pending_op"]["entry"]
        assert entry == {"project": "p-1", "milestone": "ms-3"}
