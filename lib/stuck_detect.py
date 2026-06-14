"""STUCK signal detector (ms-55 e-1649).

Idle-timeout based detector for sessions that have stopped making
progress. Per SPEC §7 / e-1649: deadlock can't be auto-resolved (= it
needs interpretation of both sides' intent), but silent-stop *can* be
detected via timeout — if a session hasn't touched the bus or a tool
call in N minutes, it's almost certainly stuck waiting on something
that isn't going to happen.

The detector then escalates by emitting a stop signal with
``reason_kind == "stuck"`` (= the same protocol implemented in
lib/stop_signal.py, by design). The morning briefing (e-1650)
categorizes STUCK signals into the "intervention needed" bucket.

This module is pure logic — it expects the caller to provide:

  * Now (= datetime to compare against; injected for testing).
  * The list of "session telemetry" dicts to inspect, with at least:
      - session_id
      - last_active (ISO8601 string)
      - one of: `my_ms_id`, `my_task_id` (= what is this session
        attached to, so the emitted STUCK signal can scope correctly)

The bus inspection + emission orchestration lives in the CLI in
lib/commands.py — this module just decides "which of these is stuck?"
and produces the stop signal payload(s).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import stop_signal as _stop


#: Default idle timeout. Lifted from SPEC §7 (= 30 min).
DEFAULT_TIMEOUT_MINUTES = 30


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> Optional[datetime]:
    """Lenient ISO 8601 parser. Returns None on garbage."""
    if not ts:
        return None
    # Strip trailing "Z" (= UTC) so datetime.fromisoformat accepts it on
    # Python < 3.11. Python 3.11+ handles "Z" natively but we prefer the
    # back-compat path.
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Telemetry record
# ---------------------------------------------------------------------------

@dataclass
class SessionTelemetry:
    """One session's recent-activity snapshot.

    Fields:
      session_id: the session_id (= sv-...).
      last_active: ISO8601 timestamp of last known activity (= last
        bus poll, last tool call, etc.). Empty string == "never seen".
      ms_id: the MS this session is currently working on, or empty.
      task_id: the task this session is currently working on, or empty.
      ignore: set True to skip from detection (e.g. session explicitly
        marked as observer / paused / human-attended).
    """
    session_id: str
    last_active: str = ""
    ms_id: str = ""
    task_id: str = ""
    ignore: bool = False


# ---------------------------------------------------------------------------
# Core detector
# ---------------------------------------------------------------------------

def find_stuck_sessions(
    telemetry: list[SessionTelemetry],
    *,
    now: datetime,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> list[SessionTelemetry]:
    """Return the subset of telemetry rows whose last_active is older
    than `now - timeout_minutes`.

    Rows with empty last_active or `ignore=True` are skipped. A row
    with a future last_active (= clock skew) is also skipped — we
    don't want to raise STUCK on a session that just looks futuristic.

    Sort order of the returned list mirrors input order.
    """
    if timeout_minutes <= 0:
        # 0 or negative timeout would flag everything; reject so the
        # caller doesn't accidentally emit a global STUCK barrage.
        raise ValueError("timeout_minutes must be > 0")

    threshold = now - timedelta(minutes=timeout_minutes)
    out: list[SessionTelemetry] = []
    for t in telemetry:
        if t.ignore:
            continue
        if not t.session_id:
            continue
        dt = _parse_iso(t.last_active)
        if dt is None:
            # No last_active or malformed → don't flag (= treat as
            # not-yet-observed rather than infinitely stale; STUCK
            # should be specific, not "everyone we don't know").
            continue
        if dt > now:
            continue  # clock skew, skip
        if dt <= threshold:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# STUCK signal builder
# ---------------------------------------------------------------------------

def build_stuck_signal(
    telemetry: SessionTelemetry,
    *,
    issued_by_session_id: str,
    now: Optional[datetime] = None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> dict:
    """Build a stop signal payload for a session deemed stuck.

    The wire form is just a stop signal with `reason_kind == "stuck"`
    and a `machine_reason` block describing the idle window — by
    design, STUCK is not a new protocol, it's a specific shape of
    the existing stop signal (SPEC §7: "stop protocol の特殊形").

    Target selection logic:
      * If telemetry.session_id is set → target_kind = "session"
        (= halt this specific worker without disturbing siblings).
      * Falls back to MS or task targeting if session_id missing
        (defensive — should not normally happen).
    """
    if not issued_by_session_id:
        raise ValueError("issued_by_session_id is required")
    if not telemetry or not telemetry.session_id:
        raise ValueError("telemetry must include session_id")

    now = now or datetime.now(timezone.utc)
    last_dt = _parse_iso(telemetry.last_active)
    idle_seconds = (
        int((now - last_dt).total_seconds()) if last_dt else -1
    )

    machine_reason = {
        "detector": "stuck",
        "timeout_minutes": timeout_minutes,
        "last_active": telemetry.last_active or "",
        "idle_seconds": idle_seconds,
    }
    if telemetry.ms_id:
        machine_reason["ms_id"] = telemetry.ms_id
    if telemetry.task_id:
        machine_reason["task_id"] = telemetry.task_id

    reason = (
        f"session idle > {timeout_minutes} min "
        f"(last_active={telemetry.last_active or '?'})"
    )

    return _stop.build_stop_payload(
        scope=_stop.SCOPE_SCOPED,
        issued_by_session_id=issued_by_session_id,
        target_kind=_stop.TARGET_SESSION,
        target_id=telemetry.session_id,
        reason=reason,
        reason_kind="stuck",
        machine_reason=machine_reason,
        issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
