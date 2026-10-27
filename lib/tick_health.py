"""Tick-liveness evaluation — notice when the periodic scheduler tick stops.

Companion to ``lib/deploy_health.py`` for a *different* silent-death failure
mode. A 2026-07-24 review found that the server-side periodic tick
(``POST /api/system/tick``, which fires Operations + deadline reminders via
``lib/tick_scheduler``; ms-185 e-7065 moved it off the old
``/api/system/trek-scheduler/tick`` path, which now forwards) lost its
production driver in the Cloud Run → VPS migration (ms-102 / ms-96). Nothing
was hitting the endpoint, so the server-side autonomy silently stopped firing
in production and it was only noticed by accident (e-1391 / ms-66).

The structural fix has two halves:

  1. an in-box driver (``deploy/systemd/beacon-tick.timer`` →
     ``scripts/vps-tick.sh``) that curls the endpoint on a cadence, and
  2. an *out-of-box* watchdog (``scripts/tick-health-monitor.py``, run from
     GitHub Actions cron) that alerts when ticks stop — because a fully-down
     box cannot self-report.

This module is the pure decision layer for (2): given "when did the server
last tick" and "now", decide ok / stale / never and whether to alert. Kept
free of IO so the cadence math is unit-tested without a live prod / bus.

ms-185 e-7066 adds a **second dimension**: the tick is not one job but several
*sources* riding the same cadence (Operation firing, deadline reminders).
A tick that still runs every minute while one of its sources has silently
stopped is green on the liveness axis and broken in fact — so
``evaluate_scheduled_sources`` reads the source list the server reports and
flags any required source that is missing. Watching only the tick (or only
Operations) is precisely the blind spot this closes.

ms-185 e-7066 follow-up (independent AX review, high): "silently stopped" has
**two shapes**, and the first cut only caught one of them.

  * the source's row left the registry → it is absent from the reported list
    (``missing``), and
  * the source is still in the registry and still called every tick, but every
    call raises → it stays in the reported list forever (``failing``).

The second shape was invisible here: the reported list is the registry's keys,
which are computed independently of whether any call succeeded, so a source
that failed 100% of the time reported ``ok``. That is the exact failure class
this milestone exists to close (e-5366: 44 days / ~60k silent failures;
e-7078: Operation autonomy that had never once succeeded in production), and
a watchdog whose own prose promised to catch it is worse than no watchdog —
a green signal gets trusted. ``evaluate_scheduled_sources`` therefore also
reads the per-source outcome tally the tick already publishes
(``last_tick_report.scheduled_by_source``, built by
``summarize_scheduled_fires``) and flags a required source whose most recent
tick produced errors and **zero** successful fires.

Known limit, stated rather than papered over: the endpoint exposes only the
*most recent* tick, so ``failing`` means "every attempt in the last tick
failed", not "has been failing for N ticks". A lone flaky tick can therefore
alert once (the dedup key then holds it). That bias is deliberate here —
this module exists because a silent failure ran unnoticed for 44 days.
"""
from __future__ import annotations

import datetime
from typing import Optional


# A tick is expected roughly every minute (beacon-tick.timer), and the tick
# stamps ``last_tick_at`` on EVERY run — including runs where nothing was due.
# So the threshold measures the *driver*, not the workload: 10 missed driver
# cadences (10 x 1 min) without any tick means the driver itself is dead.
# Ten rather than two to avoid flapping on a single slow tick / deploy restart.
# (ms-185 e-7066: this used to be justified by the Trek progress-check cadence.
# Trek is being folded away, and a liveness threshold must not rest on the
# cadence of one particular workload — the driver's own cadence is the honest
# basis, and it is the thing this watchdog actually watches.)
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


# The sources that MUST ride the periodic tick. Declared here **independently**
# of the server's own registry (``server/app.py: SCHEDULED_SOURCES``) on
# purpose: this module is the out-of-box watchdog, and a watchdog that derives
# its expectations from the thing it watches cannot notice that thing losing a
# source (delete the source → both sides agree → silence). The two lists are
# held together by tests/test_scheduled_source_registry_e7066.py, which fails
# loudly if the server stops driving one of these. Removing a source for real
# therefore takes an explicit edit in both places — which is the point.
REQUIRED_SCHEDULED_SOURCES = ("operation", "deadline")

# Bucket for report entries that carry no ``source`` stamp (e.g. a failure
# raised before any source ran, such as ``list_all_projects`` blowing up).
UNATTRIBUTED_SOURCE = "unattributed"


