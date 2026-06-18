"""Trek progress-check scheduler logic (ms-83 / e-1997 / e-1998).

The Cloud Scheduler-driven loop that fires periodic "next, please"
progress-check DMs into each active Trek's leader/claim session. This
module hosts the **pure decision logic**:

  * ``is_trek_due(trek, now, default_cadence)``: decide whether a
    given trek should fire this tick, based on its cadence_minutes and
    last fired-at timestamp on trek.meta.
  * ``build_progress_check_payload(trek, project_data, now)``: render
    the structured "next, please" payload (= DM body + target task /
    commit references) from a trek's scope and the project's task
    list. Template-driven, no LLM, so unit tests can pin exact strings.

Both functions are I/O-free so the server endpoint (server/app.py) and
unit tests can exercise them without standing up Firestore.

The decision *flow* lives in server/app.py:
  1. List active treks.
  2. For each, call is_trek_due → list of due treks.
  3. For each due trek, mint a T1-system envelope, build the payload,
     post to ``trek-progress-check`` bus channel with auto-execute
     delivery, then write back ``meta.last_progress_check_at``.

That keeps this module pure (= testable) and the orchestration in one
place (= app.py).
"""

from __future__ import annotations

import datetime
from typing import Iterable, Optional

# Cadence × N rule for idle escalation (ms-83 / e-2001). Re-used here as
# the read-side default so callers don't need to import lib.trek for one
# constant.
DEFAULT_CADENCE_MINUTES = 10


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_iso(value: str) -> Optional[datetime.datetime]:
    """Parse an ISO8601 timestamp (Beacon convention = ``Z`` suffix)."""
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _ensure_utc(value: datetime.datetime) -> datetime.datetime:
    """Coerce naive datetimes to UTC so comparisons don't blow up."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# is_trek_due — pure cadence decision
# ---------------------------------------------------------------------------

def get_cadence_minutes(trek_doc: dict,
                        default: int = DEFAULT_CADENCE_MINUTES) -> int:
    """Return the trek's effective cadence (= meta override or default)."""
    meta = trek_doc.get("meta") or {}
    val = meta.get("cadence_minutes")
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def get_last_progress_check_at(trek_doc: dict) -> Optional[datetime.datetime]:
    """Return the trek's last progress-check fire time, or None if never fired."""
    meta = trek_doc.get("meta") or {}
    return _parse_iso(meta.get("last_progress_check_at", ""))


def is_trek_due(
    trek_doc: dict,
    *,
    now: datetime.datetime,
    default_cadence: int = DEFAULT_CADENCE_MINUTES,
) -> bool:
    """Decide whether this trek's cadence has elapsed.

    Rules:
      * Trek must be ``status == 'active'`` — planning / archived treks
        are never due (= the scheduler skips them).
      * ``halt`` field set (= Andon cord engaged) → not due (= the trek
        operator pulled the stop signal, do not wake sessions).
      * Never fired (= meta.last_progress_check_at missing) → due.
      * Fired before now - cadence_minutes → due.

    The decision is computed at minute resolution so a 10-minute cadence
    fires once per 10-minute window regardless of where in the window
    Cloud Scheduler happens to tick.

    Returns True iff the trek should fire on this tick.
    """
    if trek_doc.get("status") != "active":
        return False
    if trek_doc.get("halt"):
        return False
    now = _ensure_utc(now)
    cadence = get_cadence_minutes(trek_doc, default=default_cadence)
    last = get_last_progress_check_at(trek_doc)
    if last is None:
        return True
    last = _ensure_utc(last)
    elapsed = now - last
    return elapsed >= datetime.timedelta(minutes=cadence)


def select_due_treks(
    treks: Iterable[dict],
    *,
    now: datetime.datetime,
    default_cadence: int = DEFAULT_CADENCE_MINUTES,
) -> list[dict]:
    """Filter ``treks`` to the subset whose cadence has elapsed."""
    return [t for t in treks if is_trek_due(
        t, now=now, default_cadence=default_cadence,
    )]


# ---------------------------------------------------------------------------
# DM payload generation (e-1998)
# ---------------------------------------------------------------------------

# Template constants — tests pin substrings of these so accidental
# wording drift surfaces in CI rather than silently changing the
# user-visible DM body.
_PROGRESS_HEADER = "Trek 進捗確認 (= server-mint T1-system)"
_PROGRESS_EMPTY_SCOPE = (
    "Trek の scope が空です。 `beacon trek scope-add` で MS / task / "
    "Operation を追加してから次の cadence を待ってください。"
)
_PROGRESS_ALL_DONE = (
    "Trek scope 内の todo task が見当たりません。 goal_state 達成済か、 "
    "新規タスク追加を検討してください。"
)


def _collect_scope_task_ids(scope: list[dict]) -> list[str]:
    """Return the list of explicit task / milestone / op refs in scope."""
    refs: list[str] = []
    for entry in scope or []:
        for k in ("task", "milestone", "operation"):
            v = entry.get(k)
            if v:
                refs.append(v)
    return refs


def _summarize_tasks(tasks_by_status: dict) -> tuple[list[dict], list[dict]]:
    """Split a task list into (todo, done) buckets, newest-first."""
    todo = [t for t in (tasks_by_status.get("todo") or [])
            if t.get("type") == "task" or t.get("type") is None]
    done = [t for t in (tasks_by_status.get("done") or [])
            if t.get("type") == "task" or t.get("type") is None]
    return todo, done


