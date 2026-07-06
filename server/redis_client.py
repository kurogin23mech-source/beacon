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
