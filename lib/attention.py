"""ms-159 / e-6246 — the attention projection: which sessions need a human, now.

`beacon attention` answers the MS's core question — "which of my many sessions
are waiting on ME?" — without the human opening each terminal. Given the session
directory rows (each carrying the canonical ``state`` + ``state_since`` stamped
by e-6245), this module keeps ONLY the human-attention states and orders them by
how long they have been waiting.

The filter (slice SPEC AC4):
  * KEEP  ``awaiting_human`` — a concrete decision is pending on the human.
  * KEEP  ``blocked``        — stopped on an external cause (conditional attention).
  * KEEP  ``terminated`` **only when it failed** — a clean exit is not attention
    (state model ``np2fSUqpE5LSIkOqHLuK`` 判断1: "failed のみ注意"). Failure detail
    is not captured in this slice, so a plain ``terminated`` is folded away; the
    predicate is forward-correct for when a ``failed`` detail lands.
  * FOLD  ``running`` / ``idle`` — actively driving or resting, no attention.
  * FOLD  ``unknown`` — the server can't confirm the state; not an actionable
    "waiting for you" (AC4 lists only the three states above).

Pure functions only, so the filter + sort can be pinned without a live bus. The
CLI (both frontends) fetches the directory and hands the rows here.
"""
from __future__ import annotations

import datetime
from typing import Optional

import bus_liveness

# States that warrant surfacing a session to the human (AC4). ``terminated`` is
# handled separately (failed-only), never by membership here.
_ATTENTION_STATES = frozenset({
    bus_liveness.STATE_AWAITING_HUMAN,
    bus_liveness.STATE_BLOCKED,
})

# The failure detail on a terminated row that makes it attention-worthy. The
# field/value are not populated in this slice; kept as the single named contract
# for the follow-up that captures SessionEnd failure reasons.
_TERMINATED_DETAIL_KEYS = ("state_detail", "terminated_reason")
_FAILED_DETAIL = "failed"


def needs_attention(row: dict) -> bool:
    """Whether a directory row should surface on the attention面 (AC4)."""
    state = row.get("state") or ""
    if state in _ATTENTION_STATES:
        return True
    if state == bus_liveness.STATE_TERMINATED:
        for k in _TERMINATED_DETAIL_KEYS:
            if (row.get(k) or "") == _FAILED_DETAIL:
                return True
    return False


