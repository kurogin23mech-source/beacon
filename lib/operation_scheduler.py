"""Operation scheduler — pure cadence decision for server-fired Operations.

ms-107 e-3434 (chunk 3, "trek tick 相乗り"). The existing Trek scheduler tick
(``POST /api/system/trek-scheduler/tick``, driven by Cloud Scheduler at
minute resolution) already runs on a cadence. Rather than stand up a second
Cloud Scheduler job, that same tick also fires **due Operations** — e.g. the
sales meeting-end detector — using this module's pure decision logic.

Design mirrors ``lib/trek_scheduler.py`` so the cadence math is unit-tested
without an HTTP server, and the server endpoint stays a thin wiring layer.

Structural guard (why an opt-in flag): the server tick must NOT start firing
every existing Operation (most fire client-side, daily, via
``beacon trigger tick``). Only Operations that explicitly opt into server-tick
firing — ``meta.server_tick == true`` — are considered here. This keeps the new
autonomous path scoped to Operations designed for it.
"""

from __future__ import annotations

import datetime
from typing import Optional

DEFAULT_OPERATION_CADENCE_MINUTES = 60  # hourly default (meeting detection)

# Operation statuses that are allowed to fire on the server tick.
_FIREABLE_STATUS = "open"


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


def is_server_fired(op: dict) -> bool:
    """Whether an Operation opts into server-tick firing (meta.server_tick).
    Everything else keeps firing client-side via ``beacon trigger tick``."""
    meta = op.get("meta") or {}
    val = meta.get("server_tick")
    return val is True or (isinstance(val, str) and val.strip().lower() in ("1", "true", "yes"))


def get_cadence_minutes(op: dict,
                        default: int = DEFAULT_OPERATION_CADENCE_MINUTES) -> int:
    """The Operation's effective cadence in minutes (meta.cadence_minutes
    override, else default). Non-positive / unparseable falls back to default."""
    meta = op.get("meta") or {}
    raw = meta.get("cadence_minutes")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def is_operation_due(op: dict, now,
                     default_cadence: int = DEFAULT_OPERATION_CADENCE_MINUTES) -> bool:
    """Decide whether this Operation should fire on the current tick.

    Due when ALL hold:
      * status == "open"
      * opts into server-tick firing (meta.server_tick truthy)
      * never fired (no meta.last_fired_at) OR cadence_minutes has elapsed since
        the last fire.

    ``now`` may be an aware datetime or an ISO8601 string. Minute-resolution
    comparison (like the Trek scheduler) so a 60-minute cadence fires ~hourly."""
    if op.get("status") != _FIREABLE_STATUS:
        return False
    if not is_server_fired(op):
        return False
    now_dt = now if isinstance(now, datetime.datetime) else _parse_dt(now)
    if now_dt is None:
        return False
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=datetime.timezone.utc)
    meta = op.get("meta") or {}
    last = _parse_dt(meta.get("last_fired_at"))
    if last is None:
        return True  # never fired → due
    cadence = get_cadence_minutes(op, default=default_cadence)
    elapsed = now_dt - last
    return elapsed >= datetime.timedelta(minutes=cadence)


def select_due_operations(ops: list, now,
                          default_cadence: int = DEFAULT_OPERATION_CADENCE_MINUTES) -> list:
    """Filter a list of Operation dicts to those due to fire this tick."""
    return [op for op in (ops or []) if is_operation_due(op, now, default_cadence)]
