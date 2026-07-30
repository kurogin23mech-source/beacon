"""Deploy-health detection + alert-recipient resolution (ms-105 e-3230).

Pure logic for "本番が壊れても気付ける" (= notice when production is broken):

- ``evaluate_deploy_health`` compares production's live git revision (from
  ``/api/version``) against the **deploy marker** — the ``deployed-prod`` git
  tag, moved by ``beacon deploy record`` to the rev we intended to deploy — and
  decides whether prod is ``ok`` / ``deploying`` (a fresh mismatch still inside
  the grace window) / ``lagging`` (the recorded deploy never landed = a stuck
  deploy, the 2026-07-10 incident class) / ``ahead`` (prod is *newer* than the
  marker = a deploy happened but wasn't recorded — drift, not an incident) /
  ``no_target`` (no marker yet) / ``unreachable``.
- ``resolve_alert_recipient`` picks WHO the beacon-bus DM alert goes to: the
  session that deployed the offending revision when it can be identified,
  otherwise a user-scoped DM to the project owner (which surfaces at the
  owner's next session-start catch-up even after days away — ms-54 e-2974).

Why the marker and not ``origin/main`` (the original basis)? The VPS pull-timer
that made prod auto-track main was disabled on 2026-07-28; prod now advances
only on a manual deploy. So "prod behind main HEAD" became the *normal* state
(main marches ahead of a deliberately-frozen prod) and made the monitor red on
every tick. The marker restores a truth source that means "what prod should be
serving" independent of whether auto-deploy is on — and it lives in git, so the
GitHub Actions monitor reads it token-free.

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
# prod is a *descendant* of the marker: a deploy landed but `beacon deploy
# record` wasn't run, so the marker is stale-behind. NOT an incident — a soft
# "please record it" nudge (green). Distinguishing this from LAGGING is the
# whole reason we take a direction (ancestor/descendant), not just equality.
AHEAD = "ahead"
# No deploy marker exists yet (fresh repo / never recorded a prod deploy). Can't
# tell stuck from healthy on rev, so stay quiet on lag — only unreachable fires.
NO_TARGET = "no_target"

# statuses that warrant an alert
ALERT_STATUSES = frozenset({LAGGING, UNREACHABLE})

# git-ancestry of prod vs the marker (a 3-value string, NOT Optional[bool], so
# "unknown" can't be misread as a falsy "not behind" — AX review 2026-07-30).
ANCESTRY_BEHIND = "behind"      # prod is an ancestor of the marker  → stuck
ANCESTRY_AHEAD = "ahead"        # prod is a descendant of the marker → unrecorded
ANCESTRY_UNKNOWN = "unknown"    # direction undetermined → treated as behind


def evaluate_deploy_health(
    reachable: bool,
    prod_rev: str,
    target_rev: str,
    target_age_seconds: Optional[float],
    grace_seconds: float = 900.0,
    prod_ancestry: str = ANCESTRY_UNKNOWN,
) -> dict:
    """Decide whether production is healthy relative to the deploy marker.

    Args:
      reachable: did ``/api/version`` respond at all.
      prod_rev: git revision production reports serving (``/api/version`` git_rev).
      target_rev: the rev prod *should* be serving — the ``deployed-prod`` marker
        (empty string when no marker exists yet).
      target_age_seconds: how long ago the marker was set (its tag date); ``None``
        when unknown (treated as past the grace window).
      grace_seconds: how long a mismatch is tolerated as "a deploy in flight"
        before it's called a stuck deploy. Default 15 min (> the pull/restart +
        health settle).
      prod_ancestry: git ancestry of prod vs the marker, one of
        ``ANCESTRY_BEHIND`` (prod is an ancestor of the marker = stuck),
        ``ANCESTRY_AHEAD`` (prod is a descendant = unrecorded deploy), or
        ``ANCESTRY_UNKNOWN`` (direction couldn't be determined — treated
        conservatively as behind so a real stuck deploy is never missed). A
        3-value string (not ``Optional[bool]``) so "unknown" can't be misread as
        a falsy "not behind".

    Returns a dict ``{"status": ..., "prod_rev", "target_rev", ...}``. Only
    ``LAGGING`` / ``UNREACHABLE`` should trigger an alert (see ``ALERT_STATUSES``).
    """
    if not reachable:
        return {"status": UNREACHABLE, "prod_rev": prod_rev, "target_rev": target_rev}

    if not (target_rev or "").strip():
        # No marker to compare against — a stuck deploy is indistinguishable from
        # a healthy one on rev alone, so don't cry wolf. (unreachable still fires
        # above; that needs no marker.)
        return {"status": NO_TARGET, "prod_rev": prod_rev, "target_rev": target_rev}

    # Compare on short-rev prefixes so a 40-char marker matches prod's
    # `git rev-parse --short` output (or vice versa).
    if _rev_matches(prod_rev, target_rev):
        return {"status": OK, "prod_rev": prod_rev, "target_rev": target_rev}

    # A mismatch inside the grace window is a normal in-flight deploy, not a
    # stuck one — don't cry wolf while the restart/health check is still settling.
    within_grace = (
        target_age_seconds is not None
        and target_age_seconds < grace_seconds
    )
    if within_grace:
        status = DEPLOYING
    elif prod_ancestry == ANCESTRY_AHEAD:
        # prod is newer than the marker → a deploy happened without recording it.
        # Not stuck; surface as a soft nudge so the marker gets caught up.
        status = AHEAD
    else:
        # prod is behind the marker (ANCESTRY_BEHIND) or the direction is unknown,
        # past the grace window → the deploy we recorded never landed = stuck.
        status = LAGGING
    return {
        "status": status,
        "prod_rev": prod_rev,
        "target_rev": target_rev,
        "target_age_seconds": target_age_seconds,
        "grace_seconds": grace_seconds,
        "prod_ancestry": prod_ancestry,
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

    Preference order (each session step yields a session to unicast to):
      1. The **deploying session** — a live session sitting at ``target_rev``
         (= whoever pushed the revision prod should be serving is usually at
         that HEAD). If several match, the most recently active wins.
      2. **Any live session of the owner** — the owner is online elsewhere
         (a different repo / rev). Still a real session to unicast to.
      3. Fallback: **user-scoped** (owner fully offline). No live session to
         target — a beacon-bus DM needs a session, and the bus is co-located
         with prod anyway, so a truly offline owner + down box is the
         external-channel case (out of scope here).

    Returns one of:
      ``{"mode": "session", "session_id": ..., "user_id": ..., "reason": ...}``
      ``{"mode": "user",    "user_id": ...,    "reason": ...}``
    """
    live = [s for s in (live_sessions or []) if s.get("live", True)]

    def _most_recent(rows):
        return sorted(rows, key=lambda s: s.get("last_active") or "",
                      reverse=True)[0]

    at_rev = [
        s for s in live
        if _rev_matches(str((s.get("git") or {}).get("head_short", "")), target_rev)
    ]
    if at_rev:
        top = _most_recent(at_rev)
        return {
            "mode": "session",
            "session_id": top.get("session_id", ""),
            "user_id": top.get("user_id", ""),
            "reason": "live session at the deployed revision",
        }

    owner_live = [
        s for s in live if owner_user_id and s.get("user_id") == owner_user_id
    ]
    if owner_live:
        top = _most_recent(owner_live)
        return {
            "mode": "session",
            "session_id": top.get("session_id", ""),
            "user_id": top.get("user_id", ""),
            "reason": "owner has a live session (not at the deployed rev)",
        }

    return {
        "mode": "user",
        "user_id": owner_user_id,
        "reason": "owner has no live session; nothing to unicast to",
    }
