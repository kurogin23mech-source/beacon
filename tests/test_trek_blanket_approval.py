"""ms-97 / Phase 7-C / AC24 — blanket scope-add pre-approval (e-2603).

Pre-AC24 every scope-add staged a pending op (= AC23) that needed an
explicit ``beacon trek scope-approve`` call to commit. AC24 lets the
leader pre-approve whole categories so matching scope-adds bypass the
DM hop entirely (= auto-commit). This file pins:

  (a) Lib-level helpers (= ``is_blanket_approved`` /
      ``add_blanket_approval`` / ``remove_blanket_approval`` /
      ``list_blanket_approvals``) — category-match coverage on the 5
      supported shapes plus add/remove roundtrip.
  (b) Server-side flow: PUT /api/treks/{id}/scope auto-commits when a
      matching blanket exists; the same call falls through to the
      pending stage when no blanket matches. The blanket-approve /
      blanket-revoke endpoints are leader-only.

Same harness as test_trek_scope_pending_approval.py (= mocked
firestore_client surface).
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"
os.environ["BEACON_SCHEDULER_INTERNAL_KEY"] = "test-scheduler-key"

import trek as trek_mod  # noqa: E402


# ---------------------------------------------------------------------------
# (a) Pure lib-level helpers
# ---------------------------------------------------------------------------

class TestBlanketHelpers:
    def _make_trek(self, *, blanket: list[str] | None = None) -> dict:
        meta: dict = {}
        if blanket is not None:
            meta["blanket_scope_approvals"] = list(blanket)
        return {
            "trek_id": "tk-blanket1",
            "status": "active",
            "scope": [],
            "members": [],
            "meta": meta,
            "updated_at": "2026-06-28T00:00:00.000000Z",
        }

    def test_is_blanket_approved_matches_simple_category(self):
        t = self._make_trek(blanket=["operation"])
        assert trek_mod.is_blanket_approved(
            t, {"project": "p1", "operation": "op-2"},
        )
        # milestone-narrowing entry: no match for "operation" category.
        assert not trek_mod.is_blanket_approved(
            t, {"project": "p1", "milestone": "ms-1"},
        )

    def test_is_blanket_approved_matches_project_scoped_category(self):
        t = self._make_trek(blanket=["project:p1"])
        # Any narrowing on p1 matches.
        assert trek_mod.is_blanket_approved(
            t, {"project": "p1", "milestone": "ms-5"},
        )
        assert trek_mod.is_blanket_approved(
            t, {"project": "p1", "operation": "op-9"},
        )
        # Different project doesn't match.
        assert not trek_mod.is_blanket_approved(
            t, {"project": "p2", "milestone": "ms-5"},
        )

    def test_is_blanket_approved_matches_milestone_scoped_category(self):
        t = self._make_trek(blanket=["milestone:ms-97"])
        # Any project narrowing on ms-97 matches.
        assert trek_mod.is_blanket_approved(
            t, {"project": "p1", "milestone": "ms-97"},
        )
        assert trek_mod.is_blanket_approved(
            t, {"project": "p2", "milestone": "ms-97"},
        )
        # Different milestone doesn't match.
        assert not trek_mod.is_blanket_approved(
            t, {"project": "p1", "milestone": "ms-99"},
        )

    def test_add_remove_blanket_roundtrip(self):
        t = self._make_trek()
        trek_mod.add_blanket_approval(t, "operation")
        trek_mod.add_blanket_approval(t, "project:proj-7")
        assert trek_mod.list_blanket_approvals(t) == [
            "operation", "project:proj-7",
        ]
        # Idempotent add.
        trek_mod.add_blanket_approval(t, "operation")
        assert trek_mod.list_blanket_approvals(t) == [
            "operation", "project:proj-7",
        ]
        # Remove leaves the other category.
        trek_mod.remove_blanket_approval(t, "operation")
        assert trek_mod.list_blanket_approvals(t) == ["project:proj-7"]
        # Idempotent remove of absent category.
        trek_mod.remove_blanket_approval(t, "operation")
        assert trek_mod.list_blanket_approvals(t) == ["project:proj-7"]

    def test_unknown_category_shape_rejected(self):
        t = self._make_trek()
        with pytest.raises(ValueError, match="not in supported forms"):
            trek_mod.add_blanket_approval(t, "garbage")
        with pytest.raises(ValueError, match="not in supported forms"):
            trek_mod.add_blanket_approval(t, "unknown:foo")
        with pytest.raises(ValueError, match="non-empty"):
            trek_mod.add_blanket_approval(t, "")


# ---------------------------------------------------------------------------
# (b) Server endpoint integration
# ---------------------------------------------------------------------------

import firestore_client  # noqa: E402,F401  (forces module load)
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


_treks: dict[str, dict] = {}
_appended_logs: list[dict] = []


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


def _mock_append_trek_log(trek_id: str, entry: dict) -> str:
    payload = dict(entry)
    payload["trek_id"] = trek_id
    payload.setdefault("log_id", f"log-mock-{len(_appended_logs):06d}")
    _appended_logs.append(payload)
    return payload["log_id"]


def _mock_list_trek_logs(trek_id: str, *, limit=100, since=""):
    rows = [r for r in _appended_logs if r.get("trek_id") == trek_id]
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
    rows.sort(key=lambda r: r.get("created_at", ""))
    return rows[: limit if limit and limit > 0 else None]


@pytest.fixture(autouse=True)
def _rebind_db():
    db_module = app_module.db
    prior = {}
    binds = [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("append_trek_log", _mock_append_trek_log),
        ("list_trek_logs", _mock_list_trek_logs),
    ]
    for name, mock in binds:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _appended_logs.clear()
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)


def _seed_trek(*, blanket: list[str] | None = None,
               leader_sid: str = "sv-leader") -> dict:
    meta: dict = {}
    if blanket is not None:
        meta["blanket_scope_approvals"] = list(blanket)
    t = {
        "trek_id": "tk-blank0001",
        "title": "blanket-approval test",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": "uid-leader", "email": "a@b.com"},
        "leader_session_id": leader_sid,
        "members": [{
            "user_id": "uid-leader", "email": "a@b.com",
            "role": "leader", "invited_at": "2026-06-18T00:00:00.000000Z",
            "joined_at": "2026-06-18T00:00:00.000000Z",
            "invited_by": "uid-leader",
            "session_id": leader_sid,
        }],
        "scope": [{"project": "p0", "milestone": "ms-0"}],
        "halt": None,
        "goal_state": "",
        "meta": meta,
        "pending_scope_ops": [],
        "created_at": "2026-06-18T00:00:00.000000Z",
        "updated_at": "2026-06-18T00:00:00.000000Z",
        "archived_at": None,
    }
    _treks[t["trek_id"]] = copy.deepcopy(t)
    return t


class TestScopeAddAutoCommit:
    def test_scope_add_with_matching_blanket_auto_commits(self):
        _seed_trek(blanket=["operation"])
        resp = client.put(
            "/api/treks/tk-blank0001/scope",
            json={"project": "p1", "operation": "op-2"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("auto_committed") is True
        assert body.get("committed_via") == "blanket_approval"
        # Scope grew immediately (= no pending hop).
        saved = _treks["tk-blank0001"]
        assert {"project": "p1", "operation": "op-2"} in saved["scope"]
        # Pending list is untouched.
        assert (saved.get("pending_scope_ops") or []) == []

    def test_scope_add_without_blanket_goes_to_pending(self):
        _seed_trek(blanket=[])
        resp = client.put(
            "/api/treks/tk-blank0001/scope",
            json={"project": "p1", "milestone": "ms-9"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("auto_committed") is False
        assert body["pending_op"]["action"] == "scope_add"
        # Scope unchanged on stage.
        saved = _treks["tk-blank0001"]
        assert {"project": "p1", "milestone": "ms-9"} not in saved["scope"]
        # Pending list has the staged op.
        assert len(saved.get("pending_scope_ops") or []) == 1


class TestBlanketEndpoint:
    def test_blanket_approve_leader_succeeds(self):
        _seed_trek(blanket=[])
        resp = client.post(
            "/api/treks/tk-blank0001/blanket-approve",
            json={"category": "milestone:ms-97"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "milestone:ms-97" in body["blanket_scope_approvals"]

    def test_blanket_revoke_drops_category(self):
        _seed_trek(blanket=["operation", "milestone:ms-97"])
        resp = client.post(
            "/api/treks/tk-blank0001/blanket-revoke",
            json={"category": "operation"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["blanket_scope_approvals"] == ["milestone:ms-97"]

    def test_blanket_approve_invalid_category_400(self):
        _seed_trek(blanket=[])
        resp = client.post(
            "/api/treks/tk-blank0001/blanket-approve",
            json={"category": "garbage"},
        )
        assert resp.status_code == 400

    def test_blanket_approve_leader_only(self):
        """Non-leader (= ``member`` role) is forbidden via
        ``_require_trek_leader_session``. With auth disabled in this
        harness the path is exercised by setting up a trek where the
        caller's user_id is not the leader user — the leader-session
        guard short-circuits the request.
        """
        # Re-enable auth for this single test so role checks apply.
        prior = app_module._auth_enabled
        app_module._auth_enabled = True
        try:
            t = _seed_trek(blanket=[])
            # Add a non-leader member; the dependency injection in
            # require_auth still returns a placeholder user but the
            # leader-session check reads the user_id.
            t["members"].append({
                "user_id": "uid-member", "email": "m@b.com",
                "role": "member", "invited_at": "2026-06-18T00:00:00.000000Z",
                "joined_at": "2026-06-18T00:00:00.000000Z",
                "invited_by": "uid-leader",
                "session_id": "sv-member",
            })
            _treks["tk-blank0001"] = copy.deepcopy(t)

            # Override require_auth to inject a non-leader user.
            from fastapi import Depends  # noqa: F401
            app_module.app.dependency_overrides[app_module.require_auth] = (
                lambda: {"sub": "uid-member", "email": "m@b.com"}
            )
            try:
                resp = client.post(
                    "/api/treks/tk-blank0001/blanket-approve",
                    json={"category": "operation"},
                )
                # Non-leader is rejected with 403.
                assert resp.status_code == 403, resp.text
            finally:
                app_module.app.dependency_overrides.pop(
                    app_module.require_auth, None,
                )
        finally:
            app_module._auth_enabled = prior
