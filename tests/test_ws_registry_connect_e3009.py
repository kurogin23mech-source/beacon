"""ms-101 / e-3009: WS 接続/切断で接続台帳を更新 + ping/pong keepalive のテスト。

ここでテストするのは WS エンドポイント ws_project の配線:
  - session_id 付きで接続すると redis_client.ws_register が呼ばれる (= 台帳登録)
  - "ping" を送るたび ws_refresh が呼ばれる (= keepalive TTL 更新)
  - 切断すると ws_unregister が呼ばれる (= 台帳から除去)
  - session_id 無し (= Web UI ダッシュボード) では台帳に一切触れない
  - client ping が idle timeout の間途絶えると server が silent-dead とみなし close

redis_client は実 Redis を使わず、呼び出しを記録するスタブに差し替える。auth /
firestore は既存 WS テスト (test_ws_broadcast_after_write.py) と同型で無効化 /
mock する。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client
import redis_client
import app as app_module
from fastapi.testclient import TestClient


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


class _RegistrySpy:
    """redis_client の WS 台帳ヘルパーの呼び出しを記録するスタブ。"""

    def __init__(self):
        self.registered: list[tuple] = []
        self.refreshed: list[tuple] = []
        self.unregistered: list[tuple] = []

    def ws_register(self, project_id, session_id, conn_id, **kw):
        self.registered.append((project_id, session_id, conn_id))
        return True

    def ws_refresh(self, project_id, session_id, conn_id, **kw):
        self.refreshed.append((project_id, session_id, conn_id))
        return True

    def ws_unregister(self, project_id, session_id, conn_id, **kw):
        self.unregistered.append((project_id, session_id, conn_id))
        return True


@pytest.fixture
def spy(monkeypatch):
    s = _RegistrySpy()
    monkeypatch.setattr(app_module.redis_client, "ws_register", s.ws_register)
    monkeypatch.setattr(app_module.redis_client, "ws_refresh", s.ws_refresh)
    monkeypatch.setattr(app_module.redis_client, "ws_unregister", s.ws_unregister)
    return s


client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _lifespan():
    with client:
        yield


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


# --- 接続で登録 / ping で refresh / 切断で除去 -----------------------------

def test_connect_with_session_id_registers(spy):
    pid = "p-e3009-1"
    _seed(pid)
    with client.websocket_connect(f"/ws/projects/{pid}?session_id=sess-A") as ws:
        assert ws.receive_json()["type"] == "ws_ready"
        # 接続直後に台帳登録されている。
        assert len(spy.registered) == 1
        proj, sess, conn = spy.registered[0]
        assert proj == pid and sess == "sess-A" and conn
    # with を抜ける = 切断。台帳から除去される (同じ conn_id で)。
    assert spy.unregistered == [(pid, "sess-A", conn)]


def test_ping_refreshes_liveness(spy):
    pid = "p-e3009-2"
    _seed(pid)
    with client.websocket_connect(f"/ws/projects/{pid}?session_id=sess-B") as ws:
        ws.receive_json()  # ws_ready
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
    # 2 回の ping で 2 回 refresh される。
    conn = spy.registered[0][2]
    assert spy.refreshed == [(pid, "sess-B", conn), (pid, "sess-B", conn)]


def test_connect_without_session_id_skips_registry(spy):
    """Web UI ダッシュボード相当 (session_id 無し) は台帳に一切触れない。"""
    pid = "p-e3009-3"
    _seed(pid)
    with client.websocket_connect(f"/ws/projects/{pid}") as ws:
        ws.receive_json()  # ws_ready
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
    assert spy.registered == []
    assert spy.refreshed == []
    assert spy.unregistered == []


def test_idle_timeout_reclaims_silent_dead(spy, monkeypatch):
    """client ping が idle timeout の間途絶えたら server が close して台帳から外す。"""
    monkeypatch.setattr(app_module, "_WS_IDLE_TIMEOUT_SECONDS", 0.2)
    pid = "p-e3009-4"
    _seed(pid)
    from starlette.websockets import WebSocketDisconnect as _WSD

    with client.websocket_connect(f"/ws/projects/{pid}?session_id=sess-C") as ws:
        ws.receive_json()  # ws_ready
        # 何も送らない → 0.2s 後に server が silent-dead とみなし close する。
        with pytest.raises(_WSD):
            ws.receive_text()
    # close 後の finally で台帳から除去されている。
    conn = spy.registered[0][2]
    assert (pid, "sess-C", conn) in spy.unregistered
