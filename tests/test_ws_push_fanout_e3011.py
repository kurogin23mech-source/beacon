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


# --- _fanout_bus_event: publish vs fallback --------------------------------

def test_fanout_publishes_when_redis_up(monkeypatch):
    pub, delivered = [], []
    monkeypatch.setattr(
        app_module.redis_client, "publish_ws_push",
        lambda pid, eid, **kw: pub.append((pid, eid)) or True,
    )

    async def _fake_local(pid, eid):
        delivered.append((pid, eid))

    monkeypatch.setattr(app_module, "_deliver_bus_signal_local", _fake_local)
    asyncio.run(app_module._fanout_bus_event("p1", {"event_id": "e1"}))
    # 中継は publish 経由。発行元プロセスは直接ローカル配信しない (二重配信回避)。
    assert pub == [("p1", "e1")]
    assert delivered == []


def test_fanout_falls_back_to_local_when_redis_down(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        app_module.redis_client, "publish_ws_push", lambda pid, eid, **kw: False
    )

    async def _fake_local(pid, eid):
        delivered.append((pid, eid))

    monkeypatch.setattr(app_module, "_deliver_bus_signal_local", _fake_local)
    asyncio.run(app_module._fanout_bus_event("p1", {"event_id": "e2"}))
    # Redis 不通 → 同プロセスのローカル配信に fallback (e-2380 挙動を保つ)。
    assert delivered == [("p1", "e2")]


# --- subscriber: pub/sub message → ローカル配信 -----------------------------

def test_subscriber_delivers_message_to_local_clients(monkeypatch):
    class _FakePubSub:
        def listen(self):
            yield {"type": "subscribe"}  # 購読確認メッセージ (無視される)
            yield {
                "type": "message",
                "data": json.dumps({"project_id": "p1", "event_id": "e9"}),
            }
            # generator が尽きると _run_ws_push_subscriber は return する

    monkeypatch.setattr(
        app_module.redis_client, "ws_push_subscription", lambda: _FakePubSub()
    )
    # 配信が発火する条件: event loop 設定済 + 該当 project にローカル接続あり。
    monkeypatch.setattr(app_module, "_event_loop", object())
    monkeypatch.setattr(app_module, "_ws_connections", {"p1": {object()}})

    scheduled = []

    def _fake_schedule(coro, loop):
        scheduled.append(coro)

    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", _fake_schedule)

    app_module._run_ws_push_subscriber()

    # message 1 通につき 1 回、ローカル配信コルーチンが event loop に乗る。
    assert len(scheduled) == 1
    scheduled[0].close()  # 未 await の coroutine を閉じて警告回避


def test_subscriber_skips_when_no_local_connection(monkeypatch):
    class _FakePubSub:
        def listen(self):
            yield {
                "type": "message",
                "data": json.dumps({"project_id": "p-other", "event_id": "e9"}),
            }

    monkeypatch.setattr(
        app_module.redis_client, "ws_push_subscription", lambda: _FakePubSub()
    )
    monkeypatch.setattr(app_module, "_event_loop", object())
    monkeypatch.setattr(app_module, "_ws_connections", {"p1": {object()}})
    scheduled = []
    monkeypatch.setattr(
        app_module.asyncio, "run_coroutine_threadsafe",
        lambda coro, loop: scheduled.append(coro),
    )
    app_module._run_ws_push_subscriber()
    # p-other にローカル接続が無いので配信は乗らない。
    assert scheduled == []


def test_subscriber_returns_quietly_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(
        app_module.redis_client, "ws_push_subscription", lambda: None
    )
    # 例外を投げずに即 return する (= fail-open、process は poll-only で継続)。
    app_module._run_ws_push_subscriber()
