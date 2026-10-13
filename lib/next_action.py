"""Pick the next task to work on, by priority (ms-166 e-6911).

Why this exists in code rather than in the Skill
------------------------------------------------
/beacon-session-start Step 2.9 rule 6 has always *described* how the "next
move" is chosen: a ``highest`` priority task wins, then an ``in_progress``
one, then whatever sits first in the list. Described, but never executed —
the rule lived only in Skill prose, so the choosing was done by whichever AI
happened to read the list, and in practice the list order won every time.
On 2026-10-13 that was measured end to end: ms-166 had four ``high`` tasks
among fifteen open ones and the session still opened on the one the previous
session's hand-written notes named.

A rule that only exists in a prompt is a request. This module is the same
rule as code, so the ordering is a fact a test can pin down and the Skill
asks for an answer instead of re-deriving one.

The severity order itself is NOT redefined here — it is read from
``core._PRIORITY_SCALE_ORDER``, which is the one place the five severities
are ordered. Copying the order into a second file is how the two drift.

What "no priority" means in the ordering
----------------------------------------
``untriaged`` is a sentinel meaning "a machine filed this and no human has
judged it yet" (see ``core.UNTRIAGED_PRIORITY``). It is not a severity, so it
cannot be ranked among them; it sorts after all five, alongside an entry with
no priority stored at all. That is deliberate: an un-judged task should not
outrank a task a person actually called ``low``, and leaving it last is what
makes the un-judged pile visible as the debt it is rather than quietly
seeding the top of the queue.
"""

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core  # noqa: E402

# Statuses that mean the task is finished one way or another, so it is never a
# candidate for "what should I do next".
TERMINAL_STATUSES = frozenset({"done", "cancelled"})

# Status tie-break among tasks of equal priority. Work already underway comes
# before work not yet started; anything parked (waiting on someone else, out
# for review) comes after both, because picking it up is not the session's
# call to make.
_STATUS_ORDER = {"in_progress": 0, "todo": 1}
_STATUS_ORDER_DEFAULT = 2


def priority_rank(priority: Optional[str]) -> int:
    """Rank a stored priority value. Lower sorts earlier.

    Unknown values, ``untriaged`` and a missing priority all share the rank
    just past the five severities (see the module docstring).
    """
    scale = core._PRIORITY_SCALE_ORDER
    if not priority:
        return len(scale)
    normalized = core.normalize_priority(priority)
    if normalized in scale:
        return scale.index(normalized)
    return len(scale)


def entry_priority(entry: Dict[str, Any]) -> str:
    """Read an entry's priority from either surface.

    ``priority`` is stored under ``meta`` but ``core.entries_to_json``
    surfaces it top-level as well (e-6911), so a caller may hold either
    shape depending on whether the entry came from storage or from JSON.
    Reading both means this function works on each without the caller having
    to know which one it has.
    """
    top = entry.get("priority")
    if top:
        return top
    meta = entry.get("meta")
    if isinstance(meta, dict) and meta.get("priority"):
        return meta["priority"]
    return ""


def iter_open_tasks(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collect open task entries, descending into sub-entries."""
    found: List[Dict[str, Any]] = []
    for entry in entries or []:
        if entry.get("type") == "task" and entry.get("status") not in TERMINAL_STATUSES:
            found.append(entry)
        found.extend(iter_open_tasks(entry.get("entries") or []))
    return found


def sort_key(entry: Dict[str, Any]) -> Tuple[int, int, str]:
    return (
        priority_rank(entry_priority(entry)),
        _STATUS_ORDER.get(entry.get("status", "todo"), _STATUS_ORDER_DEFAULT),
        entry.get("created_at") or "",
    )


def rank_open_tasks(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the open tasks, most-worth-doing first.

    The sort is stable, so entries that tie on all three keys keep the order
    they were stored in.
    """
    return sorted(iter_open_tasks(entries), key=sort_key)


def select_next_task(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the single next task to work on, or None when there are none."""
    ranked = rank_open_tasks(entries)
    return ranked[0] if ranked else None


def explain_choice(entry: Dict[str, Any]) -> str:
    """One line saying why this entry came first, for the Skill to print.

    The caller showing a recommendation without its grounds is how a wrong
    pick goes unchallenged, so the reason travels with the pick.
    """
    priority = entry_priority(entry)
    status = entry.get("status", "todo")
    if priority and priority != core.UNTRIAGED_PRIORITY:
        reason = "priority={0}".format(priority)
    elif priority == core.UNTRIAGED_PRIORITY:
        reason = "priority 未 triage (= 判断待ち、全 severity より後)"
    else:
        reason = "priority 未設定 (= 全 severity より後)"
    if status == "in_progress":
        reason += " / 着手済み"
    return reason
