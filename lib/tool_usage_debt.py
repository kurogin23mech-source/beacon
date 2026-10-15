"""tool_usage_debt — tools that were USED without being declared (ms-180 e-7002).

The ledger deliberately does not try to enumerate every tool on the machine
(SPEC 方針6): scanning ``~/.claude/skills`` and reconciling would reproduce the
disease ``skills-drift`` already is — detecting divergence forever instead of
removing the possibility of it. Completeness is not needed, because the ledger's
promise is only "what is listed gets recorded".

But "not listed → not recorded" must not be SILENT, or the gap stops being
visible and the promise quietly covers less and less. ``PreToolUse`` observes
every call, so an unregistered tool can be noticed the moment it is used. That is
what this module stores: a small aggregate of "this tool was used and the ledger
had nothing to say about it".

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
    clean = [r for r in rows if isinstance(r, dict) and r.get("name")]
    clean.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    return clean


def record_unregistered(path: str, name: str, at: str = "") -> Optional[dict]:
    """Note one use of an undeclared tool. Returns the updated row, or ``None``
    when nothing was written (empty name, or any I/O failure).

    Best-effort and never raises: the caller is the ``PreToolUse`` gate, and a
    failure to note the debt must not disturb the tool call it observed."""
    tool = str(name or "").strip()
    if not tool:
        return None
    try:
        rows = load_debt(path)
        by_name = {str(r.get("name")): r for r in rows}
        row = by_name.get(tool)
        if row is None:
            row = {"name": tool, "count": 0, "first_seen": at, "last_seen": at}
            rows.append(row)
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


__all__ = ["DEBT_FILENAME", "MAX_ROWS", "debt_path", "load_debt",
           "record_unregistered", "clear_debt"]
