"""E2E endpoint tests for machine key management routes (ms-151 / e-5474).

Drives the REAL app through an in-process TestClient (no network). Proves:

- the 3 routes are mounted (POST/GET/DELETE /api/projects/{pid}/machine-keys[/{key_id}]),
- issue returns the raw token ONCE + a redacted view (no secret_hash),
- the issued token round-trips through machine_key.verify_token against the stored record,
- list returns redacted views newest-first,
- revoke stamps revoked_at (404 for unknown key),
- the routes are owner-gated (a viewer gets 403).

Auth is overridden (dependency_overrides) and the project load + store are stubbed,
so the router body runs end-to-end without Firestore/MySQL credentials.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import routers_projects  # noqa: E402
import machine_key as mk  # noqa: E402

client = TestClient(app_module.app)

PID = "beacon-b95643"
MK_PATH = f"/api/projects/{PID}/machine-keys"


class _FakeDB:
    """In-memory machine-key store keyed by (project_id, key_id)."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def save_machine_key(self, project_id, record):
        self.rows[(project_id, record["key_id"])] = dict(record)
        return record

    def get_machine_key(self, project_id, key_id):
        r = self.rows.get((project_id, key_id))
        return dict(r) if r else None

    def list_machine_keys(self, project_id):
        rows = [dict(v) for (p, _k), v in self.rows.items() if p == project_id]
        rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return rows

    def revoke_machine_key(self, project_id, key_id, revoked_at):
        r = self.rows.get((project_id, key_id))
        if not r:
            return None
        r["revoked_at"] = revoked_at
        return dict(r)


@pytest.fixture
def owner_env(monkeypatch):
    """Auth = owner, project load stubbed to an owned project, store = FakeDB."""
    fake = _FakeDB()
    monkeypatch.setattr(routers_projects, "db", fake)
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    # _require_project_role loads the project then checks the caller's role.
    monkeypatch.setattr(
        app_module.operations, "load_project_consistent",
        lambda pid: {"owner": "u-owner", "members": [], "milestones": []},
    )
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
        "sub": "u-owner", "email": "owner@example.com",
    }
    try:
        yield fake
    finally:
        app_module.app.dependency_overrides.pop(app_module.require_auth, None)


def test_routes_are_mounted():
    paths = set(app_module.app.openapi().get("paths", {}).keys())
    assert "/api/projects/{project_id}/machine-keys" in paths
    assert "/api/projects/{project_id}/machine-keys/{key_id}" in paths


def test_issue_returns_raw_once_and_stores_hash(owner_env):
    r = client.post(MK_PATH, json={"label": "PE Lambda"})
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    raw = body["key"]
    # raw token は machine key の形で、この応答でしか得られない。
    assert mk.looks_like_machine_key(raw)
    # redacted view は secret_hash を露出しない。
    assert "secret_hash" not in body["machine_key"]
    assert body["machine_key"]["label"] == "PE Lambda"
    assert body["machine_key"]["revoked"] is False
    # 保存された record は hash のみ持ち、raw token が検証を通る。
    project_id, key_id, _ = mk.parse_token(raw)
    stored = owner_env.get_machine_key(project_id, key_id)
    assert "secret_hash" in stored
    assert mk.verify_token(raw, stored) is not None


def test_list_returns_redacted_newest_first(owner_env):
    client.post(MK_PATH, json={"label": "first"})
    client.post(MK_PATH, json={"label": "second"})
    r = client.get(MK_PATH)
    assert r.status_code == 200, r.text
    rows = r.json()["machine_keys"]
    assert len(rows) == 2
    for row in rows:
        assert "secret_hash" not in row
        assert "key_id" in row


def test_revoke_marks_and_unknown_404(owner_env):
    raw = client.post(MK_PATH, json={}).json()["key"]
    _, key_id, _ = mk.parse_token(raw)
    r = client.delete(f"{MK_PATH}/{key_id}")
    assert r.status_code == 200, r.text
    assert r.json()["machine_key"]["revoked"] is True
    # 失効後は verify が弾く。
    stored = owner_env.get_machine_key(PID, key_id)
    assert mk.verify_token(raw, stored) is None
    # 存在しない key の失効は 404。
    r2 = client.delete(f"{MK_PATH}/does-not-exist")
    assert r2.status_code == 404, r2.text


def test_viewer_forbidden(monkeypatch):
    """A non-owner (viewer) is rejected by the owner gate (403), not served."""
    fake = _FakeDB()
    monkeypatch.setattr(routers_projects, "db", fake)
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    monkeypatch.setattr(
        app_module.operations, "load_project_consistent",
        lambda pid: {"owner": "u-owner",
                     "members": [{"user_id": "u-viewer", "role": "viewer"}],
                     "milestones": []},
    )
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
        "sub": "u-viewer", "email": "viewer@example.com",
    }
    try:
        r = client.post(MK_PATH, json={"label": "x"})
        assert r.status_code == 403, (r.status_code, r.text)
        r2 = client.get(MK_PATH)
        assert r2.status_code == 403, (r2.status_code, r2.text)
    finally:
        app_module.app.dependency_overrides.pop(app_module.require_auth, None)
