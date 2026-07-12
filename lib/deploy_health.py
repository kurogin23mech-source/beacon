"""Deploy-health detection + alert-recipient resolution (ms-105 e-3230).

Pure logic for "本番が壊れても気付ける" (= notice when production is broken):

- ``evaluate_deploy_health`` compares production's live git revision (from
  ``/api/version``) against ``origin/main`` HEAD and decides whether prod is
  ``ok`` / ``deploying`` (a fresh mismatch still inside the grace window) /
  ``lagging`` (main advanced long ago but prod never caught up = a stuck deploy,
  the 2026-07-10 incident) / ``unreachable``.
- ``resolve_alert_recipient`` picks WHO the beacon-bus DM alert goes to: the
  session that deployed the offending revision when it can be identified,
  otherwise a user-scoped DM to the project owner (which surfaces at the
  owner's next session-start catch-up even after days away — ms-54 e-2974).

These are kept side-effect free so both the server alert endpoint and the CLI
can share them and they can be unit-tested without a live prod / bus. The
monitor cadence itself (GitHub Actions cron, independent of the VPS so a fully
down box is still noticed) lives in ``.github/workflows/deploy-health-monitor``.

Caveat (bus co-location): the beacon-bus backend runs on the same VPS as prod,
so a *fully down* box can't enqueue a DM. This covers the stuck-deploy class
(server up, serving stale/broken code — the actual incident), not a total
outage; an external channel (Discord/email) would be needed for the latter.
"""
from __future__ import annotations

from typing import Optional


# status values (kept as plain strings so callers/tests don't import an enum)
OK = "ok"
DEPLOYING = "deploying"
LAGGING = "lagging"
UNREACHABLE = "unreachable"

# statuses that warrant an alert
ALERT_STATUSES = frozenset({LAGGING, UNREACHABLE})


def evaluate_deploy_health(
    reachable: bool,
    prod_rev: str,
    main_rev: str,
    main_head_age_seconds: Optional[float],
    grace_seconds: float = 900.0,
) -> dict:
    """Decide whether production is healthy relative to ``origin/main``.

    Args:
      reachable: did ``/api/version`` respond at all.
      prod_rev: git revision production reports serving (``/api/version`` git_rev).
      main_rev: current ``origin/main`` HEAD revision.
      main_head_age_seconds: how long ago the main HEAD commit was authored;
        ``None`` when unknown (treated as past the grace window).
      grace_seconds: how long a mismatch is tolerated as "a deploy in flight"
        before it's called a stuck deploy. Default 15 min (> the 2 min pull
        timer + build/health settle).

    Returns a dict ``{"status": ..., "prod_rev", "main_rev", ...}``. Only
    ``LAGGING`` / ``UNREACHABLE`` should trigger an alert (see ``ALERT_STATUSES``).
    """
    if not reachable:
        return {"status": UNREACHABLE, "prod_rev": prod_rev, "main_rev": main_rev}

    # Compare on short-rev prefixes so a 40-char main HEAD matches prod's
    # `git rev-parse --short` output (or vice versa).
    if _rev_matches(prod_rev, main_rev):
        return {"status": OK, "prod_rev": prod_rev, "main_rev": main_rev}

    # A mismatch inside the grace window is a normal in-flight deploy, not a
    # stuck one — don't cry wolf while the pull timer is still catching up.
    within_grace = (
        main_head_age_seconds is not None
        and main_head_age_seconds < grace_seconds
    )
    status = DEPLOYING if within_grace else LAGGING
    return {
        "status": status,
        "prod_rev": prod_rev,
        "main_rev": main_rev,
        "main_head_age_seconds": main_head_age_seconds,
        "grace_seconds": grace_seconds,
    }


def _rev_matches(a: str, b: str) -> bool:
    """True if two git revisions refer to the same commit by short-prefix.

    Production reports a short rev (``git rev-parse --short``); ``origin/main``
    may be full-length. Compare on the shorter of the two, min 7 chars to avoid
    accidental collisions.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    if n < 7:
        return a == b
    return a[:n] == b[:n]


def resolve_alert_recipient(
    live_sessions: list,
    target_rev: str,
    owner_user_id: str,
) -> dict:
    """Pick the beacon-bus DM recipient for a deploy-health alert.

    Preference order:
      1. The **deploying session** — a live session sitting at ``target_rev``
         (= whoever pushed the revision prod should be serving is usually at
         that HEAD). If several match, the most recently active wins.
      2. Fallback: a **user-scoped** DM to the project owner, which the owner
         sees at their next session-start catch-up even if no session is live
         now (ms-54 e-2974).

    Returns one of:
      ``{"mode": "session", "session_id": ..., "user_id": ..., "reason": ...}``
      ``{"mode": "user",    "user_id": ...,    "reason": ...}``
    ``user_id`` may be empty in the session case if the directory entry lacks it.
    """
    candidates = [
        s for s in (live_sessions or [])
        if s.get("live", True)
        and _rev_matches(str((s.get("git") or {}).get("head_short", "")), target_rev)
    ]
    if candidates:
        # most recently active first (missing last_active sorts last)
        candidates.sort(key=lambda s: s.get("last_active") or "", reverse=True)
        top = candidates[0]
        return {
            "mode": "session",
            "session_id": top.get("session_id", ""),
            "user_id": top.get("user_id", ""),
            "reason": "live session at the deployed revision",
        }
    return {
        "mode": "user",
        "user_id": owner_user_id,
        "reason": "no live session at the deployed revision; owner user-scoped",
    }
