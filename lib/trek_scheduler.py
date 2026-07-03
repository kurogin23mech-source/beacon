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
# ms-97 / e-2667 — Dual-form lazy import helper for Cloud Run flat layout.
#
# Cloud Run uses WORKDIR=/app/server + PYTHONPATH=/app/lib:/app/server, so
# ``lib`` is NOT a package on the path — modules under lib/ are reachable
# only via their bare names (= ``import trek``, matching server/app.py:33
# ``import trek as trek_mod``). Local pytest / dev runs typically have the
# repo root on sys.path instead, where ``from lib.trek import ...`` works.
#
# Pre-fix, every lazy import site tried only ``from lib.trek import ...``,
# which silently fell back to ``None`` on Cloud Run. That broke
# ``should_fire_executor_tick`` so every executor with no unclaim todo
# (= the common steady-state) got skipped from executor_targets, which is
# the real root cause behind the dogfood "全 tick で executor skip" symptom.
#
# This helper centralises the dual-form fallback so the four call sites
# below stay short and the fallback order is identical everywhere.
# ---------------------------------------------------------------------------

def _import_trek():
    """Import the lib.trek module under both flat (Cloud Run) and package layouts.

    Returns the trek module on success, or ``None`` if neither import
    path works (= a genuinely broken deploy; callers should fall back
    to their conservative "no info" branch).
    """
    try:
        import trek as _t  # type: ignore[import-not-found]
        return _t
    except ImportError:
        pass
    try:
        from lib import trek as _t  # type: ignore[no-redef]
        return _t
    except ImportError:
        return None


# ms-75 / e-2048 + ms-88 / e-2107 — Trek task state machine constants.
# Re-export so we can recognise terminal vs working states without dragging
# the whole lib.trek import (= keeps module pure for tests that mock that
# side). Note: ms-88 / e-2107 changed DEFAULT_TASK_STATE from `working` to
# `todo`, so we now use a dedicated `WORKING_TASK_STATE` constant to anchor
# the auto-stall TTL check (= "tasks that are actively progressing").
_trek_for_constants = _import_trek()
if _trek_for_constants is not None:
    try:
        DEFAULT_TASK_STATE = _trek_for_constants.DEFAULT_TASK_STATE  # (= "todo")
        TERMINAL_TASK_STATES = _trek_for_constants.TERMINAL_TASK_STATES  # (= ("done", "user_review"))
    except AttributeError:
        DEFAULT_TASK_STATE = "todo"
        TERMINAL_TASK_STATES = ("done", "user_review")
else:
    DEFAULT_TASK_STATE = "todo"
    TERMINAL_TASK_STATES = ("done", "user_review")
del _trek_for_constants

WORKING_TASK_STATE = "working"

