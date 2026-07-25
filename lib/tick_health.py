"""Tick-liveness evaluation — notice when the periodic scheduler tick stops.

Companion to ``lib/deploy_health.py`` for a *different* silent-death failure
mode. The 2026-07-24 Trek review found that the server-side periodic tick
(``POST /api/system/trek-scheduler/tick``, which fires Treks AND rides-along
Operations via ``lib/tick_scheduler``) lost its production driver in the
Cloud Run → VPS migration (ms-102 / ms-96). Nothing was hitting the endpoint,
so Trek / Operation autonomy silently stopped firing in production and it was
only noticed by accident during a review (e-1391 / ms-66).

The structural fix has two halves:

  1. an in-box driver (``deploy/systemd/beacon-tick.timer`` →
     ``scripts/vps-tick.sh``) that curls the endpoint on a cadence, and
  2. an *out-of-box* watchdog (``scripts/tick-health-monitor.py``, run from
     GitHub Actions cron) that alerts when ticks stop — because a fully-down
     box cannot self-report.

This module is the pure decision layer for (2): given "when did the server
last tick" and "now", decide ok / stale / never and whether to alert. Kept
free of IO so the cadence math is unit-tested without a live prod / bus.
"""
from __future__ import annotations

import datetime
from typing import Optional


# A tick is expected roughly every minute (beacon-tick.timer). The default
# trek progress-check cadence is 10 min (trek_scheduler.DEFAULT_CADENCE_MINUTES),
# so 10 min without ANY tick means the driver itself is dead, not merely that
# nothing was due — comfortably above the 1-min driver cadence to avoid
# flapping on a single slow tick / deploy restart.
DEFAULT_STALE_AFTER_SECONDS = 600

# e-1391 follow-up (AX/maint review H1): a server that is reachable but has
# NEVER ticked is the exact migration failure this watchdog exists to catch
# (tick driver not installed / dead). We can't alert on ``never`` immediately —
# a just-booted server legitimately shows ``never`` until its first tick lands.
# So we grace-window it against the server's uptime: once the process has been
# up longer than this and STILL never ticked, the driver is overdue and we
# promote ``never`` → ``stale`` so the alert fires. Comfortably above the 1-min
# driver cadence + a deploy's startup slack.
DEFAULT_NEVER_GRACE_SECONDS = 300


def _parse_iso(value: str) -> Optional[datetime.datetime]:
    """Parse an ISO8601 timestamp (Beacon convention = ``Z`` suffix)."""
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def evaluate_tick_health(
    last_tick_at: str,
    now: datetime.datetime,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    uptime_seconds: Optional[float] = None,
    never_grace_seconds: int = DEFAULT_NEVER_GRACE_SECONDS,
) -> dict:
    """Classify tick liveness from the last-tick timestamp.

    ``last_tick_at`` is the ISO8601 string the server records at the end of
    each successful tick (empty / missing = the process has not ticked since
    it started). ``now`` must be timezone-aware UTC. ``uptime_seconds`` is how
    long the server process has been up (from ``/api/system/tick-health``);
    when provided it lets a *persistent* ``never`` be caught (see below).

    Returns a dict with:
      * ``status``: ``"ok"`` | ``"stale"`` | ``"never"``
      * ``seconds_since``: float seconds since the last tick, or ``None`` when
        never ticked
      * ``last_tick_at``: echoed back (normalized ISO, or ``""``)
      * ``stale_after_seconds`` / ``never_grace_seconds`` / ``uptime_seconds``
      * ``detail``: a short reason string for the never/stale case

    ``never`` (no tick since boot) is distinguished from ``stale`` (ticked
    once, then stopped): a just-restarted process legitimately shows ``never``
    for up to one cadence window. But a server that has been UP longer than
    ``never_grace_seconds`` and STILL never ticked is the migration failure
    (driver not installed / dead), so we **promote never → stale** in that case
    — otherwise a driver that never starts would stay silent forever (the exact
    hole this watchdog exists to close). Without a known uptime we stay
    conservative and report ``never`` (the caller can't tell boot from dead).
    """
    parsed = _parse_iso(last_tick_at)
    if parsed is None:
        overdue = (
            uptime_seconds is not None
            and never_grace_seconds is not None
            and uptime_seconds > never_grace_seconds
        )
        return {
            "status": "stale" if overdue else "never",
            "seconds_since": None,
            "last_tick_at": "",
            "stale_after_seconds": stale_after_seconds,
            "never_grace_seconds": never_grace_seconds,
            "uptime_seconds": uptime_seconds,
            "detail": (
                "reachable but never ticked and past the grace window "
                "(driver not installed / dead)"
                if overdue else "never ticked yet (within boot grace window)"
            ),
        }
    seconds_since = (now - parsed).total_seconds()
    status = "stale" if seconds_since > stale_after_seconds else "ok"
    return {
        "status": status,
        "seconds_since": seconds_since,
        "last_tick_at": parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "stale_after_seconds": stale_after_seconds,
        "uptime_seconds": uptime_seconds,
        "detail": "",
    }


