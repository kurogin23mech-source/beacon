"""ms-127 e-4868 (B フェーズ scaffold): app.py god-module split — the first
resource (/api/version) is extracted into server/routers_version.py and mounted
via app.include_router(make_router()). This test IS the E2E verification "型"
the rest of the app.py split follows: import the real app, drive the endpoint
through full FastAPI routing with an in-process TestClient (no network), and
assert (a) the endpoint still works and (b) the route is served exactly once
(no stale duplicate left behind in app.py).
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

client = TestClient(app_module.app)


def test_version_endpoint_served_by_included_router():
    """GET /api/version works end-to-end through the mounted router and returns
    the unchanged response shape (pure move — no behavior change)."""
    r = client.get("/api/version")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert set(body.keys()) == {"cli", "git_rev"}, body
    assert isinstance(body["cli"], str)
    assert isinstance(body["git_rev"], str)


def test_no_stale_version_route_left_in_app_py():
    """The split must not leave a stale handler behind: server/app.py no longer
    declares `@app.(get)("/api/version")` — the route now lives solely in
    routers_version. Checked at the SOURCE level (deterministic, isolation-proof)
    rather than by iterating app.routes at runtime, which the full test suite can
    perturb via other tests that reload/reassign the app module.

    Combined with test_version_endpoint_served_by_included_router (the route DOES
    respond via the mounted router), this proves the move is complete: served by
    the router, and not duplicated in the god-module."""
    app_src = (
        Path(__file__).resolve().parent.parent / "server" / "app.py"
    ).read_text(encoding="utf-8")
    # any @app.<method>("/api/version") decorator would be a stale duplicate.
    assert not re.search(r'@app\.\w+\(\s*["\']/api/version["\']', app_src), (
        "server/app.py still declares an /api/version route — the split left a "
        "stale duplicate; remove it (the route belongs in routers_version.py)."
    )


def test_version_router_factory_is_standalone():
    """routers_version.make_router() builds a mountable APIRouter with no
    external dependency (the minimal scaffold; auth routers take require_auth)."""
    import routers_version
    from fastapi import APIRouter

    router = routers_version.make_router()
    assert isinstance(router, APIRouter)
    paths = [r.path for r in router.routes]
    assert "/api/version" in paths
