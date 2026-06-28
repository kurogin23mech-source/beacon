"""Trek model — cross-project / cross-session collaboration area (ms-69 e-1652).

See SPEC doc fnwI2KzzDSJdERIfcwtq (`ms-69 SPEC: Trek — 分散協奏のための作業領域`).

Phase 1 scope (this module):
- Schema constants (valid types / statuses / roles).
- Pure builders / validators for trek docs, members, and scope entries.
- ID minting.

Backend integration (Firestore / DynamoDB CRUD) lives in
``server/firestore_client.py`` and ``server/dynamodb_client.py``. They are
routed through ``server/store_router.py`` exactly like projects / users.

CLI wiring (= ``beacon trek create / show / list / archive`` etc.) is a
follow-up task (e-1653). This module intentionally has no I/O so the
schema can be exercised in unit tests without standing up a DB mock.
"""
from __future__ import annotations

import datetime
import secrets
from typing import Iterable

# Trek lifecycle: planning → active → archived  (3 states only)
# - planning: scope/invites being staged, sessions not yet joining
# - active: members can claim work, DM, run Operations under this trek
# - archived: terminal for temporary treks; persistent treks may archive
#   as "hibernate" and be reactivated later (= caller-enforced)
#
# Halt is **not a status**. The STOP signal sets a separate ``halt`` field
# on the trek doc (= Andon cord); sessions observe it and pause their
# autonomous work. Recovery happens by the leader instructing sessions
# to resume — no state transition required. This collapsed the prior
# planning/active/paused/archived 4-state machine into 3 states because
# pause+resume turned out to be redundant with STOP+leader-instruction.
VALID_TREK_TYPES = ("temporary", "persistent")
VALID_TREK_STATUSES = ("planning", "active", "archived")
VALID_MEMBER_ROLES = ("leader", "member")

# ms-88 / e-2138 — Trek Kickoff Ritual (= per-session kickoff state map).
# 同 user の複数 session が peer 状況を共有できていなかった race (= 2026-06-20
# tk-3045b8d1 dogfood で観測、 3 session が同 cwd を共有して unstaged 編集が
# 5 分間消失) を構造的に塞ぐため、 join / take-over した session に「peer に
# 1 度は声をかけて自分の plan を共有する」 を強制する。
#
# Schema: trek_doc.kickoff_status: dict[session_id -> KickoffStatusEntry]
#     KickoffStatusEntry {
#       session_id: str,
#       user_id: str,           # session が所属する member の user_id
#       pending: bool,          # True = まだ kickoff 未送信、 pulse-ack 拒否対象
#       sent_at: ISO timestamp, # 完了時のスタンプ
#       kickoff_dm_event_id: str (optional, トレース用)
#     }
#
# Lazy init 設計: join / take-over 時に pre-populate しない。 pulse-ack endpoint
# で session_id が status map に居なければ「未 kickoff」 と判定して 400 reject。
# /beacon-trek-pulse Skill の Step 0 が kickoff DM 送信 + kickoff endpoint call で
# 状態を完了に flip する。 既存 member (= deploy 前から joined) も次の pulse-ack で
# 初めて触れた時点で「kickoff 未」 として扱われる (= migration なし、 backward
# compat は existing trek docs の kickoff_status 不在 default 経由で吸収)。
KICKOFF_HISTORY_KEY = "kickoff_status"

# ms-75 / e-2048 + ms-88 / e-2107 — Trek-internal task state machine.
# 5-state model (= CORE doc 5nfTSmCDVUzD4SLzIhI5 § "Trek task state machine"):
#   - todo: scope-registered but executor has not claimed yet
#   - working: executor actively progressing (= scheduler keeps firing tick)
#   - leader_review: leader judgment requested (executor 自発 OR server 強制).
#     Leader picks 3-way: done / user_review / working with guidance DM.
#   - user_review: user judgment requested (= leader can't decide, or
#     deploy / release / external write 等の不可逆 action 含むため leader が
#     forward した経路). Terminal-ish (= Trek 完遂判定に算入)。
#   - done: full completion, irreversible terminal.
#
# Why 5 vs the older 3 (= ms-75 / e-2048 の working/done/waiting-review):
# 旧 `waiting-review` 1 状態が「leader 判断要請」 と「user 判断要請」 を
# conflate しており、 dogfood (tk-40b0b27c, 2026-06-19) で「ms-84 executor
# が PR submit 後に leader が見れば判断つく場合でも waiting-review (= 文面上
# 『user 介入要請』) を選ばざるを得ず混乱」 が露呈した。 5 状態に厳密化して
# leader / user 判断境界を構造的に分離する。
#
# Trek 完遂判定: 全 task が `done` OR `user_review` に至った時点で完遂。
# todo / working / leader_review が 1 つでもあれば scheduler + leader は
# 走り続ける (= leader_review は中途中継、 user_review は terminal 扱い)。
VALID_TASK_STATES = ("todo", "working", "leader_review", "user_review", "done")
DEFAULT_TASK_STATE = "todo"
TERMINAL_TASK_STATES = ("done", "user_review")

# Backward compat (= ms-88 / e-2107 migration). 旧 `waiting-review` で書かれた
# 既存データは server-forced auto-stall 経路 (= old e-2067) からのものが多く、
# semantic 的には新 `leader_review` (= 「leader 判断要請、 server 強制」) に
# 最も近い。 set_task_state は新コードに対して waiting-review を拒否し
# (= 新コードは 5 状態を直接使う) 、 get_task_state は読み出し時に migrate
# して呼び出し側に新 token を返す。
LEGACY_TASK_STATE_MIGRATIONS = {
    "waiting-review": "leader_review",
}

# ms-75 / e-2067 + ms-88 / e-2107 + ms-95 / e-2646 — server-side TTL safety net.
# **TTL 12 min → 1440 min (24h) に再緩和**: 2026-06-28 dogfood で 12 min は
# prep 待機中 (= staging URL 受領待ち / 別 session のレビュー待ち) の
# executor を「stuck」と誤判定し、 working → leader_review に勝手に flip
# して attribution を奪う構造問題が確定 (= e-2646 / dogfood findings
# `e70cUf8IS5uEIS1HIEXt` § #14)。 24h baseline = 「待機」 と「stuck」 を
# 実用上区別できる距離、 かつ翌日まで完全 silent な絶対 halt は依然 catch
# する境界。 「すぐ stall 判定したい」 個別 Trek は trek.meta.working_ttl_minutes
# で短い override 可能 (= 既存 field と互換)、 「prep 待機中」 marker は
# task_states[*].meta.working_pause_until (ISO8601) で立てる (= e-2646)。
DEFAULT_WORKING_TTL_MINUTES = 1440

# Allowed transitions (5 状態、 計 10 経路 + idempotent no-op):
# - claim 1 経路: todo → working
# - executor 3 経路 (= pulse Skill の terminal 選択): working → {done, leader_review, user_review}
# - leader 3 経路 (= /beacon-trek-review forced picker): leader_review → {done, user_review, working}
# - user 2 経路 (= 会話 + leader CLI 代行): user_review → {done, working}
# - server 強制 1 経路 (= 罰則): working → leader_review (= 全 working 一括)
#
# CORE doc `5nfTSmCDVUzD4SLzIhI5` § "1 枚 transition diagram" 参照。
VALID_TASK_STATE_TRANSITIONS = {
    "todo": ("working",),
    "working": ("done", "leader_review", "user_review"),
    "leader_review": ("done", "user_review", "working"),
    "user_review": ("done", "working"),
    "done": ("working",),
}

DEFAULT_STATUS = "planning"
DEFAULT_TYPE = "persistent"

# ms-86 / e-2225 — session_history: per-Trek cumulative join log.
#
# Why: Trek 文書はもともと session_id を 3 箇所 (= leader_session_id /
# halt.issued_by_session_id / task_states[].updated_by_session_id) に
# 散在させていたが、 「この Trek に過去 join した session 全員の累積記録」
# を 1 箇所にまとめる field が存在しなかった。 MEMBERS & AGENTS table が
# 旧 ``state.openTrekMemberSessions`` (= live-only endpoint 由来) に依存して
# おり、 一度 offline になった session が UI から消える regression が
# 2026-06-22 user 指摘で顕在化した。 Trek は persistent record である
# べきという原則 (= e-2225 motivation) に従い、 join 時点で永続的に記録する。
#
# Schema: trek_doc.session_history: list[SessionHistoryEntry]
#     SessionHistoryEntry {
#       session_id: str,
#       user_id: str,
#       email: str,
#       joined_at: ISO timestamp,
#       role_at_join: "leader" | "member",  # 加入時点の role。 transfer-leader で
#                                            # 後から role が変わっても元のまま残す
#                                            # (= 「いつ leader だった session」 を
#                                            # 後から audit できるよう保存)
#     }
#
# 同 session_id 既存なら no-op (= upsert)。 新規追加時のみ list 末尾に append。
# 順序は append 順 (= 加入時系列) を保つ。
SESSION_HISTORY_KEY = "session_history"


def mint_trek_id() -> str:
    """Generate a fresh trek id (= 8 hex chars, ~64 bits of entropy).

    Format: ``tk-<8 hex>``. Short enough for CLI legibility, large enough
    to avoid collisions across all treks the system will ever hold.
    """
    return f"tk-{secrets.token_hex(4)}"