# ms-75 / e-2067 + ms-88 / e-2107 + ms-95 / e-2646 — server-side TTL safety
# net default. **24h (= 1440 min)** baseline (was 12 min, was 30 min).
#
# 2026-06-28 dogfood で 12 min は **短すぎ** が確定: prep 待機中の executor
# (= 例: staging URL を待っている LPS / 別 session のレビュー待ち) を
# 「stuck」 と誤判定して working → leader_review に勝手に flip し、 executor
# の attribution を奪う病理が観察された (= e-2646 / dogfood findings memo
# `e70cUf8IS5uEIS1HIEXt` § #14 / #19 連鎖)。
#
# 24h default = 「待機」 を「stuck」 と誤判定する確率を実用上ゼロに、 かつ
# 完全 silent halt (= 翌日になっても無反応) は依然 catch する境界。 個別
# Trek は ``trek_doc.meta.stall_threshold_minutes`` で短い override 可能
# (= per-Trek の急ぎ事情に合わせる経路、 既存の `working_ttl_minutes`
# field と互換)。 「prep 待機中」 を明示的に表現したい時は
# ``task_states[*].meta.working_pause_until`` (ISO8601) で「この時刻まで
# stall 判定をスキップ」 と marker を立てる経路を別途用意 (= e-2646)。
DEFAULT_WORKING_TTL_MINUTES = 1440


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
    # Lazy import inside the call so the module stays light when imported
    # by tests that mock lib.trek. ms-97 / e-2667 — dual-form fallback so
    # Cloud Run's flat layout (PYTHONPATH=/app/lib:/app/server) also resolves.
    _trek = _import_trek()
    _get_state = getattr(_trek, "get_task_state", None) if _trek else None
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
# Per-executor / per-leader lazy start (ms-97 / e-2613, AC33)
#
# Pre-e-2613 the scheduler fired ticks at every cadence window for every
# live session of every member, leaving only the global aggregate-terminal
# quiesce (= ms-88 / e-2107) and per-session terminal-claim filter
# (= ms-88 / e-2109) as silence gates. AC33 narrows further:
#
#   * Per-executor tick fires iff the executor "has >=1 active claim
#     slot" (= todo / working stamped to this session_id) OR "has unclaim
#     todo in scope" (= there exist todo entries with no
#     updated_by_session_id stamp, which the executor could pick up).
#     Stop: all claim slots terminal AND no unclaim todo float.
#
#   * Per-leader (= leader-digest) tick fires iff "leader_review queue
#     non-empty" OR "todo float exists" OR "completion imminent (=
#     within COMPLETION_IMMINENT_SLOTS slots of all-terminal)" OR
#     "abnormal executor (= leader_review queue counted above already)".
#     Stop: all slots terminal.
#
#   * Even when all gates close, a single MINIMAL tick still fires per
#     cadence window (= no complete silence). This is the broadcast
#     fallback the existing fanout uses for sessions without claims;
#     leader-digest gets the same minimal-tick treatment.
# ---------------------------------------------------------------------------

COMPLETION_IMMINENT_SLOTS = 2


def _task_state_of(trek_doc: dict, task_id: str, entry: dict) -> str:
    """Best-effort state getter for a task_states entry.

    Wraps the lib.trek.get_task_state lazy import so callers do not need
    to retry on each task. ``entry`` is the raw map; fallback to its
    ``state`` field when the canonical getter is unavailable.
    """
    # ms-97 / e-2667 — dual-form lazy import so Cloud Run's flat layout
    # (PYTHONPATH=/app/lib:/app/server) also resolves the canonical getter.
    _trek = _import_trek()
    _get_state = getattr(_trek, "get_task_state", None) if _trek else None
    if _get_state is not None:
        return _get_state(trek_doc, task_id)
    return (entry or {}).get("state") or DEFAULT_TASK_STATE


def has_unclaim_todo(trek_doc: dict) -> bool:
    """Return True iff at least one ``task_states`` entry is in ``todo``
    state without a stamped owner (= fresh todo waiting for an executor).

    ms-97 / e-2613 (AC33) — used by the per-executor lazy-start decision.
    An executor with no active claim still gets a tick if there's an
    unclaim todo to pick up; otherwise they stay quiet.
    """
    states = trek_doc.get("task_states") or {}
    for tid, entry in states.items():
        state = _task_state_of(trek_doc, tid, entry or {})
        if state != DEFAULT_TASK_STATE:
            continue
        owner = (entry or {}).get("updated_by_session_id") or ""
        if not owner:
            return True
    return False


def has_leader_review_queue(trek_doc: dict) -> bool:
    """Return True iff at least one task is in ``leader_review`` state.

    ms-97 / e-2613 (AC33) — leader-digest condition. ``leader_review`` is
    the "leader judgment requested" slot; while any exist, the leader's
    digest must keep firing so they can act.
    """
    states = trek_doc.get("task_states") or {}
    for tid, entry in states.items():
        state = _task_state_of(trek_doc, tid, entry or {})
        if state == "leader_review":
            return True
    return False


def has_todo_float(trek_doc: dict) -> bool:
    """Return True iff at least one task is in ``todo`` state (claimed or not).

    ms-97 / e-2613 (AC33) — leader-digest condition. While todo float
    exists, leader visibility is useful (= "what's unclaimed / waiting").
    """
    states = trek_doc.get("task_states") or {}
    for tid, entry in states.items():
        state = _task_state_of(trek_doc, tid, entry or {})
        if state == DEFAULT_TASK_STATE:
            return True
    return False


