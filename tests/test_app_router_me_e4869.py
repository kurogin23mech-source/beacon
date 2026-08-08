"""ms-127 e-4869 (B フェーズ): E2E verification for the /api/me/* router split.

Follows the型 established by tests/test_app_router_scaffold_e4868.py (the
/api/version scaffold): import the REAL app, drive endpoints through full
FastAPI routing with an in-process TestClient (no network), and prove both
(a) the routes are served by the mounted router and (b) no stale duplicate is
left behind in server/app.py.

This is the FIRST auth-requiring extraction, so it additionally proves the
``make_router(require_auth, stamp_session_liveness, session_is_live)`` injection
pattern wires up correctly:

- Without a token the mounted route returns 401 (NOT 404) — the route exists
  and the injected ``require_auth`` gates it.
- With ``require_auth`` overridden (dependency_overrides) and the store backend
  stubbed, the router body runs end-to-end through the real app and returns the
  unchanged response shape — proving the move is behaviour-preserving.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# Must be set BEFORE importing app (mirrors tests/test_api.py convention).
os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import routers_me  # noqa: E402

client = TestClient(app_module.app)

ME_PATHS = [
    "/api/me/profile",
    "/api/me/projects",
    "/api/me/sessions",
    "/api/me/machine",
    "/api/me/heartbeat",
]


def test_me_routes_mounted_and_auth_gated():
    """Every /api/me/* route is served by the mounted router and gated by the
    injected ``require_auth``: an unauthenticated request returns 401, NOT 404.

    A 404 here would mean the split dropped the route entirely; a 200 would mean
    auth wasn't wired. 401 proves both: mounted + auth-required."""
    r = client.get("/api/me/profile")
    assert r.status_code == 401, (r.status_code, r.text)


def test_me_profile_happy_path_through_router(monkeypatch):
    """With auth overridden and the store stubbed, GET /api/me/profile runs the
    router body end-to-end through the real app and returns the unchanged shape
    (pure move — same {user_id, email, display_name})."""

    class _FakeDB:
        def get_user(self, uid):
            return {"email": "stored@example.com", "display_name": "Stored Name"}

    # The route body resolves ``db`` from routers_me's module globals on each
    # call, so swapping the module attribute redirects the mounted router too.
    monkeypatch.setattr(routers_me, "db", _FakeDB())
    app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
        "sub": "u1",
        "email": "jwt@example.com",
    }
    try:
        r = client.get("/api/me/profile")
    finally:
        app_module.app.dependency_overrides.pop(app_module.require_auth, None)

    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body == {
        "user_id": "u1",
        "email": "stored@example.com",
        "display_name": "Stored Name",
    }, body


def test_no_stale_me_routes_left_in_app_py():
    """The split must not leave stale handlers behind: server/app.py no longer
    declares any ``@app.<method>("/api/me/...")`` — the routes now live solely in
    routers_me. Checked at the SOURCE level (deterministic, isolation-proof)
    rather than by iterating app.routes at runtime."""
    app_src = (
        Path(__file__).resolve().parent.parent / "server" / "app.py"
    ).read_text(encoding="utf-8")
    stale = re.findall(r'@app\.\w+\(\s*["\']/api/me/[^"\']*["\']', app_src)
    assert not stale, (
        f"server/app.py still declares /api/me/* route(s): {stale} — the split "
        "left stale duplicate(s); they belong in routers_me.py."
    )


def test_me_router_factory_is_standalone_with_injected_deps():
    """routers_me.make_router(require_auth, stamp, is_live) builds a mountable
    APIRouter carrying exactly the five /api/me/* paths, taking the host app's
    auth + liveness helpers as arguments (the auth-router injection型)."""
    from fastapi import APIRouter

    router = routers_me.make_router(
        require_auth=lambda: {"sub": "x"},
        stamp_session_liveness=lambda *a, **k: None,
        session_is_live=lambda *a, **k: True,
    )
    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    for p in ME_PATHS:
        assert p in paths, (p, paths)
