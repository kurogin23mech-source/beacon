"""Server endpoint integration tests for Trek slot schema v2 (ms-99 / e-2830).

Pins the four ``/api/treks/{trek_id}/slots`` endpoints against the same
``pending_scope_ops`` staging contract used by scope-add / scope-remove
(SPEC AC 15). Shape validation surfaces as 400 (bad request) distinct
from 404 (slot not found) and 409 (duplicate).
"""
from __future__ import annotations

import copy
import os
import sys

import pytest


# Path setup mirrors tests/test_trek_scope_pending_approval.py.
_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_REPO, "lib"))
sys.path.insert(0, os.path.join(_REPO, "server"))
sys.path.insert(0, _REPO)

import firestore_client  # noqa: E402,F401  (forces module load)
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


_treks: dict[str, dict] = {}


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
    yield
    for name, val in prior.items():
        if val is None:
            if hasattr(db_module, name):
                delattr(db_module, name)
        else:
            setattr(db_module, name, val)


app_module._auth_enabled = False
client = TestClient(app_module.app)


def _seed_trek(scope: list[dict] | None = None) -> dict:
    """Seed a minimal active trek with the given scope entries."""
    t = {
        "trek_id": "tk-slot0001",
        "title": "slot endpoint test",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": "uid-leader", "email": "a@b.com"},
        "leader_session_id": "sv-leader",
        "members": [{
            "user_id": "uid-leader", "email": "a@b.com",
            "role": "leader", "invited_at": "2026-07-03T00:00:00.000000Z",
            "joined_at": "2026-07-03T00:00:00.000000Z",
            "invited_by": "uid-leader",
        }],
        "scope": copy.deepcopy(scope or []),
        "halt": None,
        "goal_state": "",
        "meta": {},
        "pending_scope_ops": [],
        "created_at": "2026-07-03T00:00:00.000000Z",
        "updated_at": "2026-07-03T00:00:00.000000Z",
        "archived_at": None,
    }
    _treks[t["trek_id"]] = copy.deepcopy(t)
    return t


# ---------------------------------------------------------------------------
# POST /api/treks/{trek_id}/slots — add
# ---------------------------------------------------------------------------

