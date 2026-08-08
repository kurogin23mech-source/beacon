"""ms-127 e-4869 (B フェーズ): E2E verification for the /api/orgs/* router split.

Follows the型 of tests/test_app_router_me_e4869.py. These are the **Beacon** org
endpoints (ms-113 / ms-118), distinct from /api/trailnode/orgs.

Guards baked in from the me-split CI lessons (see the me test for the full
rationale):
- mount is checked via app.openapi()["paths"] (version-stable) rather than by
  iterating app.routes and reading a ``.path`` that included routers may not
  expose uniformly across FastAPI versions;
- the auth-gate check forces app._auth_enabled=True (another test module flips
  the shared flag to False at import), so the assertion is deterministic;
- the happy path swaps routers_orgs.db for an in-memory fake so it never touches
  a real store backend.
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
import routers_orgs  # noqa: E402

client = TestClient(app_module.app)

ORG_PATHS = [
    "/api/orgs",
    "/api/orgs/{org_id}",
    "/api/orgs/{org_id}/members",
    "/api/orgs/{org_id}/members/{target}",
    "/api/orgs/{org_id}/overview",
]


def test_org_routes_are_mounted():
    """All /api/orgs/* paths are served by the mounted router (checked against the
    OpenAPI path map — deterministic, version-stable, no HTTP request)."""
    paths = set(app_module.app.openapi().get("paths", {}).keys())
    for p in ORG_PATHS:
        assert p in paths, (p, sorted(x for x in paths if "/api/orgs" in x))


def test_org_list_auth_gated_when_auth_enabled(monkeypatch):
    """With auth enabled and no token, GET /api/orgs is rejected at the injected
    require_auth (401) before any handler/store code runs — proving the route is
    mounted AND auth-gated. Forces _auth_enabled True because another test module
    flips the shared app flag to False at import."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)
    r = client.get("/api/orgs")
    assert r.status_code == 401, (r.status_code, r.text)


def test_org_list_happy_path_through_router(monkeypatch):
    """With auth overridden and the store stubbed, GET /api/orgs runs the router
    body end-to-end through the real app and returns the store's list verbatim
    (pure move — the endpoint just delegates to db.list_orgs_for_user)."""

    class _FakeDB:
        def list_orgs_for_user(self, user_filter):
            return [{"org_id": "org-1", "name": "Acme"}]

    monkeypatch.setattr(routers_orgs, "db", _FakeDB())
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
        "sub": "u1",
        "email": "u1@example.com",
    }
    try:
        r = client.get("/api/orgs")
    finally:
        app_module.app.dependency_overrides.pop(app_module.require_auth, None)

    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json() == [{"org_id": "org-1", "name": "Acme"}], r.json()


def test_no_stale_org_routes_left_in_app_py():
    """server/app.py no longer declares any ``@app.<method>("/api/orgs...")`` — the
    routes live solely in routers_orgs. Source-level check (deterministic)."""
    app_src = (
        Path(__file__).resolve().parent.parent / "server" / "app.py"
    ).read_text(encoding="utf-8")
    stale = re.findall(r'@app\.\w+\(\s*["\']/api/orgs[^"\']*["\']', app_src)
    assert not stale, (
        f"server/app.py still declares /api/orgs route(s): {stale} — they belong "
        "in routers_orgs.py."
    )


def test_org_router_factory_is_standalone_with_injected_deps():
    """routers_orgs.make_router(require_auth, *, is_auth_enabled, load_org_for_member)
    builds a mountable APIRouter carrying exactly the five /api/orgs/* paths, taking
    the host app's auth flag + org-load helper as keyword-only callables."""
    from fastapi import APIRouter

    router = routers_orgs.make_router(
        require_auth=lambda: {"sub": "x"},
        is_auth_enabled=lambda: True,
        load_org_for_member=lambda org_id, user: {"org_id": org_id},
    )
    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    for p in ORG_PATHS:
        assert p in paths, (p, paths)
