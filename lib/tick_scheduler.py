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
