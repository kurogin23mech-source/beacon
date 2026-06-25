"""Regression tests for the explicit WS broadcast after write (ms-43 / e-2128).

Background — dogfood (2026-06-19) で観測された病理: cloud mode で
``beacon milestone add`` を打っても WebUI に live 反映されない。 元の設計は
PUT /api/projects + apply_operation の broadcast を Firestore の
``on_snapshot`` listener に **完全に委ねていた**。 listener は silent
disconnect / multi-instance watcher 偏り / identical-content dedup で
fire しなくなる経路があり、 そうなると WebUI 側は **dark** のままになる。

構造解 (ms-43 / e-2128): broadcast を listener に依存させず、 write 経路の
HTTP endpoint で必ず ``_apply_op_and_broadcast`` を呼んで explicit に流す。

これらのテストは:
  * Firestore on_snapshot を mock している環境で (= listener が物理的に発火
    しない) 、 WS frame が write 後に届くことを assert する。 これは旧設計
    では再現しなかった bug を構造的に locked-in する経路。
  * 各 write 経路 (= PUT /api/projects / milestone create / task done / 等)
    が broadcast を triggers することを 1 つずつ確認する。

mock 戦略は test_ws_auth_close_codes.py と同型: firestore_client.get_project /
save_project を in-memory dict にすり替えるだけ。
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Mock backend to avoid the Firestore transactional path (which expects a
# real client). ``operations.apply_operation`` で BEACON_OPERATIONS_BACKEND=mock
# が選ばれると `_apply_mock` 経路に流れて in-memory dict を使う。
os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client
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
    # store_router re-exports get_project / save_project at import time, so
    # patching firestore_client alone is insufficient — operations.py reads
    # from store_router. Patch both for full coverage.
    import store_router  # noqa: F401  (= ensure module loaded)

    prev_fc_get = firestore_client.get_project
    prev_fc_save = firestore_client.save_project
    prev_sr_get = store_router.get_project
    prev_sr_save = store_router.save_project
    firestore_client.get_project = _mock_get_project
    firestore_client.save_project = _mock_save_project
    store_router.get_project = _mock_get_project
    store_router.save_project = _mock_save_project
    _store.clear()
    yield
    firestore_client.get_project = prev_fc_get
    firestore_client.save_project = prev_fc_save
    store_router.get_project = prev_sr_get
    store_router.save_project = prev_sr_save
    _store.clear()


@pytest.fixture(autouse=True)
def _disable_auth():
    """Auth が不要な test path (= ms-43 broadcast の本質は auth に依存しない)。"""
    prev = app_module._auth_enabled
    app_module._auth_enabled = False
    yield
    app_module._auth_enabled = prev


@pytest.fixture(autouse=True)
def _stub_firestore_watcher():
    """ms-43 broadcast の test は listener 非起動が前提 (= 旧 bug の再現条件)。

    ``_start_watcher`` / ``_stop_watcher`` は本物の Firestore client を握って
    on_snapshot を張ろうとする。 test 環境では実 Firestore に到達できないので
    watcher 起動を no-op にすり替える。 explicit broadcast helper はこの watcher
    に依存しないので、 noop でも assertion は通る (= それこそが構造解の証跡)。
    """
    prev_start = app_module._start_watcher
    prev_stop = app_module._stop_watcher
    app_module._start_watcher = lambda pid: None
    app_module._stop_watcher = lambda pid: None
    yield
    app_module._start_watcher = prev_start
    app_module._stop_watcher = prev_stop


# Lifespan context — TestClient ``with`` enables FastAPI startup events so
# ``_capture_event_loop`` runs and ``_event_loop`` gets bound. Without this
# wrapper, ``_broadcast_project_after_write`` would silently no-op (= the
# test itself wouldn't have proved the helper triggers).
client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _lifespan_context():
    """Force startup events so ``_event_loop`` gets bound (= broadcast helper
    can actually fire). Without ``with`` TestClient skips lifespan and the
    broadcast becomes a no-op, masking the very contract this test verifies.
    """
    with client:
        yield


def _seed_project(pid: str, milestones: list[dict] | None = None) -> dict:
    p = {
        "name": "test project",
        "summary": "",
        "owner": "",
        "members": [],
        "milestones": milestones or [],
        "operations": [],
        "schema_version": 1,  # v1 path keeps the write logic simple
    }
    _store[pid] = copy.deepcopy(p)
    return p


def _ws_url(pid: str) -> str:
    return f"/ws/projects/{pid}"


# ---------------------------------------------------------------------------
# Test 1: PUT /api/projects — whole-doc replace
# ---------------------------------------------------------------------------

def test_put_project_broadcasts_to_subscribed_ws_client():
    """ms-43 / e-2128: PUT /api/projects で WS frame が確実に届く。

    旧設計では Firestore on_snapshot listener が fire するのを期待していたが
    mock 環境 (= listener 非起動) では fire しなかった。 新設計の explicit
    broadcast 経路が動いていれば、 mock でも frame が届く。"""
    pid = "p-put-1"
    _seed_project(pid, milestones=[])
    with client.websocket_connect(_ws_url(pid)) as ws:
        # 接続直後の initial frame は ws_ready のシグナル (e-2326 で signal-only 化)
        initial = ws.receive_json()
        assert initial["type"] == "ws_ready"
        # PUT で milestones を 1 件入れた状態に置換
        new_body = {
            "name": "test project",
            "summary": "",
            "milestones": [
                {"id": "ms-1", "title": "First MS", "status": "todo",
                 "progress": 0, "target_date": "", "entries": []},
            ],
            "operations": [],
            "schema_version": 1,
        }
        resp = client.put(f"/api/projects/{pid}", json=new_body)
        assert resp.status_code == 200, resp.text
        # 直後の WS frame は project_changed のシグナルのみ (body なし、e-2326)
        update = ws.receive_json()
        assert update["type"] == "project_changed"
        # 実データは signal を受けてから REST で再 fetch する設計
        got = client.get(f"/api/projects/{pid}")
        assert got.status_code == 200
        ms_titles = [m["title"] for m in (got.json().get("milestones") or [])]
        assert "First MS" in ms_titles, (
            f"expected REST GET after project_changed signal to carry new milestone; "
            f"got titles={ms_titles!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: POST /api/projects/{id}/milestones — granular create
# ---------------------------------------------------------------------------

def test_post_milestones_broadcasts_to_subscribed_ws_client():
    """ms-43 / e-2128: granular milestone create でも broadcast が走る。

    apply_operation 経由の path がすべて _apply_op_and_broadcast 化されている
    ことを assert する。"""
    pid = "p-post-1"
    _seed_project(pid, milestones=[])
    with client.websocket_connect(_ws_url(pid)) as ws:
        ws.receive_json()  # initial (= ws_ready)
        resp = client.post(
            f"/api/projects/{pid}/milestones",
            json={"title": "Granular MS", "target_date": ""},
        )
        assert resp.status_code == 200, resp.text
        update = ws.receive_json()
        assert update["type"] == "project_changed"
        # signal-only protocol (e-2326): refetch via REST
        got = client.get(f"/api/projects/{pid}")
        assert got.status_code == 200
        ms_titles = [m["title"] for m in (got.json().get("milestones") or [])]
        assert "Granular MS" in ms_titles


# ---------------------------------------------------------------------------
# Test 3: PATCH /api/projects/{id}/milestones/{ms} — update
# ---------------------------------------------------------------------------

def test_patch_milestone_broadcasts_progress_change():
    """ms-43 / e-2128: milestone update (= progress 変更) でも frame が届く。"""
    pid = "p-patch-1"
    _seed_project(pid, milestones=[
        {"id": "ms-1", "title": "Existing", "status": "in_progress",
         "progress": 30, "target_date": "", "entries": []},
    ])
    with client.websocket_connect(_ws_url(pid)) as ws:
        ws.receive_json()  # initial (= ws_ready)
        resp = client.patch(
            f"/api/projects/{pid}/milestones/ms-1",
            json={"progress": "80"},  # MilestoneUpdate expects str
        )
        assert resp.status_code == 200, resp.text
        update = ws.receive_json()
        assert update["type"] == "project_changed"
        # signal-only protocol (e-2326): refetch via REST
        got = client.get(f"/api/projects/{pid}")
        assert got.status_code == 200
        ms = next((m for m in got.json()["milestones"] if m["id"] == "ms-1"), None)
        assert ms is not None
        # progress is normalized to int in core.milestone_update
        assert int(ms.get("progress", 0)) == 80


# ---------------------------------------------------------------------------
# Test 4: Multiple subscribers all receive the broadcast
# ---------------------------------------------------------------------------

def test_broadcast_reaches_all_subscribers():
    """ms-43 / e-2128: 同 project に 2 WS が繋がっている時、 両方が更新を受ける。"""
    pid = "p-multi-1"
    _seed_project(pid, milestones=[])
    with client.websocket_connect(_ws_url(pid)) as ws1:
        with client.websocket_connect(_ws_url(pid)) as ws2:
            ws1.receive_json()  # ws_ready
            ws2.receive_json()  # ws_ready
            resp = client.post(
                f"/api/projects/{pid}/milestones",
                json={"title": "Multi-Sub MS", "target_date": ""},
            )
            assert resp.status_code == 200, resp.text
            u1 = ws1.receive_json()
            u2 = ws2.receive_json()
            # signal-only protocol (e-2326): both subscribers get project_changed
            for u in (u1, u2):
                assert u["type"] == "project_changed"
            # refetch once via REST and confirm data landed
            got = client.get(f"/api/projects/{pid}")
            assert got.status_code == 200
            titles = [m["title"] for m in (got.json().get("milestones") or [])]
            assert "Multi-Sub MS" in titles


# ---------------------------------------------------------------------------
# Test 5: write without subscribers is silent + non-fatal
# ---------------------------------------------------------------------------

def test_write_without_subscribers_is_silent_and_succeeds():
    """ms-43 / e-2128: WS subscriber がいない project への write は
    broadcast 経路を no-op で通り、 write 自体は通常通り成功する。"""
    pid = "p-nosub-1"
    _seed_project(pid, milestones=[])
    # No WS connect.
    resp = client.put(
        f"/api/projects/{pid}",
        json={"name": "x", "milestones": [], "operations": [], "schema_version": 1},
    )
    assert resp.status_code == 200
    # Verify the write actually landed.
    got = client.get(f"/api/projects/{pid}")
    assert got.status_code == 200
    assert got.json().get("name") == "x"
