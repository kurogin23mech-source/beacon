"""ms-101 / e-3008: WS 接続レジストリ + push pub/sub の土台テスト。

ここでテストするのは redis_client に足した接続台帳ヘルパー群 (= WS 接続を Redis
の sorted set に登録し、期限内の接続数で liveness を判定する層) と push 中継の
pub/sub プリミティブ。実 Redis は使わず、必要なコマンド (zadd / zrem / zcount /
expire / publish / pubsub) だけを実装した in-memory フェイクを注入して検証する。

Redis 不通時の fail-open 契約 (= liveness helper は None、書き込み系は False を
返し、呼び出し側は従来の poll 判定へ落ちる) も併せて locked-in する。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import redis_client


class _FakeRedis:
    """redis_client のヘルパーが叩くコマンドだけを持つ最小フェイク。

    sorted set は {key: {member: score}} で表現する。score は有効期限 (epoch 秒)。
    """

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []

    def zadd(self, key, mapping):
        z = self.zsets.setdefault(key, {})
        z.update(mapping)

    def zrem(self, key, member):
        z = self.zsets.get(key)
        if z is not None:
            z.pop(member, None)

    def zcount(self, key, min_, max_):
        z = self.zsets.get(key, {})

        def _bound(v, default):
            if v == "+inf":
                return float("inf")
            if v == "-inf":
                return float("-inf")
            return float(v)

        lo = _bound(min_, float("-inf"))
        hi = _bound(max_, float("inf"))
        return sum(1 for score in z.values() if lo <= score <= hi)

    def expire(self, key, ttl):
        self.expires[key] = ttl

    def publish(self, channel, message):
        self.published.append((channel, message))

    def pubsub(self, ignore_subscribe_messages=False):
        return _FakePubSub()


class _FakePubSub:
    def __init__(self):
        self.subscribed: list[str] = []

    def subscribe(self, channel):
        self.subscribed.append(channel)


@pytest.fixture
def fake(monkeypatch):
    fr = _FakeRedis()
    monkeypatch.setattr(redis_client, "_client", lambda: fr)
    return fr


@pytest.fixture
def redis_down(monkeypatch):
    monkeypatch.setattr(redis_client, "_client", lambda: None)


# --- 登録 → live 判定 → 切断 の基本サイクル -------------------------------

def test_register_makes_session_live(fake):
    assert redis_client.ws_register("proj", "sess-A", "conn-1") is True
    assert redis_client.ws_session_live("proj", "sess-A") is True


def test_unregister_makes_session_not_live(fake):
    redis_client.ws_register("proj", "sess-A", "conn-1")
    assert redis_client.ws_unregister("proj", "sess-A", "conn-1") is True
    assert redis_client.ws_session_live("proj", "sess-A") is False


def test_unknown_session_is_not_live(fake):
    assert redis_client.ws_session_live("proj", "never-seen") is False


# --- 複数接続 / 再接続レース: 片方が抜けても残りが live なら live ----------

def test_multiple_connections_one_disconnect_stays_live(fake):
    redis_client.ws_register("proj", "sess-A", "conn-1")
    redis_client.ws_register("proj", "sess-A", "conn-2")
    redis_client.ws_unregister("proj", "sess-A", "conn-1")
    # conn-2 がまだ生きているので live のまま (= 再接続の重なりで誤判定しない)。
    assert redis_client.ws_session_live("proj", "sess-A") is True


# --- 期限切れ (crash したプロセスの残留) は live に数えない ---------------

def test_expired_connection_is_not_live(fake, monkeypatch):
    # ttl=0 で登録 → score が現在時刻ちょうど。少し時間を進めれば期限切れ扱い。
    redis_client.ws_register("proj", "sess-A", "conn-stale", ttl=0)
    import time as _t

    real_time = _t.time()
    monkeypatch.setattr(redis_client.time, "time", lambda: real_time + 5)
    assert redis_client.ws_session_live("proj", "sess-A") is False


def test_refresh_extends_liveness(fake, monkeypatch):
    import time as _t

    base = _t.time()
    monkeypatch.setattr(redis_client.time, "time", lambda: base)
    redis_client.ws_register("proj", "sess-A", "conn-1", ttl=60)
    # 30 秒後: まだ期限内。keepalive で更新すれば更に延びる。
    monkeypatch.setattr(redis_client.time, "time", lambda: base + 30)
    assert redis_client.ws_session_live("proj", "sess-A") is True
    assert redis_client.ws_refresh("proj", "sess-A", "conn-1", ttl=60) is True
    monkeypatch.setattr(redis_client.time, "time", lambda: base + 80)
    # 元の期限 (base+60) は過ぎたが refresh で base+90 まで延びているので live。
    assert redis_client.ws_session_live("proj", "sess-A") is True


def test_key_gets_ttl_bound(fake):
    redis_client.ws_register("proj", "sess-A", "conn-1", ttl=60)
    key = redis_client._ws_conns_key("proj", "sess-A")
    # 空 key の自然消滅用に key 全体へ TTL (ttl + grace) が張られる。
    assert fake.expires[key] == 60 + redis_client._WS_KEY_GRACE


# --- push pub/sub プリミティブ --------------------------------------------

def test_publish_ws_push_emits_signal_only(fake):
    assert redis_client.publish_ws_push("proj", "evt-123") is True
    assert len(fake.published) == 1
    channel, message = fake.published[0]
    assert channel == redis_client._WS_PUSH_CHANNEL
    import json

    payload = json.loads(message)
    # routing metadata のみ。本文 / 送信者 / envelope を載せない。
    assert payload == {"project_id": "proj", "event_id": "evt-123", "kind": "bus_event"}
    assert "payload" not in payload and "sender_session_id" not in payload


def test_push_subscription_subscribes_channel(fake):
    sub = redis_client.ws_push_subscription()
    assert sub is not None
    assert redis_client._WS_PUSH_CHANNEL in sub.subscribed


# --- fail-open: Redis 不通なら書き込み系 False / liveness None -------------

def test_fail_open_when_redis_down(redis_down):
    assert redis_client.ws_register("proj", "sess-A", "conn-1") is False
    assert redis_client.ws_refresh("proj", "sess-A", "conn-1") is False
    assert redis_client.ws_unregister("proj", "sess-A", "conn-1") is False
    assert redis_client.ws_session_live("proj", "sess-A") is None
    assert redis_client.publish_ws_push("proj", "evt-1") is False
    assert redis_client.ws_push_subscription() is None
