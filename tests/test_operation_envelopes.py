"""Integration tests for Operation envelope endpoints (ms-60 / e-1339).

Covers the SPEC → approve → revoke → list round-trip and the error paths
(missing SPEC, wrong op binding, malformed approved_actions, empty list).
Uses an in-memory mock store mirroring the pattern in
``test_envelope_integration.py``.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
os.environ.setdefault("BEACON_ENVELOPE_SECRET", "operation-itest-secret")

import firestore_client  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

_envelope_store: dict[str, dict[str, dict]] = {}  # project_id → env_id → record
_doc_store: dict[str, dict[str, dict]] = {}       # project_id → doc_id → doc

PROJECT_ID = "op-env-itest"
OP_ID = "op-1"
PROJECT_DATA = {
    "name": "test",
    "milestones": [],
    "operations": [{"id": OP_ID, "title": "Test op", "status": "open"}],
}


def _mock_get_document(project_id: str, doc_id: str) -> dict | None:
    return copy.deepcopy(_doc_store.get(project_id, {}).get(doc_id))


def _mock_get_active_operation_envelope(project_id: str, op_id: str) -> dict | None:
    bucket = _envelope_store.get(project_id, {})
    actives = [
        copy.deepcopy(r) for r in bucket.values()
        if r.get("op_id") == op_id and r.get("status") == "active"
    ]
    if not actives:
        return None
    actives.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return actives[0]


def _mock_issue_operation_envelope(
    project_id: str, op_id: str, spec_doc_id: str, spec_revision_id: str,
    envelope_dict: dict, approved_actions: list[str], created_by: str,
) -> dict:
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    bucket = _envelope_store.setdefault(project_id, {})
    # Auto-revoke prior active for this op.
    for rec in bucket.values():
        if rec.get("op_id") == op_id and rec.get("status") == "active":
            rec["status"] = "revoked"
            rec["revoked_at"] = now
            rec["revoked_by"] = created_by
            rec["revoke_reason"] = "superseded by new approve"
    nonce = envelope_dict["nonce"]
    rec = {
        "envelope_id": nonce,
        "op_id": op_id,
        "spec_doc_id": spec_doc_id,
        "spec_revision_id": spec_revision_id,
        "envelope": copy.deepcopy(envelope_dict),
        "approved_actions": list(approved_actions),
        "status": "active",
        "created_at": now,
        "created_by": created_by,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }
    bucket[nonce] = rec
    return copy.deepcopy(rec)


def _mock_revoke_operation_envelope(
    project_id: str, envelope_id: str, revoked_by: str, reason: str,
) -> dict | None:
    import datetime
    rec = _envelope_store.get(project_id, {}).get(envelope_id)
    if not rec:
        return None
    if rec.get("status") == "revoked":
        return copy.deepcopy(rec)
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    rec["status"] = "revoked"
    rec["revoked_at"] = now
    rec["revoked_by"] = revoked_by
    rec["revoke_reason"] = reason
    return copy.deepcopy(rec)


def _mock_list_operation_envelopes(
    project_id: str, op_id: str | None = None, status: str | None = None,
) -> list[dict]:
    items = []
    for rec in _envelope_store.get(project_id, {}).values():
        if op_id and rec.get("op_id") != op_id:
            continue
        if status and rec.get("status") != status:
            continue
        items.append(copy.deepcopy(rec))
    items.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return items


def _mock_get_operation_envelope(project_id: str, envelope_id: str) -> dict | None:
    return copy.deepcopy(_envelope_store.get(project_id, {}).get(envelope_id))


from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

# ms-95 e-2441 (re-fix): the module-level mutations of firestore_client and
# app_module used to happen at collection time, before any test executed.
# `teardown_module` restored them after this file's tests finished, but by
# then earlier-collected files (test_api.py at position 4) had already run
# with the polluted `_load` / `_require_project_role` / `get_project`, causing
# 8+ test_api failures visible in the ms-98 refactor window. The right shape
# is per-test monkeypatch (below) — pytest auto-restores on teardown so the
# pollution never survives past this file.
_store_router_module = app_module.db  # store_router (= app.py's db)
client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    # Mirror mocks onto BOTH firestore_client and store_router because
    # store_router snapshots firestore_client at its own import time — it
    # would otherwise resolve `db.get_project` to the real Firestore call.
    for name, ref in (
        ("get_document", _mock_get_document),
        ("get_active_operation_envelope", _mock_get_active_operation_envelope),
        ("issue_operation_envelope", _mock_issue_operation_envelope),
        ("revoke_operation_envelope", _mock_revoke_operation_envelope),
        ("list_operation_envelopes", _mock_list_operation_envelopes),
        ("get_operation_envelope", _mock_get_operation_envelope),
        ("get_project", lambda pid: copy.deepcopy(PROJECT_DATA)),
        ("save_project", lambda pid, data: None),
        ("list_projects", lambda: []),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(_store_router_module, name, ref, raising=False)
    # app_module attrs — auth off, no watchers, canned project role/load
    monkeypatch.setattr(app_module, "_auth_enabled", False, raising=False)
    monkeypatch.setattr(app_module, "_start_watcher",
                        lambda project_id: None, raising=False)
    monkeypatch.setattr(app_module, "_stop_watcher",
                        lambda project_id: None, raising=False)
    monkeypatch.setattr(
        app_module, "_require_project_role",
        lambda project_id, user, **kw: (copy.deepcopy(PROJECT_DATA), "owner"),
        raising=False,
    )
    monkeypatch.setattr(
        app_module, "_load",
        lambda project_id, user=None: copy.deepcopy(PROJECT_DATA),
        raising=False,
    )
    _envelope_store.clear()
    _doc_store.clear()
    yield


def _make_spec_doc(doc_id: str, op_id: str = OP_ID,
                   approved_actions_block: str = "  - extract:profile:*\n  - task done:e-*") -> None:
    """Seed a SPEC doc in the in-memory store."""
    content = (
        "---\n"
        "scope: spec\n"
        f"operation: {op_id}\n"
        "approved_actions:\n"
        f"{approved_actions_block}\n"
        "---\n"
        "# Body\n"
    )
    _doc_store.setdefault(PROJECT_ID, {})[doc_id] = {
        "doc_id": doc_id,
        "title": "Test SPEC",
        "content": content,
        "revision_id": "rev-1",
    }


# ---------------------------------------------------------------------------
# Approve endpoint
# ---------------------------------------------------------------------------

def test_approve_happy_path():
    _make_spec_doc("spec-1")
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    )
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["op_id"] == OP_ID
    assert rec["spec_doc_id"] == "spec-1"
    assert rec["status"] == "active"
    assert rec["approved_actions"] == ["extract:profile:*", "task done:e-*"]
    env = rec["envelope"]
    assert env["tier"] == "T2"
    assert env["scope"] == OP_ID
    assert env["signature"]
    assert env["actions_authorized"] == ["extract:profile:*", "task done:e-*"]


def test_approve_rejects_missing_spec():
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "ghost"},
    )
    assert resp.status_code == 404
    assert "SPEC doc not found" in resp.json()["detail"]


def test_approve_rejects_wrong_scope():
    _doc_store.setdefault(PROJECT_ID, {})["m-doc"] = {
        "doc_id": "m-doc",
        "content": "---\nscope: memo\n---\nbody\n",
    }
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "m-doc"},
    )
    assert resp.status_code == 400
    assert "expected 'spec'" in resp.json()["detail"]


def test_approve_rejects_wrong_op_binding():
    _make_spec_doc("spec-other", op_id="op-99")
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-other"},
    )
    assert resp.status_code == 400
    assert "bound to operation" in resp.json()["detail"]


def test_approve_rejects_nonexistent_operation():
    _make_spec_doc("spec-99", op_id="op-99")
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/op-99/envelopes",
        json={"spec_doc_id": "spec-99"},
    )
    assert resp.status_code == 404
    assert "operation not found" in resp.json()["detail"]


def test_approve_rejects_missing_approved_actions():
    _doc_store.setdefault(PROJECT_ID, {})["no-actions"] = {
        "doc_id": "no-actions",
        "content": (
            "---\n"
            "scope: spec\n"
            f"operation: {OP_ID}\n"
            "---\n"
            "body\n"
        ),
    }
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "no-actions"},
    )
    # ms-60 / e-2306 regression: this scenario previously surfaced as a 500
    # because store_router omitted `_extract_frontmatter_field` from its
    # re-export list, so `_spec_doc_for_op` raised AttributeError before any
    # 4xx handler could fire. The test now pins the 400 path open.
    assert resp.status_code == 400
    assert "no `approved_actions`" in resp.json()["detail"]


def test_store_router_reexports_extract_frontmatter_field():
    """e-2306 regression guard.

    `_spec_doc_for_op` in `server/app.py` calls
    `db._extract_frontmatter_field(...)` where `db` is `store_router`. If the
    router stops re-exporting that helper, every `operation approve` call
    raises AttributeError → catch-all 500. Pin the contract at the module
    level so the symptom shows up here instead of in production.
    """
    import store_router as router_under_test
    assert hasattr(router_under_test, "_extract_frontmatter_field"), (
        "store_router must re-export `_extract_frontmatter_field` for both "
        "firestore and dynamodb backends (consumed by operation_approve)"
    )
    # Smoke-call the helper to confirm it's the real function, not a stub.
    sample = (
        "---\n"
        "scope: spec\n"
        f"operation: {OP_ID}\n"
        "---\n"
        "body\n"
    )
    assert router_under_test._extract_frontmatter_field(sample, "scope") == "spec"
    assert router_under_test._extract_frontmatter_field(sample, "operation") == OP_ID
    assert router_under_test._extract_frontmatter_field(sample, "missing") == ""


def test_approve_rejects_empty_approved_actions():
    _doc_store.setdefault(PROJECT_ID, {})["empty"] = {
        "doc_id": "empty",
        "content": (
            "---\n"
            "scope: spec\n"
            f"operation: {OP_ID}\n"
            "approved_actions: []\n"
            "---\n"
            "body\n"
        ),
    }
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "empty"},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"]


def test_approve_rejects_invalid_action_syntax():
    _make_spec_doc(
        "bad-syntax",
        approved_actions_block="  - extract:*:user",  # middle wildcard
    )
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "bad-syntax"},
    )
    assert resp.status_code == 400
    assert "invalid approved_actions" in resp.json()["detail"]


def test_reapprove_auto_revokes_prior_active():
    _make_spec_doc("spec-1")
    r1 = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    )
    first_id = r1.json()["envelope_id"]
    # Issue again with same SPEC — a fresh envelope is minted, prior revoked.
    r2 = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    )
    assert r2.status_code == 200
    second_id = r2.json()["envelope_id"]
    assert second_id != first_id

    # Verify list state: first is revoked, second is active.
    listed = client.get(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes"
    ).json()
    actives = [r for r in listed if r["status"] == "active"]
    assert len(actives) == 1
    assert actives[0]["envelope_id"] == second_id

    revoked = [r for r in listed if r["status"] == "revoked"]
    assert any(r["envelope_id"] == first_id for r in revoked)
    superseded = next(r for r in revoked if r["envelope_id"] == first_id)
    assert superseded["revoke_reason"] == "superseded by new approve"


# ---------------------------------------------------------------------------
# Revoke endpoint
# ---------------------------------------------------------------------------

def test_revoke_happy_path():
    _make_spec_doc("spec-1")
    issued = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    ).json()
    env_id = issued["envelope_id"]

    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes/{env_id}/revoke",
        json={"reason": "manual stop"},
    )
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["status"] == "revoked"
    assert rec["revoke_reason"] == "manual stop"
    assert rec["revoked_at"]


def test_revoke_is_idempotent():
    _make_spec_doc("spec-1")
    issued = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    ).json()
    env_id = issued["envelope_id"]
    first = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes/{env_id}/revoke",
        json={"reason": "first"},
    ).json()
    second = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes/{env_id}/revoke",
        json={"reason": "second"},
    ).json()
    # First revoke wins — reason from second call should NOT replace first's.
    assert first["revoke_reason"] == "first"
    assert second["revoke_reason"] == "first"
    assert first["revoked_at"] == second["revoked_at"]


def test_revoke_rejects_missing_envelope():
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes/ghost/revoke",
        json={"reason": "x"},
    )
    assert resp.status_code == 404


def test_revoke_rejects_op_id_mismatch():
    # Mint envelope under op-1, try to revoke via op-99 URL — must reject.
    _make_spec_doc("spec-1")
    issued = client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    ).json()
    env_id = issued["envelope_id"]
    resp = client.post(
        f"/api/projects/{PROJECT_ID}/operations/op-99/envelopes/{env_id}/revoke",
        json={"reason": "wrong"},
    )
    assert resp.status_code == 400
    assert "belongs to operation" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------

def test_list_empty():
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes"
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_returns_newest_first():
    _make_spec_doc("spec-1")
    _make_spec_doc("spec-2")
    client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    )
    client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-2"},
    )
    listed = client.get(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes"
    ).json()
    assert len(listed) == 2
    assert listed[0]["created_at"] >= listed[1]["created_at"]


def test_list_filters_status():
    _make_spec_doc("spec-1")
    _make_spec_doc("spec-2")
    client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-1"},
    )
    client.post(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes",
        json={"spec_doc_id": "spec-2"},
    )
    actives = client.get(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes?status=active"
    ).json()
    revoked = client.get(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes?status=revoked"
    ).json()
    assert len(actives) == 1
    assert len(revoked) == 1


def test_list_rejects_invalid_status():
    resp = client.get(
        f"/api/projects/{PROJECT_ID}/operations/{OP_ID}/envelopes?status=garbage"
    )
    assert resp.status_code == 400
