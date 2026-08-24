"""E2E tests for machine direct-write endpoints (ms-151 / e-5476).

run_record / incident を machine 認証で Operation に直接書く 2 endpoint を、実 app +
TestClient + in-memory mock store で駆動する。auth は REAL の require_auth machine 経路
(bmk. token + stub した get_machine_key) を通し、永続化は mock operations backend
(firestore_client.get_project/save_project を in-memory 化) を通す。

証明:
- machine key で run_record / incident を書ける (envelope 不要 = header 無しで通る)。
- incident の opened_at (machine が報告する発生時刻) が保存される。
- 別 project の machine key での書き込みは 403。
- 人間 / CLI トークン (machine 文脈なし) は 403 (この口は machine 専用)。
- op_id 不在は 404、status 不正は 400。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# Must be set BEFORE importing operations / app (mirror tests/test_api.py).
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import machine_key as mk  # noqa: E402

client = TestClient(app_module.app)

PID = "beacon-b95643"
NOW = "2026-08-24T12:00:00Z"


def _fresh_project():
    return {
        "name": "Test",
        "milestones": [],
        "operations": [
            {"id": "op-1", "title": "Health", "status": "open", "entries": []}
        ],
        "owner": "u-owner",
        "members": [],
        "schema_version": 1,
    }


@pytest.fixture
def machine_env(monkeypatch):
    """auth on + in-memory store seeded with PID/op-1 + a valid machine key for PID."""
    store = {PID: _fresh_project()}

    def get_project(pid):
        return copy.deepcopy(store.get(pid))

    def save_project(pid, data):
        store[pid] = copy.deepcopy(data)

    for mod in (firestore_client, app_module.db):
        monkeypatch.setattr(mod, "get_project", get_project)
        monkeypatch.setattr(mod, "save_project", save_project)

    raw, record = mk.issue(PID, label="PE", created_by="u-owner", now=NOW)
    monkeypatch.setattr(
        app_module.db, "get_machine_key",
        lambda pid, kid: record if (pid == PID and kid == record["key_id"]) else None,
    )
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    return {"store": store, "raw": raw, "record": record}


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


def test_routes_mounted():
    paths = set(app_module.app.openapi().get("paths", {}).keys())
    assert "/api/projects/{project_id}/operations/{op_id}/run-records" in paths
    assert "/api/projects/{project_id}/operations/{op_id}/incidents" in paths


def test_machine_writes_run_record_no_envelope(machine_env):
    # envelope header を一切付けずに書ける (= 記録に action 承認を課さない)。
    r = client.post(
        f"/api/projects/{PID}/operations/op-1/run-records",
        json={"batch": "nightly", "status": "ok", "description": "all green"},
        headers=_auth(machine_env["raw"]),
    )
    assert r.status_code == 200, (r.status_code, r.text)
    entry = r.json()
    assert entry["type"] == "run_record"
    assert entry["status"] == "ok"
    # 永続化を確認: 保存された project の op-1 に run_record が 1 件。
    op = machine_env["store"][PID]["operations"][0]
    runs = [e for e in op["entries"] if e["type"] == "run_record"]
    assert len(runs) == 1 and runs[0]["batch"] == "nightly"


def test_machine_writes_incident_with_opened_at(machine_env):
    r = client.post(
        f"/api/projects/{PID}/operations/op-1/incidents",
        json={"title": "disk full", "description": "90%",
              "priority": "high", "opened_at": "2026-08-24T09:00:00Z"},
        headers=_auth(machine_env["raw"]),
    )
    assert r.status_code == 200, (r.status_code, r.text)
    entry = r.json()
    assert entry["type"] == "incident"
    assert entry["status"] == "open"
    # machine が報告した発生時刻が保存される (server 受信時刻で上書きしない)。
    assert entry["opened_at"] == "2026-08-24T09:00:00Z"


def test_cross_project_machine_forbidden(monkeypatch, machine_env):
    # 別 project の machine key で PID に書こうとすると 403。
    raw_other, record_other = mk.issue("beacon-OTHER", now=NOW)
    monkeypatch.setattr(
        app_module.db, "get_machine_key",
        lambda pid, kid: record_other if pid == "beacon-OTHER" else None,
    )
    r = client.post(
        f"/api/projects/{PID}/operations/op-1/run-records",
        json={"batch": "b", "status": "ok"},
        headers=_auth(raw_other),
    )
    assert r.status_code == 403, (r.status_code, r.text)


def test_human_token_forbidden(monkeypatch, machine_env):
    # 人間トークン (machine 文脈なし) は 403 — この口は machine 専用。
    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda tok: {"sub": "u-owner", "email": "o@x"})
    monkeypatch.setattr(app_module.db, "get_or_create_user", lambda uid, email: {})
    monkeypatch.setattr(app_module, "_ensure_personal_org", lambda uid, email="": None)
    r = client.post(
        f"/api/projects/{PID}/operations/op-1/run-records",
        json={"batch": "b", "status": "ok"},
        headers=_auth("human-id-token"),
    )
    assert r.status_code == 403, (r.status_code, r.text)


def test_unknown_op_404(machine_env):
    r = client.post(
        f"/api/projects/{PID}/operations/op-DOES-NOT-EXIST/run-records",
        json={"batch": "b", "status": "ok"},
        headers=_auth(machine_env["raw"]),
    )
    assert r.status_code == 404, (r.status_code, r.text)


def test_invalid_status_422(machine_env):
    # e-5502 AX review C: status は Literal になったので不正値は Pydantic が 422 で弾く
    # (core の 400 に届く前に schema 層で reject、OpenAPI にも enum が出る)。
    r = client.post(
        f"/api/projects/{PID}/operations/op-1/run-records",
        json={"batch": "b", "status": "bogus"},
        headers=_auth(machine_env["raw"]),
    )
    assert r.status_code == 422, (r.status_code, r.text)
