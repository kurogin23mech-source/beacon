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