class TestSlotAddEndpoint:
    def test_add_stages_pending_with_fresh_slot_id(self):
        _seed_trek()
        resp = client.post(
            "/api/treks/tk-slot0001/slots",
            json={"project": "p1", "milestone": "ms-5"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "pending_op" in body
        po = body["pending_op"]
        assert po["action"] == "scope_add"
        entry = po["entry"]
        assert entry["slot_id"].startswith("sl-")
        assert entry["target_kind"] == "milestone"
        assert entry["target_id"] == "ms-5"
        # Scope itself is NOT mutated at stage time.
        saved = _treks["tk-slot0001"]
        assert saved["scope"] == []
        assert len(saved["pending_scope_ops"]) == 1

    def test_add_with_children_records_opt_in_list(self):
        _seed_trek()
        resp = client.post(
            "/api/treks/tk-slot0001/slots",
            json={
                "project": "p1", "milestone": "ms-5",
                "included_task_ids": ["e-A", "e-B"],
            },
        )
        assert resp.status_code == 200
        po = resp.json()["pending_op"]
        assert po["entry"]["included_task_ids"] == ["e-A", "e-B"]

    def test_add_children_on_task_slot_rejects_400(self):
        _seed_trek()
        resp = client.post(
            "/api/treks/tk-slot0001/slots",
            json={
                "project": "p1", "task": "e-99",
                "included_task_ids": ["e-A"],
            },
        )
        assert resp.status_code == 400
        assert "milestone" in resp.text.lower()

    def test_add_without_narrowing_rejects_400(self):
        _seed_trek()
        resp = client.post(
            "/api/treks/tk-slot0001/slots",
            json={"project": "p1"},
        )
        assert resp.status_code == 400
        assert "narrowing" in resp.text.lower()

    def test_add_with_two_narrowing_keys_rejects_400(self):
        _seed_trek()
        resp = client.post(
            "/api/treks/tk-slot0001/slots",
            json={"project": "p1", "milestone": "ms-1", "task": "e-1"},
        )
        assert resp.status_code == 400

    def test_add_duplicate_slot_returns_409(self):
        # Seed with an existing legacy row for the same (project, ms).
        _seed_trek(scope=[{"project": "p1", "milestone": "ms-5"}])
        resp = client.post(
            "/api/treks/tk-slot0001/slots",
            json={"project": "p1", "milestone": "ms-5"},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# PATCH /api/treks/{trek_id}/slots/{slot_id} — amend
# ---------------------------------------------------------------------------

class TestSlotAmendEndpoint:
    def _seed_with_slot(self) -> str:
        t = _seed_trek(scope=[{
            "project": "p1",
            "milestone": "ms-3",
            "slot_id": "sl-target01",
            "target_kind": "milestone",
            "target_id": "ms-3",
            "included_task_ids": None,
            "claim_session_id": "",
            "claimed_at": None,
        }])
        return "sl-target01"

    def test_amend_stages_pending(self):
        slot_id = self._seed_with_slot()
        resp = client.patch(
            f"/api/treks/tk-slot0001/slots/{slot_id}",
            json={"add_children": ["e-A", "e-B"], "remove_children": []},
        )
        assert resp.status_code == 200, resp.text
        po = resp.json()["pending_op"]
        assert po["action"] == "scope_amend"
        assert po["slot_id"] == slot_id
        assert po["add_children"] == ["e-A", "e-B"]

    def test_amend_unknown_slot_returns_404(self):
        self._seed_with_slot()
        resp = client.patch(
            "/api/treks/tk-slot0001/slots/sl-noexist",
            json={"add_children": ["e-A"], "remove_children": []},
        )
        assert resp.status_code == 404

    def test_amend_empty_payload_returns_400(self):
        slot_id = self._seed_with_slot()
        resp = client.patch(
            f"/api/treks/tk-slot0001/slots/{slot_id}",
            json={"add_children": [], "remove_children": []},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/treks/{trek_id}/slots/{slot_id}/claim — claim / unclaim
# ---------------------------------------------------------------------------

class TestSlotClaimEndpoint:
    def _seed_with_slot(self) -> str:
        _seed_trek(scope=[{
            "project": "p1",
            "milestone": "ms-3",
            "slot_id": "sl-claim001",
            "target_kind": "milestone",
            "target_id": "ms-3",
            "included_task_ids": None,
            "claim_session_id": "",
            "claimed_at": None,
        }])
        return "sl-claim001"

    def test_claim_stages_pending_with_session_id(self):
        slot_id = self._seed_with_slot()
        resp = client.post(
            f"/api/treks/tk-slot0001/slots/{slot_id}/claim",
            json={"session_id": "sv-worker"},
        )
        assert resp.status_code == 200, resp.text
        po = resp.json()["pending_op"]
        assert po["action"] == "scope_claim"
        assert po["session_id"] == "sv-worker"

    def test_claim_empty_session_is_unclaim(self):
        slot_id = self._seed_with_slot()
        resp = client.post(
            f"/api/treks/tk-slot0001/slots/{slot_id}/claim",
            json={"session_id": ""},
        )
        assert resp.status_code == 200
        po = resp.json()["pending_op"]
        assert po["session_id"] == ""

    def test_claim_unknown_slot_returns_404(self):
        self._seed_with_slot()
        resp = client.post(
            "/api/treks/tk-slot0001/slots/sl-noexist/claim",
            json={"session_id": "sv-x"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/treks/{trek_id}/slots — list
# ---------------------------------------------------------------------------

class TestSlotListEndpoint:
    def test_list_empty_trek_returns_empty_slots(self):
        _seed_trek()
        resp = client.get("/api/treks/tk-slot0001/slots")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trek_id"] == "tk-slot0001"
        assert body["slots"] == []

    def test_list_projects_v2_row(self):
        _seed_trek(scope=[{
            "project": "p1",
            "milestone": "ms-3",
            "slot_id": "sl-listrow1",
            "target_kind": "milestone",
            "target_id": "ms-3",
            "included_task_ids": ["e-A"],
            "claim_session_id": "sv-c",
            "claimed_at": "2026-07-03T00:00:00Z",
        }])
        resp = client.get("/api/treks/tk-slot0001/slots")
        assert resp.status_code == 200
        rows = resp.json()["slots"]
        assert len(rows) == 1
        r = rows[0]
        assert r["slot_id"] == "sl-listrow1"
        assert r["target_kind"] == "milestone"
        assert r["included_task_ids"] == ["e-A"]
        assert r["claim_session_id"] == "sv-c"

    def test_list_falls_back_for_legacy_row(self):
        _seed_trek(scope=[{"project": "p1", "milestone": "ms-3"}])
        resp = client.get("/api/treks/tk-slot0001/slots")
        assert resp.status_code == 200
        rows = resp.json()["slots"]
        assert rows[0]["slot_id"] == ""
        assert rows[0]["target_kind"] == "milestone"
        assert rows[0]["target_id"] == "ms-3"
        assert rows[0]["included_task_ids"] is None


# ---------------------------------------------------------------------------
# End-to-end: add → approve → amend → approve → claim → approve
# ---------------------------------------------------------------------------

class TestSlotEndToEnd:
    def test_full_chain_via_endpoints(self):
        _seed_trek()
        # 1. add
        r_add = client.post(
            "/api/treks/tk-slot0001/slots",
            json={"project": "p1", "milestone": "ms-7"},
        )
        assert r_add.status_code == 200
        add_po = r_add.json()["pending_op"]
        slot_id = add_po["entry"]["slot_id"]
        # approve add
        r_ap1 = client.post(
            f"/api/treks/tk-slot0001/scope/approve/{add_po['pending_id']}",
        )
        assert r_ap1.status_code == 200
        # 2. amend children
        r_am = client.patch(
            f"/api/treks/tk-slot0001/slots/{slot_id}",
            json={"add_children": ["e-1"], "remove_children": []},
        )
        assert r_am.status_code == 200
        am_po = r_am.json()["pending_op"]
        r_ap2 = client.post(
            f"/api/treks/tk-slot0001/scope/approve/{am_po['pending_id']}",
        )
        assert r_ap2.status_code == 200
        # 3. claim
        r_cl = client.post(
            f"/api/treks/tk-slot0001/slots/{slot_id}/claim",
            json={"session_id": "sv-worker"},
        )
        assert r_cl.status_code == 200
        cl_po = r_cl.json()["pending_op"]
        r_ap3 = client.post(
            f"/api/treks/tk-slot0001/scope/approve/{cl_po['pending_id']}",
        )
        assert r_ap3.status_code == 200
        # 4. list — verify all mutations applied
        r_list = client.get("/api/treks/tk-slot0001/slots")
        rows = r_list.json()["slots"]
        assert len(rows) == 1
        assert rows[0]["slot_id"] == slot_id
        assert rows[0]["included_task_ids"] == ["e-1"]
        assert rows[0]["claim_session_id"] == "sv-worker"
