"""Tests for the request-wide timeout middleware (ms-145 / e-5317).

本番インシデント: 認証時の外部依存ハングで 1 本のリクエストが約 3.6h 滞留し、
本番プロセスが飢えて突然死した。ここではリクエスト全体に上限時間を課し、上限超過は
504 で即座に閉じる (= 数時間かかるリクエストを機構として不可能にする) ことを検証する。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


@pytest.fixture
def app_module(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    monkeypatch.delenv("BEACON_AUTH_PROVIDER", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    return importlib.import_module("app")


def _build_client(app_module, timeout):
    """Fresh FastAPI app wired with only the timeout middleware + 2 routes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # dispatch reads the module-level _REQUEST_TIMEOUT each request
    app_module._REQUEST_TIMEOUT = timeout
    app = FastAPI()
    app.add_middleware(app_module.RequestTimeoutMiddleware)

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(1.0)
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_slow_request_returns_504(app_module):
    client = _build_client(app_module, timeout=0.2)
    resp = client.get("/slow")
    assert resp.status_code == 504
    # 504 本文は閾値を含める (AI 呼び出し側が「設定が短い」か「実際に遅い」かを判別できる)
    assert resp.json()["detail"].startswith("Request timed out after")


def test_fast_request_passes(app_module):
    client = _build_client(app_module, timeout=0.2)
    resp = client.get("/fast")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_zero_timeout_disables_cap(app_module):
    """BEACON_REQUEST_TIMEOUT_S<=0 は opt-out (= 上限を課さない)。"""
    client = _build_client(app_module, timeout=0.0)
    resp = client.get("/slow")
    assert resp.status_code == 200


def test_default_timeout_is_bounded(app_module):
    """既定値は有界 (無限待ちにならない)。"""
    assert app_module._REQUEST_TIMEOUT > 0
