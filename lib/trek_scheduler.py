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
# Idle detection (ms-83 / e-2001)
# ---------------------------------------------------------------------------

# Multiplier on cadence to decide "claim session is silent". cadence × 3
# (= 30 minutes for the default 10-minute cadence) gives the session
# three full cadence windows to recover before user escalation, which
# matches the SPEC § 設計方針 5 / 受入条件 7 wording (= "cadence の 3 倍").
IDLE_CADENCE_MULTIPLIER = 3

# Avoid flooding the user when the same trek stays idle across many
# scheduler ticks. We re-fire an escalation DM at most once per this
# many minutes — the SPEC doesn't pin a number, so we pick 30 minutes
# (= one default cadence × 3 window) which balances "user notices"
# against "user is buried".
ESCALATION_REFIRE_COOLDOWN_MINUTES = 30


def get_last_session_response_at(
    trek_doc: dict,
) -> Optional[datetime.datetime]:
    """Return the trek's last session-response time, or None.

    Stamped by ``POST /api/treks/{id}/session-heartbeat`` (= the AI side
    pings the server after completing each tick) and by bus-event
    handlers when a message from the leader session lands.
    """
    meta = trek_doc.get("meta") or {}
    return _parse_iso(meta.get("last_session_response_at", ""))


def get_last_idle_escalation_at(
    trek_doc: dict,
) -> Optional[datetime.datetime]:
    """Return the trek's last idle-escalation fire time, or None."""
    meta = trek_doc.get("meta") or {}
    return _parse_iso(meta.get("last_idle_escalation_at", ""))


def is_trek_idle(
    trek_doc: dict,
    *,
    now: datetime.datetime,
    default_cadence: int = DEFAULT_CADENCE_MINUTES,
    multiplier: int = IDLE_CADENCE_MULTIPLIER,
) -> bool:
    """Decide whether this trek's claim session is idle (= silent N×cadence).

    Rules:
      * Only ``status == 'active'`` treks can be idle (= planning /
        archived treks aren't expected to respond).
      * ``halt`` set → not idle (= leader pulled the cord deliberately).
      * If ``last_session_response_at`` is unset, fall back to
        ``last_progress_check_at`` as the activity anchor — the first
        progress check itself counts as "we know the session is alive"
        when the AI side hasn't heartbeated yet. If both are unset, the
        trek has never been pinged, which is NOT idle (the next tick
        will fire the first progress check and start the clock).
      * Idle iff now - last_activity >= cadence * multiplier.

    The decision is pure so unit tests pin it without HTTP.
    """
    if trek_doc.get("status") != "active":
        return False
    if trek_doc.get("halt"):
        return False
    cadence = get_cadence_minutes(trek_doc, default=default_cadence)
    threshold = datetime.timedelta(minutes=cadence * multiplier)
    last_response = get_last_session_response_at(trek_doc)
    last_check = get_last_progress_check_at(trek_doc)
    last_activity = last_response or last_check
    if last_activity is None:
        return False
    now = _ensure_utc(now)
    last_activity = _ensure_utc(last_activity)
    return (now - last_activity) >= threshold


def should_fire_idle_escalation(
    trek_doc: dict,
    *,
    now: datetime.datetime,
    default_cadence: int = DEFAULT_CADENCE_MINUTES,
    multiplier: int = IDLE_CADENCE_MULTIPLIER,
    refire_cooldown_minutes: int = ESCALATION_REFIRE_COOLDOWN_MINUTES,
) -> bool:
    """Wrap is_trek_idle with refire-cooldown check.

    Avoids flooding the user with the same escalation DM every tick.
    Returns True only when the trek is idle AND we haven't fired an
    escalation for it within the last ``refire_cooldown_minutes``.
    """
    if not is_trek_idle(trek_doc, now=now, default_cadence=default_cadence,
                        multiplier=multiplier):
        return False
    last_fire = get_last_idle_escalation_at(trek_doc)
    if last_fire is None:
        return True
    now = _ensure_utc(now)
    last_fire = _ensure_utc(last_fire)
    return (now - last_fire) >= datetime.timedelta(
        minutes=refire_cooldown_minutes,
    )


def build_idle_escalation_payload(
    trek_doc: dict,
    *,
    now: Optional[datetime.datetime] = None,
    default_cadence: int = DEFAULT_CADENCE_MINUTES,
) -> dict:
    """Render the idle-escalation DM payload (ms-83 / e-2001).

    Pure function. The notify channel posts this so the user sees
    "trek X session Y が N 分 idle、 要確認" without needing to query
    further state. Numbers are computed against the same activity anchor
    is_trek_idle uses (= last_session_response_at or last_progress_check_at).
    """
    now = _ensure_utc(now or datetime.datetime.now(datetime.timezone.utc))
    trek_id = trek_doc.get("trek_id", "")
    leader_session = trek_doc.get("leader_session_id", "")
    cadence = get_cadence_minutes(trek_doc, default=default_cadence)
    last_response = get_last_session_response_at(trek_doc)
    last_check = get_last_progress_check_at(trek_doc)
    last_activity = last_response or last_check
    if last_activity is not None:
        last_activity = _ensure_utc(last_activity)
        elapsed_min = int((now - last_activity).total_seconds() // 60)
    else:
        elapsed_min = -1  # never pinged — should not normally reach here
    body = (
        f"[Trek 自律実行 idle 警告] trek={trek_id} "
        f"leader_session={leader_session} が {elapsed_min} 分間 idle "
        f"(cadence={cadence}分の {IDLE_CADENCE_MULTIPLIER} 倍超)。 "
        f"session 状態を確認してください。"
    )
    return {
        "trek_id": trek_id,
        "leader_session_id": leader_session,
        "kind": "trek-idle-escalation",
        "body": body,
        "idle_minutes": elapsed_min,
        "cadence_minutes": cadence,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


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
# ms-83 / e-2013: 自律権限の reminder。 受信側の /beacon-trek-execute Skill
# は判断境界 protocol を持つが、 scheduler 経由の周期 DM 本体にも明示することで
# 「scope 内は事前承認、 判断境界は leader DM で AI 間決定、 user 停止は限定的」
# という権限の輪郭を毎 fire ごとに想起させる (= protocol drift 構造的防止)。
_AUTONOMY_REMINDER = (
    "\n\n[Trek 自律権限の再確認] Trek scope 内 action は事前承認 (= user 確認不要)。 "
    "判断境界は leader DM 経路で AI 間決定する (= /beacon-trek-execute Skill の判断境界 "
    "protocol 参照)。 user 停止は 不可逆操作 / 嗜好 / cross-Trek 副作用 / 権限・secret / "
    "scope 外 のみに限定。"
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
        "body": body + _AUTONOMY_REMINDER,
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