def build_progress_check_payload(
    trek_doc: dict,
    *,
    project_data: Optional[dict] = None,
    last_commit_summary: str = "",
    now: Optional[datetime.datetime] = None,
) -> dict:
    """Render a "next, please" DM payload for one trek tick.

    Pure function: ``project_data`` is the local-mode-shaped
    ``{"entries": [...]}`` dict the caller assembled by walking the
    trek's scope. ``last_commit_summary`` is a short string like
    "abc1234 feat: foo (e-1234)" the caller pulled from git or the
    cloud commit log. ``now`` defaults to UTC now.

    Returned dict shape (= the bus event payload field):
        {
          "trek_id": "tk-...",
          "body": "<rendered DM body, with embedded entry IDs>",
          "target_entries": ["e-1994", ...],  # at least 1 when non-empty
          "kind": "trek-progress-check",
          "created_at": "<ISO8601>",
        }

    ``body`` always contains at least one trek-scope-internal entry id
    when target_entries is non-empty (= acceptance criterion 3 of e-1998).
    Empty scope / all-done cases use canonical fallback strings so the
    AI Skill can recognise the "nothing to do" case without parsing.
    """
    now = _ensure_utc(now or datetime.datetime.now(datetime.timezone.utc))
    trek_id = trek_doc.get("trek_id", "")
    scope = trek_doc.get("scope") or []
    scope_refs = _collect_scope_task_ids(scope)

    if not scope:
        body = (
            f"[{_PROGRESS_HEADER}] trek_id={trek_id}\n"
            f"{_PROGRESS_EMPTY_SCOPE}"
        )
        return _payload(trek_id, body, [], now)

    # Collect todo / done tasks from the project data the caller assembled.
    # The caller is responsible for stitching this together across multiple
    # projects when a trek scopes more than one — this function just consumes.
    todo: list[dict] = []
    done: list[dict] = []
    if project_data:
        all_entries = project_data.get("entries") or []
        by_status: dict[str, list[dict]] = {}
        for e in all_entries:
            by_status.setdefault(e.get("status", ""), []).append(e)
        todo, done = _summarize_tasks(by_status)

    # Filter to trek-scope-relevant entries. If scope has explicit task /
    # milestone / op refs, prefer those over the global todo list.
    if scope_refs:
        todo = _filter_entries_by_scope(todo, scope, scope_refs)
        done = _filter_entries_by_scope(done, scope, scope_refs)

    if not todo:
        latest_done = done[0].get("id", "") if done else ""
        body = (
            f"[{_PROGRESS_HEADER}] trek_id={trek_id}\n"
            f"{_PROGRESS_ALL_DONE}"
        )
        if latest_done:
            body += f"\n直近 done: {latest_done}"
        # Even in all-done case we surface the latest done id so the
        # receiver Skill has a known anchor entry (= AC 3 of e-1998).
        target_entries = [latest_done] if latest_done else []
        return _payload(trek_id, body, target_entries, now)

    # Happy path: at least one todo. Render up to top 3 todos to give the
    # AI an action plan without flooding the DM.
    head = todo[0]
    rest = todo[1:3]
    target_entries = [head.get("id", "")]
    lines = [
        f"[{_PROGRESS_HEADER}] trek_id={trek_id}",
        f"次やってください: {head.get('id', '')} — "
        f"{(head.get('description') or '').strip()[:120]}",
    ]
    if rest:
        rest_ids = ", ".join(t.get("id", "") for t in rest if t.get("id"))
        if rest_ids:
            lines.append(f"続きの候補: {rest_ids}")
            target_entries.extend(t.get("id", "") for t in rest if t.get("id"))
    if done:
        lines.append(f"直近 done: {done[0].get('id', '')}")
    if last_commit_summary:
        lines.append(f"最新 commit: {last_commit_summary}")
    body = "\n".join(lines)
    return _payload(trek_id, body, target_entries, now)


def _payload(trek_id: str, body: str, target_entries: list[str],
             now: datetime.datetime) -> dict:
    return {
        "trek_id": trek_id,
        "body": body,
        "target_entries": [e for e in target_entries if e],
        "kind": "trek-progress-check",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def _filter_entries_by_scope(entries: list[dict], scope: list[dict],
                             scope_refs: list[str]) -> list[dict]:
    """Keep entries whose id matches a scope ref OR whose milestone matches.

    Scope entries with ``task=e-XXXX`` directly point at one entry.
    Scope entries with ``milestone=ms-XX`` cover every task under that MS.
    Scope entries with ``operation=op-XX`` cover Operation-driven tasks.
    """
    ms_refs = {e.get("milestone") for e in scope if e.get("milestone")}
    out: list[dict] = []
    for e in entries:
        eid = e.get("id", "")
        meta = e.get("meta") or {}
        if eid and eid in scope_refs:
            out.append(e)
            continue
        # ms-scope match: entry's milestone field equals a scoped MS.
        if e.get("milestone") in ms_refs and ms_refs:
            out.append(e)
            continue
        # Some loaders nest milestone in meta — be tolerant.
        if meta.get("milestone") in ms_refs and ms_refs:
            out.append(e)
    return out