def should_alert(health: dict, reachable: bool) -> bool:
    """Decide whether a tick-health condition warrants an alert.

    Alert when the endpoint is unreachable (box down / endpoint removed) or the
    tick is ``stale``. ``stale`` covers two cases that ``evaluate_tick_health``
    collapses into it: (1) the tick ran before and then stopped, and (2) the
    server has been up past ``never_grace_seconds`` and STILL never ticked
    (= driver not installed / dead — ``never`` is promoted to ``stale`` there).
    A plain ``never`` — still within the boot grace window, or with unknown
    uptime — does NOT alert, so a fresh deploy doesn't false-positive before
    its first tick lands. This is the honest behaviour: promotion happens in
    ``evaluate_tick_health`` (given uptime), not "some later window" by magic.
    """
    if not reachable:
        return True
    return health.get("status") == "stale"


def alert_dedup_key(reachable: bool, health: dict) -> str:
    """Stable key so a persistent outage alerts once, not every cron tick.

    Mirrors ``deploy-health-monitor``'s per-rev dedup: the key changes only
    when the *nature* of the outage changes (unreachable ↔ stale), so a
    steady-state dead tick does not re-notify each run.
    """
    if not reachable:
        return "unreachable"
    return f"stale:{health.get('status', '')}"


def format_alert_message(reachable: bool, health: dict, prod_url: str) -> str:
    """One-line human-readable alert body for a beacon-bus DM."""
    if not reachable:
        return (
            f"⚠ 本番の定期ティックが確認できません ({prod_url}/api/system/"
            f"tick-health が unreachable)。Trek/Operation の自律発火が止まって "
            f"いる恐れ。beacon-tick.timer / beacon-api.service を確認してください。"
        )
    # never-overdue case (promoted never → stale): no prior tick at all.
    if not health.get("last_tick_at"):
        up = health.get("uptime_seconds")
        up_min = f"{up / 60:.0f}分" if isinstance(up, (int, float)) else "?"
        return (
            f"⚠ 本番の定期ティックが一度も発火していません (server 起動から "
            f"{up_min} 経過、tick 0 回)。beacon-tick.timer が未設置 / 停止の "
            f"恐れ (= VPS 移行で driver を配線し忘れた形)。Trek/Operation 自律が "
            f"駆動されていません。{prod_url}/api/system/tick-health を確認して "
            f"ください。"
        )
    secs = health.get("seconds_since")
    mins = f"{secs / 60:.0f}分" if isinstance(secs, (int, float)) else "?"
    return (
        f"⚠ 本番の定期ティックが {mins} 停止しています "
        f"(last_tick_at={health.get('last_tick_at') or 'never'}、閾値 "
        f"{health.get('stale_after_seconds')}s)。beacon-tick.timer が停止 or "
        f"beacon-api が unresponsive の恐れ。Trek/Operation 自律が駆動されて "
        f"いません。"
    )
