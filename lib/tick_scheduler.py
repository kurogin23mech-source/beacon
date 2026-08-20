"""Periodic tick scheduler — target-agnostic cadence decision.

ms-107 e-3434 / e-3461. The periodic-activation *function* is decoupled from any
one entity. Beacon's targets come in two flavours per instance — finite (価値
創造: dev Milestone / sales Opportunity) and **persistent (運用: dev Operation /
sales Account)**. A persistent target recurs, so it needs periodic activation;
but the tick is a shared function, not owned by Operation. Anything that exposes
a *schedule descriptor* rides this same primitive:

  * a persistent target (Operation, or an Account's 定期連絡 job), and
  * a short-lived task (e.g. the reply-watcher's per-thread watch, E)

Operation is merely the first consumer, not the owner.

A **schedule descriptor** is a plain, self-contained dict — no entity coupling:

    {
      "enabled":        bool,        # is this thing active / should it fire at all
      "cadence_minutes": int | None, # how often (None → default)
      "last_fired_at":   str | None, # ISO8601 of the previous fire ("" / None = never)
    }

The entity→descriptor mapping (e.g. "an Operation is enabled when status==open
and meta.server_tick") lives in the caller's adapter, keeping this module pure
and reusable. Mirrors ``lib/trek_scheduler.py`` so cadence math is unit-tested
without an HTTP server.
"""

from __future__ import annotations

import datetime
from typing import Optional

DEFAULT_CADENCE_MINUTES = 60  # hourly default


def _parse_dt(value) -> Optional[datetime.datetime]:
    """Parse an ISO8601 timestamp (offset or trailing Z) to an aware UTC
    datetime, or None when empty / unparseable."""
    if not value or not str(value).strip():
        return None
    txt = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


# --- 失敗時の後退 (2026-08-20 本番停止の再発防止) ------------------------------
#
# 発火に失敗した対象は last_fired_at が進まないため「期限到来」のまま残り、
# 次の tick でまた同じ失敗をする。実際に trek_id を持たない Trek 3 件が 44 日間
# (= 6 万回以上) 毎分失敗し続け、そのたびにバスへ通知を書いた結果、bus は
# 98,943 件 / 72MB まで肥大し、それを毎秒読み直す経路と噛み合って本番が停止した。
#
# 対処は「失敗も記録して、失敗するほど間隔を広げる」。一時的な障害 (ネットが一瞬
# 切れた等) は放っておけば復帰し、壊れているものは自然に静かになる。上限を置くの
# は、いつまでも間隔が伸びて事実上の永久停止になるのを避けるため。
FAILURE_BACKOFF_CAP_MINUTES = 60


def _backoff_minutes(failures: int) -> int:
    """連続失敗 n 回に対する待ち時間 (分)。1, 2, 4, 8 ... 上限 60。"""
    if failures <= 0:
        return 0
    return min(2 ** (failures - 1), FAILURE_BACKOFF_CAP_MINUTES)


def failure_backoff_active(meta: dict, now, *, key: str = "fire") -> bool:
    """直前の失敗から、まだ待ち時間が明けていないなら True (= 今回は発火しない)。

    ``meta`` は対象の meta dict。``key`` を分けることで、同じ対象でも用途ごとに
    (進捗チェック / エスカレーション / Operation 実行) 独立した回数を持てる。
    記録が無い・壊れているときは False (= 従来どおり発火) に倒し、この仕組みが
    原因で発火が止まることを避ける。
    """
    meta = meta or {}
    failures = 0
    try:
        failures = int(meta.get(f"{key}_failures") or 0)
    except (TypeError, ValueError):
        return False
    if failures <= 0:
        return False
    last = _parse_dt(meta.get(f"{key}_last_failed_at"))
    if last is None:
        return False
    now_dt = now if isinstance(now, datetime.datetime) else _parse_dt(now)
    if now_dt is None:
        return False
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
    wait = datetime.timedelta(minutes=_backoff_minutes(failures))
    return now_dt < last + wait


def record_failure(meta: dict, now_iso: str, *, key: str = "fire") -> int:
    """失敗を 1 回数え、次に待つ起点の時刻を刻む。連続失敗回数を返す。"""
    try:
        failures = int(meta.get(f"{key}_failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    failures += 1
    meta[f"{key}_failures"] = failures
    meta[f"{key}_last_failed_at"] = now_iso
    return failures


def clear_failure(meta: dict, *, key: str = "fire") -> None:
    """発火に成功したら失敗の記録を消す (次の失敗は 1 回目から数え直す)。"""
    meta.pop(f"{key}_failures", None)
    meta.pop(f"{key}_last_failed_at", None)


def cadence_minutes(descriptor: dict,
                    default: int = DEFAULT_CADENCE_MINUTES) -> int:
    """The descriptor's effective cadence in minutes (non-positive / unparseable
    falls back to ``default``)."""
    raw = (descriptor or {}).get("cadence_minutes")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def is_due(descriptor: dict, now,
           default_cadence: int = DEFAULT_CADENCE_MINUTES) -> bool:
    """Whether a schedule descriptor should fire on the current tick.

    Due when ALL hold:
      * ``enabled`` is truthy,
      * never fired (no ``last_fired_at``) OR its cadence has elapsed.

    ``now`` may be an aware datetime or an ISO8601 string. Minute-resolution
    comparison (like the Trek scheduler)."""
    descriptor = descriptor or {}
    if not descriptor.get("enabled"):
        return False
    now_dt = now if isinstance(now, datetime.datetime) else _parse_dt(now)
    if now_dt is None:
        return False
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
    last = _parse_dt(descriptor.get("last_fired_at"))
    if last is None:
        return True  # never fired → due
    elapsed = now_dt - last
    return elapsed >= datetime.timedelta(minutes=cadence_minutes(descriptor, default_cadence))


def select_due(descriptors: list, now,
               default_cadence: int = DEFAULT_CADENCE_MINUTES) -> list:
    """Filter a list of schedule descriptors to those due to fire this tick."""
    return [d for d in (descriptors or []) if is_due(d, now, default_cadence)]


def truthy(value) -> bool:
    """Shared truthiness for descriptor flags read from stored data (bool or the
    string forms ``"1"`` / ``"true"`` / ``"yes"``). Adapters use it to compute
    ``enabled`` from an entity's fields."""
    return value is True or (isinstance(value, str)
                             and value.strip().lower() in ("1", "true", "yes"))
