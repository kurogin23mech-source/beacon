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

# ms-75 / e-2048 + ms-88 / e-2107 — Trek task state machine constants.
# Re-export so we can recognise terminal vs working states without dragging
# the whole lib.trek import (= keeps module pure for tests that mock that
# side). Note: ms-88 / e-2107 changed DEFAULT_TASK_STATE from `working` to
# `todo`, so we now use a dedicated `WORKING_TASK_STATE` constant to anchor
# the auto-stall TTL check (= "tasks that are actively progressing").
try:
    from lib.trek import (
        DEFAULT_TASK_STATE,  # noqa: F401  (= "todo")
        TERMINAL_TASK_STATES,  # noqa: F401  (= ("done", "user_review"))
    )
except Exception:
    DEFAULT_TASK_STATE = "todo"
    TERMINAL_TASK_STATES = ("done", "user_review")

WORKING_TASK_STATE = "working"

# ms-75 / e-2067 + ms-88 / e-2107 — server-side TTL safety net default.
# Shortened 30 → 12 min so silent halts are caught within one scheduler
# cadence (+ 2 min buffer). 罰則 = 全 working を leader_review に強制遷移
# (= server/app.py の orchestrator 経路、 ここは検出のみ)。
DEFAULT_WORKING_TTL_MINUTES = 12


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


def is_trek_task_aggregate_terminal(trek_doc: dict) -> bool:
    """Return True iff every stamped Trek task state is terminal.

    ms-88 / e-2107 — terminal = ``done`` OR ``user_review`` (per the
    5-state model and CORE doc 5nfTSmCDVUzD4SLzIhI5 § "Trek 完遂判定").
    ``todo`` / ``working`` / ``leader_review`` keep the scheduler firing
    because there is still active work or a pending leader judgment.

    A trek with **no** stamped states (= empty task_states map) is NOT
    terminal — it means "no executor has declared anything yet", and the
    scheduler should keep firing obligation DMs so the executor knows to
    act and stamp state. This preserves the existing pre-state-machine
    behaviour when no one is using the new API.

    Legacy ``waiting-review`` is migrated transparently via lib.trek
    (= maps to ``leader_review`` = non-terminal) so old data keeps the
    scheduler running until the leader makes a call.
    """
    states = trek_doc.get("task_states") or {}
    if not states:
        return False
    try:
        # Lazy import inside the call so the module stays light when
        # imported by tests that mock lib.trek.
        from lib.trek import get_task_state as _get_state
    except Exception:
        _get_state = None
    for tid, entry in states.items():
        if _get_state is not None:
            # Use the canonical getter so legacy tokens migrate.
            state = _get_state(trek_doc, tid)
        else:
            state = (entry or {}).get("state") or DEFAULT_TASK_STATE
        if state not in TERMINAL_TASK_STATES:
            return False
    return True


# ---------------------------------------------------------------------------
# Auto-stall detection (ms-75 / e-2067)
# ---------------------------------------------------------------------------