def summarize_scheduled_fires(report, source_names=()) -> dict:
    """Count a tick's per-fire report by source: fired / skipped / error / total.

    ``report`` is the list the tick returns (``scheduled_fires``); each entry is
    stamped with ``source`` by the server's registry loop. ``source_names`` are
    the sources the server says it drove — every one of them gets a bucket even
    with zero entries, because "this source ran and had nothing due" and "this
    source is gone" must not look the same. Entries with no stamp land in
    ``unattributed``.

    Pure: no IO, no clock. Shared by the server (to build the tick report) and
    by the watchdog (to read it back).
    """
    summary = {name: {"fired": 0, "skipped": 0, "error": 0, "total": 0}
               for name in (source_names or ())}
    for entry in (report or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("source") or UNATTRIBUTED_SOURCE)
        bucket = summary.setdefault(
            name, {"fired": 0, "skipped": 0, "error": 0, "total": 0})
        bucket["total"] += 1
        if entry.get("error"):
            bucket["error"] += 1
        elif entry.get("fired"):
            bucket["fired"] += 1
        else:
            bucket["skipped"] += 1
    return summary


def failing_scheduled_sources(fire_summary, required_sources) -> list:
    """Required sources whose last tick produced errors and no successful fire.

    ``fire_summary`` is the per-source tally the tick publishes as
    ``last_tick_report.scheduled_by_source`` (``summarize_scheduled_fires``'s
    output): ``{name: {"fired", "skipped", "error", "total"}}``.

    A source counts as failing when ``error > 0`` **and** ``fired == 0``: every
    attempt that happened, failed. ``error > 0`` with ``fired > 0`` is NOT
    failing — that is one bad project among many, and alerting on it would make
    this watchdog cry wolf (a muted watchdog guards nothing). ``error == 0``
    with ``fired == 0`` is the ordinary "nothing was due" tick.

    Pure: no IO, no clock. Returns [] when the tally is absent or unusable, so
    an older deployment that does not publish it stays silent.
    """
    if not isinstance(fire_summary, dict):
        return []
    failing = []
    for name in tuple(required_sources or ()):
        bucket = fire_summary.get(name)
        if not isinstance(bucket, dict):
            continue
        try:
            errors = int(bucket.get("error") or 0)
            fired = int(bucket.get("fired") or 0)
        except (TypeError, ValueError):
            continue
        if errors > 0 and fired == 0:
            failing.append(name)
    return failing


def evaluate_scheduled_sources(
    reported_sources,
    required_sources=REQUIRED_SCHEDULED_SOURCES,
    fire_summary=None,
) -> dict:
    """Classify whether every required source is still riding the tick AND
    actually getting work done on it.

    ``reported_sources`` is the ``scheduled_sources`` list from
    ``/api/system/tick-health`` (the server echoes its own registry).
    ``fire_summary`` is that response's ``last_tick_report.scheduled_by_source``
    (optional — omit it and only the registry axis is judged).

    Returns ``{"status", "reported", "missing", "failing", "detail"}`` where
    status is:

      * ``"unknown"`` — the server did not report a source list at all. This is
        an **older deployment**, not a fault: stay silent rather than alert on
        every box that predates e-7066 (a watchdog that cries wolf during a
        rollout gets muted, and then it guards nothing).
      * ``"missing"`` — the server reports its sources and at least one
        required source is absent = that job silently stopped while the tick
        itself kept looking healthy.
      * ``"failing"`` — every required source is present, but at least one of
        them errored on every attempt of the last tick (e-7066 follow-up: the
        registry axis alone reports ``ok`` for a source that is wired, called,
        and broken 100% of the time).
      * ``"ok"`` — all required sources present, none wholly failing.

    ``missing`` outranks ``failing``: a source that left the registry is the
    more fundamental breakage, and naming it first keeps the recovery advice
    pointed at the right repair.
    """
    required = tuple(required_sources or ())
    if reported_sources is None:
        return {"status": "unknown", "reported": None, "missing": [],
                "failing": [],
                "detail": "server does not report scheduled_sources "
                          "(deployment predates ms-185 e-7066)"}
    if not isinstance(reported_sources, (list, tuple, set)):
        return {"status": "unknown", "reported": None, "missing": [],
                "failing": [],
                "detail": "scheduled_sources is not a list"}
    reported = [str(x) for x in reported_sources]
    missing = [name for name in required if name not in reported]
    if missing:
        return {
            "status": "missing", "reported": reported, "missing": missing,
            "failing": [],
            "detail": ("required scheduled source(s) no longer driven by the "
                       "tick: " + ", ".join(missing)),
        }
    failing = failing_scheduled_sources(fire_summary, required)
    if failing:
        return {
            "status": "failing", "reported": reported, "missing": [],
            "failing": failing,
            "detail": ("required scheduled source(s) ran but failed every "
                       "attempt on the last tick: " + ", ".join(failing)),
        }
    return {"status": "ok", "reported": reported, "missing": [],
            "failing": [], "detail": ""}


