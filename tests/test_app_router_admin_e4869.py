"""ms-127 e-4869 (B フェーズ): E2E verification for the /api/admin/* router split.

Follows the型 of tests/test_app_router_me_e4869.py and _orgs_e4869.py, with the
same CI-hardening lessons: OpenAPI-path mount check (version-stable), auth-gate
forced via _auth_enabled=True, happy path with the store stubbed, and a check
that the injected admin gate (require_admin) is actually called through the full
router stack.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import routers_admin  # noqa: E402

client = TestClient(app_module.app)

ADMIN_PATHS = [
    "/api/admin/users",
    "/api/admin/users/{user_id}",
    "/api/admin/projects",
    "/api/admin/projects/ownerless",
    "/api/admin/projects/{project_id}",
    "/api/admin/trash/sweep",
    "/api/admin/projects/{project_id}/owner",
    "/api/admin/me",
]


def test_admin_routes_are_mounted():
    """All /api/admin/* paths are served by the mounted router (OpenAPI path map —
    deterministic, version-stable, no HTTP request)."""
    paths = set(app_module.app.openapi().get("paths", {}).keys())
    for p in ADMIN_PATHS:
        assert p in paths, (p, sorted(x for x in paths if "/api/admin" in x))


def test_admin_users_auth_gated_when_auth_enabled(monkeypatch):
    """With auth enabled and no token, GET /api/admin/users is rejected at the
    injected require_auth (401) before any handler/store code runs — proving the
    route is mounted AND auth-gated."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)
    r = client.get("/api/admin/users")
    assert r.status_code == 401, (r.status_code, r.text)


def test_admin_me_happy_path_through_router(monkeypatch):
    """GET /api/admin/me runs the router body end-to-end (auth overridden, store
    stubbed) and returns {is_admin: True} verbatim for an admin user. admin_me is
    the one admin route with no require_admin gate, so it exercises the router
    body without needing the gate."""

    class _FakeDB:
        def get_user(self, uid):
            return {"role": "admin"}

    monkeypatch.setattr(routers_admin, "db", _FakeDB())
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
        "sub": "u1",
        "email": "u1@example.com",
    }
    try:
        r = client.get("/api/admin/me")
    finally:
        app_module.app.dependency_overrides.pop(app_module.require_auth, None)

    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json() == {"is_admin": True}, r.json()


def test_require_admin_is_wired_and_called():
    """The injected ``require_admin`` gate is the security boundary for the
    mutating admin routes. Prove it is actually called through the full router
    stack: build a router whose gate raises 403, mount it, and confirm a guarded
    endpoint (GET /api/admin/users) surfaces that 403. A mis-wire that dropped the
    gate would let the request through."""
    from fastapi import FastAPI, HTTPException

    def _deny(user):
        raise HTTPException(status_code=403, detail="Admin access required")

    probe = FastAPI()
    probe.include_router(
        routers_admin.make_router(
            require_auth=lambda: {"sub": "u1"},
            require_admin=_deny,
            apply_op_and_broadcast=lambda *a, **k: {},
        )
    )
    r = TestClient(probe).get("/api/admin/users")
    assert r.status_code == 403, (r.status_code, r.text)


def test_make_router_rejects_non_callable_dep():
    """Passing a non-callable where a callable is expected fails at construction
    with a TypeError, not at request time."""
    import pytest

    with pytest.raises(TypeError):
        routers_admin.make_router(
            require_auth=lambda: {"sub": "x"},
            require_admin=True,  # bug: not a callable
            apply_op_and_broadcast=lambda *a, **k: {},
        )


def test_no_stale_admin_routes_left_in_app_py():
    """server/app.py no longer declares any ``@app.<method>("/api/admin...")`` —
    the routes live solely in routers_admin. Source-level check."""
    app_src = (
        Path(__file__).resolve().parent.parent / "server" / "app.py"
    ).read_text(encoding="utf-8")
    stale = re.findall(r'@app\.\w+\(\s*["\']/api/admin[^"\']*["\']', app_src)
    assert not stale, (
        f"server/app.py still declares /api/admin route(s): {stale} — they belong "
        "in routers_admin.py."
    )


def test_admin_router_factory_is_standalone_with_injected_deps():
    """routers_admin.make_router(require_auth, *, require_admin, apply_op_and_broadcast)
    builds a mountable APIRouter carrying exactly the eight /api/admin/* paths."""
    from fastapi import APIRouter

    router = routers_admin.make_router(
        require_auth=lambda: {"sub": "x"},
        require_admin=lambda user: None,
        apply_op_and_broadcast=lambda *a, **k: {},
    )
    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    for p in ADMIN_PATHS:
        assert p in paths, (p, paths)
