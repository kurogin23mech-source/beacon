#!/usr/bin/env python3
"""Deploy-health monitor — notice when production is stuck / down (ms-105 e-3230).

Independent watchdog for the 2026-07-10 incident (auto-deploy stalled 3 days,
noticed only by accident). Runs OUTSIDE the monitored box (GitHub Actions cron)
so a fully-down VPS is still caught. It:

  1. fetches production's live git revision from ``<prod>/api/version``,
  2. compares it to ``origin/main`` HEAD (from the CI checkout) with a grace
     window (``lib.deploy_health.evaluate_deploy_health``),
  3. on ``lagging`` / ``unreachable``, resolves the alert recipient — the
     session that deployed the offending rev, else the owner user-scoped
     (``lib.deploy_health.resolve_alert_recipient``) — and sends a beacon-bus
     DM via the ``beacon`` CLI,
  4. dedups on a state file so a persistent stuck deploy alerts once per rev,
     not every tick.

The pure detection / recipient logic lives in ``lib/deploy_health.py`` (unit
tested). This script is the IO glue; ``decide_and_alert`` takes the IO as
injected callables so it can be tested without a live prod / bus / network.

Exit codes: 0 = ok/deploying or alert sent, 2 = alert condition but send
failed. Never raises for a healthy prod.

Usage (GitHub Actions cron / VPS push-side):
  python3 scripts/deploy-health-monitor.py \
    --prod-url https://beacon-ai.dev \
    --owner-user-id <uid> \
    --state-file .beacon/.deploy-health-alerted
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import deploy_health as dh  # noqa: E402


# --- IO primitives (mocked in tests) ---------------------------------------

def fetch_prod_version(prod_url: str, timeout: float = 8.0) -> tuple:
    """Return (reachable, git_rev). reachable=False on any error / non-2xx."""
    url = prod_url.rstrip("/") + "/api/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status // 100 != 2:
                return (False, "")
            data = json.loads(resp.read().decode("utf-8"))
        return (True, str(data.get("git_rev", "") or ""))
    except Exception:
        return (False, "")


def git_main_rev_and_age(ref: str = "origin/main") -> tuple:
    """Return (rev, age_seconds) for ``ref`` HEAD, or ("", None) on failure."""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", ref], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        ct = subprocess.run(
            ["git", "show", "-s", "--format=%ct", ref],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        import time
        age = max(0.0, time.time() - int(ct)) if ct else None
        return (rev, age)
    except Exception:
        return ("", None)


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
    """Send the alert via ``beacon bus send --to <session>``. Returns True on
    exit 0.

    Only session-scoped recipients can be unicast (``beacon bus send`` requires
    a session ``--to`` for channel=dm). A ``user`` recipient means the owner has
    no live session, so there is nothing to target — return False (the monitor
    then exits non-zero, which reddens the CI run as its own signal).
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


def _write_state(state_file: str, rev: str) -> None:
    try:
        p = Path(state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rev, encoding="utf-8")
    except Exception:
        pass


def decide_and_alert(
    *,
    reachable: bool,
    prod_rev: str,
    main_rev: str,
    main_age: float,
    owner_user_id: str,
    live_sessions: list,
    last_alerted_rev: str,
    send,
    grace_seconds: float = 900.0,
) -> dict:
    """Evaluate health and (dedup-gated) alert. Returns a verdict dict.

    ``send(recipient, text) -> bool`` is injected so tests avoid the CLI. The
    dedup key is ``main_rev``: a stuck deploy at the same target alerts once,
    not every tick. ``unreachable`` dedups on the literal ``"unreachable"`` key
    so a flapping box doesn't spam but a *new* stuck rev still alerts.
    """
    verdict = dh.evaluate_deploy_health(
        reachable, prod_rev, main_rev, main_age, grace_seconds=grace_seconds
    )
    status = verdict["status"]
    verdict["alerted"] = False
    if status not in dh.ALERT_STATUSES:
        return verdict

    dedup_key = "unreachable" if status == dh.UNREACHABLE else main_rev
    if last_alerted_rev == dedup_key:
        verdict["alerted"] = False
        verdict["dedup"] = True
        return verdict

    recipient = dh.resolve_alert_recipient(live_sessions, main_rev, owner_user_id)
    if status == dh.UNREACHABLE:
        text = (
            "⚠ deploy-health: 本番 (/api/version) が応答しません。デプロイ / サービスが "
            "停止している可能性があります。"
        )
    else:
        text = (
            f"⚠ deploy-health: 本番が main から遅れています (prod={prod_rev[:7]} < "
            f"main={main_rev[:7]})。自動デプロイが stuck している可能性があります。"
        )
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
    ap.add_argument("--main-ref", default="origin/main")
    ap.add_argument("--grace-seconds", type=float, default=float(
        os.environ.get("BEACON_DEPLOY_GRACE_SECONDS", "900")))
    ap.add_argument("--state-file", default=os.environ.get(
        "BEACON_DEPLOY_STATE_FILE", ".beacon/.deploy-health-alerted"))
    args = ap.parse_args(argv)

    reachable, prod_rev = fetch_prod_version(args.prod_url)
    main_rev, main_age = git_main_rev_and_age(args.main_ref)
    live = fetch_live_sessions()
    last = _read_state(args.state_file)

    verdict = decide_and_alert(
        reachable=reachable, prod_rev=prod_rev, main_rev=main_rev,
        main_age=main_age if main_age is not None else 10_000.0,
        owner_user_id=args.owner_user_id, live_sessions=live,
        last_alerted_rev=last, send=send_alert_dm,
        grace_seconds=args.grace_seconds,
    )
    if verdict.get("alerted"):
        _write_state(args.state_file, verdict.get("dedup_key", main_rev))

    print(json.dumps(verdict, ensure_ascii=False))
    if verdict["status"] in dh.ALERT_STATUSES and verdict.get(
            "sent") is False and not verdict.get("dedup"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