def is_completion_imminent(
    trek_doc: dict,
    *,
    threshold: int = COMPLETION_IMMINENT_SLOTS,
) -> bool:
    """Return True iff the trek is within ``threshold`` non-terminal slots
    of full aggregate-terminal completion.

    ms-97 / e-2613 (AC33) — leader-digest condition. When most slots are
    done and only a couple remain, the leader benefits from seeing the
    finish line approach (= "ship soon, decide last few"). For a trek
    with N=0 non-terminal slots ``is_trek_task_aggregate_terminal``
    already triggers, so this helper is only meaningful for 1..N case.

    A trek with no stamped states returns False (= "no signal").
    """
    states = trek_doc.get("task_states") or {}
    if not states:
        return False
    non_terminal = 0
    for tid, entry in states.items():
        state = _task_state_of(trek_doc, tid, entry or {})
        if state not in TERMINAL_TASK_STATES:
            non_terminal += 1
            if non_terminal > threshold:
                return False
    # 1..threshold non-terminal slots means imminent. 0 means already
    # terminal (= caller should use the dedicated terminal predicate).
    return 0 < non_terminal <= threshold


def should_fire_executor_tick(
    trek_doc: dict,
    *,
    session_id: str,
) -> bool:
    """Lazy-start decision for one executor's progress-check tick.

    ms-97 / e-2613 (AC33) — returns True iff the executor either:
      * has at least one active claim slot in this trek (= a stamped
        todo / working entry pointing to this session_id), or
      * the trek has an unclaim todo waiting to be picked up (= fresh
        executor with empty claim history, but real work to do).

    Returns False when the executor has only terminal claims AND there
    is no unclaim todo float (= "nothing to do here, stay quiet"). The
    server-side orchestrator falls back to a single minimal broadcast
    when EVERY executor returns False, preserving the SPEC's
    "no complete silence" rule.

    Pure / I/O-free so unit tests can pin the matrix without standing
    up an HTTP layer. Wraps ``session_has_active_claim`` from lib.trek
    + the ``has_unclaim_todo`` helper above.
    """
    # ms-97 / e-2667 — dual-form lazy import. The pre-fix code only tried
    # ``from lib.trek import ...`` which silent-failed on Cloud Run (flat
    # layout: PYTHONPATH=/app/lib:/app/server, ``lib`` is not a package),
    # so both helpers fell back to None. That made the function always
    # short-circuit to has_unclaim_todo(trek_doc) only, which returns False
    # for any trek with no fresh todo entries — and that skipped LPS / PE
    # from executor_targets every tick (= dogfood 不着 root cause).
    _trek = _import_trek()
    _has_active = getattr(_trek, "session_has_active_claim", None) if _trek else None
    _has_any = getattr(_trek, "session_has_any_claim", None) if _trek else None
    if _has_active is not None and _has_active(trek_doc, session_id=session_id):
        return True
    # A fresh executor with NO claims at all may still pick up an unclaim
    # todo. If neither condition holds the executor genuinely has nothing
    # to do; skip.
    if _has_any is not None and _has_any(trek_doc, session_id=session_id):
        # Has claims, but none active → all terminal. Only fire if there
        # is unclaim todo float to pick up next.
        return has_unclaim_todo(trek_doc)
    # No claims yet (= fresh executor). Fire only if there's something to
    # claim — otherwise they're idle by design.
    return has_unclaim_todo(trek_doc)


