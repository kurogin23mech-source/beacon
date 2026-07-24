#!/usr/bin/env python3
"""Tick-health monitor — notice when the production periodic tick stops (e-1391 / ms-66).

Independent watchdog for the 2026-07-24 finding: the server-side periodic tick
(``POST /api/system/trek-scheduler/tick``, which drives Trek AND rides-along
Operation autonomy) lost its driver in the Cloud Run → VPS migration and
silently stopped firing — noticed only by accident during a review. This is
the *same class* of silent death that ``deploy-health-monitor.py`` guards for
deploy freshness, so this monitor is deliberately its twin:

  1. polls production ``<prod>/api/system/tick-health`` (in-memory last-tick
     timestamp the server records at the end of each tick),
  2. classifies ok / stale / never / unreachable
     (``lib.tick_health.evaluate_tick_health`` / ``should_alert``),
  3. on a dead tick, resolves the alert recipient (reusing
     ``lib.deploy_health.resolve_alert_recipient``) and sends a beacon-bus DM,
  4. dedups on a state file so a persistent dead tick alerts once, not every
     cron tick.

Runs OUTSIDE the monitored box (GitHub Actions cron), because a down /
misconfigured VPS cannot self-report. Pure decision logic lives in
``lib/tick_health.py`` (unit tested); this script is the IO glue and takes the
send callable injected so ``decide_and_alert`` is testable without a live
prod / bus.

Exit codes: 0 = ok / dedup / alert sent, 2 = alert condition but send failed.

Usage (GitHub Actions cron):
  python3 scripts/tick-health-monitor.py \
    --prod-url https://beacon-ai.dev \
    --owner-user-id <uid> \
    --state-file .beacon/.tick-health-alerted
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import tick_health as th  # noqa: E402
import deploy_health as dh  # noqa: E402  (reuse recipient resolution)


# --- IO primitives (mocked in tests) ---------------------------------------

def fetch_tick_health(prod_url: str, timeout: float = 8.0) -> tuple:
    """Return (reachable, payload_dict). reachable=False on any error / non-2xx."""
    url = prod_url.rstrip("/") + "/api/system/tick-health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status // 100 != 2:
                return (False, {})
            data = json.loads(resp.read().decode("utf-8"))
        return (True, data if isinstance(data, dict) else {})
    except Exception:
        return (False, {})


def fetch_live_sessions() -> list:
    """Live sessions from ``beacon bus directory --live --json`` (or [])."""
    try:
        out = subprocess.run(
            ["beacon", "bus", "directory", "--live", "--json"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        data = json.loads(out) if out else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def send_alert_dm(recipient: dict, text: str) -> bool:
    """Send the alert via ``beacon bus send --to <session>``. True on exit 0.

    Only session-scoped recipients can be unicast (channel=dm needs a session
    ``--to``). A ``user`` recipient means the owner has no live session — return
    False, and the monitor exits non-zero so the CI run reddens as its own
    signal.
    """
    if recipient.get("mode") != "session" or not recipient.get("session_id"):
        return False
    cmd = ["beacon", "bus", "send", "--channel", "dm",
           "--to", recipient["session_id"],
           "--payload", json.dumps({"text": text})]
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=25).returncode == 0
    except Exception:
        return False


# --- Orchestration (pure w.r.t. injected IO — unit tested) ------------------

def _read_state(state_file: str) -> str:
    try:
        return Path(state_file).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_state(state_file: str, key: str) -> None:
    try:
        p = Path(state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(key, encoding="utf-8")
    except Exception:
        pass


def decide_and_alert(
    *,
    reachable: bool,
    payload: dict,
    now: datetime.datetime,
    prod_url: str,
    owner_user_id: str,
    live_sessions: list,
    last_alerted_key: str,
    send,
    stale_after_seconds: int = th.DEFAULT_STALE_AFTER_SECONDS,
) -> dict:
    """Evaluate tick liveness and (dedup-gated) alert. Returns a verdict dict.

    ``send(recipient, text) -> bool`` is injected so tests avoid the CLI. The
    dedup key changes only when the nature of the outage changes
    (unreachable ↔ stale), so a steady-state dead tick alerts once.
    """
    if reachable:
        health = th.evaluate_tick_health(
            str(payload.get("last_tick_at", "")), now,
            stale_after_seconds=stale_after_seconds,
        )
    else:
        health = {"status": "unreachable", "seconds_since": None,
                  "last_tick_at": "", "stale_after_seconds": stale_after_seconds}

    verdict = {"reachable": reachable, "health": health, "alerted": False}

    if not th.should_alert(health, reachable):
        return verdict

    dedup_key = th.alert_dedup_key(reachable, health)
    if last_alerted_key == dedup_key:
        verdict["dedup"] = True
        verdict["dedup_key"] = dedup_key
        return verdict

    recipient = dh.resolve_alert_recipient(live_sessions, "", owner_user_id)
    text = th.format_alert_message(reachable, health, prod_url)
    verdict["recipient"] = recipient
    verdict["dedup_key"] = dedup_key
    verdict["sent"] = bool(send(recipient, text))
    verdict["alerted"] = verdict["sent"]
    return verdict


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod-url", default=os.environ.get(
        "BEACON_PROD_URL", "https://beacon-ai.dev"))
    ap.add_argument("--owner-user-id", default=os.environ.get(
        "BEACON_OWNER_USER_ID", ""))
    ap.add_argument("--stale-after-seconds", type=int, default=int(
        os.environ.get("BEACON_TICK_STALE_SECONDS",
                       str(th.DEFAULT_STALE_AFTER_SECONDS))))
    ap.add_argument("--state-file", default=os.environ.get(
        "BEACON_TICK_STATE_FILE", ".beacon/.tick-health-alerted"))
    args = ap.parse_args(argv)

    reachable, payload = fetch_tick_health(args.prod_url)
    live = fetch_live_sessions()
    last = _read_state(args.state_file)
    now = datetime.datetime.now(datetime.timezone.utc)

    verdict = decide_and_alert(
        reachable=reachable, payload=payload, now=now,
        prod_url=args.prod_url, owner_user_id=args.owner_user_id,
        live_sessions=live, last_alerted_key=last, send=send_alert_dm,
        stale_after_seconds=args.stale_after_seconds,
    )
    if verdict.get("alerted"):
        _write_state(args.state_file, verdict.get("dedup_key", ""))

    print(json.dumps(verdict, ensure_ascii=False))
    if verdict.get("health", {}).get("status") in ("stale", "unreachable") \
            and verdict.get("sent") is False and not verdict.get("dedup"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
