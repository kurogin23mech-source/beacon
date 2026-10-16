"""tool_usage_debt — tool USES that left no record (ms-180 e-7002 / 受入条件2).

The ledger deliberately does not try to enumerate every tool on the machine
(SPEC 方針6): scanning ``~/.claude/skills`` and reconciling would reproduce the
disease ``skills-drift`` already is — detecting divergence forever instead of
removing the possibility of it. Completeness is not needed, because the ledger's
promise is only "what is listed gets recorded".

But "not listed → not recorded" must not be SILENT, or the gap stops being
visible and the promise quietly covers less and less. ``PreToolUse`` observes
every call, so a gap can be noticed the moment it is used. That is what this
module stores: a small aggregate of "this tool was used and no record was
written for it".

TWO reasons a use leaves no record, kept apart (受入条件2)
---------------------------------------------------------
  ``unregistered``  — the ledger has no row for this tool at all.
  ``undecided``     — there IS a row, but nobody has judged its record sink yet.
  ``write-failed``  — the ledger said to record and the write did not happen.

Both are debts and both must be visible; a tool that is ``declined`` is NOT here,
because that is a decision, not a debt. The two reasons stay separate fields
rather than merging into "no record": the fix differs (register it vs. close the
open question), and merging them is the same collapse of *absent* and *unexamined*
the 3-valued sink exists to prevent.

The ``undecided`` half is why this module is not named after "unregistered" any
more. Listing an unjudged ROW is not enough — ``beacon tool list --undecided``
already did that. What was missing is that the unjudged row was being USED, which
is what turns an open question into a loss.

Two properties the shape enforces:

  * **Aggregated, not append-only.** One row per tool name with a count and a
    first/last timestamp. A raw log of every call would grow without bound on the
    hot path and would bury the distinct tools — the actionable unit is "which
    tools need a decision", not "how many calls happened".
  * **Observation never blocks the call.** Noticing a tool is undeclared must not
    make undeclared tools unusable (SPEC e-7002 受入条件3); the ledger is a record
    of declarations, not an allowlist. Every function here is best-effort and
    swallows its own errors, because its caller is a ``PreToolUse`` hook whose
    contract is to never brick the harness.
"""

from __future__ import annotations

import json
import os
from typing import Optional

DEBT_FILENAME = "tool-unregistered.json"

#: Cap on distinct tool names retained. A ceiling exists so a misconfigured
#: session cannot grow this file without bound; when it is hit the LEAST recently
#: seen rows are dropped, since the recent ones are the ones worth declaring.
MAX_ROWS = 500


#: Why a use left no record. Closed set — a row with any other reason is a bug,
#: not a new category to interpret at read time.
REASON_UNREGISTERED = "unregistered"
REASON_UNDECIDED = "undecided"
#: The ledger DID say to record and the write still did not happen (no worked
#: Target, a class with no evidence arm, the CLI unreachable…). Its own reason,
#: because collapsing it into silence hides the one case where the ledger's
#: promise — "what is listed gets recorded" — was actually broken. Found by an
#: independent philosophy review of PR #802: the hook ran the writer with its
#: output discarded and never looked at the exit code, so "decided not to write"
#: and "could not write" were the same nothing.
REASON_WRITE_FAILED = "write-failed"
REASONS = (REASON_UNREGISTERED, REASON_UNDECIDED, REASON_WRITE_FAILED)


def debt_path(beacon_dir: str) -> str:
    return os.path.join(str(beacon_dir), DEBT_FILENAME)


def load_debt(path: str) -> list:
    """Rows ordered most-recently-seen first. Unreadable file → no rows.

    Tolerant like the ledger itself: a corrupt debt file must read as "nothing
    recorded", never as wrong rows."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    rows = data.get("unregistered") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    clean = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        # Rows written before the reason field existed were all unregistered —
        # that was the only case recorded then. Defaulting keeps an older file
        # readable instead of dropping its rows (which would read as "no debt").
        if row.get("reason") not in REASONS:
            row = dict(row, reason=REASON_UNREGISTERED)
        clean.append(row)
    clean.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    return clean


def debt_by_reason(rows: list, reason: str) -> list:
    return [r for r in rows if r.get("reason") == reason]


def record_no_record_use(path: str, name: str, *, reason: str,
                         at: str = "", detail: str = "") -> Optional[dict]:
    """Note one use of a tool that left no record. Returns the updated row, or
    ``None`` when nothing was written (empty name, bad reason, or I/O failure).

    Best-effort and never raises: the caller is a tool-call interrupt, and a
    failure to note the debt must not disturb the call it observed.

    A tool whose reason CHANGES (it gets registered, so later uses are
    ``undecided`` rather than ``unregistered``) keeps one row and the latest
    reason, because the row's job is "what needs doing now".
    """
    tool = str(name or "").strip()
    if not tool or reason not in REASONS:
        return None
    try:
        rows = load_debt(path)
        by_name = {str(r.get("name")): r for r in rows}
        row = by_name.get(tool)
        if row is None:
            row = {"name": tool, "reason": reason, "count": 0,
                   "first_seen": at, "last_seen": at}
            rows.append(row)
        row["reason"] = reason
        if detail:
            row["detail"] = detail
        elif "detail" in row:
            del row["detail"]  # a later use with no detail must not keep a stale one
        row["count"] = int(row.get("count") or 0) + 1
        row["last_seen"] = at or row.get("last_seen") or ""
        if not row.get("first_seen"):
            row["first_seen"] = row["last_seen"]
        rows.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)
        _write(path, rows[:MAX_ROWS])
        return row
    except Exception:
        return None


def clear_debt(path: str) -> int:
    """Drop every row; return how many were dropped."""
    rows = load_debt(path)
    try:
        _write(path, [])
    except Exception:
        return 0
    return len(rows)


def _write(path: str, rows: list) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"unregistered": rows}, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def record_unregistered(path: str, name: str, at: str = "") -> Optional[dict]:
    """Back-compat alias for the ``unregistered`` reason."""
    return record_no_record_use(path, name, reason=REASON_UNREGISTERED, at=at)


__all__ = ["DEBT_FILENAME", "MAX_ROWS", "REASON_UNREGISTERED",
           "REASON_UNDECIDED", "REASON_WRITE_FAILED", "REASONS",
           "debt_path", "load_debt",
           "debt_by_reason", "record_no_record_use", "record_unregistered",
           "clear_debt"]
