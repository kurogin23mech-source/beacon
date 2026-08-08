"""ms-127 e-4869 (B フェーズ, final slice): E2E verification for the /api/auth/*
router split.

Follows the型 of the sibling router tests, with the same CI-hardening lessons:
OpenAPI-path mount check, forced flags where auth mode matters, store stubbed for
happy paths, and a check that the injected token minter is actually on the hot
path.
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
import routers_auth  # noqa: E402

client = TestClient(app_module.app)

AUTH_PATHS = [
    "/api/auth/config",
    "/api/auth/dev-login",
    "/api/auth/exchange-cli-token",
    "/api/auth/cli-start",
    "/api/auth/cli-approve",
    "/api/auth/cli-poll",
]


def test_auth_routes_are_mounted():
    """All /api/auth/* paths are served by the mounted router (OpenAPI path map —
    deterministic, version-stable)."""
    paths = set(app_module.app.openapi().get("paths", {}).keys())
    for p in AUTH_PATHS:
        assert p in paths, (p, sorted(x for x in paths if "/api/auth" in x))


def test_auth_config_reflects_injected_provider_flag(monkeypatch):
    """GET /api/auth/config runs the router body end-to-end and reads the provider
    through the injected get_auth_provider() callable. Flipping app._AUTH_PROVIDER
    at runtime must be reflected (proving the callable injection, not a frozen
    snapshot). No auth required for this endpoint."""
    monkeypatch.setattr(app_module, "_AUTH_PROVIDER", "cognito", raising=False)
    r = client.get("/api/auth/config")
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json().get("provider") == "cognito", r.json()

    monkeypatch.setattr(app_module, "_AUTH_PROVIDER", "firebase", raising=False)
    r2 = client.get("/api/auth/config")
    assert r2.json().get("provider") == "firebase", r2.json()


def test_exchange_cli_token_auth_gated_when_auth_enabled(monkeypatch):
    """With auth enabled and no token, POST /api/auth/exchange-cli-token is
    rejected at the injected require_auth (401), proving the route is mounted AND
    auth-gated."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)
    r = client.post("/api/auth/exchange-cli-token")
    assert r.status_code == 401, (r.status_code, r.text)


def test_dev_login_404_when_local_dev_disabled(monkeypatch):
    """POST /api/auth/dev-login is hard-gated on the injected get_local_dev_enabled():
    when app._local_dev_enabled is False it returns 404 (unreachable in prod).
    Proves the flag is read through the callable per-request."""
    monkeypatch.setattr(app_module, "_local_dev_enabled", False, raising=False)
    r = client.post("/api/auth/dev-login", json={"email": "x@example.com"})
    assert r.status_code == 404, (r.status_code, r.text)


def test_dev_login_mints_token_via_injected_make_cli_token(monkeypatch):
    """With local-dev enabled and the store stubbed, POST /api/auth/dev-login runs
    the router body end-to-end and mints a token through the injected
    make_cli_token. Prove that injection is on the hot path: a sentinel minter is
    called and its token is returned verbatim."""
    minted = []

    class _FakeDB:
        def get_or_create_user(self, sub, email):
            return {"sub": sub, "email": email}

        def update_user(self, sub, patch):
            return True

    def _fake_mint(sub, email):
        minted.append((sub, email))
        return ("bcli.sentinel", 4102444800)

    from fastapi import FastAPI

    probe = FastAPI()
    probe.include_router(
        routers_auth.make_router(
            require_auth=lambda: {"sub": "unused"},
            make_cli_token=_fake_mint,
            get_local_dev_enabled=lambda: True,
            get_auth_provider=lambda: "firebase",
        )
    )
    import routers_auth as _ra
    old_db = _ra.db
    _ra.db = _FakeDB()
    try:
        r = TestClient(probe).post("/api/auth/dev-login", json={"email": "A@Example.com"})
    finally:
        _ra.db = old_db

    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["id_token"] == "bcli.sentinel", body
    # email is lowercased and sub derived deterministically as dev:<email>
    assert minted == [("dev:a@example.com", "a@example.com")], minted


def test_cli_pairing_round_trip_through_router():
    """cli-start → cli-approve → cli-poll shares the auth-local _cli_pending state
    and issues a token via the injected minter. Exercises the whole CLI-pairing
    flow through a freshly-mounted router (auth-local state moved with the routes)."""
    from fastapi import FastAPI

    probe = FastAPI()
    probe.include_router(
        routers_auth.make_router(
            require_auth=lambda: {"sub": "u1", "email": "u1@example.com"},
            make_cli_token=lambda sub, email: ("bcli.paired", 4102444800),
            get_local_dev_enabled=lambda: True,
            get_auth_provider=lambda: "firebase",
        )
    )
    c = TestClient(probe)
    code = c.post("/api/auth/cli-start").json()["code"]
    assert c.get("/api/auth/cli-poll", params={"code": code}).json()["status"] == "pending"
    assert c.post("/api/auth/cli-approve", json={"code": code}).json()["status"] == "approved"
    approved = c.get("/api/auth/cli-poll", params={"code": code}).json()
    assert approved["status"] == "approved" and approved["id_token"] == "bcli.paired", approved


def test_cli_approve_requires_auth(monkeypatch):
    """POST /api/auth/cli-approve mutates pairing state on behalf of the signed-in
    user, so it MUST be auth-gated. With auth enabled and no token it returns 401
    on the real app — guards against a future move accidentally dropping the
    Depends(require_auth) (maintainability review, PR #610)."""
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)
    r = client.post("/api/auth/cli-approve", json={"code": "ABCD1234"})
    assert r.status_code == 401, (r.status_code, r.text)


def test_cli_poll_is_one_time_after_consume():
    """cli-poll consumes the pairing code on success (issues token + deletes it),
    so a replay (second poll of the same code) must NOT re-issue — it returns 404.
    Guards the one-time property of the flow (AX/maintainability review, PR #610)."""
    from fastapi import FastAPI

    probe = FastAPI()
    probe.include_router(
        routers_auth.make_router(
            require_auth=lambda: {"sub": "u1", "email": "u1@example.com"},
            make_cli_token=lambda sub, email: ("bcli.once", 4102444800),
            get_local_dev_enabled=lambda: True,
            get_auth_provider=lambda: "firebase",
        )
    )
    c = TestClient(probe)
    code = c.post("/api/auth/cli-start").json()["code"]
    c.post("/api/auth/cli-approve", json={"code": code})
    first = c.get("/api/auth/cli-poll", params={"code": code})
    assert first.json()["id_token"] == "bcli.once", first.json()
    # replay: the code was consumed/deleted on the first successful poll.
    replay = c.get("/api/auth/cli-poll", params={"code": code})
    assert replay.status_code == 404, (replay.status_code, replay.text)


def test_no_stale_auth_routes_left_in_app_py():
    """server/app.py no longer declares any ``@app.<method>("/api/auth...")`` — the
    routes live solely in routers_auth. Source-level check."""
    app_src = (
        Path(__file__).resolve().parent.parent / "server" / "app.py"
    ).read_text(encoding="utf-8")
    stale = re.findall(r'@app\.\w+\(\s*["\']/api/auth[^"\']*["\']', app_src)
    assert not stale, (
        f"server/app.py still declares /api/auth route(s): {stale} — they belong "
        "in routers_auth.py."
    )


def test_make_router_rejects_non_callable_dep():
    """Passing a non-callable where a callable is expected fails at construction."""
    import pytest

    with pytest.raises(TypeError):
        routers_auth.make_router(
            require_auth=lambda: {"sub": "x"},
            make_cli_token=lambda s, e: ("t", 0),
            get_local_dev_enabled=True,  # bug: not a callable
            get_auth_provider=lambda: "firebase",
        )


def test_auth_router_factory_is_standalone_with_injected_deps():
    """routers_auth.make_router builds a mountable APIRouter with exactly the six
    /api/auth/* paths."""
    from fastapi import APIRouter

    router = routers_auth.make_router(
        require_auth=lambda: {"sub": "x"},
        make_cli_token=lambda s, e: ("t", 0),
        get_local_dev_enabled=lambda: True,
        get_auth_provider=lambda: "firebase",
    )
    assert isinstance(router, APIRouter)
    paths = {r.path for r in router.routes}
    for p in AUTH_PATHS:
        assert p in paths, (p, paths)
