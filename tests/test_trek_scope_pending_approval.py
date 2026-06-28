"""ms-97 / e-2611 + e-2626 — scope-add / scope-remove approval flow.

Pre-e-2611 / e-2626 scope-add and scope-remove mutated the trek's
``scope[]`` immediately. AC23 (= scope-add, e-2626) and AC25
(= scope-remove, e-2611) now both require that the mutation go through
a ``pending_user_approval`` state, with explicit
``beacon trek scope-approve <pending_id>`` to commit.

This file pins:

  (a) Lib-level pending op helpers (= ``add_pending_scope_op``,
      ``approve_pending_scope_op``, ``reject_pending_scope_op``,
      ``find_pending_scope_op``, ``list_pending_scope_ops``) for both
      ``scope_add`` and ``scope_remove`` actions.
  (b) Server-side flow: PUT /api/treks/{id}/scope (add) and
      DELETE /api/treks/{id}/scope (remove) both return a pending
      record + do NOT mutate scope[] yet; approve endpoint flushes
      the pending into scope[]; reject endpoint drops it without
      mutating scope.

The (a) tests use pure trek_mod helpers. The (b) tests stand up the
FastAPI test client with mocked Firestore. Same pattern as
test_trek_scheduler_api.py.
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

class TestPendingScopeOpHelpers:
    def _make_trek(self) -> dict:
        return {
            "trek_id": "tk-test1234",
            "status": "active",
            "scope": [
                {"project": "p1", "milestone": "ms-1"},
                {"project": "p2", "operation": "op-2"},
            ],
            "members": [],
            "updated_at": "2026-06-28T00:00:00.000000Z",
        }

    def test_add_pending_scope_op_remove_records_entry(self):
        t = self._make_trek()
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p1", "milestone": "ms-1"},
            requested_by_session_id="sv-abc",
        )
        assert rec["pending_id"].startswith("sp-")
        assert rec["action"] == "scope_remove"
        assert rec["entry"] == {"project": "p1", "milestone": "ms-1"}
        assert rec["requested_by_session_id"] == "sv-abc"
        assert rec["requested_at"]
        # The scope itself is NOT mutated yet.
        assert len(t["scope"]) == 2
        # The pending list now carries one record.
        assert len(t["pending_scope_ops"]) == 1
        assert t["pending_scope_ops"][0] == rec

    def test_add_pending_scope_op_remove_unknown_entry_rejects(self):
        t = self._make_trek()
        with pytest.raises(ValueError, match="not found"):
            trek_mod.add_pending_scope_op(
                t,
                action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
                entry={"project": "pX", "milestone": "ms-nope"},
            )
        # No partial state on rejection.
        assert t.get("pending_scope_ops") in (None, [])

    def test_add_pending_scope_op_unknown_action_rejects(self):
        t = self._make_trek()
        with pytest.raises(ValueError, match="pending scope action"):
            trek_mod.add_pending_scope_op(
                t,
                action="scope_bogus",
                entry={"project": "p1", "milestone": "ms-1"},
            )

    def test_approve_pending_scope_op_remove_commits(self):
        t = self._make_trek()
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p1", "milestone": "ms-1"},
        )
        applied = trek_mod.approve_pending_scope_op(
            t, pending_id=rec["pending_id"],
        )
        assert applied == {"project": "p1", "milestone": "ms-1"}
        # Scope shrunk by 1.
        assert t["scope"] == [{"project": "p2", "operation": "op-2"}]
        # Pending list now empty.
        assert t["pending_scope_ops"] == []

    def test_approve_pending_scope_op_unknown_id_raises(self):
        t = self._make_trek()
        with pytest.raises(ValueError, match="not found"):
            trek_mod.approve_pending_scope_op(t, pending_id="sp-nonexist")

    def test_reject_pending_scope_op_drops_without_apply(self):
        t = self._make_trek()
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p1", "milestone": "ms-1"},
        )
        dropped = trek_mod.reject_pending_scope_op(
            t, pending_id=rec["pending_id"],
        )
        # Scope unchanged.
        assert len(t["scope"]) == 2
        # Pending dropped.
        assert t["pending_scope_ops"] == []
        # Returned record matches.
        assert dropped["pending_id"] == rec["pending_id"]
        assert dropped["action"] == "scope_remove"

    def test_reject_pending_scope_op_unknown_id_raises(self):
        t = self._make_trek()
        with pytest.raises(ValueError, match="not found"):
            trek_mod.reject_pending_scope_op(t, pending_id="sp-nope")

    def test_find_and_list_pending_scope_ops(self):
        t = self._make_trek()
        # Two pending removes side-by-side.
        rec1 = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p1", "milestone": "ms-1"},
        )
        rec2 = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p2", "operation": "op-2"},
        )
        assert trek_mod.find_pending_scope_op(
            t, pending_id=rec1["pending_id"],
        ) == rec1
        assert trek_mod.find_pending_scope_op(
            t, pending_id=rec2["pending_id"],
        ) == rec2
        assert trek_mod.find_pending_scope_op(
            t, pending_id="sp-nope",
        ) is None
        all_listed = trek_mod.list_pending_scope_ops(t)
        assert len(all_listed) == 2
        # list_ should return a shallow copy so callers can't mutate state.
        all_listed.clear()
        assert len(trek_mod.list_pending_scope_ops(t)) == 2

    # -----------------------------------------------------------------
    # ms-97 / e-2626 (AC23) — scope_add helper cases (mirror of remove).
    # -----------------------------------------------------------------

    def test_add_pending_scope_op_add_records_entry(self):
        t = self._make_trek()
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_ADD,
            entry={"project": "p3", "milestone": "ms-9"},
            requested_by_session_id="sv-xyz",
        )
        assert rec["pending_id"].startswith("sp-")
        assert rec["action"] == "scope_add"
        assert rec["entry"] == {"project": "p3", "milestone": "ms-9"}
        assert rec["requested_by_session_id"] == "sv-xyz"
        # Scope itself is NOT mutated yet (= AC23 staging).
        assert len(t["scope"]) == 2
        # Pending list now carries one record.
        assert len(t["pending_scope_ops"]) == 1
        assert t["pending_scope_ops"][0] == rec

    def test_add_pending_scope_op_add_duplicate_rejects(self):
        """ms-97 / e-2626 — duplicate-add 409 surfaces at stage-time.

        Without the symmetric check, two pending-add records for the
        same entry could pile up and the second approve would fail
        opaquely at apply-time. Catching it at request-time keeps the
        UX legible (= same 409 the immediate path used to return).
        """
        t = self._make_trek()
        with pytest.raises(ValueError, match="already present"):
            trek_mod.add_pending_scope_op(
                t,
                action=trek_mod.PENDING_SCOPE_ACTION_ADD,
                entry={"project": "p1", "milestone": "ms-1"},
            )
        # No partial state on rejection.
        assert t.get("pending_scope_ops") in (None, [])

    def test_add_pending_scope_op_add_normalises_entry(self):
        """Entry normalisation runs at stage-time (= same path as remove)."""
        t = self._make_trek()
        # ``parse_scope_arg`` is the documented input shape but tests
        # exercise ``normalize_scope_entry`` via raw dict; verify the
        # pending record carries the normalised entry, not the raw one.
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_ADD,
            entry={"project": "p4", "task": "e-77"},
        )
        # Normaliser keeps narrowing keys it understands.
        assert rec["entry"] == {"project": "p4", "task": "e-77"}

    def test_approve_pending_scope_op_add_commits(self):
        t = self._make_trek()
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_ADD,
            entry={"project": "p3", "milestone": "ms-9"},
        )
        applied = trek_mod.approve_pending_scope_op(
            t, pending_id=rec["pending_id"],
        )
        assert applied == {"project": "p3", "milestone": "ms-9"}
        # Scope grew by 1 (= add is now committed).
        assert {"project": "p3", "milestone": "ms-9"} in t["scope"]
        assert len(t["scope"]) == 3
        # Pending list empty.
        assert t["pending_scope_ops"] == []

    def test_reject_pending_scope_op_add_drops_without_apply(self):
        t = self._make_trek()
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_ADD,
            entry={"project": "p3", "milestone": "ms-9"},
        )
        dropped = trek_mod.reject_pending_scope_op(
            t, pending_id=rec["pending_id"],
        )
        # Scope unchanged (= reject = "no, drop this").
        assert len(t["scope"]) == 2
        # Pending dropped.
        assert t["pending_scope_ops"] == []
        assert dropped["pending_id"] == rec["pending_id"]
        assert dropped["action"] == "scope_add"

    def test_pending_add_and_remove_can_coexist(self):
        """ms-97 / e-2626 — a pending add for entry X and a pending
        remove for entry Y must coexist cleanly (= no interference).
        """
        t = self._make_trek()
        add_rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_ADD,
            entry={"project": "p3", "milestone": "ms-9"},
        )
        rem_rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry={"project": "p1", "milestone": "ms-1"},
        )
        assert len(t["pending_scope_ops"]) == 2
        # Approve add, then approve remove — final scope = p2 + p3.
        trek_mod.approve_pending_scope_op(t, pending_id=add_rec["pending_id"])
        trek_mod.approve_pending_scope_op(t, pending_id=rem_rec["pending_id"])
        assert t["scope"] == [
            {"project": "p2", "operation": "op-2"},
            {"project": "p3", "milestone": "ms-9"},
        ]
        assert t["pending_scope_ops"] == []


# ---------------------------------------------------------------------------
# (b) Server endpoint integration
# ---------------------------------------------------------------------------

import firestore_client  # noqa: E402,F401  (forces module load)
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


_treks: dict[str, dict] = {}
_audit_lines: list[dict] = []


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


@pytest.fixture(autouse=True)
def _rebind_db():
    db_module = app_module.db
    prior = {}
    binds = [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
    ]
    for name, mock in binds:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    _treks.clear()
    _audit_lines.clear()
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)


def _seed_trek_with_scope() -> dict:
    t = {
        "trek_id": "tk-pend0001",
        "title": "scope-remove-flow test",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": "uid-leader", "email": "a@b.com"},
        "leader_session_id": "sv-leader",
        "members": [{
            "user_id": "uid-leader", "email": "a@b.com",
            "role": "leader", "invited_at": "2026-06-18T00:00:00.000000Z",
            "joined_at": "2026-06-18T00:00:00.000000Z",
            "invited_by": "uid-leader",
        }],
        "scope": [
            {"project": "p1", "milestone": "ms-1"},
            {"project": "p2", "operation": "op-2"},
        ],
        "halt": None,
        "goal_state": "",
        "meta": {},
        "pending_scope_ops": [],
        "created_at": "2026-06-18T00:00:00.000000Z",
        "updated_at": "2026-06-18T00:00:00.000000Z",
        "archived_at": None,
    }
    _treks[t["trek_id"]] = copy.deepcopy(t)
    return t


class TestScopeRemovePendingEndpoint:
    def test_delete_scope_stages_pending_does_not_mutate_scope(self):
        _seed_trek_with_scope()
        resp = client.request(
            "DELETE",
            "/api/treks/tk-pend0001/scope",
            json={"project": "p1", "milestone": "ms-1"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The endpoint returns the trek doc plus the new pending record.
        assert "pending_op" in body
        po = body["pending_op"]
        assert po["action"] == "scope_remove"
        assert po["entry"] == {"project": "p1", "milestone": "ms-1"}
        assert po["pending_id"].startswith("sp-")
        # Scope unchanged on stage.
        saved = _treks["tk-pend0001"]
        assert saved["scope"] == [
            {"project": "p1", "milestone": "ms-1"},
            {"project": "p2", "operation": "op-2"},
        ]
        # Pending list now has 1 entry on the saved doc.
        assert len(saved.get("pending_scope_ops") or []) == 1

    def test_delete_unknown_scope_entry_returns_404_no_pending_left(self):
        _seed_trek_with_scope()
        resp = client.request(
            "DELETE",
            "/api/treks/tk-pend0001/scope",
            json={"project": "pX", "milestone": "ms-nope"},
        )
        assert resp.status_code == 404
        saved = _treks["tk-pend0001"]
        assert not saved.get("pending_scope_ops")

    def test_approve_endpoint_flushes_pending_into_scope(self):
        _seed_trek_with_scope()
        # Stage first.
        r1 = client.request(
            "DELETE",
            "/api/treks/tk-pend0001/scope",
            json={"project": "p1", "milestone": "ms-1"},
        )
        assert r1.status_code == 200, r1.text
        pid = r1.json()["pending_op"]["pending_id"]
        # Approve.
        r2 = client.post(
            f"/api/treks/tk-pend0001/scope/approve/{pid}",
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["scope"] == [{"project": "p2", "operation": "op-2"}]
        assert body.get("pending_scope_ops") == []

    def test_approve_unknown_pending_id_returns_404(self):
        _seed_trek_with_scope()
        resp = client.post(
            "/api/treks/tk-pend0001/scope/approve/sp-nope",
        )
        assert resp.status_code == 404

    def test_reject_endpoint_drops_pending_without_mutating_scope(self):
        _seed_trek_with_scope()
        r1 = client.request(
            "DELETE",
            "/api/treks/tk-pend0001/scope",
            json={"project": "p2", "operation": "op-2"},
        )
        assert r1.status_code == 200, r1.text
        pid = r1.json()["pending_op"]["pending_id"]
        # Reject.
        r2 = client.post(
            f"/api/treks/tk-pend0001/scope/reject/{pid}",
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        # Scope unchanged.
        assert body["scope"] == [
            {"project": "p1", "milestone": "ms-1"},
            {"project": "p2", "operation": "op-2"},
        ]
        # Pending dropped.
        assert body.get("pending_scope_ops") == []


# ---------------------------------------------------------------------------
# ms-97 / e-2626 (AC23) — scope-add pending endpoint integration
# ---------------------------------------------------------------------------

class TestScopeAddPendingEndpoint:
    """Mirror of ``TestScopeRemovePendingEndpoint`` for the PUT path.

    AC23 requires that the scope-add HTTP call (= PUT
    ``/api/treks/{id}/scope``) stage a pending op instead of mutating
    ``scope[]`` immediately. The same approve / reject endpoints already
    handle both ``scope_add`` and ``scope_remove`` actions (= dispatched
    by ``rec.action``), so this class only checks the request-time
    staging behaviour and verifies approve / reject reach the add path.
    """

    def test_put_scope_stages_pending_does_not_mutate_scope(self):
        _seed_trek_with_scope()
        resp = client.put(
            "/api/treks/tk-pend0001/scope",
            json={"project": "p3", "milestone": "ms-9"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The endpoint returns the trek doc plus the new pending record.
        assert "pending_op" in body
        po = body["pending_op"]
        assert po["action"] == "scope_add"
        assert po["entry"] == {"project": "p3", "milestone": "ms-9"}
        assert po["pending_id"].startswith("sp-")
        # Scope unchanged on stage (= AC23 contract: no commit without
        # explicit user approve).
        saved = _treks["tk-pend0001"]
        assert saved["scope"] == [
            {"project": "p1", "milestone": "ms-1"},
            {"project": "p2", "operation": "op-2"},
        ]
        # Pending list now has 1 entry on the saved doc.
        assert len(saved.get("pending_scope_ops") or []) == 1

    def test_put_duplicate_scope_entry_returns_409_no_pending_left(self):
        """Duplicate-add surfaces a 409 at stage-time (= e-2626).

        Pre-e-2626 the immediate add also returned 409 on duplicates;
        the staging path preserves the same contract so callers do not
        have to learn a new failure mode.
        """
        _seed_trek_with_scope()
        resp = client.put(
            "/api/treks/tk-pend0001/scope",
            json={"project": "p1", "milestone": "ms-1"},
        )
        assert resp.status_code == 409
        saved = _treks["tk-pend0001"]
        assert not saved.get("pending_scope_ops")

    def test_approve_endpoint_flushes_pending_add_into_scope(self):
        _seed_trek_with_scope()
        # Stage first.
        r1 = client.put(
            "/api/treks/tk-pend0001/scope",
            json={"project": "p3", "milestone": "ms-9"},
        )
        assert r1.status_code == 200, r1.text
        pid = r1.json()["pending_op"]["pending_id"]
        # Approve.
        r2 = client.post(
            f"/api/treks/tk-pend0001/scope/approve/{pid}",
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert {"project": "p3", "milestone": "ms-9"} in body["scope"]
        assert len(body["scope"]) == 3
        assert body.get("pending_scope_ops") == []

    def test_reject_endpoint_drops_pending_add_without_mutating_scope(self):
        _seed_trek_with_scope()
        r1 = client.put(
            "/api/treks/tk-pend0001/scope",
            json={"project": "p3", "milestone": "ms-9"},
        )
        assert r1.status_code == 200, r1.text
        pid = r1.json()["pending_op"]["pending_id"]
        # Reject.
        r2 = client.post(
            f"/api/treks/tk-pend0001/scope/reject/{pid}",
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        # Scope unchanged (= add was never applied).
        assert body["scope"] == [
            {"project": "p1", "milestone": "ms-1"},
            {"project": "p2", "operation": "op-2"},
        ]
        assert body.get("pending_scope_ops") == []
