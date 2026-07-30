#!/usr/bin/env python3
"""Deploy-health monitor — notice when production is stuck / down (ms-105 e-3230).

Independent watchdog for the 2026-07-10 incident (auto-deploy stalled 3 days,
noticed only by accident). Runs OUTSIDE the monitored box (GitHub Actions cron)
so a fully-down VPS is still caught. It:

  1. fetches production's live git revision from ``<prod>/api/version``,
  2. compares it to the **deploy marker** — the ``deployed-prod`` git tag, moved
     by ``beacon deploy record`` to the rev we intended to deploy — with a grace
     window (``lib.deploy_health.evaluate_deploy_health``). The marker replaced
     ``origin/main`` HEAD as the basis on 2026-07-28: the VPS pull-timer that
     made prod auto-track main was disabled, so "prod behind main" became the
     normal state and reddened the monitor every tick. The marker is a truth
     source for "what prod should serve" that is independent of auto-deploy and
     lives in git, so this token-free CI job can read it.
  3. on ``lagging`` / ``unreachable``, resolves the alert recipient — the
     session that deployed the offending rev, else the owner user-scoped
     (``lib.deploy_health.resolve_alert_recipient``) — and sends a beacon-bus
     DM via the ``beacon`` CLI,
  4. dedups on a state file so a persistent stuck deploy alerts once per rev,
     not every tick.

``ahead`` (prod newer than the marker = a deploy that wasn't recorded) and
``no_target`` (no marker yet) are NOT alerts — they print a nudge and exit 0.

The pure detection / recipient logic lives in ``lib/deploy_health.py`` (unit
tested). This script is the IO glue; ``decide_and_alert`` takes the IO as
injected callables so it can be tested without a live prod / bus / network.

Exit codes: 0 = ok/deploying/ahead/no_target or alert sent, 2 = alert condition
but send failed. Never raises for a healthy prod.

Usage (GitHub Actions cron):
  python3 scripts/deploy-health-monitor.py \
    --prod-url https://beacon-ai.dev \
    --owner-user-id <uid> \
    --marker-ref deployed-prod \
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


def read_deploy_marker(ref: str = "deployed-prod") -> tuple:
    """Return (rev, age_seconds) for the deploy marker tag, or ("", None).

    ``rev`` is the commit the ``deployed-prod`` tag points at (the rev prod
    should be serving). ``age_seconds`` is how long ago the marker was set —
    the tag's own date (``creatordate``: the tagger date for an annotated tag,
    else the commit date) — which is when the deploy was initiated, so a
    just-moved marker is still inside the grace window. A missing marker returns
    ("", None) and the evaluator treats it as ``no_target`` (no lag alert).
    """
    try:
        # --verify --quiet: a missing ref yields empty stdout + nonzero exit
        # (plain `git rev-parse <missing>` echoes the arg back, which would look
        # like a bogus rev and mis-fire a lagging alert).
        rp = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=10)
        rev = rp.stdout.strip() if rp.returncode == 0 else ""
        if not rev:
            return ("", None)
        ct = subprocess.run(
            ["git", "for-each-ref", "--format=%(creatordate:unix)",
             f"refs/tags/{ref}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not ct:
            # Not a tag (e.g. a branch passed for local testing) — fall back to
            # the commit date so age is still meaningful.
            ct = subprocess.run(
                ["git", "show", "-s", "--format=%ct", ref],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        import time
        age = max(0.0, time.time() - int(ct)) if ct else None
        return (rev, age)
    except Exception:
        return ("", None)


def git_prod_ancestry(prod_rev: str, target_rev: str) -> str:
    """Return the ancestry of prod vs the marker as a 3-value string.

    ``dh.ANCESTRY_BEHIND``  → ``prod_rev`` is an ancestor of ``target_rev`` (prod
                is *behind* the marker = the recorded deploy never landed = stuck).
    ``dh.ANCESTRY_AHEAD``   → prod is a descendant of the marker (prod is *ahead*
                = a deploy happened that wasn't recorded — drift, not an incident).
    ``dh.ANCESTRY_UNKNOWN`` → couldn't determine (either rev missing from the
                checkout, or diverged branches); the evaluator then treats it
                conservatively as behind (LAGGING) so a real stuck deploy is
                never silently dropped.

    A string (not a tri-state bool) so callers can't misread "unknown" as a
    falsy "not behind" (AX review 2026-07-30). Requires both commits in the
    local history — the workflow checks out with ``fetch-depth: 0`` so prod's
    (possibly older) rev is present.
    """
    prod_rev = (prod_rev or "").strip()
    target_rev = (target_rev or "").strip()
    if not prod_rev or not target_rev:
        return dh.ANCESTRY_UNKNOWN
    try:
        # Both revs must resolve locally, else ancestry is meaningless.
        for r in (prod_rev, target_rev):
            if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{r}^{{commit}}"],
                              capture_output=True, text=True, timeout=10).returncode != 0:
                return dh.ANCESTRY_UNKNOWN
        behind = subprocess.run(
            ["git", "merge-base", "--is-ancestor", prod_rev, target_rev],
            capture_output=True, text=True, timeout=10,
        ).returncode == 0
        if behind:
            return dh.ANCESTRY_BEHIND
        ahead = subprocess.run(
            ["git", "merge-base", "--is-ancestor", target_rev, prod_rev],
            capture_output=True, text=True, timeout=10,
        ).returncode == 0
        # ahead → prod is descendant of marker. Neither (diverged) → unknown
        # (conservative → LAGGING).
        return dh.ANCESTRY_AHEAD if ahead else dh.ANCESTRY_UNKNOWN
    except Exception:
        return dh.ANCESTRY_UNKNOWN


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
    target_rev: str,
    target_age_seconds: float,
    owner_user_id: str,
    live_sessions: list,
    last_alerted_rev: str,
    send,
    grace_seconds: float = 900.0,
    prod_ancestry: str = dh.ANCESTRY_UNKNOWN,
) -> dict:
    """Evaluate health and (dedup-gated) alert. Returns a verdict dict.

    ``send(recipient, text) -> bool`` is injected so tests avoid the CLI. The
    dedup key is ``target_rev``: a stuck deploy at the same marker alerts once,
    not every tick. ``unreachable`` dedups on the literal ``"unreachable"`` key
    so a flapping box doesn't spam but a *new* stuck rev still alerts.

    ``prod_ancestry`` is the git-ancestry direction (see ``git_prod_ancestry``)
    — it separates a genuine stuck deploy (prod behind the marker → alert) from
    an unrecorded deploy (prod ahead → soft ``ahead`` nudge, no alert).
    """
    verdict = dh.evaluate_deploy_health(
        reachable, prod_rev, target_rev, target_age_seconds,
        grace_seconds=grace_seconds, prod_ancestry=prod_ancestry,
    )
    status = verdict["status"]
    verdict["alerted"] = False
    if status not in dh.ALERT_STATUSES:
        return verdict

    dedup_key = "unreachable" if status == dh.UNREACHABLE else target_rev
    if last_alerted_rev == dedup_key:
        verdict["alerted"] = False
        verdict["dedup"] = True
        return verdict

    recipient = dh.resolve_alert_recipient(live_sessions, target_rev, owner_user_id)
    if status == dh.UNREACHABLE:
        text = (
            "⚠ deploy-health: 本番 (/api/version) が応答しません。デプロイ / サービスが "
            "停止している可能性があります。"
        )
    else:
        text = (
            f"⚠ deploy-health: 本番が記録済みデプロイ rev より遅れています "
            f"(prod={prod_rev[:7]} < 記録={target_rev[:7]})。デプロイが stuck "
            f"している可能性があります (記録は `beacon deploy record` の deployed-prod マーカー)。"
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
    ap.add_argument("--marker-ref", default=os.environ.get(
        "BEACON_DEPLOY_MARKER_REF", "deployed-prod"),
        help="git ref that marks the rev prod should be serving "
             "(moved by `beacon deploy record`)")
    ap.add_argument("--grace-seconds", type=float, default=float(
        os.environ.get("BEACON_DEPLOY_GRACE_SECONDS", "900")))
    ap.add_argument("--state-file", default=os.environ.get(
        "BEACON_DEPLOY_STATE_FILE", ".beacon/.deploy-health-alerted"))
    args = ap.parse_args(argv)

    reachable, prod_rev = fetch_prod_version(args.prod_url)
    target_rev, target_age = read_deploy_marker(args.marker_ref)
    ancestry = git_prod_ancestry(prod_rev, target_rev)
    live = fetch_live_sessions()
    last = _read_state(args.state_file)

    verdict = decide_and_alert(
        reachable=reachable, prod_rev=prod_rev, target_rev=target_rev,
        target_age_seconds=target_age if target_age is not None else 10_000.0,
        owner_user_id=args.owner_user_id, live_sessions=live,
        last_alerted_rev=last, send=send_alert_dm,
        grace_seconds=args.grace_seconds, prod_ancestry=ancestry,
    )
    if verdict.get("alerted"):
        _write_state(args.state_file, verdict.get("dedup_key", target_rev))

    print(json.dumps(verdict, ensure_ascii=False))
    # Non-alert states that still need a human/AI action print a recovery nudge
    # to stderr (still exit 0 / green). Kept symmetric so neither is a silent
    # dead-end (AX review 2026-07-30 flagged no_target had no nudge).
    if verdict["status"] == dh.AHEAD:
        print(f"ℹ deploy-health: 本番 ({prod_rev[:7]}) は deployed-prod マーカー "
              f"({(target_rev or '')[:7]}) より新しい = デプロイ記録漏れ。"
              f"`beacon deploy record` を打ってマーカーを追随させてください。",
              file=sys.stderr)
    elif verdict["status"] == dh.NO_TARGET:
        print(f"ℹ deploy-health: deployed-prod マーカーが未作成のため遅延判定を "
              f"スキップしました (本番 {prod_rev[:7]} の死活のみ監視)。"
              f"`beacon deploy record` を一度打つとマーカーが作られ遅延監視が有効化されます。",
              file=sys.stderr)
    if verdict["status"] in dh.ALERT_STATUSES and verdict.get(
            "sent") is False and not verdict.get("dedup"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
