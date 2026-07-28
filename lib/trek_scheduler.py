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
from typing import Callable, Iterable, Optional

# ms-99 / e-2833 — Slot inventory primitive (Phase 2). The six tick decision
# functions below all delegate slot resolution to ``materialize_slots`` so
# the source of truth for "what work exists in this Trek" is ``scope[] ×
# project pool`` rather than the ``task_states`` cache alone. This closes
# the silent-quiesce loop where a partial-stamp ``task_states`` map made the
# scheduler read the Trek as complete while pool-side todo remained (see
# ``tests/test_trek_silent_quiesce_regression.py`` for the tk-29a11d2f
# reproduction).
try:
    from trek_slots import Slot, materialize_slots  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — flat-layout import fallback
    from lib.trek_slots import Slot, materialize_slots  # type: ignore[no-redef]

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
        # ms-128 方針5: done を Trek 状態機械から除去、user_review が唯一の terminal。
        TERMINAL_TASK_STATES = _trek_for_constants.TERMINAL_TASK_STATES  # (= ("user_review",))
        # ms-128 方針 (e-4287): 二層 tick の executor 発火集合。状態機械の正典
        # (lib/trek.py) から import し、分割の定義を 1 ファイルに集約する。
        EXECUTOR_ACTIVE_STATES = _trek_for_constants.EXECUTOR_ACTIVE_STATES  # (= ("todo","working"))
    except AttributeError:
        DEFAULT_TASK_STATE = "todo"
        TERMINAL_TASK_STATES = ("user_review",)
        EXECUTOR_ACTIVE_STATES = ("todo", "working")
else:
    DEFAULT_TASK_STATE = "todo"
    TERMINAL_TASK_STATES = ("user_review",)
    EXECUTOR_ACTIVE_STATES = ("todo", "working")
del _trek_for_constants

WORKING_TASK_STATE = "working"

# (EXECUTOR_ACTIVE_STATES は上の _trek_for_constants 経路で lib/trek.py の状態機械
#  正典から import 済 = 二層 tick の分割定義を 1 ファイルに集約。)

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


