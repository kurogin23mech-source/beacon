"""Tests for /ws/projects/{project_id} auth failure close codes (e-639).

Before this change the WS endpoint always closed with 1008 (policy violation)
for any auth failure. That made it impossible for the client to distinguish
"never signed in" from "your token just expired" — both produced the same
opaque 3-second reconnect loop.

After: the server uses application-private codes:
  4401 — token missing      (client should redirect to login)
  4403 — token invalid/expired (client should refresh or prompt re-sign-in)

These tests use FastAPI's WebSocketTestSession (TestClient.websocket_connect)
which raises WebSocketDisconnect on close — and the disconnect carries the
.code attribute we want to assert on.
"""

from __future__ import annotations

import json
import os
import sys
import copy

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client

_store: dict[str, dict] = {}


def _mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


# Capture the original (test_api.py's or production) mocks so we can restore
# them on teardown. Otherwise test_api tests that run after these will see our
# isolated _store and fail with KeyError / missing seeds (e-639 regression bait).
_original_get_project = firestore_client.get_project
_original_save_project = firestore_client.save_project


from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import app as app_module

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _patch_firestore_mocks():
    """Swap firestore_client mocks for this file only; restore after each test."""
    prev_get = firestore_client.get_project
    prev_save = firestore_client.save_project
    firestore_client.get_project = _mock_get_project
    firestore_client.save_project = _mock_save_project
    yield
    firestore_client.get_project = prev_get
    firestore_client.save_project = prev_save


@pytest.fixture(autouse=True)
def _enable_auth():
    """Toggle _auth_enabled True for the duration of each test in this file.

    test_api.py disables auth globally at import time; if we leave that disabled
    our reject-path assertions become impossible. Restore the previous value
    after each test so we don't pollute siblings.
    """
    prev = app_module._auth_enabled
    app_module._auth_enabled = True
    yield
    app_module._auth_enabled = prev


@pytest.fixture(autouse=True)
def _seed_project():
    _store.clear()
    _store["p1"] = {
        "name": "WS Test", "milestones": [], "owner": "", "members": [],
    }
    yield
    _store.clear()


def test_token_missing_closes_with_4401():
    """No ?token= → server must close with 4401, not 1008.

    Clients use this to decide "redirect to login" vs "retry with refresh".
    """
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/projects/p1") as ws:
            # The server closes immediately, so receive_text triggers the
            # disconnect.
            ws.receive_text()
    assert exc_info.value.code == 4401


def test_token_invalid_closes_with_4403(monkeypatch):
    """Garbage / expired token → server must close with 4403."""
    def fake_verify(token):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid token: simulated")

    monkeypatch.setattr(app_module, "_verify_id_token", fake_verify)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/projects/p1?token=garbage") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4403


def test_token_valid_accepts(monkeypatch):
    """Sanity: a valid token does NOT trigger any close-code path."""
    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda token: {"sub": "user1", "email": "u@x.test"})
    # _start_watcher calls into real Firestore (doc_ref.on_snapshot) which we
    # have no way to mock here without a live Firestore. Stub it out so the
    # endpoint can run to the point of accepting + sending the initial frame.
    monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)

    with client.websocket_connect("/ws/projects/p1?token=valid") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "project"
        assert msg["data"]["name"] == "WS Test"


def test_unknown_project_closes_with_4404(monkeypatch):
    """e-1252: a signed-in user requesting a project that doesn't exist
    must get 4404, not a silent reconnect loop. Without this the client
    would keep retrying against a non-existent project forever.
    """
    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda token: {"sub": "user1", "email": "u@x.test"})
    monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/projects/does-not-exist?token=valid") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4404


def test_no_role_closes_with_4403_forbidden(monkeypatch):
    """e-1252 (= 「サインインさえできれば他人のプロジェクトの中身が誰でも
    読めてしまう」状態の根本修正): if the project HAS an owner and the
    requesting user is not the owner / not in members[], the WS must close
    with 4403 reason="forbidden". Before this change the WS handler skipped
    the role check entirely and dumped the project contents.
    """
    # Seed a project owned by someone else (NOT user2).
    _store["p2"] = {
        "name": "Owned by user1",
        "milestones": [],
        "owner": "user1",
        "members": [],
    }

    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda token: {"sub": "user2", "email": "u2@x.test"})
    monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/projects/p2?token=valid") as ws:
            ws.receive_text()
    assert exc_info.value.code == 4403
    # Distinguishes "forbidden" from "token_expired_or_invalid" — the
    # client uses .reason to decide whether to retry (token state) or not
    # (forbidden / no role).
    assert exc_info.value.reason == "forbidden"


def test_member_role_accepts(monkeypatch):
    """A signed-in user with a role on the project (viewer is enough) must
    be allowed through. This guards against an overly tight allowlist in
    _require_project_role (a regression here would silently lock viewers
    out of live updates).
    """
    _store["p3"] = {
        "name": "Member project",
        "milestones": [],
        "owner": "user1",
        "members": [{"user_id": "user2", "role": "viewer"}],
    }

    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda token: {"sub": "user2", "email": "u2@x.test"})
    monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
    monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)

    with client.websocket_connect("/ws/projects/p3?token=valid") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "project"
        assert msg["data"]["name"] == "Member project"
