"""ms-101 / e-3011: 新着 DM をサーバから WS で直接 push (cross-process fanout)。

ここでテストするのは _fanout_bus_event (= 全プロセスへの中継) と
_run_ws_push_subscriber (= pub/sub を受けてローカル配信する subscriber) の分岐:

  - Redis 利用可: publish_ws_push で全プロセスへ中継し、二重配信を避けるため
    発行元プロセスでは直接ローカル配信しない
  - Redis 不通: publish が False を返し、同プロセスのローカル配信に fallback
  - subscriber: pub/sub message を受けて該当 project のローカル接続へ配信を乗せる
  - subscriber: Redis 不通 (subscription None) では静かに return する

実 Redis は使わず、redis_client のヘルパーと _deliver_bus_signal_local を
スタブに差し替えて分岐だけを検証する。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import app as app_module  # noqa: E402


# --- _fanout_bus_event: 常にローカル配信 + cross-process publish -------------

def test_fanout_always_delivers_local_and_publishes(monkeypatch):
    """review fix B: Redis UP でもローカル配信は必ず行う (subscriber スレッドの
    生死に依存させない)。加えて cross-process 用に publish もする。"""
    pub, delivered = [], []
    monkeypatch.setattr(
        app_module.redis_client, "publish_ws_push",
        lambda pid, eid, **kw: pub.append((pid, eid)) or True,
    )

    async def _fake_local(pid, eid):
        delivered.append((pid, eid))

    monkeypatch.setattr(app_module, "_deliver_bus_signal_local", _fake_local)
    asyncio.run(app_module._fanout_bus_event("p1", {"event_id": "e1"}))
    assert delivered == [("p1", "e1")]  # ローカル配信は常に効く
    assert pub == [("p1", "e1")]        # cross-process 中継も行う


def test_fanout_local_delivery_survives_redis_down(monkeypatch):
    """Redis 不通 (publish が False / no-op) でもローカル配信は保証される。"""
    delivered = []
    monkeypatch.setattr(
        app_module.redis_client, "publish_ws_push", lambda pid, eid, **kw: False
    )

    async def _fake_local(pid, eid):
        delivered.append((pid, eid))

    monkeypatch.setattr(app_module, "_deliver_bus_signal_local", _fake_local)
    asyncio.run(app_module._fanout_bus_event("p1", {"event_id": "e2"}))
    assert delivered == [("p1", "e2")]


# --- subscriber: 1 メッセージのルーティング (_dispatch_ws_push_message) -------
# resilient loop 本体は `while True` の無限ループなので、per-message の判断だけを
# 切り出した _dispatch_ws_push_message を直接検証する。

def test_dispatch_delivers_message_to_local_clients(monkeypatch):
    monkeypatch.setattr(app_module, "_event_loop", object())
    monkeypatch.setattr(app_module, "_ws_connections", {"p1": {object()}})
    scheduled = []

    def _fake_schedule(coro, loop):
        scheduled.append(coro)

    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", _fake_schedule)

    msg = {"type": "message",
           "data": json.dumps({"project_id": "p1", "event_id": "e9"})}
    assert app_module._dispatch_ws_push_message(msg) is True
    assert len(scheduled) == 1
    scheduled[0].close()  # 未 await の coroutine を閉じて警告回避


def test_dispatch_skips_when_no_local_connection(monkeypatch):
    monkeypatch.setattr(app_module, "_event_loop", object())
    monkeypatch.setattr(app_module, "_ws_connections", {"p1": {object()}})
    scheduled = []
    monkeypatch.setattr(
        app_module.asyncio, "run_coroutine_threadsafe",
        lambda coro, loop: scheduled.append(coro),
    )
    msg = {"type": "message",
           "data": json.dumps({"project_id": "p-other", "event_id": "e9"})}
    # p-other にローカル接続が無いので配信は乗らない。
    assert app_module._dispatch_ws_push_message(msg) is False
    assert scheduled == []


def test_dispatch_ignores_non_message_and_bad_json(monkeypatch):
    monkeypatch.setattr(app_module, "_event_loop", object())
    monkeypatch.setattr(app_module, "_ws_connections", {"p1": {object()}})
    monkeypatch.setattr(
        app_module.asyncio, "run_coroutine_threadsafe",
        lambda coro, loop: None,
    )
    # subscribe 確認メッセージは無視。
    assert app_module._dispatch_ws_push_message({"type": "subscribe"}) is False
    # 壊れた JSON も握りつぶす (= throw しない)。
    assert app_module._dispatch_ws_push_message(
        {"type": "message", "data": "{not json"}) is False


def test_subscriber_returns_quietly_when_redis_py_absent(monkeypatch):
    # redis-py 未インストール環境では subscriber は retry せず即 return する
    # (= 無限ループに入らない、fail-open)。
    monkeypatch.setattr(app_module.redis_client, "_redis", None)
    app_module._run_ws_push_subscriber()