def get_working_ttl_minutes(trek_doc: dict,
                            default: int = DEFAULT_WORKING_TTL_MINUTES) -> int:
    """Return the trek's working-state TTL (= meta override or default).

    Mirror of ``lib.trek.get_working_ttl_minutes`` kept here so the
    scheduler can stay decoupled from the schema module (= same pattern
    we use for DEFAULT_TASK_STATE). Both functions must read the same
    field name; a divergence would cause the server to interpret the
    TTL one way and the CLI to render it another.
    """
    meta = trek_doc.get("meta") or {}
    val = meta.get("working_ttl_minutes")
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def detect_auto_stalled_tasks(
    trek_doc: dict,
    *,
    now: datetime.datetime,
    default_ttl: int = DEFAULT_WORKING_TTL_MINUTES,
) -> list[dict]:
    """Return the list of trek task entries that should auto-stall.

    A task qualifies iff:
      * Trek is ``status == 'active'`` and not halted (= safety nets do not
        fire while the leader has deliberately paused the trek).
      * Its current ``task_states[task_id].state`` is ``working`` (= we
        never re-stall a terminal task).
      * Its ``last_activity_at`` is older than the trek's effective TTL.
        Tasks without ``last_activity_at`` fall back to ``updated_at`` so
        legacy entries (= pre-e-2067 stamps) still get evaluated; if both
        are missing we treat the task as "never active" and skip it (= a
        Trek can't auto-stall something that was never started).

    Each returned entry is::

        {
            "task_id": str,
            "last_activity_at": str | None,
            "silence_minutes": int,
            "ttl_minutes": int,
        }

    Pure / I/O-free so unit tests pin the threshold semantics. The
    server orchestrator (server/app.py) consumes the list, transitions
    each task via lib.trek.set_task_state, and emits the leader DMs.
    """
    if trek_doc.get("status") != "active":
        return []
    if trek_doc.get("halt"):
        return []
    states = trek_doc.get("task_states") or {}
    if not states:
        return []
    ttl_minutes = get_working_ttl_minutes(trek_doc, default=default_ttl)
    threshold = datetime.timedelta(minutes=ttl_minutes)
    now = _ensure_utc(now)
    out: list[dict] = []
    for tid, entry in states.items():
        # ms-88 / e-2107: auto-stall は `working` task のみ対象 (= 旧 default
        # `working` = 「実行中」 だった意味的位置を新 `WORKING_TASK_STATE`
        # に明示移管)。 todo / leader_review / user_review は対象外。
        # legacy `waiting-review` は migrate されて leader_review 扱いに
        # なるので自然に対象外。
        if not entry or (entry or {}).get("state") != WORKING_TASK_STATE:
            continue
        # ms-95 / e-2308 — per-task TTL extension. Set by the leader via
        # ``trek.extend_task_ttl`` (CLI ``beacon trek extend-ttl``) when
        # delegating to an Agent-tool subagent that cannot stamp
        # ``last_activity_at`` itself. While the extension is in the
        # future, skip auto-stall regardless of how stale
        # ``last_activity_at`` is. Once the extension expires, normal
        # TTL semantics resume (= the leader is expected to renew or
        # let the safety net fire).
        ext_str = entry.get("ttl_extended_until") or ""
        if ext_str:
            ext = _parse_iso(ext_str)
            if ext is not None and _ensure_utc(ext) > now:
                continue
        last_str = entry.get("last_activity_at") or entry.get("updated_at") or ""
        last = _parse_iso(last_str)
        if last is None:
            # Never active and no anchor — skip; the scheduler will fire
            # progress-check obligation DMs anyway, which will eventually
            # produce a state stamp and an anchor.
            continue
        last = _ensure_utc(last)
        elapsed = now - last
        if elapsed < threshold:
            continue
        out.append({
            "task_id": tid,
            "last_activity_at": last_str,
            "silence_minutes": int(elapsed.total_seconds() // 60),
            "ttl_minutes": ttl_minutes,
        })
    return out


def build_auto_stall_note(silence_minutes: int) -> str:
    """Render the system-generated note attached to an auto-stalled task.

    Stable wording so the leader's review Skill can recognise the auto
    transition (= "did the executor stamp this or did the server?") and
    the dogfood retro can grep for the marker.
    """
    return (
        f"auto-stalled by TTL: {silence_minutes} min 無活動 "
        f"(executor が working のまま reaffirm を忘れた可能性、"
        f"leader が re-stamp working で復旧できます)"
    )


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
# ms-75 / e-2048 — Trek task state machine integration. When the aggregate
# of Trek-internal task_states reaches terminal (= all done /
# waiting-review / mixed), scheduler stops firing obligation DMs and
# instead emits a one-time "review required" summary so the leader
# sees the transition without having to poll. Once the leader records
# their review decision (= moves a task back to "working" or accepts
# the terminal state via archive), the message is not re-sent.
_AGGREGATE_ALL_DONE = (
    "Trek scope 内の全 task が done に到達しました (= AI 自律実行完了)。 "
    "leader review (= /beacon-trek-review) で archive 判断、 もしくは "
    "scope に追加 task を入れるかを決めてください。"
)
_AGGREGATE_ALL_USER_REVIEW = (
    "Trek scope 内の全 task が user_review に到達しました (= user 介入要)。 "
    "leader review (= /beacon-trek-review) で各 task の処遇 (forward-to-user の "
    "user 対話 / working 復帰) を決めてください。"
)
# 旧 name (= old aggregate output) を読む coller 用の alias、 ms-88 / e-2107
# transition で消えるが backward-compat 表面として残す (= 未読リンク防止)。
_AGGREGATE_ALL_WAITING_REVIEW = _AGGREGATE_ALL_USER_REVIEW
_AGGREGATE_ALL_TERMINAL_MIXED = (
    "Trek scope 内の全 task が terminal state (= done / user_review の混在) に "
    "到達しました。 leader review で各 task ごとに処遇を決めてください。"
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


# ---------------------------------------------------------------------------
# ms-92 / e-2164 — leader-digest payload builder.
# ---------------------------------------------------------------------------

_LEADER_DIGEST_HEADER = "Trek leader digest"
_LEADER_DIGEST_KIND = "trek-leader-digest"


def build_leader_digest_payload(
    trek_doc: dict,
    *,
    now: Optional[datetime.datetime] = None,
) -> dict:
    """Render the leader-only aggregated status snapshot for one trek tick.

    ms-92 / e-2164 — fires on the same cadence as ``trek-progress-check``
    but goes **only to the leader's session** so the leader sees "what
    is everyone doing right now" without polling each executor. The
    payload is structured (= ``sessions[]`` + ``summary`` aggregate)
    so the leader-side AI can render it deterministically without
    parsing natural language.

    Inputs come from ``trek.summarize_pulse_acks(trek_doc)`` so the
    digest stays in sync with the structured pulse-ack schema
    (= e-2165). Sessions that have never pulsed are omitted from
    ``sessions[]`` (= no false silence alarms from fresh joins).

    Returned shape (= the bus event payload field):
        {
          "kind": "trek-leader-digest",
          "trek_id": "tk-...",
          "created_at": "<ISO8601>",
          "summary": {
            "active": int,
            "stuck": int,
            "idle": int,
            "needs_leader_judgment": int,
            "total_acks_across_sessions": int,
          },
          "sessions": [
            {
              "session_id": str,
              "state_summary": str,
              "blockers": [str, ...],
              "needs_leader_judgment": bool,
              "time_on_task_seconds": int,
              "last_pulse_ack_at": ISO,
              "last_picked_choice": str,
              "total_acks": int,
            },
            ...
          ],
          "body": "<one-paragraph human-readable fallback so the
                   leader's AI can echo it to the user without going
                   through structured rendering if they choose>",
        }
    """
    # Local import to keep module-level imports light (= the scheduler
    # module is imported in lots of hot test paths and trek is a
    # comparatively heavy module).
    import trek as trek_mod

    now = _ensure_utc(now or datetime.datetime.now(datetime.timezone.utc))
    trek_id = trek_doc.get("trek_id", "")
    summary = trek_mod.summarize_pulse_acks(trek_doc)

    sessions_list: list[dict] = []
    for sid, entry in summary["sessions"].items():
        if int(entry.get("total_acks") or 0) <= 0:
            # Skip placeholders so the digest doesn't claim sessions
            # that never actually pulsed. (summarize_pulse_acks already
            # excludes them from aggregate counts but keeps them in
            # the per-session dict for completeness.)
            continue
        sessions_list.append({
            "session_id": sid,
            "state_summary": entry.get("state_summary") or "",
            "blockers": list(entry.get("blockers") or []),
            "needs_leader_judgment": bool(entry.get("needs_leader_judgment")),
            "time_on_task_seconds": int(entry.get("time_on_task_seconds") or 0),
            "last_pulse_ack_at": entry.get("last_pulse_ack_at") or "",
            "last_picked_choice": entry.get("last_picked_choice") or "",
            "total_acks": int(entry.get("total_acks") or 0),
        })
    # Sort by time_on_task descending so "longest stuck" surfaces first —
    # the most likely candidate for leader attention.
    sessions_list.sort(
        key=lambda s: s.get("time_on_task_seconds") or 0,
        reverse=True,
    )

    summary_block = {
        "active": int(summary.get("active_session_count") or 0),
        "stuck": int(summary.get("stuck_session_count") or 0),
        "idle": int(summary.get("idle_session_count") or 0),
        "needs_leader_judgment": int(
            summary.get("needs_leader_judgment_count") or 0
        ),
        "total_acks_across_sessions": int(
            summary.get("total_acks_across_sessions") or 0
        ),
    }

    # Human-readable fallback body. The leader-side AI may choose to
    # render the structured sessions[] instead, but having a body lets
    # legacy Skills / un-upgraded bridges still surface something.
    if sessions_list:
        per_session_lines = []
        for s in sessions_list[:6]:  # cap to 6 lines for readability
            tag = ""
            if s["blockers"]:
                tag = " [stuck]"
            elif "idle" in (s["state_summary"] or "").lower():
                tag = " [idle]"
            elif s["needs_leader_judgment"]:
                tag = " [needs leader]"
            per_session_lines.append(
                f"  - {s['session_id'][:8]}…{tag}: "
                f"{s['state_summary'] or '(no summary)'} "
                f"(on task {s['time_on_task_seconds']}s)"
            )
        sessions_block_str = "\n".join(per_session_lines)
    else:
        sessions_block_str = "  (まだ pulse-ack を打った session がありません)"

    body = (
        f"[{_LEADER_DIGEST_HEADER}] trek_id={trek_id}\n"
        f"active={summary_block['active']} "
        f"stuck={summary_block['stuck']} "
        f"idle={summary_block['idle']} "
        f"needs_leader_judgment={summary_block['needs_leader_judgment']}\n"
        f"{sessions_block_str}"
    )

    return {
        "kind": _LEADER_DIGEST_KIND,
        "trek_id": trek_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "summary": summary_block,
        "sessions": sessions_list,
        "body": body,
    }


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