def _parse_iso(value) -> Optional[datetime.datetime]:
    """Parse an ISO-8601 timestamp to a tz-aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# A far-future sentinel so rows with no parseable state_since sort LAST (we can't
# tell how long they have waited, so they go below the ones we can rank).
_FAR_FUTURE = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)


def attention_sort_key(row: dict):
    """Sort key: oldest ``state_since`` first (longest wait at the top).

    Rows with an unparseable/missing ``state_since`` sort last, then by
    session_id for a stable total order (so the scale test's ordering is
    deterministic even when timestamps tie)."""
    since = _parse_iso(row.get("state_since"))
    return (since is None, since or _FAR_FUTURE, row.get("session_id") or "")


def filter_attention(rows) -> list:
    """Return the attention-worthy rows, oldest-waiting first.

    ``running`` / ``idle`` / ``unknown`` / cleanly-``terminated`` are folded out;
    the survivors are ordered by ``state_since`` ascending so the session that has
    been waiting longest is first (the human clears the most-starved work first).
    """
    items = [r for r in (rows or []) if needs_attention(r)]
    items.sort(key=attention_sort_key)
    return items


# ---------------------------------------------------------------------------
# Roster (ms-159 / e-6293) — the enriched ops面.
#
# The C+A attention面 answered "who is waiting on me". The D slice widens it to
# a *roster*: ALL of my sessions, grouped by their root target (project), each
# row showing 作業 target / state / activity / wait. 要対応 (awaiting/blocked/
# failed) is demoted from the whole view to one filter over it (方針4).
# ---------------------------------------------------------------------------

# Row ordering within a root-target group: attention-worthy states first (so a
# waiting session floats up even in the full roster), then active, then resting
# / unknown. Rows tying on state fall back to oldest-`state_since` first.
_ROSTER_STATE_ORDER = {
    bus_liveness.STATE_AWAITING_HUMAN: 0,
    bus_liveness.STATE_BLOCKED: 1,
    bus_liveness.STATE_TERMINATED: 2,
    bus_liveness.STATE_RUNNING: 3,
    bus_liveness.STATE_IDLE: 4,
    bus_liveness.STATE_UNKNOWN: 5,
}


def row_identity(row: dict) -> str:
    """The row's owner identity for scope=self matching: stamped ``user_id``
    (e-6292) or the actor email it falls back to."""
    return (row.get("user_id") or (row.get("actor") or {}).get("email") or "")


def scope_matches(row: dict, my_identity) -> bool:
    """Whether ``row`` belongs to ``my_identity``. Empty identity → match all
    (can't scope, so don't hide anything).

    Matches against BOTH the stamped ``user_id`` and the row's ``actor.email``:
    the e-6292 stamp prefers ``actor.user_id`` (a Google sub) but falls back to
    email, and the caller only reliably knows its own email — so checking both
    keeps scope=self correct whichever identity the row carries."""
    if not my_identity:
        return True
    if (row.get("user_id") or "") == my_identity:
        return True
    return ((row.get("actor") or {}).get("email") or "") == my_identity


def root_of(row: dict) -> tuple:
    """``(root_id, root_label)`` for a row's working target. Falls back to a
    stable ``("", "(no target)")`` so ungrouped rows still cluster together."""
    wt = row.get("working_target") or {}
    root = (wt.get("root") if isinstance(wt, dict) else None) or {}
    rid = root.get("id") or root.get("label") or ""
    label = root.get("label") or root.get("id") or "(no target)"
    return rid, label


def target_label(row: dict) -> str:
    """Short "kind:id" (or "(derived)"/"—") for a row's working target."""
    wt = row.get("working_target") or {}
    tgt = (wt.get("target") if isinstance(wt, dict) else None) or {}
    ident = tgt.get("id") or ""
    if not ident:
        return "—"
    kind = tgt.get("kind") or ""
    return f"{kind}:{ident}" if kind else ident


def roster_sort_key(row: dict):
    """State-priority then oldest-wait, stable by session_id."""
    order = _ROSTER_STATE_ORDER.get(row.get("state") or "", 9)
    since = _parse_iso(row.get("state_since"))
    return (order, since is None, since or _FAR_FUTURE, row.get("session_id") or "")


def filter_roster(rows, *, my_identity=None, scope="self",
                  attention_only=False, root_id=None) -> list:
    """Apply the roster's scope / 要対応 / root filters (order-independent).

    * ``scope="self"`` keeps only ``my_identity``'s rows; ``"team"`` keeps all
      (器: 方針3 — model is multi-user, default view is self).
    * ``attention_only`` keeps only 要対応 rows (the old attention面 subset).
    * ``root_id`` keeps only rows under that root target.
    """
    items = list(rows or [])
    if scope == "self":
        items = [r for r in items if scope_matches(r, my_identity)]
    if attention_only:
        items = [r for r in items if needs_attention(r)]
    if root_id:
        items = [r for r in items if root_of(r)[0] == root_id]
    return items


def group_by_root(rows) -> list:
    """Group rows by root target → ``[(root_label, [rows])]``.

    Rows within a group are ``roster_sort_key``-ordered; groups are ordered by
    their most-urgent row (so the project with a long-waiting session floats to
    the top of the roster)."""
    groups: dict = {}
    for r in rows or []:
        rid, label = root_of(r)
        groups.setdefault((rid, label), []).append(r)
    out = []
    for (_rid, label), items in groups.items():
        items.sort(key=roster_sort_key)
        out.append((label, items))
    out.sort(key=lambda g: roster_sort_key(g[1][0]))
    return out


def format_wait(state_since, now) -> str:
    """Human "how long in this state" string from ``state_since`` to ``now``.

    Returns e.g. ``3h12m`` / ``8m`` / ``just now`` / ``?`` (unparseable). Coarse
    on purpose — the attention面 wants "which has waited longest", not seconds.
    """
    since = _parse_iso(state_since)
    if since is None:
        return "?"
    secs = int((now - since).total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem = mins % 60
    if hours < 24:
        return f"{hours}h{rem:02d}m"
    days = hours // 24
    return f"{days}d{hours % 24:02d}h"