def utcnow_iso() -> str:
    """ISO8601 UTC with microseconds + Z suffix (= matches firestore_client)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def validate_type(t: str) -> str:
    if t not in VALID_TREK_TYPES:
        raise ValueError(
            f"invalid trek type {t!r} — expected one of {VALID_TREK_TYPES}"
        )
    return t


def validate_status(s: str) -> str:
    if s not in VALID_TREK_STATUSES:
        raise ValueError(
            f"invalid trek status {s!r} — expected one of {VALID_TREK_STATUSES}"
        )
    return s


def validate_role(r: str) -> str:
    if r not in VALID_MEMBER_ROLES:
        raise ValueError(
            f"invalid trek role {r!r} — expected one of {VALID_MEMBER_ROLES}"
        )
    return r


def migrate_legacy_task_state(s: str) -> str:
    """Translate legacy task-state tokens to the 5-state model (ms-88 / e-2107).

    Maps old ``waiting-review`` → ``leader_review`` (= the semantic match
    for past server-forced auto-stalls). Unknown tokens pass through
    unchanged so downstream validation can flag them properly.
    """
    return LEGACY_TASK_STATE_MIGRATIONS.get(s, s)


def validate_task_state(s: str) -> str:
    """Validate Trek-internal task state (= ms-88 / e-2107).

    Legacy tokens (= ``waiting-review``) are migrated transparently to the
    5-state model before validation, so callers passing old data are
    accepted but normalised. New code should pass canonical 5-state
    tokens directly.
    """
    s = migrate_legacy_task_state(s)
    if s not in VALID_TASK_STATES:
        raise ValueError(
            f"invalid trek task state {s!r} — expected one of {VALID_TASK_STATES}"
        )
    return s


def validate_task_state_transition(from_state: str, to_state: str) -> None:
    """Enforce the 5-state machine transitions (ms-88 / e-2107).

    Raises ValueError when the proposed transition is not allowed by
    VALID_TASK_STATE_TRANSITIONS. ``from_state`` of "" or None is treated
    as the default (= ``todo``); this lets a fresh executor claim a task
    in one step (todo → working) without a prior explicit todo stamp.

    Both ``from_state`` and ``to_state`` are migrated through legacy
    tokens (= ``waiting-review`` → ``leader_review``) before checking,
    so old data + new code interop is silent.
    """
    if not from_state:
        from_state = DEFAULT_TASK_STATE
    from_state = migrate_legacy_task_state(from_state)
    to_state = migrate_legacy_task_state(to_state)
    validate_task_state(from_state)
    if from_state == to_state:
        # No-op transition is allowed (= idempotent re-affirmation by the
        # executor; useful for periodic state heartbeat without forcing
        # a fake intermediate hop).
        validate_task_state(to_state)
        return
    allowed = VALID_TASK_STATE_TRANSITIONS.get(from_state) or ()
    if to_state not in allowed:
        raise ValueError(
            f"invalid trek task state transition {from_state!r} → {to_state!r} "
            f"(allowed: {allowed})"
        )


def get_task_state(trek_doc: dict, task_id: str) -> str:
    """Return the Trek-internal state for ``task_id``, default 'todo'.

    Untracked tasks (= no entry in trek_doc.task_states) collapse to the
    default. This means a newly-added scope task starts in 'todo'
    implicitly; explicit state writes via ``set_task_state`` only happen
    when the executor claims (todo → working) or transitions further.

    Legacy ``waiting-review`` stored on existing trek docs is migrated
    transparently to ``leader_review`` so callers see the 5-state token.
    """
    states = trek_doc.get("task_states") or {}
    entry = states.get(task_id) or {}
    raw = entry.get("state") or DEFAULT_TASK_STATE
    return migrate_legacy_task_state(raw)


def set_task_state(trek_doc: dict, *, task_id: str, state: str,
                   updated_by_session_id: str = "",
                   note: str = "") -> dict:
    """Mutate ``trek_doc.task_states[task_id]`` after validating transition.

    Returns the mutated trek_doc (= chained writes friendly). The caller
    is responsible for persisting the document (= db.save_trek). A note
    is recorded if supplied (= helps the leader's review judgment).

    Legacy tokens (= ``waiting-review``) are migrated transparently to
    the 5-state model on input (ms-88 / e-2107), so old callers keep
    working while new code is expected to pass canonical state names.

    ``last_activity_at`` (= ms-75 / e-2067) is stamped to the same moment
    as ``updated_at``; this is the field the auto-stall scheduler reads to
    detect "working" tasks that have gone silent past the TTL. State stamp
    is one of the three documented activity sources (state stamp / commit /
    DM receipt) — see ``bump_task_activity`` for the non-state-change path.

    Raises ValueError if the transition is not allowed by the 5-state
    machine (see VALID_TASK_STATE_TRANSITIONS).
    """
    if not task_id:
        raise ValueError("task_id is required")
    state = migrate_legacy_task_state(state)
    validate_task_state(state)
    current = get_task_state(trek_doc, task_id)
    validate_task_state_transition(current, state)
    states = trek_doc.setdefault("task_states", {})
    now = utcnow_iso()
    states[task_id] = {
        "state": state,
        "updated_at": now,
        "updated_by_session_id": updated_by_session_id or "",
        "note": (note or "")[:500],
        "last_activity_at": now,
    }
    trek_doc["updated_at"] = now
    return trek_doc


def session_has_active_claim(trek_doc: dict, *, session_id: str) -> bool:
    """Return True iff ``session_id`` has at least one non-terminal claim (ms-88 / e-2109).

    "Active claim" = a task whose ``updated_by_session_id == session_id``
    AND current state is one of ``todo`` / ``working`` (= per CORE doc
    5nfTSmCDVUzD4SLzIhI5 § 設計方針 3, "the session has work to advance").
    Sessions whose claims are all in ``leader_review`` / ``user_review`` /
    ``done`` should not receive periodic ticks — they are waiting on
    someone else (leader / user) or fully done.

    Returns False if the session has no claims at all in this trek
    (= fresh session that hasn't stamped any state yet). The caller
    typically combines this with a "broadcast fallback for sessions
    without claims" so fresh executors still get tickled, matching the
    pre-filter behaviour while excluding sessions that explicitly finished.
    """
    if not session_id:
        return False
    states = trek_doc.get("task_states") or {}
    for entry in states.values():
        if not entry:
            continue
        if entry.get("updated_by_session_id") != session_id:
            continue
        # We use the raw 'state' field rather than get_task_state() to
        # avoid the migration shim — the LEGACY token (= waiting-review)
        # is terminal-ish in old data, so treating it as such is the
        # correct conservative default.
        st = entry.get("state") or DEFAULT_TASK_STATE
        st = migrate_legacy_task_state(st)
        if st in ("todo", "working"):
            return True
    return False


def session_has_any_claim(trek_doc: dict, *, session_id: str) -> bool:
    """Return True iff ``session_id`` has stamped any task state (ms-88 / e-2109).

    Helper for the fan-out path: a session with **no claims at all** is
    likely a fresh executor about to pick up a todo task — the scheduler
    should keep tickling them. A session with claims only in terminal-ish
    states (= leader_review / user_review / done) has explicitly finished
    and should be quiet.
    """
    if not session_id:
        return False
    states = trek_doc.get("task_states") or {}
    for entry in states.values():
        if not entry:
            continue
        if entry.get("updated_by_session_id") == session_id:
            return True
    return False


def force_stall_session_working_tasks(trek_doc: dict, *,
                                      session_id: str,
                                      reason: str = "ttl-expired") -> list[str]:
    """Server-side 罰則: 当該 session の全 working task を leader_review に強制遷移する (ms-88 / e-2107).

    pulse 不発火 + TTL 経過時に server から呼ばれる。 「該当 1 task のみ stall」
    の旧設計から強化された罰則経路 (= silent silent 経路を構造的に絶つ)。

    Returns the list of task_ids that were transitioned (= caller can
    emit one leader-review DM per task or batch them).
    """
    if not session_id:
        raise ValueError("session_id is required")
    transitioned: list[str] = []
    states = trek_doc.get("task_states") or {}
    for tid, entry in list(states.items()):
        # 「当該 session が claim 済の working task」 だけが対象。
        # updated_by_session_id が一致する working state を全部拾う。
        if get_task_state(trek_doc, tid) != "working":
            continue
        if (entry or {}).get("updated_by_session_id") != session_id:
            continue
        set_task_state(
            trek_doc, task_id=tid, state="leader_review",
            updated_by_session_id=session_id,
            note=f"server-forced: {reason}",
        )
        transitioned.append(tid)
    return transitioned


# ms-88 / e-2106 — pulse-ack log. Layer 2 (= observability) of the 3-layer
# trek autonomy harness (CORE doc 5nfTSmCDVUzD4SLzIhI5). The pulse Skill
# (= /beacon-trek-pulse) calls the server's POST /api/treks/<id>/pulse-ack
# endpoint as its very first step, before any other action. That gives the
# server **ground truth** about whether the Skill actually fired in response
# to a scheduler tick — the "Skill marker visible in executor terminal"
# observation that dogfood (= tk-40b0b27c) found could not be verified
# directly by the server.
#
# Schema on the trek doc:
#   trek_doc.pulse_acks: dict[session_id -> SessionPulseAcks]
#     SessionPulseAcks {
#       session_id: str,
#       total_acks: int,                    # monotonically increasing
#       last_pulse_ack_at: ISO timestamp,
#       last_picked_choice: str,            # 5-choice picker token, see VALID_PULSE_PICKED_CHOICES
#       history: list[PulseAckEntry] (cap 20),  # recent ring buffer
#     }
#     PulseAckEntry {
#       timestamp: ISO,
#       picked_choice: str,
#       note: str (cap 200),
#     }
#
# 20-entry cap on history keeps the trek doc small while preserving enough
# data for time-series visualisation in the Phase 4 UI dashboard (e-2108).
# Per-session aggregation (vs flat list) lets the Phase 4 UI compute
# compliance rate per session cheaply (= no scan).
PULSE_ACK_HISTORY_CAP = 20

# ms-88 / e-2139 — 5-choice executor picker (= /beacon-trek-pulse Step body).
# 'dm-peer' は ms-88 / e-2140 で導入された peer-first culture の構造実装:
# 詰まった時の default action を「user に問う」 から「peer に相談する」 に
# 移すことで、 user 起床まで Trek が autonomous に走り続ける経路を作る。
# 元の 4 択 (terminal / continue / dm-leader / no-op) のうち dm-leader が
# 「上向き相談」、 dm-peer が「横向き相談」 で responsibility 分担される。
VALID_PULSE_PICKED_CHOICES = (
    "terminal",       # executor declared a terminal transition this tick
    "continue",       # executor continues working
    "dm-leader",      # executor asked leader for judgment (= 上向き相談)
    "dm-peer",        # executor asked a peer executor for judgment (= 横向き相談、 ms-88 / e-2140)
    "no-op",          # explicit "I see the tick but nothing to act on"
    "",               # unspecified (= legacy / minimum-info pulse)
)


def validate_pulse_picked_choice(choice: str) -> str:
    """Validate the picked_choice token (ms-88 / e-2106)."""
    if choice not in VALID_PULSE_PICKED_CHOICES:
        raise ValueError(
            f"invalid pulse picked_choice {choice!r} — expected one of "
            f"{VALID_PULSE_PICKED_CHOICES}"
        )
    return choice


# ms-92 / e-2165 — pulse-ack payload structured fields. The free-form
# `note` keeps working for backward compatibility, but executors are
# encouraged to populate the structured fields so the leader-digest
# (= e-2164) can mechanically aggregate "stuck=N idle=M" counts without
# parsing natural-language notes. Hard caps protect both the doc size
# and the server-side aggregation cost.
PULSE_ACK_STATE_SUMMARY_MAX = 100  # 1-line snapshot, ≤100 chars
PULSE_ACK_BLOCKER_MAX = 200        # ≤200 chars per blocker
PULSE_ACK_BLOCKERS_CAP = 3         # at most 3 blockers per pulse


def _normalize_pulse_blockers(blockers) -> list[str]:
    """Coerce + trim the blockers list (= optional structured field).

    Accepts None / list / tuple; each item is str-coerced, stripped,
    and truncated at PULSE_ACK_BLOCKER_MAX. Empty strings are dropped.
    Excess items beyond PULSE_ACK_BLOCKERS_CAP are silently truncated
    so a buggy executor can't blow up the trek doc.
    """
    if not blockers:
        return []
    if isinstance(blockers, str):
        # Some callers may pass a single string; treat as one blocker.
        items = [blockers]
    else:
        items = list(blockers)
    out: list[str] = []
    for item in items:
        s = str(item or "").strip()
        if not s:
            continue
        if len(s) > PULSE_ACK_BLOCKER_MAX:
            s = s[:PULSE_ACK_BLOCKER_MAX]
        out.append(s)
        if len(out) >= PULSE_ACK_BLOCKERS_CAP:
            break
    return out


def record_pulse_ack(trek_doc: dict, *, session_id: str,
                     picked_choice: str = "",
                     note: str = "",
                     state_summary: str = "",
                     blockers=None,
                     needs_leader_judgment: bool = False,
                     time_on_task_seconds: int = 0) -> dict:
    """Append a pulse-ack record for ``session_id`` and bump the counter.

    Called by the server endpoint when /beacon-trek-pulse Skill self-reports
    invocation. Idempotency: each call appends a new history entry (= the
    Skill is supposed to call exactly once per tick; if it calls twice that
    is recorded so observability is honest — dedupe is the caller's choice).

    ms-92 / e-2165 — structured payload fields (= the leader-digest needs
    machine-aggregatable status, free-form ``note`` alone can't be
    counted):

      * ``state_summary`` (str, ≤100 chars): 1-line state snapshot, e.g.
        ``"working on e-100"`` / ``"stuck on e-200"`` / ``"idle"``.
        Truncated silently if too long.
      * ``blockers`` (list[str], ≤3 items, ≤200 chars each): when the
        executor is stuck. Use specific descriptions ("OOM in
        test_foo.py", not "test broken") so the leader-digest highlight
        is actionable.
      * ``needs_leader_judgment`` (bool, default False): set True when
        the executor wants the leader's attention even if not formally
        ``stuck``. Bubbles up to the leader-digest highlight band.
      * ``time_on_task_seconds`` (int, default 0): seconds the executor
        has been on the current task (= 0 if idle). Lets the digest
        sort by "longest stuck" without timestamp math.

    All structured fields are **optional and backward-compat**: existing
    callers that only pass ``picked_choice`` + ``note`` continue to work
    unchanged. The fields are stored on the history record and on the
    session-level summary so the digest can read either.

    Returns the mutated trek_doc; caller persists with ``db.save_trek``.
    """
    if not session_id:
        raise ValueError("session_id is required")
    validate_pulse_picked_choice(picked_choice)
    # Normalise structured fields. Truncation is silent (= caller buggy
    # but record is still useful) rather than raising — pulse-ack is an
    # observability event, not a write-path. We'd rather record a
    # truncated snapshot than reject and leave the digest blind.
    state_summary_norm = (state_summary or "").strip()
    if len(state_summary_norm) > PULSE_ACK_STATE_SUMMARY_MAX:
        state_summary_norm = state_summary_norm[:PULSE_ACK_STATE_SUMMARY_MAX]
    blockers_norm = _normalize_pulse_blockers(blockers)
    try:
        time_on_task_norm = max(0, int(time_on_task_seconds or 0))
    except (TypeError, ValueError):
        time_on_task_norm = 0
    needs_leader_judgment_norm = bool(needs_leader_judgment)

    acks = trek_doc.setdefault("pulse_acks", {})
    entry = acks.get(session_id) or {
        "session_id": session_id,
        "total_acks": 0,
        "last_pulse_ack_at": "",
        "last_picked_choice": "",
        "history": [],
    }
    now = utcnow_iso()
    record = {
        "timestamp": now,
        "picked_choice": picked_choice,
        "note": (note or "")[:200],
        # Structured fields (= e-2165). Always present in records so the
        # digest can rely on the keys without per-record existence checks.
        "state_summary": state_summary_norm,
        "blockers": blockers_norm,
        "needs_leader_judgment": needs_leader_judgment_norm,
        "time_on_task_seconds": time_on_task_norm,
    }
    entry["total_acks"] = int(entry.get("total_acks") or 0) + 1
    entry["last_pulse_ack_at"] = now
    entry["last_picked_choice"] = picked_choice
    # Mirror the structured fields on the session-level summary so a
    # digest can read "latest snapshot per session" without scanning
    # history. Same backward-compat guarantee — pre-e-2165 callers leave
    # them empty / False / 0.
    entry["last_state_summary"] = state_summary_norm
    entry["last_blockers"] = blockers_norm
    entry["last_needs_leader_judgment"] = needs_leader_judgment_norm
    entry["last_time_on_task_seconds"] = time_on_task_norm
    history = entry.get("history") or []
    history.append(record)
    # Ring buffer cap — drop oldest beyond PULSE_ACK_HISTORY_CAP.
    if len(history) > PULSE_ACK_HISTORY_CAP:
        history = history[-PULSE_ACK_HISTORY_CAP:]
    entry["history"] = history
    acks[session_id] = entry
    trek_doc["updated_at"] = now
    return trek_doc


def summarize_pulse_acks(trek_doc: dict) -> dict:
    """Compact per-session summary for dashboards (ms-88 / e-2108 Phase 4).

    Returns:
      {
        "sessions": {
          session_id: {
            "total_acks": int,
            "last_pulse_ack_at": ISO,
            "last_picked_choice": str,
            "choice_counts": {choice: count, ...},
            # ms-92 / e-2165 structured snapshot fields:
            "state_summary": str,
            "blockers": [str, ...],
            "needs_leader_judgment": bool,
            "time_on_task_seconds": int,
          }
        },
        "total_acks_across_sessions": int,
        # ms-92 / e-2165 — aggregates for the leader-digest (e-2164):
        "active_session_count": int,    # session who reported in this digest window
        "stuck_session_count": int,     # session whose latest snapshot has blockers
        "idle_session_count": int,      # session whose latest state_summary contains "idle" or time_on_task=0
        "needs_leader_judgment_count": int,
      }

    Compliance rate (= acks / expected ticks) requires per-session tick
    history which we don't track yet — Phase 4 can either compute it from
    bus event log scans or extend this struct. For now we expose the raw
    counters; the UI can render "session X: 5 acks since Y" without needing
    the denominator.

    ms-92 / e-2165 — the structured-field aggregates lean on the
    session-level mirrors written by ``record_pulse_ack``. Sessions
    predating e-2165 simply contribute empty / False / 0 values so
    counts stay accurate (= no false "stuck" alarms from legacy data).
    """
    sessions: dict = {}
    total = 0
    active = 0
    stuck = 0
    idle = 0
    needs_leader = 0
    for sid, entry in (trek_doc.get("pulse_acks") or {}).items():
        choice_counts: dict = {}
        for h in entry.get("history") or []:
            c = h.get("picked_choice") or ""
            choice_counts[c] = choice_counts.get(c, 0) + 1
        last_state = entry.get("last_state_summary") or ""
        last_blockers = entry.get("last_blockers") or []
        last_needs_leader = bool(entry.get("last_needs_leader_judgment") or False)
        last_time_on_task = int(entry.get("last_time_on_task_seconds") or 0)
        sessions[sid] = {
            "total_acks": int(entry.get("total_acks") or 0),
            "last_pulse_ack_at": entry.get("last_pulse_ack_at") or "",
            "last_picked_choice": entry.get("last_picked_choice") or "",
            "choice_counts": choice_counts,
            # Structured snapshot mirrors (= e-2165).
            "state_summary": last_state,
            "blockers": list(last_blockers),
            "needs_leader_judgment": last_needs_leader,
            "time_on_task_seconds": last_time_on_task,
        }
        total += int(entry.get("total_acks") or 0)
        # Aggregate counts only when the session has actually pulsed at
        # least once — otherwise legacy noise (= empty placeholder)
        # would inflate idle counts.
        if int(entry.get("total_acks") or 0) > 0:
            active += 1
            if last_blockers:
                stuck += 1
            if last_needs_leader:
                needs_leader += 1
            # idle ≈ "no work going on right now": either explicit
            # `state_summary` containing "idle" or time_on_task=0.
            if (last_state and "idle" in last_state.lower()) or last_time_on_task == 0:
                idle += 1
    return {
        "sessions": sessions,
        "total_acks_across_sessions": total,
        "active_session_count": active,
        "stuck_session_count": stuck,
        "idle_session_count": idle,
        "needs_leader_judgment_count": needs_leader,
    }


# ---------------------------------------------------------------------------
# Trek Kickoff Ritual (ms-88 / e-2138)
# ---------------------------------------------------------------------------

def get_kickoff_pending(trek_doc: dict, *, session_id: str) -> bool:
    """Return True if ``session_id`` has NOT yet sent its kickoff DM (ms-88 / e-2138).

    Lazy init semantics: missing entry → pending=True (= session never touched
    kickoff endpoint). Existing entry with ``pending=False`` → done. This
    means existing trek docs (= pre-deploy data) treat every session as
    "pending" on first interaction, which is exactly the desired backward-
    compat behaviour (= 「post-deploy で初めて見えた session は必ず kickoff
    から始める」)。
    """
    status_map = trek_doc.get(KICKOFF_HISTORY_KEY) or {}
    entry = status_map.get(session_id) or {}
    if not entry:
        return True
    return bool(entry.get("pending", True))


def mark_kickoff_completed(trek_doc: dict, *, session_id: str,
                           user_id: str = "",
                           kickoff_dm_event_id: str = "") -> dict:
    """Stamp ``session_id`` as having sent its kickoff DM (ms-88 / e-2138).

    Caller (= server endpoint) verifies the kickoff DM was actually sent
    before calling this; we just record the fact. Idempotent — re-calling
    keeps ``sent_at`` of the first call (= 1 回送れば OK)、 後続で kickoff
    DM を再送しても新 stamp は付かない。

    Returns the mutated trek_doc.
    """
    if not session_id:
        raise ValueError("session_id is required")
    status_map = trek_doc.setdefault(KICKOFF_HISTORY_KEY, {})
    existing = status_map.get(session_id) or {}
    if existing.get("pending") is False and existing.get("sent_at"):
        # Already completed; keep original stamp.
        return trek_doc
    now = utcnow_iso()
    status_map[session_id] = {
        "session_id": session_id,
        "user_id": user_id or "",
        "pending": False,
        "sent_at": now,
        "kickoff_dm_event_id": kickoff_dm_event_id or "",
    }
    trek_doc["updated_at"] = now
    return trek_doc


def reset_kickoff_pending(trek_doc: dict, *, session_id: str,
                          user_id: str = "") -> dict:
    """Force a session back into ``pending=True`` state (ms-88 / e-2138).

    Used after ``take-over`` when a fresh session inherits leadership and
    the new session has not yet announced its plan to peers. Resetting
    forces the new leader session to send its own kickoff DM before any
    further pulse-ack progresses.
    """
    if not session_id:
        raise ValueError("session_id is required")
    status_map = trek_doc.setdefault(KICKOFF_HISTORY_KEY, {})
    status_map[session_id] = {
        "session_id": session_id,
        "user_id": user_id or "",
        "pending": True,
        "sent_at": "",
        "kickoff_dm_event_id": "",
    }
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def summarize_kickoff_status(trek_doc: dict) -> dict:
    """Per-session kickoff snapshot for dashboards (ms-88 / e-2138)。"""
    status_map = trek_doc.get(KICKOFF_HISTORY_KEY) or {}
    pending = []
    completed = []
    for sid, entry in status_map.items():
        if entry.get("pending"):
            pending.append({"session_id": sid,
                            "user_id": entry.get("user_id") or ""})
        else:
            completed.append({
                "session_id": sid,
                "user_id": entry.get("user_id") or "",
                "sent_at": entry.get("sent_at") or "",
            })
    return {
        "pending_count": len(pending),
        "completed_count": len(completed),
        "pending_sessions": pending,
        "completed_sessions": completed,
    }


def extend_task_ttl(trek_doc: dict, *, task_id: str,
                    minutes: int, reason: str = "") -> dict:
    """Postpone the TTL safety net deadline on a single task (ms-95 / e-2308).

    Used by the leader (= main session) when it delegates the executor
    work to an out-of-band runner that cannot stamp ``last_activity_at``
    itself — concretely an Agent-tool subagent launched via
    ``/beacon-dispatch`` Task Mode. The subagent runs in a fresh
    ``session_id`` that is not joined to the trek, so it cannot call
    ``beacon trek task-state working``. Without this primitive the
    leader would have to babysit the subagent and re-stamp every 12
    minutes, defeating the point of delegation.

    Effect: stamps ``task_states[task_id].ttl_extended_until = now +
    minutes`` (UTC ISO8601). ``detect_auto_stalled_tasks`` (=
    ``lib.trek_scheduler``) honours this field: any task whose
    extension is in the future is skipped, regardless of how stale
    ``last_activity_at`` is.

    Idempotent: a follow-up call with a longer extension replaces the
    earlier one (= "extend further"); a shorter extension also
    replaces it (= caller can pull the deadline in early if a
    subagent returned). Use ``minutes=0`` (or a negative value) to
    clear the extension and let TTL fire normally on the next tick.

    If the task has no ``task_states`` entry yet (= executor never
    stamped), initialise a default-state record first so the extension
    has somewhere to live. The scheduler will then treat the task as
    "extension active, not yet active" — TTL is moot until the
    extension expires.

    Returns the mutated trek_doc; caller persists with
    ``db.save_trek``.
    """
    if not task_id:
        raise ValueError("task_id is required")
    try:
        minutes_int = int(minutes)
    except (TypeError, ValueError):
        raise ValueError(f"minutes must be an integer, got {minutes!r}")
    states = trek_doc.setdefault("task_states", {})
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now_iso = utcnow_iso()
    entry = states.get(task_id)
    if not entry:
        entry = {
            "state": DEFAULT_TASK_STATE,
            "updated_at": now_iso,
            "updated_by_session_id": "",
            "note": "",
        }
    if minutes_int <= 0:
        # Clear the extension. Use ``None`` rather than a stale
        # timestamp so downstream readers do a simple None check.
        entry.pop("ttl_extended_until", None)
        entry.pop("ttl_extension_reason", None)
    else:
        deadline = now_dt + datetime.timedelta(minutes=minutes_int)
        # Match utcnow_iso format (.%fZ) so downstream _parse_iso reads cleanly.
        entry["ttl_extended_until"] = deadline.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        if reason:
            entry["ttl_extension_reason"] = reason[:120]
        else:
            entry.pop("ttl_extension_reason", None)
    states[task_id] = entry
    trek_doc["updated_at"] = now_iso
    return trek_doc


def bump_task_activity(trek_doc: dict, *, task_id: str,
                       reason: str = "") -> dict:
    """Refresh ``last_activity_at`` on a task without changing its state.

    Called from non-state-change activity sources (= ms-75 / e-2067 AC 1:
    "state stamp / commit / DM 受信で update"). Concretely:
      * a commit lands on a trek-scoped task → bump
      * a DM addressed to the trek's executor arrives → bump

    If the task has no entry in ``task_states`` yet (= executor never
    stamped), we initialise it to the default state with the same
    ``last_activity_at`` so the next scheduler tick treats the task as
    "just heard from" rather than dead. This means commits / DMs alone
    can keep a task off the auto-stall radar even before the executor
    has formally declared a state.

    Returns the mutated trek_doc. Caller persists.
    """
    if not task_id:
        raise ValueError("task_id is required")
    states = trek_doc.setdefault("task_states", {})
    now = utcnow_iso()
    entry = states.get(task_id)
    if not entry:
        entry = {
            "state": DEFAULT_TASK_STATE,
            "updated_at": now,
            "updated_by_session_id": "",
            "note": "",
        }
    entry["last_activity_at"] = now
    if reason:
        entry["last_activity_reason"] = reason[:80]
    states[task_id] = entry
    trek_doc["updated_at"] = now
    return trek_doc


def get_working_ttl_minutes(trek_doc: dict,
                            default: int = DEFAULT_WORKING_TTL_MINUTES) -> int:
    """Return the working-state TTL in minutes (= meta override or default).

    Per ms-75 / e-2067 AC 6: per-trek override at ``trek.meta.
    working_ttl_minutes``. ms-95 / e-2646: also accepts
    ``trek.meta.stall_threshold_minutes`` (= new name) as a synonym, with
    the new name winning on collision. Default is 24h (= 1440 min) so
    prep-waiting executors are not stuck-judged in the small minutes.
    """
    meta = trek_doc.get("meta") or {}
    # New field name (= ms-95 / e-2646) reads first.
    val = meta.get("stall_threshold_minutes")
    if val is None:
        val = meta.get("working_ttl_minutes")
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def aggregate_task_state(trek_doc: dict, *, task_ids: list[str]) -> dict:
    """Summarise Trek state across the given task IDs (5 状態、 ms-88 / e-2107).

    Returns counts per state + an overall classification:

        {
          "todo": N0, "working": N1, "leader_review": N2,
          "user_review": N3, "done": N4, "total": T,
          "overall": "active" | "all-done" | "all-user-review" |
                     "all-terminal-mixed" | "empty",

          # Backward-compat alias for callers that still read the old
          # "waiting-review" key — combines leader_review + user_review
          # (= the conflate behaviour of the old 3-state model).
          "waiting-review": N2 + N3,
        }

    "active":  at least one task is `todo` / `working` / `leader_review`
               — scheduler keeps firing for some executor or leader queue.
    "all-done": every task reached `done` — Trek complete, archive candidate.
    "all-user-review": every task waiting for user judgment — terminal at
               Trek-completion granularity, pending external decision.
    "all-terminal-mixed": all tasks terminal but mix of done + user_review
               — Trek complete enough for archive after user消化.
    "empty":   no task IDs supplied.

    Untracked tasks collapse to `todo` (= default), so a freshly-added
    scope task keeps Trek active until claimed (todo → working).

    Trek 完遂判定 (= scheduler / leader 走り続け判定): `todo` / `working` /
    `leader_review` のいずれかが 1 つでもあれば走り続ける、 全部 `done` OR
    `user_review` で停止 (= CORE doc 5nfTSmCDVUzD4SLzIhI5 § "Trek 完遂判定")。
    """
    counts = {s: 0 for s in VALID_TASK_STATES}
    total = 0
    for tid in task_ids:
        if not tid:
            continue
        total += 1
        counts[get_task_state(trek_doc, tid)] += 1
    # active = scheduler / leader が走り続けるべき状態 (= todo / working /
    # leader_review のいずれかが残っている)。 完遂判定の補集合。
    non_terminal_count = counts["todo"] + counts["working"] + counts["leader_review"]
    if total == 0:
        overall = "empty"
    elif non_terminal_count > 0:
        overall = "active"
    elif counts["done"] == total:
        overall = "all-done"
    elif counts["user_review"] == total:
        overall = "all-user-review"
    else:
        overall = "all-terminal-mixed"
    return {
        "todo": counts["todo"],
        "working": counts["working"],
        "leader_review": counts["leader_review"],
        "user_review": counts["user_review"],
        "done": counts["done"],
        "total": total,
        "overall": overall,
        # Legacy alias for callers still reading old key.
        "waiting-review": counts["leader_review"] + counts["user_review"],
    }


def build_actor_ref(*, user_id: str, email: str) -> dict:
    """Canonical actor reference (= user_id + email pair).

    SPEC 設計方針 3 collapses member identity to user grain (user_id + email)
    so a single user with 3 terminals × 2 machines counts as 1 member.
    Leader / claim live at session grain instead (see ``leader_session_id``
    on the trek doc and ms-55 claim model).
    """
    if not user_id or not email:
        raise ValueError("actor ref requires both user_id and email")
    return {"user_id": user_id, "email": email}


def build_halt(*, issued_by_session_id: str, reason: str = "") -> dict:
    """Build a halt record (= the Andon cord signal).

    Set on the trek doc's ``halt`` field by STOP, cleared by resume.
    State stays ``active`` either way — halt is not a status (SPEC 方針 2).
    """
    if not issued_by_session_id:
        raise ValueError("halt requires issued_by_session_id")
    return {
        "issued_at": utcnow_iso(),
        "issued_by_session_id": issued_by_session_id,
        "reason": reason,
    }


def build_member(*, user_id: str, email: str,
                 role: str = "member",
                 invited_at: str | None = None,
                 joined_at: str = "",
                 invited_by: str = "") -> dict:
    """Build a trek member dict.

    ``invited_at`` defaults to now if omitted. ``joined_at`` empty means
    "invited but not yet joined" (= visible to invitee but they have not
    accepted). ``invited_by`` is the user_id of the inviter; empty for
    self-created leader membership at creation time.
    """
    validate_role(role)
    if not user_id or not email:
        raise ValueError("member requires both user_id and email")
    return {
        "user_id": user_id,
        "email": email,
        "role": role,
        "invited_at": invited_at or utcnow_iso(),
        "joined_at": joined_at,
        "invited_by": invited_by,
    }


def build_session_history_entry(*, session_id: str, user_id: str,
                                email: str, joined_at: str,
                                role_at_join: str) -> dict:
    """Build a single session_history entry.

    ms-86 / e-2225. ``role_at_join`` is the role the member had at the
    moment this session joined. We preserve it even if the member is
    later promoted / demoted via ``transfer_leader`` etc., so the history
    answers "which session was acting as leader at that point in time"
    without requiring an external audit log.
    """
    if not session_id:
        raise ValueError("session_history entry requires session_id")
    if not user_id or not email:
        raise ValueError(
            "session_history entry requires user_id and email"
        )
    validate_role(role_at_join)
    return {
        "session_id": session_id,
        "user_id": user_id,
        "email": email,
        "joined_at": joined_at or utcnow_iso(),
        "role_at_join": role_at_join,
    }


def find_session_history(trek_doc: dict, session_id: str) -> dict | None:
    """Return the session_history entry for ``session_id`` (or None).

    Linear scan — session_history is expected to stay small (= ~tens of
    entries per Trek over its lifetime). If this becomes a hot path we can
    switch to a dict keyed by session_id, but today the field is touched
    on every render so we want stable list order over indexed lookup.
    """
    if not session_id:
        return None
    for entry in trek_doc.get(SESSION_HISTORY_KEY) or []:
        if entry.get("session_id") == session_id:
            return entry
    return None


def upsert_session_history(trek_doc: dict, *, session_id: str,
                           user_id: str, email: str,
                           role_at_join: str,
                           joined_at: str = "") -> dict:
    """Append a session_history entry, or no-op if session_id already present.

    ms-86 / e-2225 AC2. Called by ``accept_invitation`` (= the moment a
    session takes a member's invitation), by ``new_trek`` (= creator's
    initial leader session), and by the one-shot backfill migration.

    Empty ``session_id`` is tolerated as a no-op (= some legacy code paths
    might call this without a session id; we don't want to raise and
    break those, but we also don't write an entry with an empty key).
    """
    if not session_id:
        return trek_doc
    if find_session_history(trek_doc, session_id) is not None:
        return trek_doc  # already recorded, no-op (= upsert semantics)
    entry = build_session_history_entry(
        session_id=session_id,
        user_id=user_id,
        email=email,
        joined_at=joined_at or utcnow_iso(),
        role_at_join=role_at_join,
    )
    trek_doc.setdefault(SESSION_HISTORY_KEY, []).append(entry)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def backfill_session_history(trek_doc: dict) -> int:
    """One-shot derivation of session_history from existing fields.

    ms-86 / e-2225 AC3. Walks the three pre-existing session_id locations
    on a Trek (= ``leader_session_id`` / ``halt.issued_by_session_id`` /
    ``task_states[].updated_by_session_id``) and adds any missing entries.
    Returns the number of entries added (= 0 if nothing to backfill).

    Used by ``scripts/backfill_trek_session_history.py`` for stored docs,
    and called lazily by ``trek_store.load_trek`` so even Treks that were
    never run through the migration script self-heal on next read.

    Heuristics for filling required entry fields when the source doesn't
    record them directly:
    - ``user_id`` / ``email``: looked up via the ``leader_session_id`` →
      leader member, otherwise via the first joined member as a best-effort
      attribution. External sessions (= halt issued by a session that
      isn't in members[]) are recorded with empty user_id / email so they
      still appear in the UI but flag as unknown.
    - ``joined_at``: prefer the relevant timestamp on the source record
      (= halt.issued_at, task_states[].updated_at), falling back to
      trek.created_at when only the session_id is known.
    - ``role_at_join``: leader_session_id → "leader"; everyone else
      → "member" (= the conservative choice that matches the new write
      path's default).
    """
    members = trek_doc.get("members") or []
    by_user_id = {m.get("user_id"): m for m in members if m.get("user_id")}
    # Fallback identity used when a session id appears outside members[]
    # (= external halt-issuer). Use the first joined member as a coarse
    # attribution so the UI still shows something readable.
    fallback_member = next(
        (m for m in members if m.get("joined_at")), None,
    )

    def _identity_for(role_hint: str) -> tuple[str, str]:
        if role_hint == "leader" and members:
            leader = next(
                (m for m in members if m.get("role") == "leader"),
                None,
            )
            if leader:
                return leader.get("user_id", ""), leader.get("email", "")
        if fallback_member:
            return (
                fallback_member.get("user_id", ""),
                fallback_member.get("email", ""),
            )
        return "", ""

    created_at = trek_doc.get("created_at") or utcnow_iso()
    added = 0

    # 1. leader_session_id — current leader's session
    leader_sid = trek_doc.get("leader_session_id") or ""
    if leader_sid and find_session_history(trek_doc, leader_sid) is None:
        uid, email = _identity_for("leader")
        # If the leader member has a joined_at, prefer that; else fall back
        # to trek.created_at (= the only earlier timestamp we have).
        leader_member = next(
            (m for m in members if m.get("role") == "leader"), None,
        )
        joined_at = (
            (leader_member or {}).get("joined_at") or created_at
        )
        # Internal write path (no upsert helper to keep backfill explicit
        # about why role_at_join is leader here).
        trek_doc.setdefault(SESSION_HISTORY_KEY, []).append({
            "session_id": leader_sid,
            "user_id": uid,
            "email": email,
            "joined_at": joined_at,
            "role_at_join": "leader",
        })
        added += 1

    # 2. task_states[].updated_by_session_id — sessions that stamped tasks
    task_states = trek_doc.get("task_states") or {}
    for task_id, ent in task_states.items():
        sid = (ent or {}).get("updated_by_session_id") or ""
        if not sid or find_session_history(trek_doc, sid) is not None:
            continue
        uid, email = _identity_for("member")
        joined_at = (ent or {}).get("updated_at") or created_at
        trek_doc.setdefault(SESSION_HISTORY_KEY, []).append({
            "session_id": sid,
            "user_id": uid,
            "email": email,
            "joined_at": joined_at,
            "role_at_join": "member",
        })
        added += 1

    # 3. halt.issued_by_session_id — halt issuer (may be external)
    halt = trek_doc.get("halt") or {}
    halt_sid = halt.get("issued_by_session_id") or ""
    if halt_sid and find_session_history(trek_doc, halt_sid) is None:
        # If the halt issuer isn't in members[], record empty identity
        # — they're an external session by definition.
        in_members = bool(by_user_id) and any(
            False for _ in ()  # placeholder, see below
        )
        # We don't know the user_id from session_id alone; degrade to
        # fallback identity (= same as task_states path).
        uid, email = _identity_for("member")
        joined_at = halt.get("issued_at") or created_at
        trek_doc.setdefault(SESSION_HISTORY_KEY, []).append({
            "session_id": halt_sid,
            "user_id": uid,
            "email": email,
            "joined_at": joined_at,
            "role_at_join": "member",
        })
        added += 1
        # Suppress unused-flag lint without introducing a real branch.
        _ = in_members

    if added:
        trek_doc["updated_at"] = utcnow_iso()
    return added


def normalize_scope_entry(entry: dict) -> dict:
    """Normalise a scope item.

    A scope entry MUST include ``project`` (= project_id) and MAY include
    one of milestone / operation / task to narrow it. Unknown keys are
    dropped to keep the on-disk schema tight (= server side can validate
    against this normalisation, CLI side can also use it before posting).
    """
    if not entry.get("project"):
        raise ValueError("scope entry missing required 'project' field")
    out: dict = {"project": entry["project"]}
    for k in ("milestone", "operation", "task"):
        if entry.get(k):
            out[k] = entry[k]
    return out


DEFAULT_CADENCE_MINUTES = 10
"""ms-83 (= server-side execution continuity / e-1994): default cadence
(= the periodic "next, please" DM interval) in minutes when ``cadence_minutes``
is not set on a trek. 10 minutes balances responsiveness against bus volume.

Stored on ``trek.meta.cadence_minutes`` as an ``int`` (or ``None`` if the
trek operator hasn't set one — the scheduler treats None as the default).
"""


def new_trek(*,
             title: str,
             creator_user_id: str,
             creator_email: str,
             creator_session_id: str,
             description: str = "",
             type_: str = DEFAULT_TYPE,
             initial_scope: Iterable[dict] | None = None,
             goal_state: str = "",
             cadence_minutes: int | None = None,
             manager_agent_url: str = "") -> dict:
    """Build a fresh trek doc (= not yet persisted, no I/O).

    The creator is:
    - recorded as ``creator_actor`` (= user grain, durable identity)
    - automatically added as the first ``member`` with role ``leader``
      (= user grain again, membership is per-person)
    - their session is recorded as ``leader_session_id`` (= the actual
      live session that currently leads the trek, can be transferred
      later via `beacon trek transfer-leader --to <session_id>`)

    Status starts at ``planning`` so the caller can stage scope / invites
    before any session joins. ``halt`` starts None — STOP / resume toggle
    it without changing status (SPEC 方針 2).

    ``goal_state`` (ms-75 / e-1865) is a free-form acceptance criterion
    describing "what completion looks like" for this trek. Optional —
    if empty, the trek's end is decided by the leader's manual archive,
    matching previous behaviour. When non-empty, ``beacon trek show``
    surfaces it so members share a common completion signal, and the
    leader can confidently archive once the criterion is met.

    ``cadence_minutes`` (ms-83 / e-1994) sets how often the server-side
    scheduler (= the loop that fires "next, please" progress-check DMs
    into the trek's claimed session) should wake this trek. ``None`` =
    default 10 minutes (scheduler honours ``DEFAULT_CADENCE_MINUTES``).
    Stored on ``meta`` so the on-disk shape stays orthogonal to
    structural fields (= status / members / scope).

    ``manager_agent_url`` (ms-83 / e-1994) is a schema reservation for
    a future "manager AI" agent endpoint that decides cadence and DM
    body in place of the built-in template. Always optional in this
    MS — the value is recorded but no consumer reads it yet.
    """
    if not title.strip():
        raise ValueError("trek title is required")
    if not creator_session_id:
        raise ValueError(
            "creator_session_id is required (= the session that creates "
            "the trek becomes its initial leader; SPEC 方針 9)"
        )
    validate_type(type_)
    if cadence_minutes is not None:
        _validate_cadence_minutes(cadence_minutes)
    now = utcnow_iso()
    creator_actor = build_actor_ref(
        user_id=creator_user_id, email=creator_email
    )
    leader_member = build_member(
        user_id=creator_user_id, email=creator_email,
        role="leader", invited_at=now, joined_at=now,
        invited_by=creator_user_id,
    )
    scope = [normalize_scope_entry(s) for s in (initial_scope or [])]
    meta: dict = {}
    if cadence_minutes is not None:
        meta["cadence_minutes"] = int(cadence_minutes)
    url = (manager_agent_url or "").strip()
    if url:
        meta["manager_agent_url"] = url
    # ms-86 / e-2225 — seed session_history with the creator's session
    # so the persistent record exists from t=0 instead of being filled
    # in lazily on the next join. The creator is by definition a leader
    # at the moment of creation.
    initial_history = [{
        "session_id": creator_session_id,
        "user_id": creator_user_id,
        "email": creator_email,
        "joined_at": now,
        "role_at_join": "leader",
    }]
    return {
        "trek_id": mint_trek_id(),
        "title": title.strip(),
        "description": description,
        "type": type_,
        "status": DEFAULT_STATUS,
        "creator_actor": creator_actor,
        "leader_session_id": creator_session_id,
        "members": [leader_member],
        "scope": scope,
        "halt": None,
        "goal_state": (goal_state or "").strip(),
        "meta": meta,
        "task_states": {},
        "session_history": initial_history,
        # ms-97 / e-2611 — pending scope mutations awaiting user approval.
        # Empty by default; populated by ``add_pending_scope_op``. Existing
        # treks (= pre-e-2611 docs) read this as ``[]`` via the helpers'
        # ``or []`` fallback, so no migration is required.
        PENDING_SCOPE_OPS_KEY: [],
        # ms-97 / e-2658 (Phase 0) — AC6 cutover safety scaffolding.
        # members[] が session_id keyed に書き換わる cutover の前に、
        # 元の user_id keyed entries をここに退避する。 Phase 0 段階
        # では空 list で seed のみ (= 実 migration は Phase 1)。 既存
        # trek (= pre-e-2658 docs) は helper の ``or []`` fallback で
        # 同等に扱われ、 schema 互換性が保たれる。
        MEMBERS_LEGACY_BACKUP_KEY: [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
    }


def _validate_cadence_minutes(value: int) -> None:
    """Cadence must be a positive int (= no boolean coercion, no zero).

    Zero would burn the bus by firing every server tick; negative makes
    no sense. ms-83 / e-1994.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"cadence_minutes must be int, got {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(
            f"cadence_minutes must be > 0, got {value}"
        )


def get_cadence_minutes(trek_doc: dict) -> int:
    """Return effective cadence for this trek (= falls back to default).

    Used by the server-side scheduler (= the loop that fires periodic
    progress-check DMs, ms-83 / e-1997). Decoupling the read from the
    field name lets callers stay ignorant of the meta location, and the
    default switch (= ``DEFAULT_CADENCE_MINUTES``) lives in one place.
    """
    meta = trek_doc.get("meta") or {}
    val = meta.get("cadence_minutes")
    if val is None:
        return DEFAULT_CADENCE_MINUTES
    return int(val)


def set_cadence_minutes(trek_doc: dict, *,
                        cadence_minutes: int | None) -> dict:
    """Set or clear ``meta.cadence_minutes`` on an existing trek (ms-83 / e-1994).

    ``None`` clears the field (= scheduler falls back to default).
    Idempotent: re-setting the same value is a no-op so fixtures and
    Skill retries don't churn ``updated_at`` (= mirrors ``set_goal_state``).
    """
    meta = trek_doc.setdefault("meta", {})
    current = meta.get("cadence_minutes")
    if cadence_minutes is None:
        if current is None:
            return trek_doc
        meta.pop("cadence_minutes", None)
    else:
        _validate_cadence_minutes(cadence_minutes)
        if current == int(cadence_minutes):
            return trek_doc
        meta["cadence_minutes"] = int(cadence_minutes)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def set_manager_agent_url(trek_doc: dict, *,
                          manager_agent_url: str) -> dict:
    """Set or clear ``meta.manager_agent_url`` on an existing trek (ms-83 / e-1994).

    Empty string clears the field. Idempotent: re-setting the same value
    is a no-op. The URL is a **schema reservation** in this MS — no
    consumer reads it yet, so this setter is the only forward edge.
    """
    meta = trek_doc.setdefault("meta", {})
    current = meta.get("manager_agent_url", "")
    new_val = (manager_agent_url or "").strip()
    if new_val == current:
        return trek_doc
    if new_val:
        meta["manager_agent_url"] = new_val
    else:
        meta.pop("manager_agent_url", None)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def set_goal_state(trek_doc: dict, *, goal_state: str) -> dict:
    """Set or update ``goal_state`` on an existing trek (ms-75 / e-1865).

    Empty string clears the field (= back to "leader decides when done").
    Idempotent: re-setting the same value is a no-op (no updated_at bump)
    so test fixtures and Skill retries don't churn the modification time.
    """
    new_val = (goal_state or "").strip()
    if trek_doc.get("goal_state", "") == new_val:
        return trek_doc
    trek_doc["goal_state"] = new_val
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


# Lifecycle transition rules (= server / CLI enforce on state changes).
# 3-state machine: planning → active → archived. ``archived`` is **terminal**
# — to restart work after archive, create a fresh trek with the same scope
# / members. Keeping archived terminal avoids hibernate / wake mode state
# explosion and matches user-facing intuition ("archived = done").
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"active", "archived"}),
    "active": frozenset({"archived"}),
    "archived": frozenset(),  # terminal
}


def validate_transition(from_status: str, to_status: str) -> None:
    """Raise ValueError if ``from_status → to_status`` is not allowed."""
    validate_status(from_status)
    validate_status(to_status)
    if from_status == to_status:
        return
    allowed = ALLOWED_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(
            f"invalid trek transition {from_status!r} → {to_status!r} "
            f"(allowed from {from_status!r}: {sorted(allowed)})"
        )


# ---------------------------------------------------------------------------
# Member operations (ms-69 / e-1654)
#
# Member identity is at user grain (= user_id + email pair), so a single
# user with multiple sessions counts as one member. Per-session presence
# is tracked separately by the session registry (= ``sessions/`` collection)
# and joined at render time, not stored inside the trek doc.
#
# These helpers are pure (= they mutate and return the dict, no I/O).
# Storage callers (lib/trek_store, server/firestore_client) wrap them.
# ---------------------------------------------------------------------------

def find_member(trek_doc: dict, user_id: str) -> dict | None:
    """Return the member dict for ``user_id``, or None if absent."""
    for m in trek_doc.get("members") or []:
        if m.get("user_id") == user_id:
            return m
    return None


def find_member_by_email(trek_doc: dict, email: str) -> dict | None:
    """Return the member dict for ``email``, or None if absent.

    Used by the CLI's local mode invite/join flow where the inviter only
    knows the invitee's email (= cloud user resolution lands in e-1656).
    """
    if not email:
        return None
    for m in trek_doc.get("members") or []:
        if m.get("email") == email:
            return m
    return None


def add_invitation(trek_doc: dict, *,
                   user_id: str, email: str,
                   invited_by_user_id: str) -> dict:
    """Add a new member to the trek with ``joined_at=""`` (= invited, not joined).

    Raises ValueError if the user is already a member. Mutates and returns
    the trek doc so callers can persist with a single save_trek.
    """
    if find_member(trek_doc, user_id) is not None:
        raise ValueError(
            f"user {user_id} is already a member of trek "
            f"{trek_doc.get('trek_id')}"
        )
    new_member = build_member(
        user_id=user_id, email=email,
        role="member",
        invited_at=utcnow_iso(),
        joined_at="",
        invited_by=invited_by_user_id,
    )
    trek_doc.setdefault("members", []).append(new_member)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def accept_invitation(trek_doc: dict, *, user_id: str,
                      session_id: str = "") -> dict:
    """Mark a member as joined (= sets ``joined_at`` to now).

    Idempotent on the member dimension: if the member already joined,
    ``joined_at`` is preserved. ``session_id`` (= ms-86 / e-2225) records
    the actual session that performed this join into ``session_history``
    so the Trek doc keeps a cumulative record of every session that has
    ever participated. The session_history write is per-session-id
    idempotent (= no duplicate entry for the same session), so re-running
    join from the same session is safe.

    Raises ValueError if ``user_id`` is not in the members list (= must
    be invited first, no self-add).
    """
    member = find_member(trek_doc, user_id)
    if member is None:
        raise ValueError(
            f"user {user_id} not invited to trek {trek_doc.get('trek_id')} "
            "(owner must `beacon trek invite` first)"
        )
    # session_history write is independent of the member.joined_at idempotency
    # because the same user can join from N sessions over time — each one
    # is a distinct history entry, but the member dict still says "joined".
    if session_id:
        role_at_join = member.get("role") or "member"
        upsert_session_history(
            trek_doc,
            session_id=session_id,
            user_id=member.get("user_id") or user_id,
            email=member.get("email") or "",
            role_at_join=role_at_join,
        )
    if member.get("joined_at"):
        return trek_doc  # already joined, member.joined_at preserved
    member["joined_at"] = utcnow_iso()
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def parse_scope_arg(arg: str) -> dict:
    """Parse a CLI ``<project>[:<ref>]`` scope argument into a normalized entry.

    ``ref`` is dispatched by prefix:
    - ``ms-...`` → milestone
    - ``op-...`` → operation
    - ``e-...``  → task
    - omitted    → project-wide scope (= no narrowing)

    Returns the normalized scope dict ready to append to ``trek_doc.scope``.
    Raises ValueError on empty input or unknown ref prefix.
    """
    if not arg or not arg.strip():
        raise ValueError("scope argument cannot be empty")
    arg = arg.strip()
    if ":" in arg:
        project, ref = arg.split(":", 1)
        project = project.strip()
        ref = ref.strip()
        if not project:
            raise ValueError(f"scope arg {arg!r} missing project")
        entry: dict = {"project": project}
        if not ref:
            return normalize_scope_entry(entry)
        if ref.startswith("ms-"):
            entry["milestone"] = ref
        elif ref.startswith("op-"):
            entry["operation"] = ref
        elif ref.startswith("e-"):
            entry["task"] = ref
        else:
            raise ValueError(
                f"unknown ref prefix in {arg!r} — expected ms-/op-/e- "
                "(or omit ref for project-wide scope)"
            )
        return normalize_scope_entry(entry)
    return normalize_scope_entry({"project": arg})


# ---------------------------------------------------------------------------
# Cross-project task add via Trek scope (ms-92 / e-2141)
# ---------------------------------------------------------------------------


# Outcomes returned by ``check_trek_task_add_allowed``. Kept as plain
# string constants so server-side rejections can map them to HTTP codes
# (allowed / project_not_in_scope / milestone_not_in_scope /
# scope_only_has_task_narrowing) without exposing internal helpers.
TASK_ADD_ALLOWED = "allowed"
TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE = "project_not_in_scope"
TASK_ADD_REJECT_MILESTONE_NOT_IN_SCOPE = "milestone_not_in_scope"
TASK_ADD_REJECT_SCOPE_ONLY_HAS_TASK_NARROWING = "scope_only_has_task_narrowing"


def check_trek_task_add_allowed(
    trek_doc: dict, *,
    target_project: str,
    target_milestone: str,
) -> tuple[bool, str]:
    """Decide whether the trek's scope authorises ``task.add`` on a target.

    ms-92 / e-2141. The CLI / server share the same yes/no question:
    *given this Trek's recorded scope, is the caller allowed to add a
    task under ``target_project`` / ``target_milestone``?* Returning
    ``(False, <reason>)`` lets callers map a single reason to either a
    403 (server) or a friendly CLI error.

    Rules (= SPEC of e-2141, also tracked by 4 unit-test paths):

    1. Empty ``target_project`` or ``target_milestone`` → reject as
       ``project_not_in_scope`` / ``milestone_not_in_scope`` respectively.
       Callers must always supply both. Empty inputs mean the picker /
       parser upstream is buggy; we refuse rather than guess.

    2. The trek's ``scope`` must contain at least one entry matching
       ``project == target_project``. Otherwise reject as
       ``project_not_in_scope``.

    3. Among the matching project entries, at least one must be **wide
       enough** to cover the target milestone:

       * project-wide entry (= ``{"project": pid}`` with no milestone /
         operation / task narrowing) → covers anything under that
         project. allowed.
       * milestone entry (= ``{"project": pid, "milestone": ms-id}``)
         whose ``milestone`` equals ``target_milestone`` → allowed.

       Otherwise reject as ``milestone_not_in_scope``.

    4. The "task-only narrowing" forbidden case (= AC #4): if **every**
       matching project entry is task-narrowed (= ``{"project": pid,
       "task": e-XXX}``) and none of them is project-wide or matches
       the target milestone, reject as
       ``scope_only_has_task_narrowing``. This preserves the user
       responsibility frame: tasks are MS-grained decisions. Letting
       Treks add tasks under another Trek's single-task scope would
       let trees of tasks sprout sideways without any MS-level intent.

    Operation-narrowed scope entries (= ``{"project": pid,
    "operation": op-XX}``) are treated like task narrowings here — they
    don't authorise sideways task creation; we reject the same way so
    Operation work also stays MS-grained. (If someone needs cross-Op
    task add later, that's its own design decision.)
    """
    if not target_project or not target_milestone:
        if not target_project:
            return False, TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE
        return False, TASK_ADD_REJECT_MILESTONE_NOT_IN_SCOPE
    scope = trek_doc.get("scope") or []
    matching_project_entries = [
        s for s in scope if s.get("project") == target_project
    ]
    if not matching_project_entries:
        return False, TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE
    for entry in matching_project_entries:
        ms_ref = entry.get("milestone") or ""
        op_ref = entry.get("operation") or ""
        task_ref = entry.get("task") or ""
        if not ms_ref and not op_ref and not task_ref:
            # Project-wide scope entry — covers anything under this project.
            return True, TASK_ADD_ALLOWED
        if ms_ref and ms_ref == target_milestone:
            return True, TASK_ADD_ALLOWED
    # No project-wide and no milestone-matching entry. Distinguish the
    # "task-only narrowing" pathology (= AC #4) from a generic milestone
    # mismatch so the CLI can show the right hint.
    only_task_or_op_narrowed = all(
        (entry.get("task") or entry.get("operation"))
        and not entry.get("milestone")
        for entry in matching_project_entries
    )
    if only_task_or_op_narrowed:
        return False, TASK_ADD_REJECT_SCOPE_ONLY_HAS_TASK_NARROWING
    return False, TASK_ADD_REJECT_MILESTONE_NOT_IN_SCOPE


def add_scope_entry(trek_doc: dict, *, entry: dict) -> dict:
    """Append a scope entry; raises ValueError if it already exists."""
    norm = normalize_scope_entry(entry)
    for existing in trek_doc.get("scope") or []:
        if existing == norm:
            raise ValueError(f"scope entry already exists: {norm}")
    trek_doc.setdefault("scope", []).append(norm)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def remove_scope_entry(trek_doc: dict, *, entry: dict) -> dict:
    """Remove a scope entry; raises ValueError if not found."""
    norm = normalize_scope_entry(entry)
    scope = trek_doc.get("scope") or []
    new_scope = [s for s in scope if s != norm]
    if len(new_scope) == len(scope):
        raise ValueError(f"scope entry not found: {norm}")
    trek_doc["scope"] = new_scope
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


# ---------------------------------------------------------------------------
# Scope mutation pending approval (ms-97 / e-2611, AC25)
#
# Scope-add and scope-remove are not immediate any more — both go through a
# ``pending_user_approval`` state recorded in ``trek_doc.pending_scope_ops``.
# The user runs ``beacon trek scope-approve <pending_id>`` (= commit) or
# ``beacon trek scope-reject <pending_id>`` (= drop) to flush each pending
# entry into ``scope[]`` (or discard it).
#
# Phase 1b lands the structural pieces ONLY for scope-remove (= the e-2611
# AC25 cut). scope-add already short-circuits at the immediate path (= AC23
# Phase 1a lives in a separate task), so the pending flow accepts both
# actions but the call sites in lib/commands.py / server/app.py only route
# scope-remove through it for this commit. Blanket pre-approval (= AC24 /
# e-2603) layers on top of this without redesign — it checks the pending
# entry's ``action`` field and auto-approves matching categories.
#
# Schema: trek_doc.pending_scope_ops: list of pending op records.
#     {
#       "pending_id": "sp-<hex8>",
#       "action": "scope_remove" | "scope_add",
#       "entry": <normalised scope entry>,
#       "requested_by_session_id": str,
#       "requested_at": ISO8601,
#     }
# ---------------------------------------------------------------------------

PENDING_SCOPE_OPS_KEY = "pending_scope_ops"
PENDING_SCOPE_ACTION_ADD = "scope_add"
PENDING_SCOPE_ACTION_REMOVE = "scope_remove"
VALID_PENDING_SCOPE_ACTIONS = (
    PENDING_SCOPE_ACTION_ADD,
    PENDING_SCOPE_ACTION_REMOVE,
)


# ---------------------------------------------------------------------------
# AC6 / AC34 migration scaffolding (= ms-97 Phase 0, e-2658)
# ---------------------------------------------------------------------------
# `members[]` を user_id keyed (= 旧 SPEC) から session_id keyed (= ms-97
# AC6) に書き換える cutover を、 既存 trek の不可逆破壊から守る 5 機構の
# 最小単位 (= backup field + migration_phase tracker)。 詳細な migration
# script / 復元 script / 不整合 alarming は別 commit で順次 land する。

MEMBERS_LEGACY_BACKUP_KEY = "members_legacy_backup"

# Phase A = backup を取って members[] を session_id keyed に書き換えた直後
# Phase B = backup 維持 + 新経路で 1 ヶ月運用 (= 観察期間)
# Phase C = backup 削除 (= 完全 cutover、 rollback 不可)
# pre-A = migration 未着手 (= 既存 trek の default、 旧 user_id keyed のまま)
DEFAULT_MIGRATION_PHASE = "pre-A"
VALID_MIGRATION_PHASES = ("pre-A", "A", "B", "C")
MIGRATION_PHASE_META_KEY = "migration_phase"


def get_migration_phase(trek_doc: dict) -> str:
    """Return the trek's members migration phase (= "pre-A" if absent).

    `trek.meta.migration_phase` が無い trek は migration 未着手として
    扱う (= 旧 user_id keyed のまま)。 既存 trek が読み出された時に
    silent に "pre-A" を返すので、 読み手の分岐が読みやすい。
    """
    meta = trek_doc.get("meta") or {}
    phase = meta.get(MIGRATION_PHASE_META_KEY) or DEFAULT_MIGRATION_PHASE
    if phase not in VALID_MIGRATION_PHASES:
        return DEFAULT_MIGRATION_PHASE
    return phase


def set_migration_phase(trek_doc: dict, phase: str) -> dict:
    """Stamp `meta.migration_phase` after validating the phase token.

    Returns the mutated trek_doc (= chain-friendly). Caller persists via
    db.save_trek. ValueError for unknown phase tokens.
    """
    if phase not in VALID_MIGRATION_PHASES:
        raise ValueError(
            f"migration_phase {phase!r} not in {VALID_MIGRATION_PHASES}"
        )
    meta = trek_doc.setdefault("meta", {})
    meta[MIGRATION_PHASE_META_KEY] = phase
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def backup_legacy_members(trek_doc: dict) -> dict:
    """Snapshot current `members[]` into `members_legacy_backup[]`.

    既存 trek を AC6 cutover (= session_id keyed) に進める時、 元の
    user_id keyed entries を別 field に退避してから本体を書き換える
    ことで、 不整合発覚時に reverse migration で復元可能にする。

    `members_legacy_backup` が既に non-empty なら ValueError (= 二重
    backup を防ぐ、 巻き戻し失敗で snapshot を上書きしないため)。
    Returns the mutated trek_doc.
    """
    existing_backup = trek_doc.get(MEMBERS_LEGACY_BACKUP_KEY) or []
    if existing_backup:
        raise ValueError(
            f"{MEMBERS_LEGACY_BACKUP_KEY} is non-empty — refusing to "
            f"overwrite snapshot (= guard against double backup)"
        )
    current_members = list(trek_doc.get("members") or [])
    trek_doc[MEMBERS_LEGACY_BACKUP_KEY] = [dict(m) for m in current_members]
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def restore_legacy_members(trek_doc: dict) -> dict:
    """Reverse `backup_legacy_members` — restore `members[]` from backup.

    Phase A → pre-A への hot rollback 経路。 backup が空なら ValueError
    (= 復元元が無い)。 復元後は backup field を空 list に戻し、
    migration_phase を pre-A に巻き戻す。
    """
    backup = trek_doc.get(MEMBERS_LEGACY_BACKUP_KEY) or []
    if not backup:
        raise ValueError(
            f"{MEMBERS_LEGACY_BACKUP_KEY} is empty — nothing to restore"
        )
    trek_doc["members"] = [dict(m) for m in backup]
    trek_doc[MEMBERS_LEGACY_BACKUP_KEY] = []
    meta = trek_doc.setdefault("meta", {})
    meta[MIGRATION_PHASE_META_KEY] = "pre-A"
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def mint_pending_scope_op_id() -> str:
    """Generate a fresh pending scope op id (= ``sp-<8 hex>``)."""
    return f"sp-{secrets.token_hex(4)}"


def add_pending_scope_op(
    trek_doc: dict,
    *,
    action: str,
    entry: dict,
    requested_by_session_id: str = "",
) -> dict:
    """Record a pending scope op (= awaiting user approval).

    Returns the new pending record (with its minted ``pending_id``).
    Raises ``ValueError`` if ``action`` is unknown or ``entry`` cannot be
    normalised (= passes through ``normalize_scope_entry``).

    For ``scope_remove`` the helper also checks that the target entry
    actually exists in ``scope[]`` — pending a removal of something that
    isn't there would always 404 at approve-time, surfacing the error
    earlier (= at request time) keeps the flow legible.

    For ``scope_add`` (ms-97 / e-2626 AC23) the helper symmetrically
    rejects entries that already exist in ``scope[]`` (= duplicate
    request), so the user gets the 409 at stage-time instead of at
    approve-time. Without this check, two duplicate-add requests could
    pile up as separate pending records, only the first of which would
    eventually apply.
    """
    if action not in VALID_PENDING_SCOPE_ACTIONS:
        raise ValueError(
            f"pending scope action {action!r} not in {VALID_PENDING_SCOPE_ACTIONS}"
        )
    norm = normalize_scope_entry(entry)
    if action == PENDING_SCOPE_ACTION_REMOVE:
        scope = trek_doc.get("scope") or []
        if not any(s == norm for s in scope):
            raise ValueError(f"scope entry not found: {norm}")
    elif action == PENDING_SCOPE_ACTION_ADD:
        scope = trek_doc.get("scope") or []
        if any(s == norm for s in scope):
            raise ValueError(f"scope entry already present: {norm}")
    record = {
        "pending_id": mint_pending_scope_op_id(),
        "action": action,
        "entry": norm,
        "requested_by_session_id": requested_by_session_id or "",
        "requested_at": utcnow_iso(),
    }
    trek_doc.setdefault(PENDING_SCOPE_OPS_KEY, []).append(record)
    trek_doc["updated_at"] = utcnow_iso()
    return record


def find_pending_scope_op(trek_doc: dict, *,
                          pending_id: str) -> dict | None:
    """Return the pending scope op with the given id, or None."""
    if not pending_id:
        return None
    for rec in trek_doc.get(PENDING_SCOPE_OPS_KEY) or []:
        if rec.get("pending_id") == pending_id:
            return rec
    return None


def _drop_pending_scope_op(trek_doc: dict, *, pending_id: str) -> None:
    """Remove the pending record from trek_doc in place."""
    items = trek_doc.get(PENDING_SCOPE_OPS_KEY) or []
    trek_doc[PENDING_SCOPE_OPS_KEY] = [
        r for r in items if r.get("pending_id") != pending_id
    ]


def approve_pending_scope_op(
    trek_doc: dict,
    *,
    pending_id: str,
) -> dict:
    """Commit a pending scope op (= apply add / remove to ``scope[]``).

    Raises ``ValueError`` if the pending id is not found, or if the
    underlying scope mutation fails (= e.g. add when the entry is
    already in scope, or remove when it's already absent). The pending
    record is removed in either case so the user does not get stuck
    on a permanently-broken entry; this matches the user expectation
    ``approve = "make this real or tell me why not, then forget it"``.

    Returns the now-applied scope entry (= normalised dict).
    """
    rec = find_pending_scope_op(trek_doc, pending_id=pending_id)
    if rec is None:
        raise ValueError(f"pending scope op not found: {pending_id}")
    action = rec.get("action")
    entry = rec.get("entry") or {}
    # Drop first so even a failure does not leave the queue dirty.
    _drop_pending_scope_op(trek_doc, pending_id=pending_id)
    if action == PENDING_SCOPE_ACTION_ADD:
        add_scope_entry(trek_doc, entry=entry)
    elif action == PENDING_SCOPE_ACTION_REMOVE:
        remove_scope_entry(trek_doc, entry=entry)
    else:
        raise ValueError(
            f"pending scope op {pending_id} has unknown action {action!r}"
        )
    trek_doc["updated_at"] = utcnow_iso()
    return entry


def reject_pending_scope_op(
    trek_doc: dict,
    *,
    pending_id: str,
) -> dict:
    """Drop a pending scope op without applying it.

    Returns the removed record. Raises ``ValueError`` if the id is not
    found (= so the CLI can tell the user "this was already approved /
    rejected by someone else" rather than silently swallow the call).
    """
    rec = find_pending_scope_op(trek_doc, pending_id=pending_id)
    if rec is None:
        raise ValueError(f"pending scope op not found: {pending_id}")
    _drop_pending_scope_op(trek_doc, pending_id=pending_id)
    trek_doc["updated_at"] = utcnow_iso()
    return rec


def list_pending_scope_ops(trek_doc: dict) -> list[dict]:
    """Return a copy of the trek's pending scope ops (= safe for read)."""
    return list(trek_doc.get(PENDING_SCOPE_OPS_KEY) or [])


# ---------------------------------------------------------------------------
# Halt + leader transfer (ms-69 / e-1662)
#
# Halt = Andon cord. The STOP signal sets the ``halt`` field; participating
# sessions observe it and stop their autonomous work. Status stays
# ``active`` either way (SPEC 方針 2). Resume clears the field.
#
# Leader transfer hands the ``leader_session_id`` from one session to
# another. The current implementation trusts the caller to verify the
# transferring session is the current leader; server-side enforcement
# lands in e-1656 with proper auth.
# ---------------------------------------------------------------------------

def is_halted(trek_doc: dict) -> bool:
    """Return True if the trek currently carries an active halt signal.

    ms-97 / e-2612 (AC32) — Halt is the Andon cord: while set, server-side
    tick fire (= executor progress-check, leader digest, auto-succession)
    must stop entirely, and DM bypass (= shared_trek_member rule in
    ``dm_gate.should_gate_dm_action``) is also suspended so cross-user
    DMs fall back to the normal budget gate.

    ``halt`` is set via ``set_halt`` (= dict) and cleared via
    ``clear_halt`` (= None). Treat ``None`` / missing / empty-dict as
    "not halted" so legacy trek docs (= pre-halt schema) read as
    not-halted by default.
    """
    halt = trek_doc.get("halt")
    if not halt:
        return False
    if isinstance(halt, dict) and not halt:
        return False
    return True


def set_halt(trek_doc: dict, *,
             issued_by_session_id: str, reason: str = "") -> dict:
    """Engage the Andon cord. Raises if trek is not currently ``active``.

    Halt is idempotent in the sense that re-issuing replaces the prior
    record (= last STOP wins, more recent ``reason`` survives). Callers
    that need atomicity should check ``trek_doc.get("halt")`` first.
    """
    if trek_doc.get("status") != "active":
        raise ValueError(
            f"can only halt an active trek "
            f"(current status: {trek_doc.get('status')!r})"
        )
    trek_doc["halt"] = build_halt(
        issued_by_session_id=issued_by_session_id, reason=reason,
    )
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def clear_halt(trek_doc: dict) -> dict:
    """Clear the halt signal. Idempotent (= no-op if already cleared)."""
    if not trek_doc.get("halt"):
        return trek_doc
    trek_doc["halt"] = None
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def transfer_leader(trek_doc: dict, *, target_session_id: str) -> dict:
    """Hand off ``leader_session_id`` to another session.

    Caller verifies the requesting session is the current leader (or owner)
    — this helper just performs the swap and stamps updated_at. Server
    auth (e-1656) will enforce the verification later.
    """
    if not target_session_id:
        raise ValueError("target_session_id is required for transfer_leader")
    if trek_doc.get("leader_session_id") == target_session_id:
        return trek_doc  # already the leader, idempotent
    trek_doc["leader_session_id"] = target_session_id
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def remove_member(trek_doc: dict, *, user_id: str) -> dict:
    """Remove a member from the trek.

    Guard rails:
    - Cannot remove the leader (= they must `transfer-leader` first).
    - Cannot remove the last member (= archive the trek instead).
    """
    members = trek_doc.get("members") or []
    target = find_member(trek_doc, user_id)
    if target is None:
        raise ValueError(
            f"user {user_id} not a member of trek {trek_doc.get('trek_id')}"
        )
    if target.get("role") == "leader":
        raise ValueError(
            f"cannot remove leader (user {user_id}); use "
            "`beacon trek transfer-leader` to hand off first"
        )
    new_members = [m for m in members if m.get("user_id") != user_id]
    if not new_members:
        raise ValueError(
            "cannot remove last member; archive the trek instead"
        )
    trek_doc["members"] = new_members
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc
