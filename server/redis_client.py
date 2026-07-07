"""Redis helper for the Beacon API (= rate limiting 等の揮発カウンタ用、ms-96)。

現状の用途は e-2381 のレート制限 (= 固定時間窓カウンタ)。接続情報は VPS の
/etc/beacon/db.env が配る `REDIS_*` を読む (上書き用 `BEACON_REDIS_*` も両対応、
mysql_client と同じ流儀)。

設計原則:
  - **fail-open**: Redis が不通・未設定・redis-py 未インストールのとき、カウンタ
    helper は None を返す。呼び出し側 (rate limit middleware) は None を
    「制限判定できない → 通す」と解釈する。可用性を制限より優先する
    (= 「制限が効かない」より「app が落ちる」方が悪い、e-2381 SPEC 設計方針)。
  - **lazy 接続**: import 時ではなく初回利用時に接続する。BEACON 経路が Redis を
    使わない環境 (= 従来の Cloud Run / local) に import コストをかけない。
"""
from __future__ import annotations

import json
import os
import time

# redis-py は VPS 経路でのみ使う。未インストール環境では import 失敗を握りつぶし、
# helper が fail-open (None) で振る舞う (= pymysql と同じ guard 方式)。
try:
    import redis as _redis
except ImportError:
    _redis = None  # type: ignore[assignment]


def _env(*names: str, default: str | None = None) -> str | None:
    """最初に見つかった env var を返す (= 別名フォールバック)。

    VPS の db.env は `REDIS_*` 名で配る。ローカル / 他環境では `BEACON_REDIS_*`
    で上書きしたいので両名を許容する (`BEACON_REDIS_*` 優先)。
    """
    for n in names:
        v = os.environ.get(n)
        if v is not None and v != "":
            return v
    return default


_CLIENT = None
_CLIENT_FAILED = False  # 一度接続に失敗したら以降 fail-open で即 None (毎回接続試行しない)