def is_trek_task_aggregate_terminal(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> bool:
    """Return True iff every materialized Trek slot is in a terminal state.

    ms-99 / e-2833 — Phase 2 refactor: reads from ``materialize_slots``
    (= scope[] × project pool) rather than the ``task_states`` cache. This
    is the fix for the tk-29a11d2f silent-quiesce dogfood (2026-07-03):
    a partial-stamp cache with one ``user_review`` entry made the old
    task_states-only path return True while three unclaim todos still
    lived in the pool, causing the scheduler to quiesce prematurely.

    Terminal = ``done`` OR ``user_review`` (ms-88 / e-2107; CORE doc
    5nfTSmCDVUzD4SLzIhI5 § "Trek 完遂判定"). A trek with an empty scope
    (= materialize_slots returns []) is NOT terminal — the scheduler
    keeps firing obligation DMs until scope is populated.

    ``get_project`` is a callable that returns the project pool doc for
    a given project_id (in production this is ``db.get_project``). It is
    keyword-only because e-2840's regression pins exercise the keyword
    signature and the primitive's own contract mirrors it.
    """
    slots = materialize_slots(trek_doc, get_project=get_project)
    if not slots:
        # Empty scope = "nothing to complete yet"; keep firing.
        return False
    for slot in slots:
        if slot.resolved_state not in TERMINAL_TASK_STATES:
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


# ms-99 / e-2833 — "Signal source" invariant: a slot whose
# ``resolved_state_source == "unstamped"`` is a placeholder — neither the
# task_states cache nor the project pool has told us anything about it
# (e.g. an MS slot whose scope entry references a project not currently
# reachable via ``get_project``, or an atomic task/op slot whose pool
# entry has been deleted). Such slots participate in
# ``is_trek_task_aggregate_terminal`` (= they keep the aggregate non-
# terminal so the scheduler keeps trying, per SPEC 方針 3 fallback), but
# they must NOT count as "there is work to pick up / show the leader"
# for the leader-digest / executor-pickup gates. Filtering them out here
# is the direct fix for the false-fire path exercised by
# ``test_lazy_start_no_signal_yields_broadcast_fallback_only``.
_UNSTAMPED_SOURCE = "unstamped"


def _signal_slots(slots: list[Slot]) -> list[Slot]:
    return [s for s in slots if s.resolved_state_source != _UNSTAMPED_SOURCE]


def has_unclaim_todo(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> bool:
    """Return True iff any materialized slot is unclaimed todo work.

    ms-99 / e-2833 — Phase 2 refactor: reads slot inventory rather than
    the task_states cache. "Unclaim todo" now means a slot whose
    ``owner_session_id`` is empty AND whose ``resolved_state`` is
    ``todo`` AND whose source is not ``unstamped`` (= there is a real
    cache/pool signal indicating pickup-able work). This surfaces pool-
    side todo tasks that never got a ``task_states`` stamp — the fresh-
    executor silent-skip loop from tk-29a11d2f dogfood — without
    spuriously firing on empty MS slots whose pool is unreachable.
    """
    for slot in _signal_slots(materialize_slots(trek_doc, get_project=get_project)):
        if slot.owner_session_id:
            continue
        if slot.resolved_state == DEFAULT_TASK_STATE:
            return True
    return False


def has_leader_review_queue(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> bool:
    """Return True iff any materialized slot is in ``leader_review``.

    ms-99 / e-2833 — Phase 2 refactor: iterate slots, not task_states.
    Any slot whose ``resolved_state == "leader_review"`` keeps the
    leader digest firing until they act. ``leader_review`` can never
    come from an unstamped source (= it requires an explicit stamp),
    so no source filter is applied here.
    """
    slots = materialize_slots(trek_doc, get_project=get_project)
    for slot in slots:
        if slot.resolved_state == "leader_review":
            return True
    return False


def has_todo_float(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> bool:
    """Return True iff any materialized slot is in ``todo`` with a real
    cache/pool signal (= not an unstamped placeholder).

    ms-99 / e-2833 — unlike ``has_unclaim_todo`` this also counts claimed
    todo slots (= "todo float exists" for the leader digest regardless
    of who owns it), but still filters unstamped slots so an empty-scope
    Trek does not spuriously fire the digest.
    """
    for slot in _signal_slots(materialize_slots(trek_doc, get_project=get_project)):
        if slot.resolved_state == DEFAULT_TASK_STATE:
            return True
    return False


def is_completion_imminent(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
    threshold: int = COMPLETION_IMMINENT_SLOTS,
) -> bool:
    """Return True iff the trek is within ``threshold`` non-terminal slots
    of full aggregate-terminal completion.

    ms-99 / e-2833 — Phase 2 refactor: counts non-terminal materialized
    slots. Unstamped placeholder slots are excluded from the count (= they
    do not represent real remaining work), so an empty-scope Trek returns
    False (= no imminence).
    """
    signal = _signal_slots(materialize_slots(trek_doc, get_project=get_project))
    if not signal:
        return False
    non_terminal = 0
    for slot in signal:
        if slot.resolved_state not in TERMINAL_TASK_STATES:
            non_terminal += 1
            if non_terminal > threshold:
                return False
    return 0 < non_terminal <= threshold


def should_fire_executor_tick(
    trek_doc: dict,
    *,
    session_id: str,
    get_project: Callable[[str], dict],
) -> bool:
    """Lazy-start decision for one executor's progress-check tick.

    ms-99 / e-2833 — Phase 2 refactor: slot-inventory based.
    ms-128 / e-4287 — 二層 tick: executor tick は claim が
    ``EXECUTOR_ACTIVE_STATES`` (= todo / working) にある時だけ発火する。
    Returns True iff:
      * The executor holds a claim on a slot (= slot.owner_session_id
        equals ``session_id``) whose resolved_state is in
        ``EXECUTOR_ACTIVE_STATES`` (todo / working — executor がまだ手を
        動かす状態), OR
      * The trek has an unclaim todo slot with a real cache/pool signal
        the executor could pick up.

    Returns False when the executor's claims are all beyond
    ``EXECUTOR_ACTIVE_STATES`` — i.e. handed off to the leader
    (``leader_review``) or terminal (``user_review``) — AND no unclaim
    todo signal remains. leader_review 以降は leader tick
    (``should_fire_leader_tick``) が引き取る。The server orchestrator
    falls back to a minimal broadcast if every executor's decision is
    False so no complete silence occurs.
    """
    slots = materialize_slots(trek_doc, get_project=get_project)
    for slot in slots:
        if slot.owner_session_id != session_id:
            continue
        # ms-128 方針 (e-4287) — 二層 tick: 実行 tick は leader_review で止める。
        # executor が slot を leader_review に倒した (= 完成し PR 化して hand-off
        # した) 時点で、その slot に対する executor の仕事は終わり (= leader が
        # review へ引き取る)。よって executor tick が still 発火するのは、自分が
        # まだ手を動かす状態 (todo = claim 済未着手 / working = 作業中) の claim を
        # 持つ時だけ。leader_review (hand-off 済) と user_review (terminal) は
        # どちらも「executor はもうやることが無い」= silent。
        if slot.resolved_state in EXECUTOR_ACTIVE_STATES:
            return True
    # No active claim → fall through to the unclaim-todo pickup check.
    for slot in _signal_slots(slots):
        if slot.owner_session_id:
            continue
        if slot.resolved_state == DEFAULT_TASK_STATE:
            return True
    return False


def should_fire_leader_tick(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> bool:
    """Lazy-start decision for the leader-digest tick.

    ms-99 / e-2833 — Phase 2 refactor: still layered on top of the three
    primitives (``has_leader_review_queue`` / ``has_todo_float`` /
    ``is_completion_imminent``) which now all read slot inventory. Each
    primitive call re-materializes the slot list; that is idempotent
    (auto-mirror stamps carry the AUTO_MIRROR_SESSION_ID marker and are
    skipped on repeats) and keeps the composition boundary readable.

    A trek with empty scope returns False — the orchestrator's minimal-
    tick fallback keeps one event per cadence window so the leader can
    still observe "nothing has happened" through the digest channel.
    """
    if has_leader_review_queue(trek_doc, get_project=get_project):
        return True
    if has_todo_float(trek_doc, get_project=get_project):
        return True
    if is_completion_imminent(trek_doc, get_project=get_project):
        return True
    return False


# ---------------------------------------------------------------------------
# Leader-digest heartbeat (ms-128 / e-4284)
#
# should_fire_leader_tick は「消費すべき signal がある時だけ」発火する
# (leader_review queue / todo float / completion imminent)。しかし dogfood で、
# 全 executor が working のまま silent に滞留 (= signal ゼロ) すると leader digest
# が 1 度も発火せず、leader は stall を観測できないまま autonomous ループが
# self-heal できなかった。signal 駆動だけでは「何も起きていないこと」自体が
# leader に届かない。
#
# 対策: signal gate が閉じていても、遅い cadence (= cadence × 3 ≒ 30 分) で leader
# digest を heartbeat として発火する。これで silent stall が必ず leader の目に入る
# 一方、毎 tick 発火の noise は避ける。完遂済 trek (summary_sent + completion
# notified) は server 側の halted_by_summary で別途止まるので heartbeat も止まる。
# ---------------------------------------------------------------------------

LEADER_DIGEST_HEARTBEAT_MULTIPLIER = 3


def get_last_leader_digest_at(
    trek_doc: dict,
) -> Optional[datetime.datetime]:
    """Return the trek's last leader-digest fire time, or None if never."""
    meta = trek_doc.get("meta") or {}
    return _parse_iso(meta.get("last_leader_digest_at", ""))


def is_leader_digest_heartbeat_due(
    trek_doc: dict,
    *,
    now: datetime.datetime,
    default_cadence_minutes: int = DEFAULT_CADENCE_MINUTES,
    multiplier: int = LEADER_DIGEST_HEARTBEAT_MULTIPLIER,
) -> bool:
    """Decide whether the leader digest should fire as a heartbeat (e-4284).

    Purpose: surface a silently-stalled trek to the leader even when the
    signal gate (``should_fire_leader_tick``) is closed. Fires on a slow
    cadence so the leader gets a periodic pulse without per-tick noise.

    Invariant (= what "heartbeat fired" means to the reader): the leader
    digest heartbeats iff the trek has been **quiet for at least
    ``cadence * multiplier``** (default 10 × 3 = 30 min). "Quiet" is
    measured from the most recent of {last leader digest, last progress
    check} — i.e. the last time the trek surfaced anything. This holds
    uniformly, including the never-digested case (no interval-zero first
    pulse), so "heartbeat" always denotes cadence×N of silence.

    Rules (mirror ``is_trek_idle``'s guards):
      * Only ``status == 'active'`` treks heartbeat (planning / archived
        have no ongoing work to pulse about).
      * ``halt`` set → no heartbeat (leader pulled the cord deliberately).
      * Anchor = last_leader_digest_at, falling back to
        last_progress_check_at when no digest has fired yet. Due iff
        ``now - anchor >= cadence * multiplier``. A trek never ticked
        (no anchor at all) is not due — its first tick starts the clock.

    Pure so unit tests pin it without HTTP. The server ORs this into
    ``leader_should_fire`` alongside the signal gate and completion_ready.
    """
    if trek_doc.get("status") != "active":
        return False
    if trek_doc.get("halt"):
        return False
    cadence = get_cadence_minutes(trek_doc, default=default_cadence_minutes)
    threshold = datetime.timedelta(minutes=cadence * multiplier)
    now = _ensure_utc(now)
    # Anchor on the last time the trek surfaced anything: a digest if one
    # has fired, else the progress-check that proves the trek is live.
    anchor = get_last_leader_digest_at(trek_doc) or get_last_progress_check_at(
        trek_doc
    )
    if anchor is None:
        # Never ticked at all → no clock yet; the first tick starts it.
        return False
    anchor = _ensure_utc(anchor)
    return (now - anchor) >= threshold


def is_leader_halted_by_summary(trek_doc: dict) -> bool:
    """Return True iff the trek's completion has already been handed off.

    ms-97 / Phase 7-A / AC21 — 完遂宣言が済んだ (leader が user へ summary DM を
    送信 = ``meta.summary_sent_at`` stamped、 かつ completion_ready が 1 回 fire
    済 = ``meta.completion_notified_at`` stamped) trek には leader digest を打ち
    続けない。片方だけ stamped の状態では従来通り fire し続ける。

    ms-128 / e-4284 — この停止条件は heartbeat / signal どちらの発火経路にも
    等しく効く必要がある。判定を 1 か所に集約し、server と
    ``compose_leader_fire_decision`` の両方から呼べるようにする (= 完遂済 trek に
    heartbeat を撃ち続ける事故を、停止条件の 2 ファイル分散でなく単一定義で防ぐ)。
    """
    meta = trek_doc.get("meta") or {}
    return bool(meta.get("summary_sent_at")) and bool(
        meta.get("completion_notified_at")
    )


def compose_leader_fire_decision(
    *,
    signal_fire: bool,
    heartbeat_due: bool,
    halted_by_summary: bool,
) -> tuple[bool, list[str]]:
    """Combine the leader-digest fire inputs into ``(should_fire, fire_reasons)``.

    ms-128 / e-4284 — 発火判断を endpoint のインライン式から純関数へ切り出し、
    OR 合成と発火理由タグを 1 行ずつ unit test で pin できるようにする
    (= 約 1300 行の tick endpoint の中腹に埋めない)。

    - ``halted_by_summary`` が最優先の停止条件 (= 完遂済 trek は何があっても
      digest しない)。
    - それ以外は ``signal_fire`` / ``heartbeat_due`` の OR で発火する。
    - ``fire_reasons`` は **常に存在する閉じた列挙** (``["signal"]`` /
      ``["heartbeat"]`` / 両方) を返す。発火理由を optional bool の有無で運ぶと
      受信側 (leader digest を読む AI) が欠落の意味を推測する余地が残るため、
      値域を閉じた list にして「なぜ発火したか」を明示する (独立 AX レビュー
      #536 指摘)。should_fire=False の時は空 list。
    """
    if halted_by_summary:
        return False, []
    reasons: list[str] = []
    if signal_fire:
        reasons.append("signal")
    if heartbeat_due:
        reasons.append("heartbeat")
    return (len(reasons) > 0), reasons


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


def is_completion_ready(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> bool:
    """Return True iff this trek is ready to fire a one-shot completion_ready marker.

    ms-97 / Phase 7-A / AC20 — fires ONCE per trek when:

      * every materialized slot is in a terminal state (= ``done`` or
        ``user_review`` — same set as ``is_trek_task_aggregate_terminal``),
        AND
      * the scope contains NO Operation slot (= per AC20 footnote,
        Op-bearing treks defer this signal pending separate SPEC), AND
      * ``meta.completion_notified_at`` is still unstamped (= ``None``
        / absent — i.e. the marker has not already been fanned out).

    ms-99 / e-2833 — signature now threads ``get_project`` through to
    ``is_trek_task_aggregate_terminal`` so completion detection reads
    the same slot inventory as every other tick predicate. This is the
    e-2840 xfail contract (= test_is_completion_ready_false_when_pool_
    has_unclaim_todo pin).
    """
    if _has_op_slot(trek_doc):
        return False
    meta = trek_doc.get("meta") or {}
    if meta.get("completion_notified_at"):
        return False
    return is_trek_task_aggregate_terminal(trek_doc, get_project=get_project)


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
# ms-128 方針2 (e-4364) — tick 文面を「監査(進捗確認)」から「前進指示」へ。
# ヘッダの最初の語が全体を枠づける (「確認」は状態報告を、「前進」は行動を
# priming する)。作業単位が目的を運ばない以上、tick が毎回 telos を明示しないと
# エージェントは状態チェックとしか読まない。待機フォールバック (「次の cadence を
# 待って」) は廃止し、scope 空 / 全 done でも行動 (未 claim Target を拾う / leader
# へ DM / clean 自己終了 / handoff) に流す。
_PROGRESS_HEADER = "Trek 前進指示 (= server-mint T1-system)"
# 毎 body に注入する telos (= 目的)。状態報告フレームへの誤読を防ぐ。
_PROGRESS_TELOS = (
    "あなたの仕事はこの Target を leader_review / user_review まで前進させること"
    "です。この tick は状態報告でなく前進の合図。待機は応答ではありません。"
)
_PROGRESS_EMPTY_SCOPE = (
    "Trek の scope が空です。待機せず、 `beacon trek scope-add` で Target "
    "(milestone / operation 等) を追加するか、 追加すべきものが無ければ leader に "
    "DM (dm-leader) で次の一手を仰いでください。"
)
_PROGRESS_ALL_DONE = (
    "Trek scope 内に着手可能な todo Target が見当たりません。待機せず、 次の "
    "いずれかを取ってください: 未 claim の Target を拾う / leader に完遂 handoff を "
    "相談 (dm-leader) / clean に自己終了。"
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
            f"{_PROGRESS_TELOS}\n"
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
            f"{_PROGRESS_TELOS}\n"
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
        _PROGRESS_TELOS,
        f"次 前進させてください: {head.get('id', '')} — "
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
        # ms-128 方針5: done は Trek 状態機械から除去。import 失敗時は
        # _task_state_of が migrate せず raw "done" が cs に混じりうるので、
        # ここで done を user_review と同一視して terminal に畳む。
        cs = [c["state"] for c in children]
        if "leader_review" in cs:
            overall_state = "leader_review"
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


# ms-128 / e-4307 — working target が「commit も leader_review も PR も出さずに」
# 一定時間 silent (= deliberate pause / session-end handoff) だと leader survey で
# 「無 commit」ラベルを付ける閾値。**独立閾値** (= gate ではなく digest の表示用)
# なので固定分で足りる。auto-stall の GATE 機構 (detect_auto_stalled_tasks、meta
# 上書き可 TTL 1440 分、pause/TTL-extension を skip) とは別物で、意図的に別語彙
# (silent) を使う (独立レビュー #537 保守性指摘)。
SILENT_WORKING_TARGET_MINUTES = 30


def build_working_targets_recency(
    trek_doc: dict,
    *,
    now: datetime.datetime,
    migrate_state: "Callable[[str], str]",
    silent_after_minutes: int = SILENT_WORKING_TARGET_MINUTES,
) -> list[dict]:
    """Per working-target の commit recency を leader digest 用に集約 (e-4307).

    2026-07-27 dogfood: executor が ``working`` のまま「fresh session を待つ」
    「形態判断で deliberate pause」等で silent に止まると、PR も leader_review も
    task-state 変化も出さないため leader が stall を見逃した (4.5h)。leader が
    PR / leader_review だけ見て commit recency を見ていなかったのが原因。

    各 working target について「最後に前進 (= commit 数増 or fingerprint 変化) した
    時刻」からの経過分を出し digest に載せる。これで「working だが N 分 commit が
    無い」silent stall が leader survey に必ず現れる。

    **進行 anchor は ``progress_last_advanced_at`` のみ** (= halt 検知 e-4367 が
    commit 数増/fingerprint 変化を観測した時に stamp)。``updated_at`` /
    ``last_activity_at`` へは fallback しない — それらは非進行の touch でも動くため
    「commit が無いのに前進したように見せて stall をマスク」してしまうから
    (独立 AX レビュー #537)。進行 stamp が無い target は ``progress_anchor_known
    = False`` / ``minutes_since_progress = None`` として **観測不能を明示** し、
    sort で先頭に置く (= 最も見えていない = 要注意。健全に見える位置に沈めない)。

    ``is_silent`` は auto-stall の GATE (``detect_auto_stalled_tasks``) とは別概念
    (= digest 表示上の「無 commit」観測) なので語を分ける。

    Returns a list (unknown-anchor first, then longest-silent first) of::

        {"target_id": str, "state": "working",
         "progress_anchor_known": bool,           # False = 進行 stamp 無し
         "minutes_since_progress": int | None,    # None = anchor 不明 (観測不能)
         "is_silent": bool,                        # anchor 有 & >= silent_after_minutes
         "halt_reason": str | None,   # halt 検知タグ: "progress-stall"|"liveness-timeout"|None
         "last_commit_count": int}

    Pure (clock injected). ``migrate_state`` は legacy state token を正規化する
    callable (= ``trek.migrate_legacy_task_state``) を注入し、モジュール間の
    重複写像を避ける。
    """
    now = _ensure_utc(now)
    states = trek_doc.get("task_states") or {}
    out: list[dict] = []
    for target_id, entry in states.items():
        if not isinstance(entry, dict):
            continue
        state = migrate_state(entry.get("state") or DEFAULT_TASK_STATE)
        if state != WORKING_TASK_STATE:
            continue
        anchor = _parse_iso(entry.get("progress_last_advanced_at", ""))
        anchor_known = anchor is not None
        minutes: Optional[int] = None
        if anchor_known:
            minutes = int((now - _ensure_utc(anchor)).total_seconds() // 60)
        out.append({
            "target_id": target_id,
            "state": WORKING_TASK_STATE,
            "progress_anchor_known": anchor_known,
            "minutes_since_progress": minutes,
            "is_silent": (
                minutes is not None and minutes >= silent_after_minutes
            ),
            "halt_reason": entry.get("halt_reason"),
            "last_commit_count": int(entry.get("last_commit_count") or 0),
        })
    # 観測不能 (anchor 無し) を先頭に (= 最も見えていない = 要注意)、その後は
    # 経過分の長い順。silent stall を「健全に見える位置」に沈めない (AX #537)。
    out.sort(
        key=lambda r: (
            not r["progress_anchor_known"],
            r["minutes_since_progress"] or 0,
        ),
        reverse=True,
    )
    return out


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
            "waiting_on_leader_count": int,    # e-4307: dm-leader 待ちの session 数
            "silent_working_targets": int,     # e-4307: commit 停滞 working target 数
            "unknown_progress_targets": int,   # e-4307: 進行 stamp 無し (観測不能) 数
            "total_acks_across_sessions": int,
          },
          "sessions": [
            {
              "session_id": str,
              "state_summary": str,
              "blockers": [str, ...],
              "needs_leader_judgment": bool,
              # e-4307: **直近 pulse で** dm-leader を選んだか。leader 返信時に
              # clear されないので、鮮度は last_pulse_ack_at と突き合わせて読む。
              "waiting_on_leader": bool,
              "time_on_task_seconds": int,
              "last_pulse_ack_at": ISO,
              "last_picked_choice": str,
              "total_acks": int,
            },
            ...
          ],
          # e-4307: working target の commit recency (観測不能→経過分長い順)。
          "working_targets_recency": [
            {"target_id": str, "state": "working",
             "progress_anchor_known": bool,
             "minutes_since_progress": int | None, "is_silent": bool,
             "halt_reason": str | None, "last_commit_count": int},
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
                # ms-128 / e-4307 — shape 一貫性のため degenerate path でも key を出す。
                "waiting_on_leader_count": 0,
                "silent_working_targets": 0,
                "unknown_progress_targets": 0,
                "leader_review_queue_count": len(
                    task_state_aggregate["leader_review_queue"]
                ),
                "total_acks_across_sessions": 0,
            },
            "sessions": [],
            "task_state_aggregate": task_state_aggregate,
            "working_targets_recency": [],
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
        picked = entry.get("last_picked_choice") or ""
        sessions_list.append({
            "session_id": sid,
            "state_summary": entry.get("state_summary") or "",
            "blockers": list(entry.get("blockers") or []),
            "needs_leader_judgment": bool(entry.get("needs_leader_judgment")),
            # ms-128 / e-4307 — executor が「leader へ問い合わせて返答待ち」に
            # 入った (= 型付き tick 応答で dm-leader を選んだ) ことを leader に
            # 明示する。PR も leader_review も出さない silent な judgment-wait を
            # survey に載せる直接シグナル。
            "waiting_on_leader": picked == "dm-leader",
            "time_on_task_seconds": int(entry.get("time_on_task_seconds") or 0),
            "last_pulse_ack_at": entry.get("last_pulse_ack_at") or "",
            "last_picked_choice": picked,
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

    # ms-128 / e-4307 — working target の commit recency + halt_reason。
    # migrate callable は trek_mod のものを注入 (この分岐は trek_mod 有効)。
    working_targets_recency = build_working_targets_recency(
        trek_doc, now=now, migrate_state=trek_mod.migrate_legacy_task_state,
    )
    waiting_on_leader_count = sum(
        1 for s in sessions_list if s.get("waiting_on_leader")
    )
    silent_working_count = sum(
        1 for r in working_targets_recency if r.get("is_silent")
    )
    unknown_progress_count = sum(
        1 for r in working_targets_recency if not r.get("progress_anchor_known")
    )

    summary_block = {
        "active": int(summary.get("active_session_count") or 0),
        "stuck": int(summary.get("stuck_session_count") or 0),
        "idle": int(summary.get("idle_session_count") or 0),
        "needs_leader_judgment": int(
            summary.get("needs_leader_judgment_count") or 0
        ),
        # ms-128 / e-4307 — leader が PR / leader_review だけ見て見逃す silent
        # wait の 3 面。count は全て *_count (int) で統一 (session の bool
        # waiting_on_leader と名前衝突しない、独立レビュー #537)。
        # waiting_on_leader_count = executor が明示的に leader 返答待ち。
        # silent_working_targets = working だが commit が一定時間止まっている。
        # unknown_progress_targets = working だが進行 stamp が無く観測不能
        # (= 最悪の見えなさ。健全と誤認させないため count を独立に出す)。
        "waiting_on_leader_count": waiting_on_leader_count,
        "silent_working_targets": silent_working_count,
        "unknown_progress_targets": unknown_progress_count,
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
                tag = " [stuck]"  # 具体的 blocker が最も actionable
            elif s.get("waiting_on_leader"):
                tag = " [waiting on leader]"  # e-4307: blocker 無しの明示 judgment-wait
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

    # ms-128 / e-4307 — silent wait 面。executor が leader 返答待ち、または
    # working だが commit が止まっている/観測不能な target を、count 非ゼロ時のみ
    # 1 行で。body の token は payload summary key と同一文字列にして、人間可読
    # fallback から構造 payload へ grep で辿れるようにする (独立レビュー #537)。
    wait_line = ""
    if (waiting_on_leader_count or silent_working_count
            or unknown_progress_count):
        silentest = next(
            (r for r in working_targets_recency if r.get("is_silent")), None
        )
        silentest_str = ""
        if silentest is not None:
            silentest_str = (
                f" (silentest: {silentest['target_id']} "
                f"{silentest['minutes_since_progress']}分 commit 無し)"
            )
        wait_line = (
            f"\n⏳ silent wait: waiting_on_leader_count={waiting_on_leader_count}"
            f" / silent_working_targets={silent_working_count}"
            f" / unknown_progress_targets={unknown_progress_count}"
            f"{silentest_str} "
            f"— PR / leader_review に現れない待ちを確認してください"
        )

    body = (
        f"[{_LEADER_DIGEST_HEADER}] trek_id={trek_id}\n"
        f"active={summary_block['active']} "
        f"stuck={summary_block['stuck']} "
        f"idle={summary_block['idle']} "
        f"needs_leader_judgment={summary_block['needs_leader_judgment']} "
        f"leader_review_queue={summary_block['leader_review_queue_count']}"
        f"{lr_line}{wait_line}\n"
        f"{sessions_block_str}"
    )

    return {
        "kind": _LEADER_DIGEST_KIND,
        "trek_id": trek_id,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "summary": summary_block,
        "sessions": sessions_list,
        "task_state_aggregate": task_state_aggregate,
        # ms-128 / e-4307 — commit recency 面 (working だが N 分 commit 無しの
        # silent stall を leader survey に載せる)。
        "working_targets_recency": working_targets_recency,
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
