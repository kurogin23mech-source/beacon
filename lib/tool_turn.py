"""tool_turn — turn bookkeeping for the ledger's ``turn`` record grain (ms-180 e-7014).

A tool's record grain is declared per tool, not globally (SPEC 方針3): "読んだ"
for a mailbox can fan out into twenty calls in one turn while "送った" is one call,
and there is no reason those share a grain. ``each`` needs no state. ``turn`` does
— something has to know whether this tool has already been recorded in the
current turn.

What counts as a turn
--------------------
A human prompt. ``UserPromptSubmit`` is already the turn boundary the ms-169
untrusted-turn machinery uses, and the same event bumps the epoch here. The
record hook registers for that event too, so the boundary comes from the hook
itself rather than from a second hook that may or may not be installed.

What happens when the boundary signal is missing
------------------------------------------------
An older install may have the hook wired for ``PostToolUse`` only. The epoch then
never advances, and a naive implementation would fold every call of that tool for
the rest of time into the one record it wrote first — silently recording almost
nothing, which is the exact failure class this milestone exists to remove.

So the absence is detected and the grain DEGRADES TO ``each``
(``current_epoch`` returns ``None`` → the caller records every call). The
degradation direction is deliberate: duplicates are visible and removable,
whereas missing records are invisible. Degrading toward MORE recording keeps the
failure loud.

And the degradation itself is RECORDED, not just chosen (受入条件3: 境界が取れない
状況では、取れないことが分かる形にする). "Degrades to each" written only in this
docstring would still be a silent fall to one of the two grains at runtime —
nobody reading the records could tell that a tool declared ``turn`` was actually
recording every call. ``note_degraded`` marks it and ``degraded_tools`` reads it
back, so ``beacon tool list`` can say so.
"""

from __future__ import annotations

import json
import os
from typing import Optional

TURN_FILENAME = "tool-turn.json"

#: Cap on sessions retained in the file (one entry per concurrent session).
MAX_SESSIONS = 50


def turn_path(beacon_dir: str) -> str:
    return os.path.join(str(beacon_dir), TURN_FILENAME)


def _read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: str, data: dict) -> bool:
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def begin_turn(path: str, session_key: str, at: str = "") -> int:
    """Advance ``session_key``'s turn epoch and forget what it folded. Returns the
    new epoch (0 on failure — the caller treats any write failure as "no marker",
    which degrades grain to ``each``)."""
    key = str(session_key or "").strip()
    if not key:
        return 0
    data = _read(path)
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    entry = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    epoch = int(entry.get("epoch") or 0) + 1
    sessions[key] = {"epoch": epoch, "at": at, "folded": []}
    # Keep the file bounded: drop the lowest-epoch sessions past the cap. The
    # current session is always retained because it was just written with the
    # highest epoch it has ever had.
    if len(sessions) > MAX_SESSIONS:
        ordered = sorted(sessions.items(),
                         key=lambda kv: int((kv[1] or {}).get("epoch") or 0),
                         reverse=True)
        keep = dict(ordered[:MAX_SESSIONS])
        keep[key] = sessions[key]
        sessions = keep
    data["sessions"] = sessions
    return epoch if _write(path, data) else 0


def current_epoch(path: str, session_key: str) -> Optional[int]:
    """This session's turn epoch, or ``None`` when no boundary has ever been
    observed for it.

    ``None`` is the signal that the turn boundary is not wired, and the caller
    MUST then treat a ``turn`` grain as ``each`` — see the module docstring for
    why the degradation goes in that direction."""
    key = str(session_key or "").strip()
    if not key:
        return None
    sessions = _read(path).get("sessions")
    if not isinstance(sessions, dict):
        return None
    entry = sessions.get(key)
    if not isinstance(entry, dict):
        return None
    epoch = entry.get("epoch")
    try:
        epoch = int(epoch)
    except Exception:
        return None
    return epoch if epoch > 0 else None


def claim_fold(path: str, session_key: str, tool_name: str) -> bool:
    """Claim the single ``turn``-grain record slot for ``tool_name`` this turn.

    True  → this is the turn's FIRST call of that tool; the caller records it.
    False → the slot is already taken this turn; the caller folds (records nothing)
            — or the claim could not be persisted, in which case returning False
            would silently drop the record, so a failed write returns True.

    The claim is "check and mark" in one call so two concurrent hook processes in
    the same turn cannot both read "not yet folded" and both record."""
    key = str(session_key or "").strip()
    tool = str(tool_name or "").strip()
    if not key or not tool:
        return True
    data = _read(path)
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return True
    entry = sessions.get(key)
    if not isinstance(entry, dict):
        return True
    folded = entry.get("folded")
    if not isinstance(folded, list):
        folded = []
    if tool in folded:
        return False
    folded.append(tool)
    entry["folded"] = folded
    sessions[key] = entry
    data["sessions"] = sessions
    # A write failure must not swallow the record: fall back to recording.
    return True if _write(path, data) else True


def note_degraded(path: str, tool_name: str, at: str = "") -> bool:
    """Record that ``tool_name`` was recorded at ``each`` grain even though its
    row declares ``turn``, because no turn boundary was observable.

    Kept OUTSIDE the per-session map on purpose: the condition is a property of
    the INSTALL (the UserPromptSubmit hook is not wired here), not of one
    session, and a reader asking "is my turn grain actually working" should get
    an answer without having to know which session ids to look under.

    Best-effort: a failure to note it must not disturb the record it describes.
    """
    tool = str(tool_name or "").strip()
    if not tool:
        return False
    try:
        data = _read(path)
        degraded = data.get("degraded")
        if not isinstance(degraded, dict):
            degraded = {}
        row = degraded.get(tool) if isinstance(degraded.get(tool), dict) else {}
        degraded[tool] = {"count": int(row.get("count") or 0) + 1,
                          "last_seen": at or row.get("last_seen") or ""}
        data["degraded"] = degraded
        return _write(path, data)
    except Exception:
        return False


def degraded_tools(path: str) -> list:
    """Tools whose ``turn`` grain silently became ``each``, most recent first.
    Empty when the boundary is working (or nothing has used a ``turn`` tool)."""
    degraded = _read(path).get("degraded")
    if not isinstance(degraded, dict):
        return []
    rows = [{"name": name, "count": int((v or {}).get("count") or 0),
             "last_seen": str((v or {}).get("last_seen") or "")}
            for name, v in degraded.items()
            if isinstance(v, dict) and name]
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    return rows


__all__ = ["TURN_FILENAME", "MAX_SESSIONS", "turn_path", "begin_turn",
           "current_epoch", "claim_fold", "note_degraded", "degraded_tools"]