def should_fire_leader_tick(trek_doc: dict) -> bool:
    """Lazy-start decision for the leader-digest tick.

    ms-97 / e-2613 (AC33) — fires when any of:
      * leader_review queue non-empty (= leader has decisions to make)
      * todo float exists (= unclaimed / queued work to visualise)
      * completion imminent (= within COMPLETION_IMMINENT_SLOTS of all-
        terminal, leader benefits from seeing the finish line)

    Returns False otherwise. Stop condition (= all slots terminal) is
    handled by the existing ``is_trek_task_aggregate_terminal`` quiesce
    path; this helper assumes the caller already filtered out fully-
    terminal treks.

    A trek with empty task_states returns False — there is no state for
    the leader to digest yet. The orchestrator's minimal-tick fallback
    keeps one event per cadence window so the leader can still observe
    "nothing has happened" through the digest channel.
    """
    if has_leader_review_queue(trek_doc):
        return True
    if has_todo_float(trek_doc):
        return True
    if is_completion_imminent(trek_doc):
        return True
    # Working-only treks (= all non-terminal slots in `working`) are
    # currently considered "executor's job, no leader action needed".
    # The auto-stall safety net (= ms-75 / e-2067, server-side) catches
    # silent halts by transitioning stuck `working` to `leader_review`,
    # which re-fires this gate via has_leader_review_queue.
    return False


# ---------------------------------------------------------------------------
# Completion-ready signal (ms-97 / Phase 7-A, AC20)
# ---------------------------------------------------------------------------

def _has_op_slot(trek_doc: dict) -> bool:
    """Return True iff any scope entry narrows to an Operation (= ``operation`` key set).

    ms-97 / AC20 — Op slot 入りの trek は completion_ready を発火しない。
    Op (= 定期 / 自動運転 task) は trek 完遂条件の外側で動くため、
    別 SPEC が決まるまで本シグナル経路を suppress する。
    """
    for entry in trek_doc.get("scope") or []:
        if isinstance(entry, dict) and entry.get("operation"):
            return True
    return False


