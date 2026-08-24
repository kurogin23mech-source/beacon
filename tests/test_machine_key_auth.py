"""require_auth machine-key branch (ms-151 / e-5475).

Drives the real ``app.require_auth`` dependency directly (async) with a fake
Request + Bearer credentials. Proves:

- a valid machine key authenticates: claims carry machine identity + project,
  and request.state gets the machine context + a backend principal scoped to
  the one project,
- a revoked / unknown key is rejected with 401,
- the HUMAN path is untouched: a non-bmk token (CLI / IdP) never enters the
  machine branch and never touches get_machine_key (後方互換).
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

from fastapi import HTTPException  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

import app as app_module  # noqa: E402
import machine_key as mk  # noqa: E402

NOW = "2026-08-24T12:00:00Z"
PID = "beacon-b95643"


def _fake_request():
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace()
    return req


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    yield


def test_valid_machine_key_authenticates(monkeypatch, auth_on):
    raw, record = mk.issue(PID, label="PE Lambda", created_by="u-owner", now=NOW)
    monkeypatch.setattr(app_module.db, "get_machine_key",
                        lambda pid, kid: record if pid == PID else None)

    req = _fake_request()
    claims = asyncio.run(app_module.require_auth(req, _creds(raw)))

    assert claims["machine"] is True
    assert claims["project_id"] == PID
    assert claims["sub"] == f"machine:{PID}:{record['key_id']}"
    # request.state carries the machine context + a project-scoped backend principal.
    assert req.state.machine["project_id"] == PID
    assert req.state.machine["key_id"] == record["key_id"]
    assert req.state.principal["agent_kind"] == "backend"
    assert req.state.principal["declared_scope"] == [PID]
    assert req.state.audit_user_id == claims["sub"]


def test_revoked_key_rejected_401(monkeypatch, auth_on):
    raw, record = mk.issue(PID, now=NOW)
    record["revoked_at"] = "2026-08-24T13:00:00Z"
    monkeypatch.setattr(app_module.db, "get_machine_key", lambda pid, kid: record)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(app_module.require_auth(_fake_request(), _creds(raw)))
    assert ei.value.status_code == 401


def test_unknown_key_rejected_401(monkeypatch, auth_on):
    raw, _ = mk.issue(PID, now=NOW)
    monkeypatch.setattr(app_module.db, "get_machine_key", lambda pid, kid: None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(app_module.require_auth(_fake_request(), _creds(raw)))
    assert ei.value.status_code == 401


def test_tampered_secret_rejected_401(monkeypatch, auth_on):
    raw, record = mk.issue(PID, now=NOW, key_id="kid", secret="right")
    forged = mk.format_token(PID, "kid", "wrong")
    monkeypatch.setattr(app_module.db, "get_machine_key", lambda pid, kid: record)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(app_module.require_auth(_fake_request(), _creds(forged)))
    assert ei.value.status_code == 401


def test_human_token_never_enters_machine_path(monkeypatch, auth_on):
    """A non-bmk token goes through _verify_id_token; get_machine_key is never
    called (= 人間 / CLI 経路が一切変わらない backward-compat proof)."""
    seen = {"verify_id": False, "get_mk": False}

    def fake_verify_id(tok):
        seen["verify_id"] = True
        return {"sub": "u1", "email": "u1@example.com"}

    def fake_get_mk(*a, **k):
        seen["get_mk"] = True
        return None

    monkeypatch.setattr(app_module, "_verify_id_token", fake_verify_id)
    monkeypatch.setattr(app_module.db, "get_machine_key", fake_get_mk)
    monkeypatch.setattr(app_module.db, "get_or_create_user", lambda uid, email: {})
    monkeypatch.setattr(app_module, "_ensure_personal_org", lambda uid, email="": None)

    claims = asyncio.run(
        app_module.require_auth(_fake_request(), _creds("bcli.some.human.token")))

    assert seen["verify_id"] is True
    assert seen["get_mk"] is False
    assert claims["sub"] == "u1"
    assert "machine" not in claims
