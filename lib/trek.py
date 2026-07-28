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
import hashlib
import secrets
from typing import Callable, Iterable

# ms-109 e-3699 (fable B-2): the Trek scope narrowing vocabulary is sourced from
# the occupation registry so Trek (L1) does not hardcode development vocabulary.
try:
    import occupation
except ImportError:  # pragma: no cover — packaged import path
    from lib import occupation

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
# ms-128 方針5 (e-4366) — Trek 完遂判定: 全 task が `user_review` に至った時点で
# 完遂 (= 唯一の terminal)。todo / working / leader_review が 1 つでもあれば
# scheduler + leader は走り続ける (= leader_review は中途中継)。
# done は Trek 状態機械から除去した。done = 配置 = 顧客到達 = 人間の判断境界で、
# Trek の外の 1 レイヤー上。Trek は user_review で打ち止める (「手前まで運ぶ」)。
# 既存の done stamp は遡行変更せず read-time migrate (done→user_review、下記
# LEGACY_TASK_STATE_MIGRATIONS) で吸収する。
# (旧: done も terminal だった。ms-88 で 5 状態化、ms-128 で done 除去し 4 状態。)
VALID_TASK_STATES = ("todo", "working", "leader_review", "user_review")
DEFAULT_TASK_STATE = "todo"
# user_review が唯一の terminal (= Trek 完遂 = 走り続け停止)。done は Trek 外。
TERMINAL_TASK_STATES = ("user_review",)

# ms-97 / e-2706 — review-notify trigger set (= TERMINAL_TASK_STATES の意味的分離)。
# `TERMINAL_TASK_STATES` (= user_review のみ) は「Trek 完遂判定 = 走り続け停止」
# semantic で使う (= trek.py § 完遂判定、 mirror/reconcile 経路)。
# 一方、 leader への「review が要る」 notify は `leader_review` を含めた 2 状態
# (= user_review / leader_review = REVIEW_TRIGGER_STATES) が真。
# 旧 SPEC では `waiting-review` (= 現 `leader_review`) を含めた notify 意図だったが、
# ms-88 e-2107 の 5-state 移行 (waiting-review → leader_review) で check 条件が
# 連動せず、 LPS exec が e-373 を `working → leader_review` に stamp しても
# leader に notify event が届かない構造 bug が dogfood (2026-06-28) で顕在化。
# server/app.py set_trek_task_state_endpoint は本集合で trek-task-review event
# 発火を判定する (= AC1)。
REVIEW_TRIGGER_STATES = ("user_review", "leader_review")