def _client():
    """Return a live Redis client, or None if unavailable (fail-open)."""
    global _CLIENT, _CLIENT_FAILED
    if _redis is None or _CLIENT_FAILED:
        return None
    if _CLIENT is not None:
        return _CLIENT
    try:
        _CLIENT = _redis.Redis(
            host=_env("BEACON_REDIS_HOST", "REDIS_HOST", default="127.0.0.1"),
            port=int(_env("BEACON_REDIS_PORT", "REDIS_PORT", default="6379")),
            password=_env("BEACON_REDIS_PASSWORD", "REDIS_PASSWORD") or None,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        # 初回に一度だけ ping して疎通確認 (失敗したら fail-open に倒す)。
        _CLIENT.ping()
    except Exception:
        _CLIENT = None
        _CLIENT_FAILED = True
        return None
    return _CLIENT


def available() -> bool:
    """Redis が使える状態か (= 監視 / デバッグ用)。"""
    return _client() is not None


def incr_fixed_window(ident: str, window_seconds: int) -> int | None:
    """固定時間窓カウンタを 1 増やし、現在窓のカウントを返す。

    window_seconds ごとの窓に区切り、その窓内で ident が何回目かを返す。
    呼び出し側はこの戻り値を上限と比較する (count > limit なら拒否)。

    Redis が不通なら None を返す (= fail-open、呼び出し側は通す)。
    """
    r = _client()
    if r is None:
        return None
    try:
        window_id = int(time.time() // window_seconds)
        key = f"rl:{ident}:{window_id}"
        count = r.incr(key)
        if count == 1:
            # 窓の寿命 + 1s だけ TTL を張る (= 窓境界の取りこぼし防止)。
            r.expire(key, window_seconds + 1)
        return int(count)
    except Exception:
        # 実行時の transient なエラーも fail-open (制限より可用性)。
        return None


# ---------------------------------------------------------------------------
# WS 接続レジストリ + push pub/sub (ms-101 / e-3008)
# ---------------------------------------------------------------------------
#
# ms-101 は「今つながっている WebSocket 接続の集合」を liveness (= 生存判定) の
# 真値源にする。接続台帳 (= どの session がつながっているか) を Redis に置くと、
# uvicorn を複数プロセス / 複数ワーカーで動かしても全プロセスが同じ live 情報を
# 共有できる。サーバ内の _ws_connections はプロセスローカル (= そのプロセス内
# だけ) なので、別プロセスにつながる相手を not-live と誤判定してしまう。これを
# Redis 共有で根治する (SPEC 設計方針 #2)。
#
# 台帳の構造 — 1 session が複数 WS を張るケースと、プロセス crash による stale
# (= 死んだのに残る) 残留の両方に耐える形にする:
#
#   key   : beacon:ws:conns:{project_id}:{session_id}   (Redis sorted set)
#   member: conn_id (= 接続 1 本ごとに振る uuid)
#   score : その接続の有効期限を表す epoch 秒 (= now + ttl)。keepalive で更新。
#
# liveness = score が現在時刻より未来の member が 1 本以上あるか (= 期限切れは
# 死んだ接続とみなして数えない)。crash したプロセスの member は keepalive で
# refresh されないため score が過去になり、自然と live 判定から外れる。全 member
# が消えた key は EXPIRE で自然消滅する (= 掃除漏れ防止)。
#
# fail-open 契約は counter helper と同じ: Redis が不通なら liveness helper は
# None を返し、呼び出し側は「Redis で判定できない → 従来の poll heartbeat 判定に
# 落ちる」と解釈する (SPEC の poll fallback 方針と整合、可用性優先)。

_WS_TTL_SECONDS = 60      # 接続 liveness の寿命 (秒)。keepalive で更新する (e-3009)
_WS_KEY_GRACE = 10        # key 全体 TTL に足す猶予 (= 最終 member が抜けた後の掃除用)
_WS_PUSH_CHANNEL = "beacon:ws:push"  # 新着 event の wake hint を配る pub/sub チャンネル


def _ws_conns_key(project_id: str, session_id: str) -> str:
    return f"beacon:ws:conns:{project_id}:{session_id}"


def ws_register(
    project_id: str, session_id: str, conn_id: str, *, ttl: int = _WS_TTL_SECONDS
) -> bool:
    """接続を live として台帳に登録する (keepalive による TTL 更新も同じ呼び出し)。

    idempotent: 同じ conn_id を再登録すると score (= 有効期限) を更新するだけ。
    Returns True if recorded, False if Redis unavailable (fail-open)。
    """
    r = _client()
    if r is None:
        return False
    try:
        key = _ws_conns_key(project_id, session_id)
        expiry = time.time() + ttl
        r.zadd(key, {conn_id: expiry})
        # 全 member が ZREM された後の空 key を自然消滅させる保険。生きている
        # 接続がある限り keepalive のたびに TTL が伸びるので消えない。
        r.expire(key, ttl + _WS_KEY_GRACE)
        return True
    except Exception:
        return False


# keepalive (= ping/pong で生存確認できた接続) の TTL 更新は register と同義。
ws_refresh = ws_register


def ws_unregister(project_id: str, session_id: str, conn_id: str) -> bool:
    """接続を台帳から外す (= clean な切断時)。

    Returns True if removed, False if Redis unavailable (fail-open)。
    """
    r = _client()
    if r is None:
        return False
    try:
        r.zrem(_ws_conns_key(project_id, session_id), conn_id)
        return True
    except Exception:
        return False


def ws_session_live(project_id: str, session_id: str) -> bool | None:
    """session が期限内の WS 接続を 1 本以上持つか (= live か) を返す。

    Returns:
      True  — 期限内の接続が 1 本以上ある (live)
      False — 期限内の接続がない (not-live)
      None  — Redis が不通で判定不能 (呼び出し側は従来の poll 判定へ fallback)
    """
    r = _client()
    if r is None:
        return None
    try:
        key = _ws_conns_key(project_id, session_id)
        now = time.time()
        # 純粋な read: 期限切れ member を消さずに、score > now の member だけ数える。
        # crash 由来の期限切れ member はここで除外され、key ごと TTL で消える。
        return r.zcount(key, now, "+inf") > 0
    except Exception:
        return None


def publish_ws_push(project_id: str, event_id: str, *, kind: str = "bus_event") -> bool:
    """新着 event の wake hint (= 起こす合図) を全プロセスへ pub/sub 中継する。

    payload は routing metadata のみ (project_id / event_id / kind)。DM 本文・
    送信者・envelope は載せない (= signal-only。受信側は wake hint を受けて REST
    inbox を引き直す。ms-97 / e-2380 の signal-only 方針と同一)。

    Returns True if published, False if Redis unavailable (fail-open)。
    """
    r = _client()
    if r is None:
        return False
    try:
        r.publish(
            _WS_PUSH_CHANNEL,
            json.dumps({"project_id": project_id, "event_id": event_id, "kind": kind}),
        )
        return True
    except Exception:
        return False


def ws_push_subscription():
    """push 中継チャンネルを購読する pubsub オブジェクトを返す (subscriber loop 用)。

    subscribe モードの接続は他コマンドを打てないため、共有 client とは別に pubsub
    を作る。Returns the pubsub object, or None if Redis unavailable (fail-open)。
    """
    r = _client()
    if r is None:
        return None
    try:
        p = r.pubsub(ignore_subscribe_messages=True)
        p.subscribe(_WS_PUSH_CHANNEL)
        return p
    except Exception:
        return None
