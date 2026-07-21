"""e-3834: /ws/projects/{project_id} の per-(project, session) 同時接続上限のテスト。

背景: 1 つのセッションの bridge が再接続を暴走させ、WebSocket 接続を 14,000 本超
まで積み上げて server を OOM→全ユーザー 502 に落とした本番障害があった。サーバ側に
「1 セッションが張れる同時接続数の上限」が無く、1 クライアントの異常再接続が全体を
巻き添えにできる構造だった。ws_project に per-(project, session) の上限
(_WS_MAX_CONNS_PER_SESSION) を入れ、超過分は accept せず 4429 で閉じる。

ここでテストするのは:
  - 同一 session_id で上限を超えた接続は 4429 (too_many_connections) で閉じられる
  - 上限は session ごとに独立 (別 session_id は影響を受けない)
  - session_id を申告しない Web UI 接続は上限の対象外 (複数タブを許容)
  - 接続を閉じるとスロットが解放され、再び接続できる (台帳がリークしない)

fixtures は既存 WS テスト (test_ws_registry_connect_e3009.py) と同型:
auth 無効化 / firestore mock / watcher スタブ / redis 台帳 no-op。
"""
from __future__ import annotations

import contextlib
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client
import app as app_module
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


_store: dict[str, dict] = {}


def _mock_get_project(project_id: str):
    data = _store.get(project_id)
    return copy.deepcopy(data) if data else None


def _mock_save_project(project_id: str, data: dict):
    _store[project_id] = copy.deepcopy(data)


@pytest.fixture(autouse=True)
def _patch_firestore_mocks():
    import store_router

    prev = (
        firestore_client.get_project,
        firestore_client.save_project,
        store_router.get_project,
        store_router.save_project,
    )
    firestore_client.get_project = _mock_get_project
    firestore_client.save_project = _mock_save_project
    store_router.get_project = _mock_get_project
    store_router.save_project = _mock_save_project
    _store.clear()
    yield
    (
        firestore_client.get_project,
        firestore_client.save_project,
        store_router.get_project,
        store_router.save_project,
    ) = prev
    _store.clear()


@pytest.fixture(autouse=True)
def _disable_auth():
    prev = app_module._auth_enabled
    app_module._auth_enabled = False
    yield
    app_module._auth_enabled = prev


@pytest.fixture(autouse=True)
def _stub_watcher():
    prev_start, prev_stop = app_module._start_watcher, app_module._stop_watcher
    app_module._start_watcher = lambda pid: None
    app_module._stop_watcher = lambda pid: None
    yield
    app_module._start_watcher, app_module._stop_watcher = prev_start, prev_stop


@pytest.fixture(autouse=True)
def _noop_registry(monkeypatch):
    # 実 Redis を使わず台帳ヘルパーを no-op に。接続上限は in-process の
    # _ws_session_conns で判定されるので Redis 台帳の中身はテスト対象外。
    monkeypatch.setattr(app_module.redis_client, "ws_register", lambda *a, **k: True)
    monkeypatch.setattr(app_module.redis_client, "ws_refresh", lambda *a, **k: True)
    monkeypatch.setattr(app_module.redis_client, "ws_unregister", lambda *a, **k: True)


client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _lifespan():
    with client:
        yield


@pytest.fixture(autouse=True)
def _clear_conn_table():
    # 各テストの前後で in-process の接続台帳を空にして相互汚染を防ぐ。
    app_module._ws_session_conns.clear()
    yield
    app_module._ws_session_conns.clear()


def _seed(pid: str):
    _store[pid] = {
        "name": "t",
        "summary": "",
        "owner": "",
        "members": [],
        "milestones": [],
        "operations": [],
        "schema_version": 1,
    }


def test_over_cap_closed_with_4429(monkeypatch):
    """同一 session が上限を超えて接続すると 4429 で閉じられる。"""
    monkeypatch.setattr(app_module, "_WS_MAX_CONNS_PER_SESSION", 2)
    pid = "p-cap-1"
    _seed(pid)
    with contextlib.ExitStack() as stack:
        ws1 = stack.enter_context(
            client.websocket_connect(f"/ws/projects/{pid}?session_id=sess-X")
        )
        assert ws1.receive_json()["type"] == "ws_ready"
        ws2 = stack.enter_context(
            client.websocket_connect(f"/ws/projects/{pid}?session_id=sess-X")
        )
        assert ws2.receive_json()["type"] == "ws_ready"
        # 3 本目は上限 (2) 超過 → accept されず 4429 で閉じられる。
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/ws/projects/{pid}?session_id=sess-X"
            ) as ws3:
                ws3.receive_text()
        assert exc.value.code == 4429


def test_cap_is_per_session(monkeypatch):
    """上限は session ごとに独立。別 session_id は満杯の影響を受けない。"""
    monkeypatch.setattr(app_module, "_WS_MAX_CONNS_PER_SESSION", 1)
    pid = "p-cap-2"
    _seed(pid)
    with client.websocket_connect(f"/ws/projects/{pid}?session_id=sA") as ws_a:
        assert ws_a.receive_json()["type"] == "ws_ready"
        # 同一 session sA の 2 本目は上限 (1) 超過 → 4429。
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/ws/projects/{pid}?session_id=sA"
            ) as ws_a2:
                ws_a2.receive_text()
        assert exc.value.code == 4429
        # 別 session sB は独立にちゃんと接続できる。
        with client.websocket_connect(f"/ws/projects/{pid}?session_id=sB") as ws_b:
            assert ws_b.receive_json()["type"] == "ws_ready"


def test_web_ui_no_session_id_not_capped(monkeypatch):
    """session_id 申告なし (= Web UI ダッシュボード / 複数タブ) は上限の対象外。"""
    monkeypatch.setattr(app_module, "_WS_MAX_CONNS_PER_SESSION", 1)
    pid = "p-cap-3"
    _seed(pid)
    with contextlib.ExitStack() as stack:
        for _ in range(3):
            ws = stack.enter_context(client.websocket_connect(f"/ws/projects/{pid}"))
            assert ws.receive_json()["type"] == "ws_ready"


def test_slot_freed_after_disconnect(monkeypatch):
    """接続を閉じるとスロットが解放され、再接続できる。台帳 key もリークしない。"""
    monkeypatch.setattr(app_module, "_WS_MAX_CONNS_PER_SESSION", 1)
    pid = "p-cap-4"
    _seed(pid)
    with client.websocket_connect(f"/ws/projects/{pid}?session_id=sZ") as ws1:
        assert ws1.receive_json()["type"] == "ws_ready"
    # ws1 は閉じた → スロット解放。同 session で再接続できる。
    with client.websocket_connect(f"/ws/projects/{pid}?session_id=sZ") as ws2:
        assert ws2.receive_json()["type"] == "ws_ready"
    # 全接続が閉じたら台帳 key は残らない (空 set を pop している)。
    assert (pid, "sZ") not in app_module._ws_session_conns
