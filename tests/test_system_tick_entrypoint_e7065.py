"""定期発火の入口が Trek 非依存になり、旧入口が転送として生きていることを実機で測る
(ms-185 e-7065)。

**なぜこのテストが要るか**: 本番の自律 (Operation の発火 / 締切リマインド) は、Trek の
集約と同じ 1 つの入口 ``POST /api/system/trek-scheduler/tick`` で駆動されていた
(コード側のコメント自身が「trek tick 相乗り」と書いていた)。Trek を畳む (ms-185) 際に
この入口を一緒に消すと、**本番で Operation の自律発火と締切リマインドが無音で止まる**。
ms-145 / e-4104 で一度踏んだ型 (= 定期発火の駆動役が黙って消え、事故で気付いた) を
繰り返さないため、入口の住所を Trek から分離した。

このファイルが固定するのは 3 点:
  1. 新しい入口 ``POST /api/system/tick`` が動く (= 正本)。
  2. 旧入口 ``POST /api/system/trek-scheduler/tick`` が新入口へ転送する
     (= 外部の駆動役 ``scripts/vps-tick.sh`` の設定更新を待つ間も止まらない)。
  3. どちらの入口も内部鍵を要求する (= 転送で認証が抜け落ちていない)。

構造 (``_run_trek_tick`` と ``_run_system_tick`` の分離、``now_iso`` の無条件束縛) は
``test_tick_now_iso_binding.py`` が構文木で固定する。こちらは HTTP の配線を見る。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"
os.environ["BEACON_SCHEDULER_INTERNAL_KEY"] = "test-scheduler-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

NEW_PATH = "/api/system/tick"
OLD_PATH = "/api/system/trek-scheduler/tick"
HEADERS_OK = {"X-Beacon-Scheduler-Key": "test-scheduler-key"}

# 応答に必ず載るキー (= 観測側 / 本番の journal がこれを読む)。
# ``scheduled_fires`` が「ターゲット非依存の発火」の報告で、ms-185 で Trek を
# 畳んだあとも残らなければならない部分。
REQUIRED_KEYS = {
    "now", "candidates", "due", "fired", "escalations",
    "leader_review_stall_escalations", "auto_stalled", "errors",
    "quiesced", "halted", "skipped_no_id", "scheduled_fires",
}


@pytest.fixture(autouse=True)
def _stub_backend():
    """保存層を差し替えてネットワークに出ないようにする (= ハーメチックにする)。

    Trek は 0 件 / プロジェクトも 0 件なので、tick は「見るものが無い」状態を
    1 周する。このファイルが見たいのは**入口の配線**であって Trek の集約や
    Operation の発火の中身ではないので、空で十分 (中身は
    test_trek_scheduler_api.py / test_operations.py が見る)。
    """
    db = app_module.db
    stubs = {
        "list_treks": lambda actor_id=None, **kw: [],
        "list_all_projects": lambda *a, **kw: [],
        "get_project": lambda *a, **kw: None,
        "save_project": lambda *a, **kw: None,
        "list_sessions": lambda *a, **kw: [],
        "append_bus_event": lambda *a, **kw: "ev-stub",
    }
    saved = {}
    missing = object()
    for name, fn in stubs.items():
        saved[name] = getattr(db, name, missing)
        setattr(db, name, fn)
    try:
        yield
    finally:
        for name, val in saved.items():
            if val is missing:
                delattr(db, name)
            else:
                setattr(db, name, val)


def _client() -> TestClient:
    return TestClient(app_module.app)


def test_new_trek_neutral_entrypoint_runs_a_tick():
    """正本の入口が 200 を返し、ターゲット非依存の発火の報告を含む。"""
    resp = _client().post(NEW_PATH, json={}, headers=HEADERS_OK)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    missing = REQUIRED_KEYS - set(body)
    assert not missing, (
        "定期発火の応答から " + ", ".join(sorted(missing)) + " が欠けている"
    )
    assert isinstance(body["scheduled_fires"], list), (
        "scheduled_fires は Operation / 締切リマインドの発火報告なので list。"
        "ここが欠けると本番の自律が駆動されたか観測できない。"
    )


def test_old_trek_named_entrypoint_still_works_as_a_forward():
    """旧入口も 200 を返す。駆動役の設定更新を待つ間の転送なので落としてはいけない。"""
    resp = _client().post(OLD_PATH, json={}, headers=HEADERS_OK)
    assert resp.status_code == 200, resp.text
    assert REQUIRED_KEYS <= set(resp.json())


def test_both_entrypoints_return_the_same_shape():
    """転送なので応答の形が同じ (= 旧入口を叩き続けている観測側が壊れない)。"""
    client = _client()
    new_body = client.post(NEW_PATH, json={}, headers=HEADERS_OK).json()
    old_body = client.post(OLD_PATH, json={}, headers=HEADERS_OK).json()
    # ``now`` は tick ごとに進むので、キーの集合だけを比べる。
    assert set(new_body) == set(old_body)


def test_new_entrypoint_requires_the_internal_key():
    """鍵なし / 誤りで 403。新入口を足したときに認証を忘れていないことを見る。"""
    client = _client()
    assert client.post(NEW_PATH, json={}).status_code == 403
    assert client.post(
        NEW_PATH, json={}, headers={"X-Beacon-Scheduler-Key": "wrong"},
    ).status_code == 403


def test_old_entrypoint_still_requires_the_internal_key():
    """転送でも認証が抜け落ちていない (= 旧入口が無認証の裏口にならない)。"""
    client = _client()
    assert client.post(OLD_PATH, json={}).status_code == 403
    assert client.post(
        OLD_PATH, json={}, headers={"X-Beacon-Scheduler-Key": "wrong"},
    ).status_code == 403


def test_tick_health_sees_a_tick_from_the_new_entrypoint():
    """新入口経由の発火が健全性監視に映る。

    ``GET /api/system/tick-health`` は外部の見張り
    (``scripts/tick-health-monitor.py``) が「駆動役が死んだ」を box の外から
    気付くための面。入口を移したときにここへの記録が切れると、
    止まっていることに気付けなくなる (= 移転で一番怖い失敗)。
    """
    client = _client()
    client.post(NEW_PATH, json={}, headers=HEADERS_OK)
    health = client.get("/api/system/tick-health")
    assert health.status_code == 200, health.text
    body = health.json()
    assert body.get("status") != "never", (
        "新入口で発火したのに tick-health が never のまま = 記録が切れている。"
        "外部の見張りが「駆動役が死んだ」を検知できなくなる (e-1391 / ms-66)。"
    )
    assert isinstance(body.get("last_tick_report"), dict)
    assert "scheduled_fires" in body["last_tick_report"], (
        "last_tick_report に scheduled_fires が無い = ターゲット非依存の発火が"
        "何件走ったかを外から読めない"
    )