def should_alert(health: dict, reachable: bool, sources: Optional[dict] = None) -> bool:
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

    ms-185 e-7066 — also alert when ``sources`` (from
    ``evaluate_scheduled_sources``) says a required source is ``missing`` or
    ``failing``: a tick that keeps its cadence while one of its jobs stopped is
    green on the liveness axis and broken in fact, and that holds whether the
    job left the registry (``missing``) or stayed in it while erroring on every
    attempt (``failing``, the e-7066 follow-up). ``unknown`` (server predates
    the source report) never alerts.
    """
    if not reachable:
        return True
    if health.get("status") == "stale":
        return True
    return bool(sources) and sources.get("status") in ("missing", "failing")


def alert_dedup_key(reachable: bool, health: dict,
                    sources: Optional[dict] = None) -> str:
    """Stable key so a persistent outage alerts once, not every cron tick.

    Mirrors ``deploy-health-monitor``'s per-rev dedup: the key changes only
    when the *nature* of the outage changes (unreachable ↔ stale ↔ which
    source went missing ↔ which source is failing), so a steady-state fault
    does not re-notify each run. A newly-missing *second* source changes the
    key, so it is not swallowed by the dedup of the first; likewise a source
    that goes from missing to failing (or back) is reported again, because the
    repair differs.
    """
    if not reachable:
        return "unreachable"
    if health.get("status") == "stale":
        return f"stale:{health.get('status', '')}"
    if sources and sources.get("status") == "missing":
        return "missing-sources:" + ",".join(sorted(sources.get("missing") or []))
    if sources and sources.get("status") == "failing":
        return "failing-sources:" + ",".join(sorted(sources.get("failing") or []))
    return f"stale:{health.get('status', '')}"


def format_alert_message(reachable: bool, health: dict, prod_url: str,
                         sources: Optional[dict] = None) -> str:
    """One-line human-readable alert body for a beacon-bus DM."""
    if not reachable:
        return (
            f"⚠ 本番の定期ティックが確認できません ({prod_url}/api/system/"
            f"tick-health が unreachable)。Operation の自律発火と締切リマインドが "
            f"止まっている恐れ。beacon-tick.timer / beacon-api.service を確認して "
            f"ください。"
        )
    # never-overdue case (promoted never → stale): no prior tick at all.
    if not health.get("last_tick_at"):
        up = health.get("uptime_seconds")
        up_min = f"{up / 60:.0f}分" if isinstance(up, (int, float)) else "?"
        return (
            f"⚠ 本番の定期ティックが一度も発火していません (server 起動から "
            f"{up_min} 経過、tick 0 回)。beacon-tick.timer が未設置 / 停止の "
            f"恐れ (= VPS 移行で driver を配線し忘れた形)。Operation の自律発火と "
            f"締切リマインドが駆動されていません。"
            f"{prod_url}/api/system/tick-health を確認してください。"
        )
    if health.get("status") != "stale" and sources and sources.get("status") == "missing":
        # 定期ティック自体は生きているのに、相乗りしている job が消えた形。
        # 「ティックは緑」で見逃されるのがこの alert の存在理由なので、何が
        # 止まったかを名指しする。
        missing = ", ".join(sources.get("missing") or [])
        reported = ", ".join(sources.get("reported") or []) or "(なし)"
        return (
            f"⚠ 本番の定期ティックは動いていますが、相乗りしている定期発火の "
            f"source が欠けています (欠落: {missing} / 報告された source: "
            f"{reported})。該当する自律 (operation=Operation の発火 / "
            f"deadline=締切リマインド) が無音で止まっています。"
            f"{prod_url}/api/system/tick-health を確認してください。"
        )
    if health.get("status") != "stale" and sources and sources.get("status") == "failing":
        # 定期ティックも登録簿も生きているのに、その source の試行が全部失敗して
        # いる形。登録簿の名前だけを見ていると ok に見えるのがこの alert の存在
        # 理由 (= e-7066 の積み残し、独立 AX レビューの high)。直し方が「欠落」
        # とは違う (driver の再配線ではなく、失敗の中身を読む) ので案内も分ける。
        failing = ", ".join(sources.get("failing") or [])
        return (
            f"⚠ 本番の定期ティックは動いていますが、相乗りしている定期発火の "
            f"source が直前のティックで全試行失敗しています (失敗: {failing})。"
            f"該当する自律 (operation=Operation の発火 / deadline=締切リマインド) "
            f"は呼ばれてはいるものの 1 件も成功していません。"
            f"{prod_url}/api/system/tick-health の last_tick_report."
            f"scheduled_by_source に source ごとの内訳があります。"
            f"driver (beacon-tick.timer) ではなく発火側の例外を確認してください。"
        )
    secs = health.get("seconds_since")
    mins = f"{secs / 60:.0f}分" if isinstance(secs, (int, float)) else "?"
    return (
        f"⚠ 本番の定期ティックが {mins} 停止しています "
        f"(last_tick_at={health.get('last_tick_at') or 'never'}、閾値 "
        f"{health.get('stale_after_seconds')}s)。beacon-tick.timer が停止 or "
        f"beacon-api が unresponsive の恐れ。Operation の自律発火と締切リマインドが "
        f"駆動されていません。"
    )