# Backward compat (= ms-88 / e-2107 migration). 旧 `waiting-review` で書かれた
# 既存データは server-forced auto-stall 経路 (= old e-2067) からのものが多く、
# semantic 的には新 `leader_review` (= 「leader 判断要請、 server 強制」) に
# 最も近い。 set_task_state は新コードに対して waiting-review を拒否し
# (= 新コードは 5 状態を直接使う) 、 get_task_state は読み出し時に migrate
# して呼び出し側に新 token を返す。
LEGACY_TASK_STATE_MIGRATIONS = {
    "waiting-review": "leader_review",
    # ms-128 方針5 (e-4366) — done は Trek 状態機械から外れた。既存の done stamp
    # は read-time に user_review (= Trek の打ち止め) として解釈する。書き込み時も
    # migrate されるので「Trek 内で done にしようとする」試みは user_review に
    # 吸収される (= Trek は線を越えない、越えるのは user + deploy)。
    "done": "user_review",
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

# Allowed transitions (ms-128 方針5: 4 状態、done 除去、user_review 打ち止め):
# - claim: todo → working
# - executor: working → {leader_review (= terminalize / 完成 PR 化), user_review}
# - server 強制 (halt): working → leader_review (e-4309)
# - leader (自律 stamp): leader_review → {user_review (思想/目的達成レビュー合格),
#     working (re-work 差し戻し / halt 救済 re-open)}
# - user 却下 (leader CLI 代行): user_review → working
# done は Trek 外 (= 配置境界)。既存 done stamp は read-time に user_review へ
# migrate される (LEGACY_TASK_STATE_MIGRATIONS) ので from-state に done は現れない。
# CORE doc `5nfTSmCDVUzD4SLzIhI5` § "1 枚 transition diagram" 参照。
VALID_TASK_STATE_TRANSITIONS = {
    "todo": ("working",),
    "working": ("leader_review", "user_review"),
    "leader_review": ("user_review", "working"),
    "user_review": ("working",),
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


def mint_slot_id() -> str:
    """Generate a fresh Trek slot id (= ``sl-<8 hex>``, ms-99 / e-2828).

    Slots are first-class scope entries under Trek schema v2. The id is
    minted on new adds; legacy on-disk scope entries without a slot_id
    read back with an empty string (= backfill deferred to Phase 3's
    interactive migration UX per director e-2828 decision, so the audit
    trail records when each slot_id was assigned rather than silently
    hashing one at read time).
    """
    return f"sl-{secrets.token_hex(4)}"


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
    """Translate legacy task-state tokens to the current model.

    Maps old ``waiting-review`` → ``leader_review`` (= the semantic match
    for past server-forced auto-stalls) and ms-128 方針5 の ``done`` →
    ``user_review`` (= Trek は user_review で打ち止め、done は Trek 外)。
    Unknown tokens pass through unchanged so downstream validation can flag
    them properly.
    """
    return LEGACY_TASK_STATE_MIGRATIONS.get(s, s)


def pool_status_to_trek_state(pool_status: str | None) -> str:
    """Map a project-pool task/op status to its Trek task-state equivalent.

    ms-128 方針5 (e-4366): 真値源である project pool の status を Trek 状態機械に
    写す唯一の関数。pool の ``done`` は Trek の terminal 等価 = ``user_review``
    (= 「pool で done = 手前まで運び終えた」)。それ以外 (todo / in_progress /
    None) は ``todo`` 扱い (= まだ拾える仕事)。slot materialize の各所がこの 1
    関数を呼ぶことで、pool→Trek 写像が単一真実源になり drift しない。
    """
    if pool_status == "done":
        return "user_review"
    return DEFAULT_TASK_STATE


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

# ms-128 方針1 (e-4372) — typed tick-response protocol. SINGLE SOURCE OF TRUTH
# for the executor picker tokens (ms-119 review consensus e-4389: the legal
# token set is derived from this taxonomy so a future edit can't drift a
# second copy).
#
# A tick response is an *action that advances the Target*. "Waiting" is not a
# valid response: a session that responds ``no-op`` / ``""`` is classified as
# **no-response** and handed to server intervention (方針6 halt / e-4309),
# exactly as if no pulse had arrived. We classify rather than hard-reject
# (SPEC 方針1) so observability stays honest — the attempt is recorded, it just
# does not count as compliance.
#
# The 4 advancing actions are the picker tokens minus ``no-op`` (dm-leader =
# 上向き相談, dm-peer = 横向き相談, ms-88 / e-2140):
TICK_RESPONSE_ACTIONS = ("continue", "terminal", "dm-leader", "dm-peer")
# Tokens that mean "no advancing action taken this tick" → no-response.
TICK_NO_RESPONSE_TOKENS = ("no-op", "")

# response_class values stamped on each pulse-ack record.
TICK_RESPONSE_CLASS_ACTION = "action"
TICK_RESPONSE_CLASS_NO_RESPONSE = "no-response"

# ms-88 / e-2139 — the legal picker token set is DERIVED (not a second literal)
# so it can't drift from the taxonomy: a token is legal iff it is an advancing
# action or an explicit no-response.
VALID_PULSE_PICKED_CHOICES = TICK_RESPONSE_ACTIONS + TICK_NO_RESPONSE_TOKENS


def _fmt_tokens(tokens) -> str:
    """Human/AI-legible token list (pipe-joined, no Python tuple repr).

    ms-119 AX review (e-4389): error messages embedding a raw tuple repr
    (= ``('continue', ...)``) read as an unclear enum boundary to an AI
    parsing the message. Pipe-join with ``<empty>`` for the "" token.
    """
    return "|".join(t if t else "<empty>" for t in tokens)


def validate_pulse_picked_choice(choice: str) -> str:
    """Validate the picked_choice token (ms-88 / e-2106)."""
    if choice not in VALID_PULSE_PICKED_CHOICES:
        raise ValueError(
            f"invalid pulse picked_choice {choice!r} — expected one of: "
            f"{_fmt_tokens(VALID_PULSE_PICKED_CHOICES)}"
        )
    return choice


def classify_tick_response(choice: str) -> str:
    """Classify a tick-response token as advancing action vs no-response.

    Returns ``TICK_RESPONSE_CLASS_ACTION`` for the 4 advancing actions,
    ``TICK_RESPONSE_CLASS_NO_RESPONSE`` for ``no-op`` / ``""`` (= waiting /
    minimum-info). Raises ``ValueError`` for tokens outside the known
    taxonomy so a typo can't silently be read as compliance.

    This is the read-side truth the server tick (e-4309) and succession
    detection (``_is_miss``) consume to decide whether a session actually
    advanced the Target this tick.
    """
    c = (choice or "").strip()
    if c in TICK_RESPONSE_ACTIONS:
        return TICK_RESPONSE_CLASS_ACTION
    if c in TICK_NO_RESPONSE_TOKENS:
        return TICK_RESPONSE_CLASS_NO_RESPONSE
    raise ValueError(
        f"unknown tick response {choice!r} — expected one of: "
        f"{_fmt_tokens(TICK_RESPONSE_ACTIONS)} (advancing action) or "
        f"{_fmt_tokens(TICK_NO_RESPONSE_TOKENS)} (no-response). Waiting is not "
        "a valid response (ms-128 方針1): if nothing to do, pick up an "
        "unclaimed Target (continue), DM the leader (dm-leader), or terminalize."
    )


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
    # ms-128 方針1 (e-4372) — classify this response as advancing action vs
    # no-response (= waiting). Stamped on every record so the server tick can
    # mechanically tell "advanced the Target" from "sat here", without
    # re-deriving from the raw token. no-op / "" → no-response → intervention.
    response_class = classify_tick_response(picked_choice)
    now = utcnow_iso()
    record = {
        "timestamp": now,
        "picked_choice": picked_choice,
        "response_class": response_class,
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
    entry["last_response_class"] = response_class
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


# ---------------------------------------------------------------------------
# ms-97 / e-2637 — Welcome tick bootstrap.
#
# 観察された病理 (= 2026-06-28 dogfood, LPS が永遠に kickoff ritual を起動
# しない構造) の真因は AC33 per-executor lazy start が「claim 済 slot を
# 1 件でも持つ」 or 「未 claim todo を引き受けうる」 を条件にしているが、
# fresh joiner は claim ゼロ + members[] にすら追加されない (= AC6 gap、
# e-2636 で構造修正) ため AC16 の members iterate で見えず、 結果 server
# tick が永久に来ない。
#
# 構造対策 = join した瞬間に 1 度だけ「welcome tick」 を fire し、 受信
# session 側で AI が kickoff ritual を /beacon-trek-execute Skill 経由で
# 自発起動する経路を作る (= AC33 lazy start の前提条件を満たさない new
# member への wake-up 経路、 通常の AC33 とは独立に動く)。
#
# Idempotency: ``meta.welcome_tick_fired_at`` を session_id keyed dict
# として持ち、 同 session_id が複数回 join しても 1 回限り fire。
# ---------------------------------------------------------------------------

WELCOME_TICK_FIRED_AT_META_KEY = "welcome_tick_fired_at"
WELCOME_TICK_KIND = "trek-progress-check"
# AC28 manual doc — beacon-trek-execute Skill が自動 show する Trek
# operating manual (= 974 words の onboarding doc)。 hint body 内で
# 参照することで receiver Skill 側の「manual を読んでから kickoff」
# 経路を pin する (= AC28 と AC33 の繋ぎ目)。
TREK_OPERATING_MANUAL_DOC_ID = "yfOufm7d2zkAhcm5QWES"


def get_welcome_tick_fired_at(trek_doc: dict, *, session_id: str) -> str:
    """Return the ISO timestamp of the welcome tick fired for ``session_id``.

    Empty string when no welcome tick has been fired yet for the session
    (= it should fire on the next eligible join).
    """
    meta = trek_doc.get("meta") or {}
    fired_map = meta.get(WELCOME_TICK_FIRED_AT_META_KEY) or {}
    if not isinstance(fired_map, dict):
        # Defensive: schema drift could land a non-dict here. Treat as empty.
        return ""
    return str(fired_map.get(session_id, "") or "")


def should_fire_welcome_tick(trek_doc: dict, *, session_id: str) -> bool:
    """Return True iff a welcome tick is owed for ``session_id``.

    ms-97 / e-2637 — gate condition: ``session_id`` non-empty AND no prior
    stamp in ``meta.welcome_tick_fired_at[session_id]``. Pure / I/O-free
    so server / scheduler / tests share the same yes/no answer.
    """
    if not session_id:
        return False
    return not get_welcome_tick_fired_at(trek_doc, session_id=session_id)


def mark_welcome_tick_fired(trek_doc: dict, *, session_id: str,
                            now: str = "") -> dict:
    """Stamp ``meta.welcome_tick_fired_at[session_id] = now`` idempotently.

    ms-97 / e-2637 — invoked by the server hook right after the bus event
    write succeeds. Re-stamping is a no-op on the prior timestamp (= 1
    stamp per session, dogfood retries safe). Returns the mutated
    ``trek_doc`` for chaining; caller persists via ``db.save_trek``.
    """
    if not session_id:
        raise ValueError("session_id is required")
    meta = trek_doc.setdefault("meta", {})
    fired_map = meta.get(WELCOME_TICK_FIRED_AT_META_KEY)
    if not isinstance(fired_map, dict):
        fired_map = {}
        meta[WELCOME_TICK_FIRED_AT_META_KEY] = fired_map
    if not fired_map.get(session_id):
        fired_map[session_id] = now or utcnow_iso()
        trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def build_welcome_tick_payload(trek_doc: dict, *, session_id: str,
                               now: str = "") -> dict:
    """Render the one-shot welcome tick DM payload (ms-97 / e-2637).

    Shape mirrors ``build_progress_check_payload`` (= ``kind`` /
    ``trek_id`` / ``body`` / ``recipient_session_id`` / ``created_at``)
    so existing receiver Skills (= beacon-trek-execute) can dispatch on
    ``channel="dm"`` + ``kind="trek-progress-check"`` without a new
    branch.

    Body includes:
      * AC28 manual doc_id (= the trek operating manual receivers should
        read first via ``beacon doc show <id>``)
      * Action hint: 「join 直後にやること: kickoff ritual を
        /beacon-trek-execute Skill で起動」 — so the AI session converts
        the welcome tick into an autonomous kickoff loop without further
        scheduler input.
    """
    trek_id = trek_doc.get("trek_id", "") or ""
    body_lines = [
        f"[Trek welcome] trek_id={trek_id}",
        "Trek に join しました。 まず operating manual を確認してください:",
        f"  beacon doc show {TREK_OPERATING_MANUAL_DOC_ID}",
        "次に kickoff ritual を /beacon-trek-execute Skill で起動して、",
        "Trek 内で何をするかをこの session の文脈に流し込んでください。",
        "(= AC28 manual + AC33 lazy start の起動経路、 ms-97 / e-2637)",
    ]
    return {
        "trek_id": trek_id,
        "kind": WELCOME_TICK_KIND,
        "body": "\n".join(body_lines),
        "recipient_session_id": session_id,
        "manual_doc_id": TREK_OPERATING_MANUAL_DOC_ID,
        "created_at": now or utcnow_iso(),
        # AC33 lazy start とは独立に動く welcome path であることを
        # receiver / observer が判別できるよう sender_type を分ける。
        "sender_type": "trek-welcome",
        "origin_channel": WELCOME_TICK_KIND,
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

    Returns counts per state + an overall classification (ms-128 方針5):

        {
          "todo": N0, "working": N1, "leader_review": N2,
          "user_review": N3, "done": 0, "total": T,
          "overall": "active" | "all-user-review" | "empty",

          # Backward-compat alias for callers that still read the old
          # "waiting-review" key — combines leader_review + user_review
          # (= the conflate behaviour of the old 3-state model).
          "waiting-review": N2 + N3,
        }

    ms-128 方針5: done は Trek 状態機械から除去され read-time に user_review へ
    migrate される。よって "done" カウントは常に 0 (後方互換のため key は残す)、
    旧 overall 値 "all-done" / "all-terminal-mixed" は消滅した。

    "active":  at least one task is `todo` / `working` / `leader_review`
               — scheduler keeps firing for some executor or leader queue.
    "all-user-review": every task reached `user_review` (唯一の terminal)
               — Trek complete at Trek-completion granularity, pending the
               external human/deploy decision. legacy done は migrate 済。
    "empty":   no task IDs supplied.

    Untracked tasks collapse to `todo` (= default), so a freshly-added
    scope task keeps Trek active until claimed (todo → working).

    Trek 完遂判定 (= scheduler / leader 走り続け判定): `todo` / `working` /
    `leader_review` のいずれかが 1 つでもあれば走り続ける、 全部 `user_review`
    で停止 (= CORE doc 5nfTSmCDVUzD4SLzIhI5 § "Trek 完遂判定")。
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
    else:
        # ms-128 方針5: done は Trek 外。user_review が唯一の terminal なので、
        # 非 active で total>0 なら全 Target が user_review (= all-user-review)。
        # 旧 "all-done" / "all-terminal-mixed" は done 除去で消滅した。
        overall = "all-user-review"
    return {
        "todo": counts["todo"],
        "working": counts["working"],
        "leader_review": counts["leader_review"],
        "user_review": counts["user_review"],
        # ms-128 方針5: done は状態集合から除去。read-time に user_review へ
        # migrate されるので常に 0。後方互換のため key は残す。
        "done": 0,
        "total": total,
        "overall": overall,
        # Legacy alias for callers still reading old key.
        "waiting-review": counts["leader_review"] + counts["user_review"],
    }


def compute_ms_slot_state(task_states_for_slot: list[dict]) -> str:
    """Aggregate MS slot 状態 from the task_state entries of its child tasks (ms-97 / AC10).

    Pure function with no I/O: accepts the already-filtered list of
    ``task_state`` entries (= dicts carrying at least a ``state`` key,
    matching the shape stored on ``trek.task_states.*``) that belong to
    a single MS slot, and returns the slot's aggregated state token.

    Precedence rule (= "the most urgent / blocking child wins, then
    terminal states cascade"):

      1. any child in ``leader_review``        → ``"leader_review"``
      2. else all children in {``user_review``} → ``"user_review"``
      3. else any child in ``working``         → ``"working"``
      4. else (all children todo or empty)     → ``"todo"``

    (ms-128 方針5: ``done`` は Trek 状態機械から除去。legacy ``done`` stamp は
    read-time に ``user_review`` へ migrate されるので、この header 集約でも
    ``user_review`` が唯一の terminal になる。)

    Empty slot returns ``"todo"`` (= "no work yet, ready to start").
    Untracked / malformed entries collapse to ``todo`` for counting so
    a freshly-added child keeps the slot at todo rather than skipping
    straight to terminal.

    The output token is one of ``VALID_TASK_STATES`` so callers can
    feed it into the same state-machine surface as raw task states
    (= UI bucket, scheduler skip rules etc.).

    See SPEC ms-97 AC10 for the rationale: MS slot acts as a *header*
    for its child tasks, and the header should surface the most
    actionable state (= leader_review blocks executor progress, while
    terminal-only slots are "done from this slot's perspective"). The
    rule is intentionally distinct from ``aggregate_task_state`` —
    that one classifies the whole Trek (= scheduler fire decision),
    this one classifies one MS slot (= header pill / progress badge).
    """
    if not task_states_for_slot:
        return "todo"
    states = []
    for entry in task_states_for_slot:
        if not isinstance(entry, dict):
            states.append("todo")
            continue
        # ms-128 方針5: migrate done→user_review BEFORE the valid-state check —
        # else a raw legacy "done" stamp would wrongly collapse to todo.
        s = migrate_legacy_task_state(entry.get("state") or "todo")
        if s not in VALID_TASK_STATES:
            s = "todo"
        states.append(s)
    if "leader_review" in states:
        return "leader_review"
    # ms-128 方針5: user_review が唯一の terminal (done は Trek 外に migrate 済)。
    if all(s == "user_review" for s in states):
        return "user_review"
    if "working" in states:
        return "working"
    return "todo"


def resolve_session_home_project(
    session_id: str,
    scope_project_ids: list[str],
    list_sessions,
) -> str:
    """Find the home project of ``session_id`` within the Trek's scope (ms-97 / AC14).

    Walks ``scope_project_ids`` in order and asks ``list_sessions(pid)``
    for each project's session registry. The first project whose
    session list contains a row with matching ``session_id`` wins —
    that's the executor's "home" project for the Trek-scope purposes
    (= the only project they may originate slot 起票 from).

    ``list_sessions`` is a callable so the helper stays unit-testable
    without dragging in the store / firestore client. In production
    ``app.py`` passes ``db.list_sessions``; tests can pass a lambda
    that returns a hand-rolled session list.

    Returns the matching project_id, or ``""`` (empty string) if the
    session is not registered in any scope project. The empty-string
    sentinel lets callers distinguish "ghost session" from "found pid"
    without an extra Optional[str] wrapper at the type signature.

    AC14 use: server-side guard on executor slot 起票 (= POST
    /api/treks/{id}/task-add). The caller's home project must equal
    the target project of the slot; otherwise we'd let executor A
    sprout slots in executor B's project, which violates the SPEC
    设计方针 14 "executor は自分の home project にしか slot を生やせない"
    rule (= cross-project authority lives with leader / scope policy).
    """
    if not session_id or not scope_project_ids:
        return ""
    for pid in scope_project_ids:
        if not pid:
            continue
        try:
            sessions = list_sessions(pid) or []
        except Exception:
            # Listing failure on one project shouldn't poison the walk
            # — the session may yet be registered on a later scope pid.
            continue
        for s in sessions or []:
            if not isinstance(s, dict):
                continue
            if (s.get("session_id") or "") == session_id:
                return pid
    return ""


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


# ms-97 Phase 6 (AC15) — accident-time leader candidate notice.
#
# Trek invite acceptance に必須挿入する事前通知。 受諾した user に対し
# 「現 leader session が不応 (= N=3 連続 pulse-ack miss + 30 min stale)
# になった時、 自動 succession 経路 (= AC22、 Phase 7 で実装) で次期
# leader 候補になり得る」 ことを明示し、 任命時に server から確認 DM
# (= 1 hop consent) が届くこと、 辞退すると次の candidate に escalate
# されることを通知する。
#
# 文面を const にして lib / server / CLI / tests から同じ文字列を参照
# できるようにし、 wording drift を構造的に防ぐ。
LEADER_CANDIDATE_NOTICE = (
    "あなたは Trek \"{title}\" のメンバーになりました。\n"
    "\n"
    "⚠ アクシデント時の leader 候補可能性 について:\n"
    "このメンバー枠は、 現 leader session が不応 (= N=3 連続 pulse-ack "
    "miss + 30 min stale) になった時、\n"
    "自動 succession 経路 (= AC22、 後 Phase で実装) で次期 leader "
    "候補になり得ます。 候補に選ばれた\n"
    "時は server から確認 DM (= 1 hop consent) が届くので、 受諾 / "
    "辞退を明示してください。\n"
    "辞退した場合、 次の candidate (= invited_at 順) に escalate "
    "されます。"
)


def build_leader_candidate_notice(trek_doc: dict) -> str:
    """Render the accident-time leader candidate notice for ``trek_doc``.

    Returns the canonical pre-notice text with the trek title substituted in.
    Surfaced at invite-acceptance time so the invitee is informed *before*
    they may be auto-promoted to leader by the (Phase 7) AC22 succession
    algorithm. The text is intentionally non-localized for now — Phase 6
    only pins the contract; i18n is a later orthogonal concern.

    ms-97 / Phase 6 (AC15).
    """
    title = trek_doc.get("title", "") or ""
    return LEADER_CANDIDATE_NOTICE.format(title=title)


def build_member(*, user_id: str, email: str,
                 role: str = "member",
                 invited_at: str | None = None,
                 joined_at: str = "",
                 invited_by: str = "",
                 session_id: str = "") -> dict:
    """Build a trek member dict.

    ``invited_at`` defaults to now if omitted. ``joined_at`` empty means
    "invited but not yet joined" (= visible to invitee but they have not
    accepted). ``invited_by`` is the user_id of the inviter; empty for
    self-created leader membership at creation time.

    ms-97 / e-2658 Phase 1 (AC6) — ``session_id`` パラメータを optional
    で受け取る。 空 (= default) なら pre-A の user_id keyed 振る舞いを
    維持し、 非空なら session_id keyed entry (= session 1 件 1 member)
    を吐く。 build_member は単なる dict builder なので、 phase 判定は
    呼び出し側 (= add_invitation / new_trek 等) が行う。
    """
    validate_role(role)
    if not user_id or not email:
        raise ValueError("member requires both user_id and email")
    entry = {
        "user_id": user_id,
        "email": email,
        "role": role,
        "invited_at": invited_at or utcnow_iso(),
        "joined_at": joined_at,
        "invited_by": invited_by,
    }
    # session_id field は AC6 phase A+ trek でのみ書き込まれる。 pre-A
    # trek の dict には field 自体を載せず、 schema 互換を完全に維持。
    if session_id:
        entry["session_id"] = session_id
    return entry


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


# ms-99 / e-2828: v2 scope entry field set (= slot schema v2). Presence of
# any of these keys on input flips ``normalize_scope_entry`` into v2 output
# mode; legacy inputs (project + narrowing key only) still round-trip to
# legacy output so pre-v2 callers and their tests stay pinned.
V2_SCOPE_KEYS: tuple = (
    "slot_id", "target_kind", "target_id",
    "included_task_ids", "claim_session_id", "claimed_at",
)

# ms-109 e-3699 (fable B-2): occupation-agnostic. dev → milestone/operation/
# task, sales → opportunity/account, sourced from the occupation registry (the
# union across occupations, since a Trek can span both). Adding an occupation's
# narrowing kinds happens in ``occupation.NARROWING_KINDS``, never here.
NARROWING_KEYS: tuple = occupation.all_narrowing_kinds()


def _scope_entry_identity_key(entry: dict) -> tuple:
    """Return the identity tuple of a scope entry (ms-99 / e-2828).

    Identity = ``(project, narrowing_kind, narrowing_id)``. v2 attributes
    (slot_id / claim_session_id / included_task_ids / claimed_at) describe
    the *state* of the same slot and are intentionally excluded — a legacy
    on-disk entry and a fresh v2-shaped entry that both point at the same
    project + milestone/operation/task are the same slot.

    Director e-2828 decision (= 案 A on the equality question): identity
    is the narrowing tuple, not full-dict equality. This keeps
    ``add_scope_entry`` and ``remove_scope_entry`` compatible across the
    v1 → v2 migration without a data rewrite.
    """
    project = entry.get("project") or ""
    for kind in NARROWING_KEYS:
        val = entry.get(kind)
        if val:
            return (project, kind, val)
    return (project, "", "")


def _scope_entry_identity_matches(a: dict, b: dict) -> bool:
    """True iff ``a`` and ``b`` refer to the same slot (identity-wise)."""
    return _scope_entry_identity_key(a) == _scope_entry_identity_key(b)


def narrowing_keys(data: dict | None = None) -> tuple:
    """Return the recognised scope-narrowing keys. Without ``data``, the
    import-time built-in set (``NARROWING_KEYS``); with a project ``data`` dict,
    the built-ins PLUS that project's descriptor-defined kinds (ms-124 e-4091)
    so a Trek can scope to a data-defined target (e.g. a ``contract``), not just
    milestones / opportunities. The import-time constant stays the default so
    existing callers are byte-for-byte unchanged."""
    if not data:
        return NARROWING_KEYS
    return occupation.all_narrowing_kinds(data)


def normalize_scope_entry(
    entry: dict, *, strict: bool = True, resolve: bool = False,
    db_or_lister=None, mint_slot: bool = False, data: dict | None = None,
) -> dict:
    """Normalise a scope item.

    A scope entry MUST include ``project`` (= project_id) and MAY include
    one narrowing key to slice it (development: milestone / operation / task;
    sales: opportunity / account — the occupation-agnostic set is
    ``occupation.all_narrowing_kinds()``, ms-109 e-3699). Unknown keys are
    dropped to keep the on-disk schema tight (= server side can validate
    against this normalisation, CLI side can also use it before posting).

    ms-97 / e-2659 (AC7 = scope strict mode):

    - ``strict=True`` (default): the entry MUST include at least one
      narrowing key (= any of ``occupation.all_narrowing_kinds()``).
      Project-wide entries (= no narrowing) raise ``ValueError`` so the
      Trek's autonomous scope cannot accidentally cover an entire project.
      This is the only mode NEW callers (= CLI ``--add-scope`` parser,
      server endpoint validation, ``add_scope_entry`` / pending op staging)
      should use.

    - ``strict=False``: legacy permissive mode. Project-wide entries are
      accepted silently so historical Trek docs (AC8 grandfather) and
      identity look-ups on already-persisted scope rows (= remove /
      pending look-up) keep working without forcing a data migration.

    The split is the structural guarantee for AC7 + AC8: new project-wide
    additions are physically impossible to land via the strict path, while
    existing on-disk project-wide entries remain readable.

    ms-97 / e-2694 dogfood fix: ``resolve=True`` (default False) runs the
    project ref through :func:`lib.project_ref.resolve_project_ref` so
    short names (= ``life-plan-simulator``) are expanded to canonical
    suffix'd ids (= ``life-plan-simulator-68c5df``) at the boundary.
    Existing tests default to ``resolve=False`` so the pure normalisation
    shape is unchanged; CLI / server entry points opt in.

    ms-99 / e-2828 (Trek slot schema v2): the output gains the v2 field
    set (``slot_id`` / ``target_kind`` / ``target_id`` /
    ``included_task_ids`` / ``claim_session_id`` / ``claimed_at``) only
    when the input already carries a v2 marker OR when the caller asks
    for a fresh slot with ``mint_slot=True``. Legacy inputs (project +
    narrowing key only) still normalise to the legacy shape, so pre-v2
    on-disk data and its unit tests keep round-tripping unchanged. New
    slot creation paths (CLI ``beacon trek slot add`` / API
    ``POST /api/treks/{id}/slots``) pass ``mint_slot=True`` to receive a
    fresh ``sl-<8 hex>`` id; legacy read paths pass the default False and
    tolerate ``slot_id=""`` (= Phase 3 interactive backfill per SPEC
    方針 3).
    """
    if not entry.get("project"):
        raise ValueError("scope entry missing required 'project' field")
    project_ref = entry["project"]
    if resolve:
        # Lazy import: project_ref isn't required for the strict-mode
        # contract — only the resolve=True opt-in path needs it. Keeping
        # the import here means base trek.py callers don't pay the
        # import cost.
        try:
            from project_ref import resolve_project_ref as _resolve
        except ImportError:
            from lib.project_ref import resolve_project_ref as _resolve
        project_ref = _resolve(project_ref, db_or_lister=db_or_lister)
    # Recognised narrowing keys are data-aware (ms-124 e-4091): when the caller
    # passes the project ``data`` a descriptor-defined kind is accepted instead
    # of being silently dropped. Default (no data) = the built-in set, so every
    # existing caller is unchanged.
    keys = narrowing_keys(data)
    out: dict = {"project": project_ref}
    for k in keys:
        if entry.get(k):
            out[k] = entry[k]
    if strict and not any(k in out for k in keys):
        raise ValueError(
            "scope entry requires a narrowing key (one of: "
            + " | ".join(keys)
            + "; = project-wide scope is no longer accepted; ms-97 AC7)"
        )
    # ms-99 / e-2828: v2 output branch. Only fires when the input signals
    # v2 shape or the caller explicitly asks to mint a slot. Legacy inputs
    # fall through and return the pre-v2 dict verbatim (backward compat).
    has_v2_marker = any(k in entry for k in V2_SCOPE_KEYS)
    if has_v2_marker or mint_slot:
        # slot_id: preserve incoming; else mint on request; else empty
        # string (= legacy row awaiting Phase 3 interactive backfill).
        incoming_slot = entry.get("slot_id")
        if incoming_slot:
            out["slot_id"] = str(incoming_slot)
        elif mint_slot:
            out["slot_id"] = mint_slot_id()
        else:
            out["slot_id"] = ""
        # target_kind / target_id: derived from the narrowing key present
        # in ``out`` (= post-strict validation). SPEC 方針 1 calls this
        # "narrowing key と冗長だが正規化のため" — the redundancy lets
        # materialize_slots (Phase 2) index slots without re-inspecting
        # the legacy key. Absent when no narrowing key exists (= only
        # possible when strict=False + legacy project-wide row).
        for kind in keys:
            if kind in out:
                out["target_kind"] = kind
                out["target_id"] = out[kind]
                break
        # included_task_ids: preserve if key present; None marks the
        # legacy "all children auto-include" semantics (SPEC AC4). A
        # non-None list opts into explicit child selection (SPEC 方針 2).
        if "included_task_ids" in entry:
            inc = entry["included_task_ids"]
            out["included_task_ids"] = None if inc is None else list(inc)
        # claim_session_id / claimed_at: preserve when present. Stamping
        # a claim never changes state (SPEC 方針 4) — that split lets
        # planning-phase "I'll take this next" be observed before any
        # todo→working transition happens.
        if "claim_session_id" in entry:
            out["claim_session_id"] = str(entry["claim_session_id"] or "")
        if "claimed_at" in entry:
            claimed_at = entry["claimed_at"]
            out["claimed_at"] = claimed_at if claimed_at else None
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
    # ms-97 / e-2659 (AC7): seeded scope on a fresh trek runs through the
    # strict gate — there is no "legacy data" to grandfather at creation
    # time. Callers that need to import historical project-wide rows can
    # populate ``trek_doc['scope']`` directly after construction.
    scope = [normalize_scope_entry(s, strict=True) for s in (initial_scope or [])]
    # ms-97 Phase 7-A (= G6, e-2660): seed the completion-flow tracking
    # fields on every new trek so AC20 (= completion_ready idempotent
    # stamp) / AC21 (= summary_sent_at leader stop) / AC11+e-2646 (=
    # stall_threshold_minutes trek-level override) have a stable
    # invariant to read against. すべて None / 未設定 で seed する
    # (= None は "まだ起きていない" を意味し、 readers は
    # ``meta.get("...")`` で同じ None を読む)。
    #
    # 既存 trek (= pre-G6 docs) は migration 不要。 これらの field が
    # 欠落していても readers は ``meta.get(...)`` で None を取り、
    # 同じ "まだ起きていない" 状態として扱う。
    meta: dict = {
        "completion_notified_at": None,
        "summary_sent_at": None,
        "stall_threshold_minutes": None,
    }
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

def find_member(
    trek_doc: dict,
    user_id: str | None = None,
    *,
    session_id: str | None = None,
) -> dict | None:
    """Return the member dict matching the given grain, or None if absent.

    ms-97 / e-2658 Phase 1 (AC6) — dual-grain lookup.

    - ``find_member(t, "u-123")`` (= positional, legacy) → user_id 検索
    - ``find_member(t, user_id="u-123")`` (= keyword) → user_id 検索
    - ``find_member(t, session_id="sv-abc")`` → session_id 検索 (= NEW)

    user_id と session_id を両方指定された場合は session_id を優先する
    (= phase A+ では session_id が member identity の真値、 user_id は
    1 user N session で曖昧)。 どちらも空 / None なら ValueError。

    session_id keyed phase の trek (= migration_phase A/B/C) でも、
    pre-A code path から positional user_id 渡しが来た時は legacy
    grain で linear scan する (= 1 user N session の場合は最初に
    マッチした member entry を返す best-effort)。 厳密な session
    grain lookup が要るなら session_id 引数を使うこと。
    """
    if session_id is None and (user_id is None or user_id == ""):
        raise ValueError(
            "find_member: either user_id or session_id must be given"
        )
    members = trek_doc.get("members") or []
    if session_id:
        for m in members:
            if m.get("session_id") == session_id:
                return m
        return None
    for m in members:
        if m.get("user_id") == user_id:
            return m
    return None


def is_session_id_keyed(trek_doc: dict) -> bool:
    """Return True if this trek's ``members[]`` is session_id keyed.

    ms-97 / e-2658 Phase 1 (AC6) helper — phase A/B/C は session_id keyed
    な write path で読み書きされる cutover 済 trek。 pre-A trek (= 旧
    user_id keyed dedup) は False を返す。 server / CLI / Skill は
    membership check の前段でこれを呼んで read path を分岐させる。
    """
    return get_migration_phase(trek_doc) in ("A", "B", "C")


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

    ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek (= session_id keyed) でも
    招待段階では session_id が未確定 (= invitee がまだ join していない)
    なので、 dedupe は user_id grain で行う (= 1 user に 1 招待まで)。
    session-grain への展開は ``accept_invitation`` 時に発生する。
    pre-A trek の振る舞いと完全に同じ。
    """
    if find_member(trek_doc, user_id=user_id) is not None:
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


def _stamp_leader_candidate_notice_shown(member: dict, *, now: str = "") -> dict:
    """Stamp ``meta.leader_candidate_notice_shown_at`` on a member entry.

    Idempotent: if a stamp already exists it is preserved (= the first
    notice delivery is the canonical audit point; re-joins from the same
    session must not re-stamp and silently bury the original). ``now``
    defaults to current UTC.

    Returns the member dict (= for chaining in accept_invitation paths).

    ms-97 / Phase 6 (AC15) — provides audit so the leader / server can
    later verify members were informed about the accident-time succession
    candidate possibility.
    """
    meta = member.setdefault("meta", {})
    if not meta.get("leader_candidate_notice_shown_at"):
        meta["leader_candidate_notice_shown_at"] = now or utcnow_iso()
    return member


def find_last_accepted_member(trek_doc: dict, *, user_id: str,
                              session_id: str = "") -> dict | None:
    """Return the most relevant member dict for a freshly-accepted invitation.

    Lookup precedence (= matches ``accept_invitation`` write order):

    1. session_id grain if non-empty and the trek is phase A+
    2. user_id grain (= pre-A path / fallback)

    Returns None if no matching entry exists. Used by the server endpoint
    to know which member.meta to stamp + which dict to surface back to
    the caller alongside the trek doc.

    ms-97 / Phase 6 (AC15).
    """
    if session_id and is_session_id_keyed(trek_doc):
        m = find_member(trek_doc, session_id=session_id)
        if m is not None:
            return m
    if user_id:
        return find_member(trek_doc, user_id=user_id)
    return None


def _migrate_to_session_keyed_inline(trek_doc: dict) -> None:
    """ms-97 / e-2636 — In-place pre-A → A migration on first session-grain join.

    Stamps ``meta.migration_phase = "A"`` so subsequent reads (=
    ``is_session_id_keyed``) take the session-grain path. Patches the
    leader entry with ``trek_doc.leader_session_id`` so the leader's
    own membership is identifiable at session grain. Other members keep
    their existing ``session_id`` (= empty for plain invitation rows).

    The legacy migration script (= ``migrate_members_to_session_keyed``)
    snapshots members into ``members_legacy_backup`` before mutating.
    The inline path here does **not** mutate existing member dicts other
    than the leader entry's ``session_id`` patch (= no information loss),
    so the backup snapshot is taken purely as audit evidence ("this trek
    was migrated inline at first join, here is what the doc looked like
    before"). If a backup already exists (= rare race with the offline
    script), we leave it alone — it represents an earlier, more original
    snapshot.

    Idempotent: re-entry on an already phase-A trek is a no-op (caller
    checks phase first; this helper also self-guards).
    """
    if is_session_id_keyed(trek_doc):
        return
    # Snapshot BEFORE any mutation so the backup faithfully captures the
    # pre-A user_id-keyed shape. Best-effort: skip if a backup already
    # exists (= prior offline migration run or concurrent writer).
    if not (trek_doc.get(MEMBERS_LEGACY_BACKUP_KEY) or []):
        try:
            backup_legacy_members(trek_doc)
        except ValueError:
            # Concurrent writer raced us — leave the prior snapshot.
            pass
    members = trek_doc.get("members") or []
    leader_sid = trek_doc.get("leader_session_id") or ""
    if leader_sid:
        for m in members:
            # Only patch the leader entry that does NOT already have a
            # session_id (= legacy shape). Members entries already keyed
            # by session_id stay as-is.
            if m.get("role") == "leader" and not m.get("session_id"):
                m["session_id"] = leader_sid
                break
    set_migration_phase(trek_doc, "A")


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

    ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek (= session_id keyed)
    での session-grain expand:

    - 招待 entry (= ``session_id`` field 未設定 + ``joined_at`` 空) が
      存在し、 join 時に session_id が渡された時は、 その entry を
      session-bound に upgrade する (= 同じ dict に session_id と
      joined_at を書き込む)。
    - 既に joined な entry がある状態で**別 session_id** から join した
      時は、 同 user_id の新規 member entry を追加して silent expand
      する (= 1 user N session が独立 member として共存)。
    - 同じ session_id から再 join (= idempotent retry) は、 該当
      session-bound entry の joined_at を保ったまま no-op。

    pre-A trek (= phase pre-A) では従来通り user_id grain idempotent
    で動作 (= 同じ user_id の 2 回目 join は no-op)。

    ms-97 / e-2636 — pre-A trek が ``session_id`` 引数を受けた join に
    遭遇した時、 **inline auto-migrate** で phase A に flip する (= 旧
    user_id keyed silent no-op を構造的に塞ぐ)。 観察された bug は
    「同 user_id 別 session の 2 つ目 join が server 側 silent no-op
    で members[] に追加されない」 で、 真因は pre-A path の
    user_id grain idempotency。 ``session_id`` 引数が渡される
    = caller が session-grain semantics を期待している、 と解釈して
    一度の join で migration を完了させ、 そのまま phase A 経路で
    expand する。 既存 leader entry には ``leader_session_id`` を
    in-place で補完する (= migration + extend の両立、 Done when #3)。
    既に session_id を持つ member は backup snapshot に含まれず on-disk
    そのまま (= migration script は session_history が無い場合の
    expand 経路、 ここは既知 session_id のため直接 stamp で済む)。

    ms-97 / Phase 6 (AC15) — 受諾された member entry に対し
    ``meta.leader_candidate_notice_shown_at`` を stamp する (= 既存
    stamp は保持する idempotent 仕様)。 stamp は accident-time leader
    candidate notice を server / CLI が表示した時の audit 痕跡。
    """
    members = trek_doc.get("members") or []
    phase_a_plus = is_session_id_keyed(trek_doc)

    # ms-97 / e-2636 — inline auto-migrate. pre-A trek + ``session_id``
    # 引数が来た時に phase A に flip して silent no-op を塞ぐ。
    # leader entry の session_id は trek_doc.leader_session_id から
    # 補完 (= Done when #3 の「既存 leader entry の in-place 補完」)。
    # backup snapshot は idempotent guard 付きで取る (= 既に backup が
    # ある場合は二重採取せずスキップ)。
    if not phase_a_plus and session_id:
        _migrate_to_session_keyed_inline(trek_doc)
        phase_a_plus = True
        members = trek_doc.get("members") or []

    if phase_a_plus and session_id:
        # session-grain expand path. Look first for a session-bound entry
        # (= already joined from this session AND owned by this user_id),
        # then for the invitation placeholder (= same user_id, empty
        # session_id) to upgrade. The user_id match on existing_sb is a
        # safety belt — without it, a session_id collision with another
        # user's entry would mis-route this join into a no-op (= ms-97 /
        # e-2636 regression caught by CLI fixtures whose default
        # BEACON_SESSION_ID was shared across users).
        existing_sb = next(
            (m for m in members
             if m.get("session_id") == session_id
             and m.get("user_id") == user_id),
            None,
        )
        if existing_sb is not None:
            # Already joined from this session — fully idempotent, but
            # still upsert session_history so retries refresh the
            # invariant "every session that ever joined is on record".
            role_at_join = existing_sb.get("role") or "member"
            upsert_session_history(
                trek_doc,
                session_id=session_id,
                user_id=existing_sb.get("user_id") or user_id,
                email=existing_sb.get("email") or "",
                role_at_join=role_at_join,
            )
            _stamp_leader_candidate_notice_shown(existing_sb)
            return trek_doc
        # No session-bound entry yet → look for an unaccepted invitation
        # at the user_id grain (= placeholder seeded by add_invitation).
        placeholder = next(
            (m for m in members
             if m.get("user_id") == user_id
             and not m.get("session_id")
             and not m.get("joined_at")),
            None,
        )
        if placeholder is not None:
            # Upgrade placeholder to session-bound. Same dict — preserves
            # invited_at / invited_by / role.
            now = utcnow_iso()
            placeholder["session_id"] = session_id
            placeholder["joined_at"] = now
            role_at_join = placeholder.get("role") or "member"
            upsert_session_history(
                trek_doc,
                session_id=session_id,
                user_id=placeholder.get("user_id") or user_id,
                email=placeholder.get("email") or "",
                role_at_join=role_at_join,
            )
            _stamp_leader_candidate_notice_shown(placeholder, now=now)
            trek_doc["updated_at"] = now
            return trek_doc
        # No placeholder either. Check if this user_id has any joined
        # session-bound entry already (= silent expand from a different
        # session of the same user).
        sibling = next(
            (m for m in members
             if m.get("user_id") == user_id and m.get("joined_at")),
            None,
        )
        if sibling is None:
            raise ValueError(
                f"user {user_id} not invited to trek "
                f"{trek_doc.get('trek_id')} "
                "(owner must `beacon trek invite` first)"
            )
        # Silent expand: add a fresh session-grain member entry. Role
        # falls back to "member" (= we do not multiply leadership;
        # leader_session_id remains pinned to the original session).
        now = utcnow_iso()
        new_member = build_member(
            user_id=sibling.get("user_id") or user_id,
            email=sibling.get("email") or "",
            role="member",
            invited_at=sibling.get("invited_at") or now,
            joined_at=now,
            invited_by=sibling.get("invited_by") or "",
            session_id=session_id,
        )
        trek_doc.setdefault("members", []).append(new_member)
        upsert_session_history(
            trek_doc,
            session_id=session_id,
            user_id=new_member["user_id"],
            email=new_member["email"],
            role_at_join="member",
        )
        _stamp_leader_candidate_notice_shown(new_member, now=now)
        trek_doc["updated_at"] = now
        return trek_doc

    # pre-A path (= legacy user_id keyed idempotency) — current behavior.
    member = find_member(trek_doc, user_id=user_id)
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
        # Already-joined idempotent path: still stamp so the notice audit
        # mark exists even for join retries from the same user (= the
        # CLI / endpoint always surfaces the notice text, the stamp pins
        # delivery happened).
        _stamp_leader_candidate_notice_shown(member)
        return trek_doc  # already joined, member.joined_at preserved
    member["joined_at"] = utcnow_iso()
    _stamp_leader_candidate_notice_shown(member)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def parse_scope_arg(arg: str, *, strict: bool = True) -> dict:
    """Parse a CLI ``<project>[:<ref>]`` scope argument into a normalized entry.

    ``ref`` is dispatched by id prefix via ``occupation.narrowing_kind_for_ref``
    (ms-109 e-3699 — the vocabulary is not hardcoded here):
    - ``ms-...``  → milestone
    - ``op-...``  → operation
    - ``e-...``   → task
    - ``opp-...`` → opportunity (sales 商談)
    - ``acc-...`` → account (sales 顧客)
    - omitted     → project-wide scope (= no narrowing)

    Returns the normalized scope dict ready to append to ``trek_doc.scope``.
    Raises ValueError on empty input or unknown ref prefix.

    ms-97 / e-2659 (AC7): ``strict=True`` (default) rejects project-wide
    scope (= no narrowing ref) via the ``normalize_scope_entry`` strict
    guard. CLI ``--add-scope`` is wired to this default so user-facing
    rejections happen at parse time with a clear hint. Legacy callers
    that need to parse already-persisted strings (= rare) can opt out
    with ``strict=False``.
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
            return normalize_scope_entry(entry, strict=strict)
        kind = occupation.narrowing_kind_for_ref(ref)
        if kind:
            entry[kind] = ref
        else:
            raise ValueError(
                f"unknown ref prefix in {arg!r} — expected one of "
                + "/".join(occupation.NARROWING_ID_PREFIXES.values())
                + " (or omit ref for project-wide scope)"
            )
        return normalize_scope_entry(entry, strict=strict)
    return normalize_scope_entry({"project": arg}, strict=strict)


# ---------------------------------------------------------------------------
# Slot done precondition — task pool 真値源 (ms-97 / e-2650)
# ---------------------------------------------------------------------------
#
# ms-97 / e-2650 (= phantom done 構造防御): Trek slot は概念的に project
# 側 task の **view** であり、 view 側だけ done になる経路を server で
# 構造的に reject する。 task done は既に commit 紐付け (= PostToolUse
# hook + /beacon-log skill) で物理 evidence verify が走っているので、
# slot done をその信頼チェーンの延長に位置付ければ phantom done と drift
# の両方を 1 つの構造原則で原理的に防げる。
#
# 2026-06-28 dogfood で観測された 2 件の病理:
#   (a) e-710 phantom done: PE 側で task が done と記録されているのに、
#       該当 commit / ファイル変更が一切なかった
#   (b) tk-7a3b88b9: Trek 側 done、 task pool todo が固定化していた
#
# どちらも「project pool が真値源、 Trek 側 task_states は derived state
# (= mirror)」 という ordering を守れば構造的に消える。

SLOT_DONE_ALLOWED = "allowed"
SLOT_DONE_REJECT_TASK_POOL_NOT_DONE = "task_pool_not_done"
SLOT_DONE_REJECT_MS_CHILDREN_NOT_ALL_DONE = "ms_children_not_all_done"
SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED = "task_pool_lookup_failed"


def check_slot_done_precondition(
    trek_doc: dict, *,
    task_id: str,
    get_project,
) -> tuple[bool, str, str]:
    """Verify that a Trek slot **terminal** transition is backed by project pool truth.

    ms-128 方針5 (e-4366): done は Trek 状態機械から除去され、terminal は
    ``user_review`` になった。この gate は endpoint から ``done`` / ``user_review``
    (= migrate 後の terminal) 書き込み時に呼ばれ、真値源の project pool の task が
    ``status == "done"`` であることを必須にする (関数名は歴史的に ``done`` を
    冠するが、実際には terminal 化 = user_review への到達を gate する)。

    Pure(-ish) function: side-effectful only via the injected
    ``get_project(pid) -> dict | None`` callable (= server passes
    ``db.get_project``, tests inject a fake). Returns
    ``(allowed, reason_code, human_message)``:

      * ``allowed=True`` → caller may proceed to ``set_task_state``.
      * ``allowed=False`` → caller must raise 4xx with the human message;
        the reason code is included for log / CLI surface symmetry.

    Two slot shapes are handled:

      1. **Task ref** (= ``task_id`` is an ``e-XXXX`` entry id) — walk the
         trek's ``scope[].project`` ids and look up the entry via
         ``find_entry``. Done iff the entry's ``status == "done"`` in the
         project pool. Pool absence / lookup failure → reject so we never
         silently accept "the task pool has no record of this slot".

      2. **MS ref** (= ``task_id`` starts with ``"ms-"``) — find the MS in
         one of the scope projects, walk its top-level ``entries[]``, and
         allow only when **every** task-typed child has ``status == "done"``.
         Empty MS (= no children at all) is rejected because "done with
         nothing inside" is a phantom done variant.

    Why a 3rd tuple slot for the message: the rejection text guides the
    AI executor toward the right recovery action (= "先に task pool で
    done してください") and is the most useful single string for the
    HTTP detail body / CLI error.

    ms-97 / e-2650 done-when 1 (server side check) + done-when 2
    (ordering 強制) + done-when 3 (archive 時の task_states sync 問題
    自動消滅). Spec ref: SPEC tnJnByg8Ymw8T3DgPdJD AC10 / AC30 補強。
    """
    if not task_id:
        return False, SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED, (
            "task_id is required for slot done precondition check"
        )
    scope = trek_doc.get("scope") or []
    project_ids: list[str] = []
    seen: set[str] = set()
    for entry in scope:
        pid = entry.get("project") if isinstance(entry, dict) else None
        if not pid or pid in seen:
            continue
        seen.add(pid)
        project_ids.append(pid)
    # Walk scope projects looking for the entry / MS. The first hit wins
    # (= same precedence the reconcile endpoint uses).
    is_ms_ref = task_id.startswith("ms-")
    for pid in project_ids:
        try:
            project_data = get_project(pid)
        except Exception:
            # Backend hiccup on one project shouldn't poison the walk —
            # the slot may yet resolve on a later scope project.
            continue
        if not project_data:
            continue
        if is_ms_ref:
            ms = _find_milestone_by_id(project_data, task_id)
            if ms is None:
                continue
            children = ms.get("entries") or []
            task_children = [
                c for c in children
                if isinstance(c, dict)
                and (c.get("type") or "task") == "task"
            ]
            if not task_children:
                return False, SLOT_DONE_REJECT_MS_CHILDREN_NOT_ALL_DONE, (
                    f"MS slot {task_id} done を拒否しました: 配下に "
                    "task が 1 件も存在しません (= 空 MS の slot done は "
                    "phantom done の一形態として禁止)。 先に MS 配下に "
                    "task を起票し、 各 task を commit 紐付けで done に "
                    "してから MS slot を done にしてください。"
                )
            not_done = [
                c.get("id", "") for c in task_children
                if (c.get("status") or "") != "done"
            ]
            if not_done:
                preview = ", ".join(not_done[:3])
                more = "" if len(not_done) <= 3 else (
                    f" 他 {len(not_done) - 3} 件"
                )
                return False, SLOT_DONE_REJECT_MS_CHILDREN_NOT_ALL_DONE, (
                    f"MS slot {task_id} done を拒否しました: 配下 task "
                    f"のうち {len(not_done)} 件が project pool で done に "
                    f"なっていません ({preview}{more})。 先に task pool 側 "
                    "で各 task を done にしてください (= 真値源は project "
                    "pool、 Trek slot は derived view、 ms-97 / e-2650)。"
                )
            return True, SLOT_DONE_ALLOWED, ""
        # Task ref path
        from core import find_entry  # lazy import to avoid cycle at module load
        found = find_entry(project_data, task_id)
        if not found:
            continue
        _, _, entry, _ = found
        pool_status = (entry or {}).get("status") or ""
        if pool_status != "done":
            return False, SLOT_DONE_REJECT_TASK_POOL_NOT_DONE, (
                f"Trek slot {task_id} done を拒否しました: project pool "
                f"側の task が status={pool_status or 'todo'} のままです。 "
                "先に task pool で done してください (= commit 紐付けによる "
                "物理 evidence verify が走り、 phantom done を構造的に防ぐ "
                "経路。 真値源は project pool、 Trek slot は derived view、 "
                "ms-97 / e-2650)。"
            )
        return True, SLOT_DONE_ALLOWED, ""
    # No scope project had a record of this id. Reject — silently allowing
    # would let "untracked" slot done leak through, which is precisely the
    # phantom done shape this guard is designed to block.
    if is_ms_ref:
        return False, SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED, (
            f"MS slot {task_id} done を拒否しました: scope project の "
            "いずれにも該当 MS が見つかりません (= scope の整合性が崩れて "
            "いる、 または MS 自体が削除済)。 trek scope を確認してください。"
        )
    return False, SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED, (
        f"Trek slot {task_id} done を拒否しました: scope project の "
        "いずれの task pool にも該当 entry が見つかりません (= task が "
        "存在しないのに slot だけ done にする経路は phantom done として "
        "構造的に reject、 ms-97 / e-2650)。"
    )


def _find_milestone_by_id(project_data: dict, ms_id: str) -> dict | None:
    """Locate a milestone dict in ``project_data.milestones[]`` by id.

    Returns ``None`` if absent. Kept as a private helper because the
    public ``core.find_entry`` walks **entries** (= task / spec children)
    and intentionally does not surface MS-level matches.
    """
    if not ms_id or not project_data:
        return None
    for ms in project_data.get("milestones") or []:
        if isinstance(ms, dict) and ms.get("id") == ms_id:
            return ms
    return None


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
    """Append a scope entry; raises ValueError if it already exists.

    ms-97 / e-2659 (AC7): runs ``normalize_scope_entry`` in strict mode,
    so project-wide additions raise ValueError before they can reach
    ``scope[]``. This is the lib-layer enforcement that closes the
    project-wide hole regardless of which caller invoked the helper.

    ms-99 / e-2828: duplicate detection uses
    ``_scope_entry_identity_matches`` (= project + narrowing tuple) so
    a legacy on-disk entry and a v2-shaped incoming entry that point at
    the same slot are recognised as duplicates. Full-dict equality would
    let v2 fields (slot_id / claim / included_task_ids) mask the
    duplicate and let two rows land for the same slot.
    """
    norm = normalize_scope_entry(entry, strict=True)
    for existing in trek_doc.get("scope") or []:
        if _scope_entry_identity_matches(existing, norm):
            raise ValueError(f"scope entry already exists: {norm}")
    trek_doc.setdefault("scope", []).append(norm)
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def remove_scope_entry(trek_doc: dict, *, entry: dict) -> dict:
    """Remove a scope entry; raises ValueError if not found.

    ms-97 / e-2659 (AC8): runs ``normalize_scope_entry`` in non-strict
    mode so legacy project-wide entries (= grandfathered on-disk data)
    can still be addressed for removal. Otherwise users could never
    delete the very entries the new strict mode is meant to retire.

    ms-99 / e-2828: match uses ``_scope_entry_identity_matches`` so a
    remove request phrased in either legacy or v2 shape correctly picks
    the corresponding slot regardless of how it was persisted.
    """
    norm = normalize_scope_entry(entry, strict=False)
    scope = trek_doc.get("scope") or []
    new_scope = [s for s in scope if not _scope_entry_identity_matches(s, norm)]
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
# ms-99 / e-2829: two new pending action verbs for slot-level mutations
# that keep AC 15 (= all CLI slot operations flow through staging) intact.
# ``scope_amend`` edits ``included_task_ids`` on an existing MS slot
# (add/remove children); ``scope_claim`` stamps ``claim_session_id`` +
# ``claimed_at`` without changing task state (SPEC 方針 4).
PENDING_SCOPE_ACTION_AMEND = "scope_amend"
PENDING_SCOPE_ACTION_CLAIM = "scope_claim"
VALID_PENDING_SCOPE_ACTIONS = (
    PENDING_SCOPE_ACTION_ADD,
    PENDING_SCOPE_ACTION_REMOVE,
    PENDING_SCOPE_ACTION_AMEND,
    PENDING_SCOPE_ACTION_CLAIM,
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


def migrate_members_to_session_keyed(trek_doc: dict) -> dict:
    """Mutate ``trek_doc`` in place: members[] → session_id keyed (= AC6 cutover).

    Shared pure mutator used by both the CLI script
    (``scripts/migrate_members_session_keyed.py``) and the server endpoint
    (``POST /api/treks/{trek_id}/migrate-members-session-keyed``). I/O free —
    callers persist via ``db.save_trek`` / ``trek_store.save_trek``.

    展開ルール:
    - 各 user_id keyed member entry について、 ``session_history[]`` の中で
      ``user_id`` が一致する entry を全て探し、 1 session = 1 member entry に
      expand する。
    - ``role`` 解決: 旧 entry の role が "leader" の時は、
      ``leader_session_id`` に一致する session のみ "leader" を維持し、
      他 session は "member" に落とす (= 1 trek に 1 leader session の
      不変条件を維持)。
    - session_history に entry が無い user_id は orphan 扱いで skip
      (= 完全破壊を避ける)。

    Raises:
        ValueError: phase が pre-A 以外 (= 二重 migrate refusal)。
    """
    phase = get_migration_phase(trek_doc)
    if phase != "pre-A":
        raise ValueError(
            f"migrate: trek already at phase {phase!r}; "
            f"refusing double migration (= run revert script first)"
        )

    # Snapshot before mutation (= rollback contract). Raises if backup
    # field is non-empty — second-line guard against silent re-runs.
    backup_legacy_members(trek_doc)

    legacy_members = trek_doc.get("members_legacy_backup") or []
    session_history = trek_doc.get("session_history") or []
    leader_sid = trek_doc.get("leader_session_id") or ""

    sessions_by_user: dict[str, list[dict]] = {}
    for h in session_history:
        uid = h.get("user_id") or ""
        if not uid:
            continue
        sessions_by_user.setdefault(uid, []).append(h)

    new_members: list[dict] = []
    orphans: list[str] = []
    for legacy in legacy_members:
        user_id = legacy.get("user_id") or ""
        role = legacy.get("role") or "member"
        email = legacy.get("email") or ""
        invited_at = legacy.get("invited_at") or ""
        invited_by = legacy.get("invited_by") or ""
        sessions = sessions_by_user.get(user_id) or []
        if not sessions:
            orphans.append(user_id)
            continue
        for h in sessions:
            sid = h.get("session_id") or ""
            if not sid:
                continue
            entry_role = role
            if role == "leader" and sid != leader_sid:
                entry_role = "member"
            new_members.append({
                "session_id": sid,
                "user_id": user_id,
                "email": email or h.get("email") or "",
                "role": entry_role,
                "joined_at": h.get("joined_at") or "",
                "invited_at": invited_at,
                "invited_by": invited_by,
            })

    trek_doc["members"] = new_members
    if orphans:
        # Stash orphan user_ids on meta so callers can audit (= the CLI
        # script prints to stderr; the server returns this via the JSON
        # body so the operator can see them from a remote curl).
        meta = trek_doc.setdefault("meta", {})
        meta["migration_orphans"] = orphans
    set_migration_phase(trek_doc, "A")
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
    mint_slot: bool = False,
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
    if action not in (PENDING_SCOPE_ACTION_ADD, PENDING_SCOPE_ACTION_REMOVE):
        # ms-99 / e-2829: ``add_pending_scope_op`` handles entry-based
        # (= scope-add / scope-remove) verbs only. Slot-id-based verbs
        # (= scope_amend / scope_claim) go through the dedicated helpers
        # ``add_pending_slot_amend_op`` / ``add_pending_slot_claim_op``
        # because their payloads differ (slot_id + child list / session id
        # rather than a full scope entry).
        raise ValueError(
            f"pending scope action {action!r} not accepted here — "
            f"use add_pending_slot_amend_op / add_pending_slot_claim_op "
            f"for slot-id-based mutations"
        )
    # ms-97 / e-2659 (AC7 / AC8): the strict mode toggle mirrors the apply
    # helpers — ``scope_add`` enforces the narrowing-key requirement (= no
    # new project-wide pending), while ``scope_remove`` must keep working
    # against grandfathered project-wide rows already on disk.
    strict_norm = action == PENDING_SCOPE_ACTION_ADD
    # ms-99 / e-2829: ``mint_slot`` is False by default so legacy
    # ``beacon trek scope-add`` calls preserve the pre-v2 shape and their
    # existing tests keep passing. New ``beacon trek slot add`` calls
    # (= cmd_trek_slot_add) pass ``mint_slot=True`` to receive a fresh
    # ``sl-<8 hex>`` id. Director e-2828 decision: no silent backfill —
    # slot_id appears only when the caller opts in.
    norm = normalize_scope_entry(entry, strict=strict_norm, mint_slot=mint_slot)
    if action == PENDING_SCOPE_ACTION_REMOVE:
        scope = trek_doc.get("scope") or []
        # ms-99 / e-2828: identity-based match (see add_scope_entry).
        if not any(_scope_entry_identity_matches(s, norm) for s in scope):
            raise ValueError(f"scope entry not found: {norm}")
    elif action == PENDING_SCOPE_ACTION_ADD:
        scope = trek_doc.get("scope") or []
        if any(_scope_entry_identity_matches(s, norm) for s in scope):
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


# ---------------------------------------------------------------------------
# ms-99 / e-2829: slot-id-based pending helpers (amend / claim) + apply
# ---------------------------------------------------------------------------
# ``scope_amend`` edits ``included_task_ids`` on an existing MS slot: add
# a task-id to the opt-in child list or remove one. Non-list (= legacy null
# = all-include) is materialised to an explicit list on first amend so
# subsequent reads know the semantics turned explicit.
#
# ``scope_claim`` stamps ``claim_session_id`` + ``claimed_at`` without
# touching task state (SPEC 方針 4). Passing an empty ``session_id``
# unclaims the slot (= clears the two fields).
#
# Both go through the same ``pending_scope_ops`` queue so the AC 15 "all
# slot CLI ops via staging" contract holds across the four slot verbs.


def _find_slot_by_id(trek_doc: dict, slot_id: str) -> dict | None:
    """Return the scope entry with ``slot_id`` or None. Empty-string safe."""
    if not slot_id:
        return None
    for s in trek_doc.get("scope") or []:
        if s.get("slot_id") == slot_id:
            return s
    return None


def add_pending_slot_amend_op(
    trek_doc: dict,
    *,
    slot_id: str,
    add_children: list[str] | None = None,
    remove_children: list[str] | None = None,
    requested_by_session_id: str = "",
) -> dict:
    """Stage a slot-amend op (= edit ``included_task_ids`` on a MS slot).

    Raises ``ValueError`` if the slot doesn't exist or if no child
    add/remove is specified (= no-op amend has no meaning). Duplicate
    add / removal of an already-absent child is caught at apply time
    via ``_apply_slot_amend`` so the queue stays legible.
    """
    if not slot_id:
        raise ValueError("slot_id is required")
    add_list = list(add_children or [])
    remove_list = list(remove_children or [])
    if not add_list and not remove_list:
        raise ValueError(
            "slot amend requires at least one --add-child or --remove-child"
        )
    target = _find_slot_by_id(trek_doc, slot_id)
    if target is None:
        raise ValueError(f"slot not found: {slot_id}")
    record = {
        "pending_id": mint_pending_scope_op_id(),
        "action": PENDING_SCOPE_ACTION_AMEND,
        "slot_id": slot_id,
        "add_children": add_list,
        "remove_children": remove_list,
        "requested_by_session_id": requested_by_session_id or "",
        "requested_at": utcnow_iso(),
    }
    trek_doc.setdefault(PENDING_SCOPE_OPS_KEY, []).append(record)
    trek_doc["updated_at"] = utcnow_iso()
    return record


def add_pending_slot_claim_op(
    trek_doc: dict,
    *,
    slot_id: str,
    session_id: str,
    requested_by_session_id: str = "",
) -> dict:
    """Stage a slot-claim op (= stamp ``claim_session_id`` + ``claimed_at``).

    ``session_id=""`` is the unclaim gesture (clears both fields on
    apply). SPEC 方針 4: claim never changes task state — the split
    lets planning-phase "I'll take this next" be observed before any
    todo→working transition happens.
    """
    if not slot_id:
        raise ValueError("slot_id is required")
    target = _find_slot_by_id(trek_doc, slot_id)
    if target is None:
        raise ValueError(f"slot not found: {slot_id}")
    record = {
        "pending_id": mint_pending_scope_op_id(),
        "action": PENDING_SCOPE_ACTION_CLAIM,
        "slot_id": slot_id,
        "session_id": session_id or "",
        "requested_by_session_id": requested_by_session_id or "",
        "requested_at": utcnow_iso(),
    }
    trek_doc.setdefault(PENDING_SCOPE_OPS_KEY, []).append(record)
    trek_doc["updated_at"] = utcnow_iso()
    return record


def _apply_slot_amend(trek_doc: dict, *, rec: dict) -> None:
    """Apply a scope_amend pending record to the target slot in place."""
    slot_id = rec.get("slot_id") or ""
    target = _find_slot_by_id(trek_doc, slot_id)
    if target is None:
        raise ValueError(f"slot not found on apply: {slot_id}")
    current = target.get("included_task_ids")
    # Legacy null → materialise to explicit list at first amend so
    # semantics stop being "all children" and become "these children".
    if current is None:
        current = []
    else:
        current = list(current)
    for eid in rec.get("add_children") or []:
        if not eid:
            continue
        if eid not in current:
            current.append(eid)
    for eid in rec.get("remove_children") or []:
        if eid in current:
            current.remove(eid)
    target["included_task_ids"] = current


def _apply_slot_claim(trek_doc: dict, *, rec: dict) -> None:
    """Apply a scope_claim pending record to the target slot in place."""
    slot_id = rec.get("slot_id") or ""
    target = _find_slot_by_id(trek_doc, slot_id)
    if target is None:
        raise ValueError(f"slot not found on apply: {slot_id}")
    session_id = rec.get("session_id") or ""
    if session_id:
        target["claim_session_id"] = session_id
        target["claimed_at"] = utcnow_iso()
    else:
        target["claim_session_id"] = ""
        target["claimed_at"] = None


def materialize_slot_view(trek_doc: dict) -> list[dict]:
    """Return a slot-oriented view of the trek's scope for CLI ``slot list``.

    Each row surfaces the identity + v2 attributes so the reader can
    tell legacy null-children slots apart from opt-in children slots
    and see who currently claims what. Materialising the full
    include-set (= expanding None to project-pool tasks) is Phase 2's
    ``materialize_slots`` primitive (e-2832); this Phase 1 view just
    projects the on-disk fields.
    """
    out: list[dict] = []
    for s in trek_doc.get("scope") or []:
        row = {
            "project": s.get("project") or "",
            "slot_id": s.get("slot_id") or "",
            "target_kind": s.get("target_kind") or "",
            "target_id": s.get("target_id") or "",
        }
        # Fall back to legacy narrowing keys when target_kind/id are
        # missing (= grandfathered rows without v2 fields). Callers get
        # a stable shape whether the source is v2 or pre-v2.
        if not row["target_kind"]:
            for k in NARROWING_KEYS:
                if s.get(k):
                    row["target_kind"] = k
                    row["target_id"] = s[k]
                    break
        row["included_task_ids"] = s.get("included_task_ids", None)
        row["claim_session_id"] = s.get("claim_session_id") or ""
        row["claimed_at"] = s.get("claimed_at") or None
        out.append(row)
    return out


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
        trek_doc["updated_at"] = utcnow_iso()
        return entry
    if action == PENDING_SCOPE_ACTION_REMOVE:
        remove_scope_entry(trek_doc, entry=entry)
        trek_doc["updated_at"] = utcnow_iso()
        return entry
    # ms-99 / e-2829: slot-id-based verbs. The pending record for these
    # actions carries ``slot_id`` and per-verb payload keys rather than a
    # full scope ``entry`` — apply the mutation on the target slot in place.
    if action == PENDING_SCOPE_ACTION_AMEND:
        _apply_slot_amend(trek_doc, rec=rec)
        trek_doc["updated_at"] = utcnow_iso()
        return rec
    if action == PENDING_SCOPE_ACTION_CLAIM:
        _apply_slot_claim(trek_doc, rec=rec)
        trek_doc["updated_at"] = utcnow_iso()
        return rec
    raise ValueError(
        f"pending scope op {pending_id} has unknown action {action!r}"
    )


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
# AC24 blanket scope approval (ms-97 Phase 7-C, e-2603)
#
# Leader が「この category の scope-add は事前承認」 と宣言する仕組み。
# 該当 category にマッチする scope-add は ``add_pending_scope_op`` 経路を
# bypass して直接 ``add_scope_entry`` で commit する (= no DM hop)。 user が
# 明示 revoke するまで有効。
#
# Schema: ``trek.meta.blanket_scope_approvals: list[str]``
#
# Category 形式 (= scope entry に対する filter 表現):
#   * ``"operation"``        — operation narrowing を持つ全 add (= 任意 project)
#   * ``"milestone"``        — milestone narrowing を持つ全 add
#   * ``"task"``             — task narrowing を持つ全 add
#   * ``"project:<pid>"``    — 指定 project へ narrowing を伴う全 add
#   * ``"milestone:ms-XX"``  — 指定 milestone id を narrowing に持つ全 add
#                              (= 任意 project でこの ms-id に narrowing する add 全部)
#
# 不一致なら blanket は False を返し、 既存 pending 経路にフォールバックする。
# Project-wide scope (= no narrowing) は ``normalize_scope_entry`` strict mode で
# はじかれるので、 そもそも blanket の引数として届かない (= AC7 と直交)。
# ---------------------------------------------------------------------------

BLANKET_SCOPE_APPROVALS_META_KEY = "blanket_scope_approvals"

# ms-109 e-3699 (fable B-2): bare-kind blanket categories track the narrowing
# vocabulary (dev milestone/operation/task + sales opportunity/account), so a
# sales Trek can blanket-approve by opportunity just as a dev Trek does by
# milestone. Sourced from the same occupation registry as NARROWING_KEYS.
_BLANKET_NARROWING_CATEGORIES = NARROWING_KEYS


def _normalised_blanket_category(category: str) -> str:
    """Validate + canonicalise a blanket category string.

    Raises ``ValueError`` if the token does not match any of the 5
    supported shapes. Returns the trimmed string on success.
    """
    if not isinstance(category, str):
        raise ValueError(
            f"blanket category must be str, got {type(category).__name__}"
        )
    cat = category.strip()
    if not cat:
        raise ValueError("blanket category required (= non-empty string)")
    if cat in _BLANKET_NARROWING_CATEGORIES:
        return cat
    if ":" in cat:
        prefix, _, suffix = cat.partition(":")
        suffix = suffix.strip()
        if not suffix:
            raise ValueError(
                f"blanket category {category!r} requires a value after ':'"
            )
        if prefix == "project":
            return f"project:{suffix}"
        # ms-109 e-3699 (fable B-2): any narrowing kind can be id-scoped
        # (milestone:<ms-id>, opportunity:<opp-id>, ...), not just milestone.
        if prefix in NARROWING_KEYS:
            return f"{prefix}:{suffix}"
    raise ValueError(
        f"blanket category {category!r} not in supported forms: "
        f"a bare narrowing kind ({' | '.join(NARROWING_KEYS)}) | "
        f"'project:<pid>' | '<kind>:<id>'"
    )


def list_blanket_approvals(trek_doc: dict) -> list[str]:
    """Return the trek's current blanket approval list (= safe read copy)."""
    meta = trek_doc.get("meta") or {}
    vals = meta.get(BLANKET_SCOPE_APPROVALS_META_KEY) or []
    if not isinstance(vals, list):
        return []
    return [v for v in vals if isinstance(v, str) and v]


def is_blanket_approved(trek_doc: dict, entry: dict) -> bool:
    """Return True if ``entry`` matches any blanket category on the trek.

    ``entry`` should already be a normalised scope entry (= strict mode,
    so at least one narrowing key is present). The check walks the trek's
    blanket list and returns True on first match.

    The match is **inclusive** — a single entry can satisfy multiple
    categories. Example: ``{"project": "p1", "milestone": "ms-5"}``
    matches both ``"milestone"`` and ``"project:p1"`` and
    ``"milestone:ms-5"`` if any are present.
    """
    approvals = list_blanket_approvals(trek_doc)
    if not approvals:
        return False
    proj = entry.get("project") or ""
    for cat in approvals:
        # bare narrowing-kind category (milestone / operation / task /
        # opportunity / account): matches any entry carrying that kind.
        if cat in NARROWING_KEYS and entry.get(cat):
            return True
        if cat.startswith("project:"):
            if proj and cat[len("project:"):] == proj:
                return True
        elif ":" in cat:
            # id-scoped category <kind>:<id> — matches an entry narrowed to
            # that exact target (ms-109 e-3699: any kind, not just milestone).
            prefix, _, suffix = cat.partition(":")
            if prefix in NARROWING_KEYS and suffix and entry.get(prefix) == suffix:
                return True
    return False


def add_blanket_approval(trek_doc: dict, category: str) -> dict:
    """Append ``category`` to ``meta.blanket_scope_approvals`` (idempotent).

    Returns the mutated trek_doc (= chain-friendly). Raises ``ValueError``
    if ``category`` is not in the supported shape. Already-present
    categories are a no-op (= idempotent, leader can repeat the call
    without piling up dupes).
    """
    norm = _normalised_blanket_category(category)
    meta = trek_doc.setdefault("meta", {})
    current = meta.get(BLANKET_SCOPE_APPROVALS_META_KEY)
    if not isinstance(current, list):
        current = []
    if norm in current:
        return trek_doc
    current.append(norm)
    meta[BLANKET_SCOPE_APPROVALS_META_KEY] = current
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def remove_blanket_approval(trek_doc: dict, category: str) -> dict:
    """Drop ``category`` from ``meta.blanket_scope_approvals``.

    Returns the mutated trek_doc. Raises ``ValueError`` if the category
    string is malformed (= same validator as add). Returns the doc
    unchanged if the category is not currently present (= idempotent on
    the "make this gone" direction).
    """
    norm = _normalised_blanket_category(category)
    meta = trek_doc.setdefault("meta", {})
    current = meta.get(BLANKET_SCOPE_APPROVALS_META_KEY) or []
    if not isinstance(current, list) or norm not in current:
        return trek_doc
    meta[BLANKET_SCOPE_APPROVALS_META_KEY] = [c for c in current if c != norm]
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


# ---------------------------------------------------------------------------
# AC22 auto-succession (ms-97 Phase 7-B, e-2684)
#
# Leader session が「不応」 (= crash / stuck / 通信途絶) になった時、 priority
# order = invited_at 昇順で次の candidate session を選び、 1 hop consent DM
# を送って leader を引き継ぐ。 candidate が辞退したら次へ escalate、 全員
# 辞退または candidate 不在の時は user に escalation。
#
# 不応判定: N=3 連続 pulse-ack miss AND last_active >= 30 min stale。
# AND 条件で「crash / stuck」 を区別せず単一概念で処理する (= SPEC AC22)。
# threshold は trek.meta で configurable (= dogfood-driven 調整)。
# ---------------------------------------------------------------------------

# Default thresholds (= overridable via trek.meta.succession_*).
SUCCESSION_DEFAULT_MISS_COUNT = 3       # N 連続 pulse-ack miss
SUCCESSION_DEFAULT_STALE_MINUTES = 30   # last_active 経過 (min)

# Meta keys (= configurable threshold + state)
SUCCESSION_MISS_COUNT_META_KEY = "succession_miss_count"
SUCCESSION_STALE_MINUTES_META_KEY = "succession_stale_minutes"
SUCCESSION_PENDING_CANDIDATE_META_KEY = "succession_pending_candidate"
SUCCESSION_PENDING_EMITTED_AT_META_KEY = "succession_pending_emitted_at"
SUCCESSION_DECLINED_META_KEY = "succession_declined"
SUCCESSION_ESCALATED_AT_META_KEY = "succession_escalated_at"


def _parse_iso_to_dt(value: str) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp string to a UTC datetime.

    Accepts the Z-suffixed format that the rest of the codebase uses
    (= utcnow_iso) and the .%f microsecond variant from session
    registry stamps. Returns None on any parsing failure so callers
    can degrade gracefully (= a missing / malformed timestamp simply
    fails the "stale" check, never throws into the tick loop).
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.rstrip("Z")
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# ms-128 方針6 (e-4367) — per-target halt 機械検知。
#
# halt は独立した「状態」ではなく working な Target に付く「属性 (タグ)」。検知は
# サーバーに一元化し、trek_doc の task_states[task_id] に per-target の
# ``last_response_fingerprint`` / ``last_commit_count`` / ``progress_last_
# advanced_at`` を持たせて前回値と比較する。2 面で判定する:
#   (i)  進捗停滞 = 直近の応答が前回と同一 (fingerprint 一致) かつ commit 増分
#        ゼロ (commit_count 不変) が一定時間継続。
#   (ii) 生存断絶 = 最後の pulse から一定時間経過。
# 旧 TTL=24h は 4h halt を救えないため流用しない (= 別機構、SPEC 方針6)。閾値は
# cadence × N で ~4h の滞留を検知できる値。halt を「状態」でなく属性にするので、
# 検知しても Target は working のまま halt_reason タグが付くだけ (= 遷移は
# サーバーが別途 leader_review へ倒す、e-4309)。
HALT_PULSE_TIMEOUT_MINUTES = 240   # 生存断絶: 最後の pulse から 4h で halt
HALT_STALL_TIMEOUT_MINUTES = 240   # 進捗停滞: 同一応答 + commit 増分ゼロが 4h 継続
HALT_REASON_LIVENESS = "liveness-timeout"   # 生存断絶 (pulse 途絶)
HALT_REASON_STALL = "progress-stall"        # 進捗停滞 (同一応答 + commit 不変)


def compute_response_fingerprint(pulse_entry: dict) -> str:
    """Normalised hash of a session's latest tick response (ms-128 e-4367).

    Two ticks with the same fingerprint mean the session said the same thing
    twice (= no new signal). Combined with a zero commit increment this is
    the "progress stall" half of halt detection. We hash the *content* the
    executor self-reported (picked_choice / state_summary / blockers), not
    the timestamp, so a re-pulse with identical content collapses to the
    same fingerprint. ``no-op`` / empty responses fingerprint stably too, so
    a session that keeps waiting is naturally caught as stalled.
    """
    entry = pulse_entry or {}
    blockers = entry.get("last_blockers") or entry.get("blockers") or []
    if isinstance(blockers, (list, tuple)):
        blockers_s = "\x1e".join(str(b).strip().lower() for b in blockers)
    else:
        blockers_s = str(blockers).strip().lower()
    parts = [
        str(entry.get("last_picked_choice") or entry.get("picked_choice") or "").strip().lower(),
        str(entry.get("last_state_summary") or entry.get("state_summary") or "").strip().lower(),
        blockers_s,
    ]
    norm = "\x1f".join(parts)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


# NOTE (ms-119 review e-4389): these halt helpers take ``target_id`` — the id
# of the Trek Target (= a target-entity: milestone / operation id), which is
# also the key into the ``task_states`` map. The arg was named ``task_id``
# before 方針3 split task ≠ target; renamed for clarity so callers pass the
# Target id, not a sub-task id.
def get_target_halt_reason(trek_doc: dict, target_id: str) -> str | None:
    """Return the halt_reason tag on a Target (ms-128 e-4367), or None.

    halt is an attribute of the working Target, not a state — so this reads
    ``task_states[target_id].halt_reason`` without touching ``state``.
    """
    entry = (trek_doc.get("task_states") or {}).get(target_id) or {}
    return entry.get("halt_reason") or None


def clear_target_halt(trek_doc: dict, target_id: str) -> dict:
    """Drop the halt_reason tag on a Target (ms-128 e-4367).

    Called when a Target advances again (e.g. leader re-opens after a halt
    rescue, or the executor resumes and the next evaluate clears it).
    """
    states = trek_doc.get("task_states") or {}
    entry = states.get(target_id)
    if entry and entry.get("halt_reason"):
        entry["halt_reason"] = None
        trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def evaluate_target_halt(
    trek_doc: dict,
    target_id: str,
    *,
    current_fingerprint: str,
    current_commit_count: int,
    last_pulse_at: str = "",
    now: datetime.datetime | None = None,
    pulse_timeout_minutes: int = HALT_PULSE_TIMEOUT_MINUTES,
    stall_timeout_minutes: int = HALT_STALL_TIMEOUT_MINUTES,
) -> str | None:
    """Update per-target halt tracking and return the halt_reason (or None).

    ms-128 e-4367 — called by the server tick for each *working* Target. It
    compares this tick's ``current_fingerprint`` / ``current_commit_count``
    against the values stored on ``task_states[target_id]`` from the previous
    tick:

      * If either advanced (fingerprint changed OR commit count increased)
        → the Target made progress: refresh the stored values, stamp
        ``progress_last_advanced_at = now``, clear any halt_reason, return
        None.
      * If neither advanced → the Target is idle. If it has been idle past
        ``stall_timeout_minutes`` → tag ``progress-stall``. Independently, if
        the last pulse is older than ``pulse_timeout_minutes`` → tag
        ``liveness-timeout`` (takes precedence, = the session is likely dead,
        not merely stuck).

    Pure w.r.t. clock: ``now`` is injected (server tick passes UTC now, the
    e2e harness a fake clock). Mutates ``task_states[target_id]`` in place and
    returns the resolved halt_reason so the caller can decide to force the
    working→leader_review transition (e-4309).
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    states = trek_doc.setdefault("task_states", {})
    entry = states.get(target_id)
    if entry is None:
        # No task_state yet → nothing to evaluate against; seed baseline.
        entry = {
            "state": DEFAULT_TASK_STATE,
            "updated_at": utcnow_iso(),
            "last_activity_at": utcnow_iso(),
        }
        states[target_id] = entry

    last_fp = entry.get("last_response_fingerprint", "")
    last_cc = int(entry.get("last_commit_count") or 0)
    advanced = (current_fingerprint != last_fp) or (
        int(current_commit_count or 0) > last_cc
    )

    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if advanced or not last_fp:
        # Progress this tick (or first observation) → reset the stall clock.
        entry["last_response_fingerprint"] = current_fingerprint
        entry["last_commit_count"] = int(current_commit_count or 0)
        entry["progress_last_advanced_at"] = now_iso
        entry["halt_reason"] = None
        states[target_id] = entry
        trek_doc["updated_at"] = now_iso
        return None

    # No advance this tick. Resolve the two halt faces.
    reason: str | None = None
    pulse_dt = _parse_iso_to_dt(last_pulse_at) if last_pulse_at else None
    if pulse_dt is not None:
        if (now - pulse_dt) >= datetime.timedelta(minutes=pulse_timeout_minutes):
            reason = HALT_REASON_LIVENESS
    if reason is None:
        advanced_dt = _parse_iso_to_dt(entry.get("progress_last_advanced_at") or "")
        if advanced_dt is not None and (
            now - advanced_dt
        ) >= datetime.timedelta(minutes=stall_timeout_minutes):
            reason = HALT_REASON_STALL

    entry["halt_reason"] = reason
    states[target_id] = entry
    trek_doc["updated_at"] = now_iso
    return reason


def force_halt_to_leader_review(
    trek_doc: dict,
    target_id: str,
    *,
    halt_reason: str,
    now: datetime.datetime | None = None,
) -> dict:
    """Force a halted working Target to ``leader_review``, keeping halt_reason.

    ms-128 e-4309/方針6 — the server tick calls this when ``evaluate_target_halt``
    tags a halt. Unlike ``set_task_state`` (which REPLACES the task_states entry
    and would wipe the halt_reason tag + tracking fields), this mutates in place
    so the leader sees the target in ``leader_review`` **with** its halt_reason —
    that is what lets the leader's verdict set branch between 完成レビュー and
    halt 救済 (方針6). Idempotent: a target already in leader_review is left as-is
    (its halt_reason preserved). Only ``working → leader_review`` is performed.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    states = trek_doc.setdefault("task_states", {})
    entry = states.get(target_id)
    if entry is None:
        return trek_doc
    current = entry.get("state") or DEFAULT_TASK_STATE
    if current == "leader_review":
        # Already escalated; keep the (existing) halt_reason, no-op the state.
        entry.setdefault("halt_reason", halt_reason)
        states[target_id] = entry
        return trek_doc
    if current != "working":
        # Only working targets are force-halted (a terminal / todo target is
        # not "stuck"). Leave other states untouched.
        return trek_doc
    validate_task_state_transition("working", "leader_review")
    entry["state"] = "leader_review"
    entry["halt_reason"] = halt_reason
    entry["updated_at"] = now_iso
    entry["last_activity_at"] = now_iso
    entry["updated_by_session_id"] = "trek-scheduler"
    entry["note"] = (
        f"server-forced to leader_review (halt: {halt_reason}, ms-128 e-4309)"
    )
    states[target_id] = entry
    trek_doc["updated_at"] = now_iso
    return trek_doc


def sweep_working_target_halts(
    trek_doc: dict,
    *,
    commit_count_for: "Callable[[str], int]",
    now: datetime.datetime | None = None,
    pulse_timeout_minutes: int = HALT_PULSE_TIMEOUT_MINUTES,
    stall_timeout_minutes: int = HALT_STALL_TIMEOUT_MINUTES,
) -> list[dict]:
    """Evaluate halt for every working Target and force the halted ones.

    ms-128 e-4309/方針6 — the tick-side orchestration over ``evaluate_target_halt``.
    For each Target whose ``task_states[*].state == "working"`` it:

      1. reads the owner session's latest pulse (for the response fingerprint)
         and this tick's commit count (``commit_count_for(target_id)`` injected
         so this stays pure w.r.t. storage),
      2. calls ``evaluate_target_halt`` (updates tracking, returns halt_reason),
      3. if halted, forces ``working → leader_review`` with the halt_reason tag.

    ``no-op`` responses need no special case: a session that keeps replying
    ``no-op`` produces an unchanging fingerprint + zero commit increment, so it
    trips ``progress-stall`` here exactly like silence does — that is how the
    "無応答 → サーバー介入" contract (e-4309) is realised.

    Returns ``[{"target_id": ..., "halt_reason": ...}, ...]`` for the forced
    transitions (so the caller can surface them to the leader / audit). Mutates
    ``trek_doc`` in place; caller persists.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    states = trek_doc.get("task_states") or {}
    forced: list[dict] = []
    pulse_acks = trek_doc.get("pulse_acks") or {}
    # Snapshot ids first — we mutate state inside the loop.
    for target_id, entry in list(states.items()):
        if (entry or {}).get("state") != "working":
            continue
        owner_sid = (entry.get("owner_session_id")
                     or entry.get("updated_by_session_id") or "")
        pulse_entry = pulse_acks.get(owner_sid) or {}
        fingerprint = compute_response_fingerprint(pulse_entry)
        last_pulse_at = pulse_entry.get("last_pulse_ack_at", "") or ""
        try:
            commit_count = int(commit_count_for(target_id) or 0)
        except Exception:
            commit_count = 0
        reason = evaluate_target_halt(
            trek_doc, target_id,
            current_fingerprint=fingerprint,
            current_commit_count=commit_count,
            last_pulse_at=last_pulse_at,
            now=now,
            pulse_timeout_minutes=pulse_timeout_minutes,
            stall_timeout_minutes=stall_timeout_minutes,
        )
        if reason:
            force_halt_to_leader_review(
                trek_doc, target_id, halt_reason=reason, now=now)
            forced.append({"target_id": target_id, "halt_reason": reason})
    return forced


def detect_unresponsive_leader(
    trek_doc: dict,
    now: datetime.datetime,
    *,
    leader_last_active: str = "",
) -> str | None:
    """Return the leader session_id if the current leader is unresponsive.

    AC22 不応判定 — N 連続 pulse-ack miss **AND** last_active >= N min stale。
    AND 条件で「crash / stuck」 を単一概念に統合する (= SPEC AC22)。

    入力:
      * ``trek_doc`` — 評価対象 trek
      * ``now`` — 評価時刻 (= UTC datetime、 server tick endpoint が渡す)
      * ``leader_last_active`` — leader session の session registry
        ``last_active`` (= 別 store なので caller 経由で渡す)

    挙動:
      * phase pre-A trek (= members[] が user_id keyed legacy) は常に
        ``None``。 AC22 は session-grain identity が要るので phase A+
        のみ対象 (= 後方互換 invariant 保持)。
      * ``leader_session_id`` 空 → ``None`` (= 既に leader 不在 / planning-era)。
      * threshold は ``trek.meta.succession_miss_count`` (default 3) /
        ``succession_stale_minutes`` (default 30) で override 可能。
      * pulse-ack history の最新 N 件が全て「miss」 (= 後述の条件) かつ
        last_active が N 分以上 stale なら ``leader_session_id`` を返す。

    Miss 判定 (= pulse-ack history の各 entry):
      * ``pulse_acks[leader_sid]`` が空 / 不在 → N 件分 miss 扱い (= 一度も
        ack していない leader は構造的に不応)。
      * history の最新 N 件のうち、 picked_choice が ``no-op`` または ``""``
        (= 未指定 / 観測しただけ) のものを miss とカウントする。
      * "miss" の解釈 (= AC22 の「不応」) を 「ack はしたが action token を
        出していない」 まで広げる (= 形だけの心拍応答だけで実体 progress
        がない状態も不応として扱う)。 ``continue`` / ``terminal`` /
        ``dm-leader`` / ``dm-peer`` は応答ありとカウントする。

    Returns ``leader_session_id`` (= 不応と判定された session) or ``None``。
    """
    if not is_session_id_keyed(trek_doc):
        return None
    leader_sid = trek_doc.get("leader_session_id") or ""
    if not leader_sid:
        return None
    meta = trek_doc.get("meta") or {}
    try:
        miss_threshold = int(
            meta.get(SUCCESSION_MISS_COUNT_META_KEY)
            or SUCCESSION_DEFAULT_MISS_COUNT
        )
    except (TypeError, ValueError):
        miss_threshold = SUCCESSION_DEFAULT_MISS_COUNT
    try:
        stale_minutes = int(
            meta.get(SUCCESSION_STALE_MINUTES_META_KEY)
            or SUCCESSION_DEFAULT_STALE_MINUTES
        )
    except (TypeError, ValueError):
        stale_minutes = SUCCESSION_DEFAULT_STALE_MINUTES
    miss_threshold = max(1, miss_threshold)
    stale_minutes = max(1, stale_minutes)

    # Stale check (= last_active gap)
    last_active_dt = _parse_iso_to_dt(leader_last_active)
    if last_active_dt is None:
        # No session registry signal → treat as fully stale (= cannot
        # observe a live heartbeat, so the AND second leg passes).
        is_stale = True
    else:
        gap = now - last_active_dt
        is_stale = gap >= datetime.timedelta(minutes=stale_minutes)
    if not is_stale:
        return None

    # Miss check (= consecutive pulse-ack misses on the leader session)
    acks_map = trek_doc.get("pulse_acks") or {}
    entry = acks_map.get(leader_sid) or {}
    history = entry.get("history") or []
    if not history:
        # Empty history + stale → trivially unresponsive.
        return leader_sid
    recent = history[-miss_threshold:]
    if len(recent) < miss_threshold:
        # Not enough samples yet (= leader fired fewer than N pulses
        # since join). Combined with stale this still counts as miss
        # (= we have no positive signal of liveness).
        return leader_sid

    def _is_miss(record: dict) -> bool:
        # ms-119 review (e-4389): use the single no-response taxonomy so a
        # future non-advancing token propagates here automatically, instead of
        # this raw ("", "no-op") copy drifting from classify_tick_response.
        choice = (record.get("picked_choice") or "").strip()
        return choice in TICK_NO_RESPONSE_TOKENS

    if all(_is_miss(h) for h in recent):
        return leader_sid
    return None


def pick_succession_candidate(trek_doc: dict) -> dict | None:
    """Return the next succession candidate (member dict), or None.

    Priority order: ``invited_at`` 昇順 (= 早く join した順)。 Excludes:
      * current leader (= 既に失敗した session)
      * 既に decline した session (= ``meta.succession_declined[]``)
      * placeholder invitation (= session_id 未設定、 まだ join していない)
      * leader 候補に再選出できない role (= 現時点では role は filter せず、
        joined であれば leader 引き継ぎ対象とする)

    Returns ``None`` when no eligible candidate exists (= caller must
    escalate to user).
    """
    if not is_session_id_keyed(trek_doc):
        return None
    leader_sid = trek_doc.get("leader_session_id") or ""
    meta = trek_doc.get("meta") or {}
    declined = set(meta.get(SUCCESSION_DECLINED_META_KEY) or [])
    members = trek_doc.get("members") or []
    eligible: list[dict] = []
    for m in members:
        msid = (m.get("session_id") or "").strip()
        if not msid:
            # placeholder invitation (= not yet joined at session grain)
            continue
        if msid == leader_sid:
            continue
        if msid in declined:
            continue
        if not (m.get("joined_at") or "").strip():
            # invited but not joined
            continue
        eligible.append(m)
    if not eligible:
        return None
    eligible.sort(key=lambda m: (m.get("invited_at") or "", m.get("session_id") or ""))
    return eligible[0]


def record_succession_decline(trek_doc: dict, session_id: str) -> dict:
    """Append ``session_id`` to ``meta.succession_declined`` and clear pending.

    Idempotent: re-declining is a no-op on the list (= unique entries).
    Clears ``meta.succession_pending_candidate`` so the next tick can
    pick a new candidate without overlapping consents.
    """
    if not session_id:
        raise ValueError("session_id required to record succession decline")
    meta = trek_doc.setdefault("meta", {})
    declined = list(meta.get(SUCCESSION_DECLINED_META_KEY) or [])
    if session_id not in declined:
        declined.append(session_id)
    meta[SUCCESSION_DECLINED_META_KEY] = declined
    meta[SUCCESSION_PENDING_CANDIDATE_META_KEY] = ""
    meta[SUCCESSION_PENDING_EMITTED_AT_META_KEY] = ""
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


def clear_succession_state(trek_doc: dict) -> dict:
    """Clear all succession pending state (= called after a successful accept).

    Resets pending candidate fields and declined list so the next
    unresponsive-leader cycle starts fresh.
    """
    meta = trek_doc.setdefault("meta", {})
    meta[SUCCESSION_PENDING_CANDIDATE_META_KEY] = ""
    meta[SUCCESSION_PENDING_EMITTED_AT_META_KEY] = ""
    meta[SUCCESSION_DECLINED_META_KEY] = []
    meta[SUCCESSION_ESCALATED_AT_META_KEY] = ""
    trek_doc["updated_at"] = utcnow_iso()
    return trek_doc


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

    AC32 halt-suspension contract — final audit (ms-97 / Phase 4):

      1. tick scheduler         — ``server/app.py`` tick endpoint skips
         halted treks at the candidate filter (= 5972, defense-in-depth
         at 6023) AND ``lib/trek_scheduler.is_trek_due`` returns False
         on halt set (= the upstream cadence decision itself).
      2. DM bypass              — ``server/dm_gate.should_gate_dm_action``
         skips halted treks when evaluating ``shared_trek_member``
         bypass, so cross-user DMs fall back to the normal budget gate
         while the Andon cord is engaged.
      3. auto-stall             — tick endpoint skip at 6648 +
         ``lib/trek_scheduler.detect_auto_stalled_tasks`` returns ``[]``
         (= 507) on halt set.
      4. auto-succession        — tick endpoint AC22 path skips halted
         treks before evaluating ``detect_unresponsive_leader`` (= ms-97
         Phase 7-B / e-2684). Halt 中は succession nomination も consent
         DM emit も escalation stamp も全停止する。 Resume 後の次 tick で
         不応判定をやり直す。 Tests in ``tests/test_trek_succession.py``
         lock the halt-suspension contract for AC22.
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


def remove_member(trek_doc: dict, *,
                  user_id: str | None = None,
                  session_id: str | None = None) -> dict:
    """Remove a member from the trek.

    Guard rails:
    - Cannot remove the leader (= they must `transfer-leader` first).
    - Cannot remove the last member (= archive the trek instead).

    ms-97 / e-2658 Phase 1 (AC6) — dual-grain removal:

    - ``session_id`` 指定 → 該当 1 session の member entry のみ削除
      (= 同 user の他 session entries は残る、 session-grain leave)
    - ``user_id`` 指定 → 該当 user の全 entries を削除 (= 旧 user-grain
      semantics、 pre-A trek の挙動と互換)
    - 両方指定すると session_id を優先 (= 厳密粒度の方を採用)
    - どちらも空 / None なら ValueError
    """
    if not user_id and not session_id:
        raise ValueError(
            "remove_member: either user_id or session_id must be given"
        )
    members = trek_doc.get("members") or []
    if session_id:
        target = find_member(trek_doc, session_id=session_id)
        if target is None:
            raise ValueError(
                f"session {session_id} not a member of trek "
                f"{trek_doc.get('trek_id')}"
            )
        if target.get("role") == "leader":
            raise ValueError(
                f"cannot remove leader session ({session_id}); use "
                "`beacon trek transfer-leader` to hand off first"
            )
        new_members = [m for m in members
                       if m.get("session_id") != session_id]
        if not new_members:
            raise ValueError(
                "cannot remove last member; archive the trek instead"
            )
        trek_doc["members"] = new_members
        trek_doc["updated_at"] = utcnow_iso()
        return trek_doc

    target = find_member(trek_doc, user_id=user_id)
    if target is None:
        raise ValueError(
            f"user {user_id} not a member of trek {trek_doc.get('trek_id')}"
        )
    # Leader guard: any matching entry with role=leader blocks removal.
    leader_match = next(
        (m for m in members
         if m.get("user_id") == user_id and m.get("role") == "leader"),
        None,
    )
    if leader_match is not None:
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