def is_completion_ready(trek_doc: dict) -> bool:
    """Return True iff this trek is ready to fire a one-shot completion_ready marker.

    ms-97 / Phase 7-A / AC20 — fires ONCE per trek when:

      * every stamped ``task_states`` entry is in a terminal state
        (= ``done`` or ``user_review`` — same set as
        ``is_trek_task_aggregate_terminal``), AND
      * the scope contains NO Operation slot (= per AC20 footnote,
        Op-bearing treks defer this signal pending separate SPEC), AND
      * ``meta.completion_notified_at`` is still unstamped (= ``None``
        / absent — i.e. the marker has not already been fanned out).

    Empty ``task_states`` returns False, mirroring
    ``is_trek_task_aggregate_terminal``.

    Pure / I/O-free. The server endpoint pairs this with the idempotent
    stamp; the test surface pins the matrix without a server.
    """
    if _has_op_slot(trek_doc):
        return False
    meta = trek_doc.get("meta") or {}
    if meta.get("completion_notified_at"):
        return False
    return is_trek_task_aggregate_terminal(trek_doc)


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

    ms-95 / e-2646 — both ``working_ttl_minutes`` (= legacy field name)
    and ``stall_threshold_minutes`` (= new name introduced with the
    24h default) are accepted. The new name reads first so callers
    that adopt the new field win, but old data + tests using the old
    name keep working unchanged (= no migration required).
    """
    meta = trek_doc.get("meta") or {}
    # New field name takes precedence (= explicit user override for the
    # 24h-default era).
    val = meta.get("stall_threshold_minutes")
    if val is None:
        val = meta.get("working_ttl_minutes")
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _task_entry_is_paused(entry: dict, now: datetime.datetime) -> bool:
    """ms-95 / e-2646 — return True iff a task_states entry is marked as
    intentionally paused (= prep waiting, not stuck).

    Two equivalent markers are accepted:

      * ``entry.meta.working_pause_until`` (ISO8601) — pause expires at
        that timestamp; before it, stall check is skipped.
      * ``entry.meta.working_paused`` truthy — explicit boolean marker,
        useful for "pause indefinitely until I un-pause" flows.

    Both live under ``entry["meta"]`` to keep the entry's top-level
    shape (state / updated_at / updated_by_session_id / note /
    last_activity_at) stable. The detector treats either as "skip stall
    for this task this tick"; the caller (server tick endpoint) treats
    the task as still actively-claimed even though no recent activity.
    """
    if not entry:
        return False
    meta = entry.get("meta") or {}
    if meta.get("working_paused"):
        return True
    pause_str = meta.get("working_pause_until") or ""
    if not pause_str:
        return False
    pause_until = _parse_iso(pause_str)
    if pause_until is None:
        return False
    return _ensure_utc(pause_until) > _ensure_utc(now)


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
        # ms-95 / e-2646 — prep 待機 marker。 executor が「意図的に保留中」
        # と明示している間は stall 判定をスキップ (= 24h default の
        # threshold より精細な per-task pause primitive)。 staging URL
        # 受領待ち / 別 session のレビュー待ちなど、 dogfood で attribution
        # を奪われる病理の構造的対策。
        if _task_entry_is_paused(entry, now):
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


def build_task_state_aggregate(trek_doc: dict) -> dict:
    """Aggregate ``trek_doc.task_states`` for the leader-digest payload (ms-97 / e-2707).

    Surfaces AC10 precedence (= ``leader_review`` / ``done`` / ``user_review``
    / ``working`` / ``todo``) so the leader can see at a glance "どこに
    判断要請があるか / どこが詰まっているか" without polling each session.
    The legacy pulse-ack derived ``needs_leader_judgment`` flag is
    executor-self-report (= updated only when an executor remembers to
    pulse-ack with the flag), so it can sit at 0 while task_states[*].state
    has flipped to ``leader_review``. This aggregate closes that blind spot.

    Returned shape:
        {
          "counts": {
            "leader_review": int, "done": int, "user_review": int,
            "working": int, "todo": int,
          },
          "leader_review_queue": [
            {
              "task_id": str,
              "updated_by_session_id": str,
              "updated_at": ISO8601,
              "note": str,
            },
            ...
          ],
          "overall_state": str,   # one of compute_ms_slot_state's outputs
        }

    Pure / I/O-free so unit tests can pin the matrix without standing up
    Firestore. Uses ``compute_ms_slot_state`` from lib.trek (via
    ``_import_trek``) for the overall_state derivation; if the trek module
    cannot be resolved, the helper falls back to the same precedence rule
    locally so the payload still ships a valid token.
    """
    states = trek_doc.get("task_states") or {}
    counts = {
        "leader_review": 0,
        "done": 0,
        "user_review": 0,
        "working": 0,
        "todo": 0,
    }
    leader_review_queue: list[dict] = []
    # Build the children list for compute_ms_slot_state in one pass.
    children: list[dict] = []
    for tid, entry in states.items():
        entry = entry or {}
        state = _task_state_of(trek_doc, tid, entry)
        if state not in counts:
            # Unknown / malformed → collapse to todo for counting (mirrors
            # compute_ms_slot_state's defensive cast).
            state = "todo"
        counts[state] += 1
        children.append({"state": state})
        if state == "leader_review":
            leader_review_queue.append({
                "task_id": tid,
                "updated_by_session_id": entry.get("updated_by_session_id") or "",
                "updated_at": entry.get("updated_at") or "",
                "note": (entry.get("note") or "")[:500],
            })
    # Stable ordering: oldest leader_review first so the leader's eye lands
    # on the one that has been waiting longest (= same intent as the
    # sessions[] sort by time_on_task desc).
    leader_review_queue.sort(key=lambda r: r.get("updated_at") or "")

    _trek = _import_trek()
    _compute = getattr(_trek, "compute_ms_slot_state", None) if _trek else None
    if _compute is not None and children:
        overall_state = _compute(children)
    elif children:
        # Local fallback mirrors lib.trek.compute_ms_slot_state precedence.
        cs = [c["state"] for c in children]
        if "leader_review" in cs:
            overall_state = "leader_review"
        elif all(s == "done" for s in cs):
            overall_state = "done"
        elif all(s in ("done", "user_review") for s in cs):
            overall_state = "user_review"
        elif "working" in cs:
            overall_state = "working"
        else:
            overall_state = "todo"
    else:
        overall_state = "todo"

    return {
        "counts": counts,
        "leader_review_queue": leader_review_queue,
        "overall_state": overall_state,
    }


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
    # comparatively heavy module). ms-97 / e-2667 — dual-form so both
    # Cloud Run flat layout and local package layout resolve.
    trek_mod = _import_trek()
    if trek_mod is None or not hasattr(trek_mod, "summarize_pulse_acks"):
        # Defensive: with no trek module we can't render a real digest.
        # Return an empty-but-well-shaped payload so callers do not crash.
        now = _ensure_utc(now or datetime.datetime.now(datetime.timezone.utc))
        # ms-97 / e-2707 — even on the fallback path, surface the
        # task_state aggregate so the leader-digest is never silent about
        # the leader_review queue (= the legacy needs_leader_judgment is
        # pulse-ack derived and can lag behind real state).
        task_state_aggregate = build_task_state_aggregate(trek_doc)
        return {
            "kind": _LEADER_DIGEST_KIND,
            "trek_id": trek_doc.get("trek_id", ""),
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "summary": {
                "active": 0, "stuck": 0, "idle": 0,
                "needs_leader_judgment": 0,
                "leader_review_queue_count": len(
                    task_state_aggregate["leader_review_queue"]
                ),
                "total_acks_across_sessions": 0,
            },
            "sessions": [],
            "task_state_aggregate": task_state_aggregate,
            "body": (
                f"[{_LEADER_DIGEST_HEADER}] trek_id={trek_doc.get('trek_id', '')}\n"
                "  (trek module unavailable — digest skipped this tick)"
            ),
        }

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

    # ms-97 / e-2707 — task_state aggregate (= AC10 precedence + leader_review
    # queue list). Surface BEFORE summary_block so the count field below
    # can read it without recomputing.
    task_state_aggregate = build_task_state_aggregate(trek_doc)

    summary_block = {
        "active": int(summary.get("active_session_count") or 0),
        "stuck": int(summary.get("stuck_session_count") or 0),
        "idle": int(summary.get("idle_session_count") or 0),
        "needs_leader_judgment": int(
            summary.get("needs_leader_judgment_count") or 0
        ),
        # ms-97 / e-2707 — task_state-derived parallel to needs_leader_judgment.
        # needs_leader_judgment is pulse-ack self-report (= executor remembers
        # to flag it); leader_review_queue_count is structural (= counted
        # straight from task_states.*.state). The two answer different
        # questions: "did executor wave a hand?" vs "did anyone flip the slot
        # to leader_review?". Leaders read whichever is non-zero.
        "leader_review_queue_count": len(
            task_state_aggregate["leader_review_queue"]
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

    # ms-97 / e-2707 — leader_review_queue line is emitted only when the
    # queue is non-empty so a clean digest stays terse. When non-empty it
    # surfaces the count + first 3 task_ids so the leader sees them inline
    # without having to re-read the structured payload.
    lr_queue = task_state_aggregate["leader_review_queue"]
    lr_line = ""
    if lr_queue:
        lr_ids = ", ".join(r["task_id"] for r in lr_queue[:3])
        more = f" (+{len(lr_queue) - 3} more)" if len(lr_queue) > 3 else ""
        lr_line = (
            f"\nleader_review queue: {len(lr_queue)} 件 [{lr_ids}{more}] "
            f"— invoke /beacon-trek-review NOW"
        )

    body = (
        f"[{_LEADER_DIGEST_HEADER}] trek_id={trek_id}\n"
        f"active={summary_block['active']} "
        f"stuck={summary_block['stuck']} "
        f"idle={summary_block['idle']} "
        f"needs_leader_judgment={summary_block['needs_leader_judgment']} "
        f"leader_review_queue={summary_block['leader_review_queue_count']}"
        f"{lr_line}\n"
        f"{sessions_block_str}"
    )

    return {
        "kind": _LEADER_DIGEST_KIND,
        "trek_id": trek_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "summary": summary_block,
        "sessions": sessions_list,
        "task_state_aggregate": task_state_aggregate,
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
