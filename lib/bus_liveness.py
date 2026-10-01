"""ms-165 / e-5965 — the *progress* dimension of session liveness.

Session liveness has three orthogonal dimensions (CORE doc
``liveness-three-dimensions``). Each answers a different question and is
computed from a different signal, so collapsing them into one field hides the
failure mode the others can't see:

  * **transport** — can a receive path reach this session at all?
    (``ws_live`` / ``poll_health.healthy`` → ``live``). The picker reads this.
  * **progress**  — is a live session actually CONSUMING (draining) its inbox,
    or receiving-but-not-consuming (wedged)?  (``draining`` → ``reachable``).
    Only the send-path strict check reads this. THIS MODULE.
  * **attention** — is a human/AI actively driving the session right now?
    (``heartbeat_fresh``). Informational only.

The wedge this dimension catches: a session whose bridge polls (so ``live`` is
true) but never advances its cursor — DMs addressed to it pile up unread. The
sender is fooled into "sent✓ delivered✗" because the transport check only
proves polling. ``draining`` is derived from the EXISTING unread + cursor state
(no new schema): if the oldest event still unread by the recipient is older
than the attentiveness window, the session is receiving but not consuming.

Pure functions only — the impure store scan that produces
``oldest_unread_created_at`` lives on the server (``app._oldest_unread_addressed_created_at``).
Keeping the derivation pure lets "stale unread ⇒ not draining" be pinned
without a bus fixture.
"""
from __future__ import annotations

import datetime
from typing import Optional

# Send-path graded verdicts (ms-165 SPEC 方針 b). Named constants so callers
# compare against a symbol, not a bare string that could drift.
SEND_NORMAL = "normal"
SEND_WEDGED = "wedged"
SEND_NOT_LIVE = "not_live"

# ---------------------------------------------------------------------------
# ms-159 / e-6243 — the *work-unit state* projection (統合オペレーションUI).
#
# The 5 canonical work-unit states (作業単位状態モデル SPEC ``np2fSUqpE5LSIkOqHLuK``
# 判断1). A session/Operation run往復 between these; every UI / inbox / attention
#面 reads ONLY this small frozen set, never an executor's native vocabulary.
STATE_RUNNING = "running"                # 自律実行中 — no human attention needed
STATE_IDLE = "idle"                      # 生きて待機、判断要求なし — no attention
STATE_AWAITING_HUMAN = "awaiting_human"  # 具体的な判断要求が立っている — attention
STATE_BLOCKED = "blocked"                # 外部要因で停止 — conditional attention
STATE_TERMINATED = "terminated"          # 終了(completed/aborted/failed)
# ``unknown`` は状態集合の一員だが *宣言できない* 特別枠 (判断1補足 / 判断4):
# セッションは自分が unknown だと報告できない (死んだ executor は「私は死んだ」と
# 言えない)。server が不在検知 / 宣言不信で立てる、人間の注意を引く側の安全弁。
STATE_UNKNOWN = "unknown"
# ms-177 — ``interrupted`` (中断) is the OTHER server-raised state, and like
# ``unknown`` it cannot be declared: a session killed by a closed terminal / an
# API error / a context-limit exit does not get to file a report on its way out.
# It means "this WAS a real working session (it declared running/idle/
# awaiting_human/blocked) and then its receive path vanished WITHOUT going
# through ``beacon session end``" — i.e. it stopped by accident, not on purpose.
# Split out of ``unknown`` so the ops room can show an accidental death loudly
# instead of burying it in the same grey "stopped" bucket as a clean exit
# (ms-177 SPEC 方針1/2). ``unknown`` thereby returns to its one true meaning:
# live but never stated what it is doing.
STATE_INTERRUPTED = "interrupted"

# The states a session may self-declare (方針1: 自己宣言が正)。``unknown`` /
# ``interrupted`` は含まない — 宣言由来では決して現れない (判断4 / ms-177)。
DECLARABLE_STATES = frozenset({
    STATE_RUNNING, STATE_IDLE, STATE_AWAITING_HUMAN, STATE_BLOCKED,
    STATE_TERMINATED,
})

# The COMPLETE range of ``derive_state`` — every value a consumer (ops room,
# Go viewer, roster ordering, attention filter) may receive. Consumers that
# enumerate states MUST check exhaustiveness against THIS set, not against
# ``DECLARABLE_STATES | {STATE_UNKNOWN}``: the server-raised states are exactly
# the ones a consumer forgets, and a guard written from the declarable set stays
# green while a new state silently falls into some `.get(..., default)` bucket
# (ms-177 — that is how ``interrupted`` would have slipped past the existing
# ``_ROSTER_STATE_ORDER`` exhaustiveness test).
ALL_STATES = DECLARABLE_STATES | frozenset({STATE_UNKNOWN, STATE_INTERRUPTED})


def derive_draining(oldest_unread_created_at, now, window_seconds) -> Optional[bool]:
    """Return whether a session is *draining* its inbox.

    - ``True``  — no unread backlog, OR the oldest unread event is younger than
      ``window_seconds`` (still in the normal in-flight grace period).
    - ``False`` — the oldest event still unread by the recipient is OLDER than
      ``window_seconds``: it is receiving (polling) but not consuming = wedged.
    - ``None``  — unknown (no timestamp to judge, or unparseable). Never a hard
      signal; callers treat ``None`` as "not definitively wedged" so an idle
      session with no backlog is never wrongly dropped.

    ``oldest_unread_created_at`` is the ``created_at`` of the OLDEST event still
    unread by the recipient (past its cursor), or a falsy value when the inbox
    has no backlog. The threshold reuses the attentiveness window (a healthy
    bridge drains in seconds; a backlog older than the human-attention window is
    a wedge, not in-flight latency) — no new constant, per SPEC 方針 a.
    """
    if not oldest_unread_created_at:
        # No backlog past the cursor → the session is keeping up.
        return True
    try:
        oldest = datetime.datetime.fromisoformat(
            str(oldest_unread_created_at).replace("Z", "+00:00"))
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        # Unparseable stamp — unknown, not dead. Fail toward reachable.
        return None
    age = (now - oldest).total_seconds()
    return age <= window_seconds


def is_reachable(live, draining) -> bool:
    """``reachable = live AND (draining is not False)``.

    Only a *definitive* wedge (``draining is False``) makes a live session
    unreachable. Unknown draining (``None``) keeps a live session reachable, so
    an idle fork with no backlog is never dropped from the picker (the SPEC 方針
    c no-regression guarantee: the ``live`` union is unchanged; ``reachable`` is
    an ADDITIONAL field only the send path reads strictly).
    """
    return bool(live) and (draining is not False)


def classify_send_delivery(live, draining) -> str:
    """3-tier graded send verdict for a DM recipient (SPEC 方針 b).

    - ``SEND_NORMAL``   — live and reachable: deliver as usual; the send
      response is byte-unchanged (the healthy common path must not regress).
    - ``SEND_WEDGED``   — live but NOT draining: still enqueued, but the send
      response carries loud ``recipient_wedged`` / ``delivery_uncertain`` flags
      so every client path (CLI --json, MCP reply, headless) surfaces it — not
      a soft field a caller can skip past.
    - ``SEND_NOT_LIVE`` — no transport liveness at all: existing behaviour
      unchanged (the picker / soft-warn path already covers a dead session).
    """
    if not live:
        return SEND_NOT_LIVE
    if draining is False:
        return SEND_WEDGED
    return SEND_NORMAL


def _declaration_is_stale(declared_at, now, stale_after_seconds) -> bool:
    """Return whether a state declaration is too old to trust.

    ``True`` when the declaration is older than ``stale_after_seconds`` — OR when
    its timestamp is missing/unparseable. A declaration we cannot date is treated
    as stale: safe side, since we would rather surface something for human
    attention than trust an undatable self-report. ``False`` only when the stamp
    parses AND is within the freshness window.

    This predicate answers only "is the stamp too old to trust". **What a stale
    declaration then projects to is NOT described here** — ``derive_state`` is the
    single source of truth for that mapping (it depends on liveness too). Naming
    the outcome in this docstring as well is how it went stale once already: it
    said ``unknown`` and kept saying so after ms-177 changed the outcome to
    ``interrupted``. Read ``derive_state`` for the mapping; keep this one about
    the stamp.
    """
    if not declared_at:
        return True
    try:
        stamped = datetime.datetime.fromisoformat(
            str(declared_at).replace("Z", "+00:00"))
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return True
    return (now - stamped).total_seconds() > stale_after_seconds


# ms-173 / e-6729 — WS 単独で立っている生存主張を、裏付けが無いときに取り下げる。
#
# 背景 (e-6583 の実測 2026-10-01): 親が死んで孤児になった bridge が 25 日間 WS を
# つないだまま ping を送り続け、死んだセッションが ws_live=true のまま居座った。真因は
# bridge 側で直したが (= ping を「生存報告が通っていること」に結び直した)、直したコードが
# 入っていない古い bridge には届かない。e-6583 の記述自身が「poll 履歴を持たない行には
# ガードが効かない設計なので、そこでリークすると永久ゾンビが再発しうる」と指摘していた穴。
#
# e-6563 の先行ガードは `last_poll_at` を一度でも書いた行だけを対象にしていた (= 古い
# bridge を誤って not-live にしないための意図的な除外)。その除外が穴そのものなので、
# 「poll 履歴があるか」で線を引くのをやめ、**生存の裏付けが何か一つでも新しいか** で引く。
#
# ここは e-6582 (汚染された『確認待ち』の降格) と違い、曖昧さが無い。「WS で生存を主張して
# いるのに、生存の痕跡がどれも 30 分以上古い (または一つも無い)」は、本物を隠す恐れのある
# 推定ではなく、嘘をついている証拠そのもの。だから倒す向きを人に問う必要がない。
#
# 誤爆しない側の安全弁が「裏付けを複数源から取る」こと: poll 報告が無い古い bridge でも、
# 人/AI が実際に動かしていれば PostToolUse hook 由来の heartbeat (last_heartbeat_at) が
# 新しい。だから「古い bridge かどうか」ではなく「生きている痕跡があるか」で救う。
WS_SUPPRESS_POLL_STALE = "ws-zombie-poll-stale"          # e-6563 と同一の理由文字列 (互換)
WS_SUPPRESS_NO_EVIDENCE = "ws-zombie-no-liveness-evidence"


def ws_only_liveness_suppression(ws_live, poll_healthy, has_poll_history,
                                 evidence_ages_seconds, max_age_seconds,
                                 max_age_seconds_no_history=None,
                                 session_age_seconds=None):
    """WS 単独の生存主張を取り下げるべきか。取り下げる理由、または ``None``。

    Args:
        ws_live: 接続台帳の raw signal (True / False / None)。
        poll_healthy: poll 報告が健全か。
        has_poll_history: ``last_poll_at`` を一度でも書いたか (理由文字列の選択だけに使う。
            **抑止するか否かの判定には使わない** — そこが e-6563 の穴だった)。
        evidence_ages_seconds: 生存の裏付けの古さ (秒) を並べたもの。``None`` は
            「その源は無い / 読めない」。poll 報告・hook heartbeat・最終活動のように、
            互いに独立した源を渡す (1 源だけだとその源を持たない正当な行を誤爆する)。
        max_age_seconds: poll 履歴がある行で、この秒数以内の裏付けが 1 つでもあれば救う。
        max_age_seconds_no_history: poll 履歴が **無い** 行に使う、より長い猶予。
            省略時は ``max_age_seconds`` と同じ。履歴が無い行は「古い bridge が正当に
            待機しているだけ」の可能性を server 側から確かめる手段が無い (ping は
            ゾンビも送る) ため、確信度が低い側に長い猶予を与えて誤爆を減らす。
            ゾンビは日単位で居座るので、時間単位の猶予でも検知力は落ちない
            (実測されたゾンビは 25 日 / 500 時間)。
        session_age_seconds: セッション自身の年齢 (秒)。猶予より若ければ **判定しない**。
            繋いだ直後の bridge は、まだ一度も生存報告を出していないのが正常なので、
            「裏付けが無い」を嘘の証拠として扱ってはならない (これを入れないと、
            起動直後の数秒だけ not-live に見える窓ができる)。年齢は生存の証拠ではなく
            「まだ証拠を期待できない」ことの根拠なので、裏付けとは別の引数で受ける。

    Returns:
        ``WS_SUPPRESS_POLL_STALE`` / ``WS_SUPPRESS_NO_EVIDENCE`` / ``None``。

    契約:
      * ``ws_live`` が True でない、または poll が健全なら **何もしない** (``None``)。
        この関数は「WS 単独で立っている主張」だけを扱う。
      * 裏付けが 1 つでも新しければ救う。誤って not-live にすると DM が届かなくなり、
        嘘を残すより重い害になるので、判定は救う側に倒す。
      * 裏付けが全て古い / 一つも無い場合に取り下げる。
      * セッションが猶予より若ければ何もしない (まだ証拠を期待できない)。
      * 閾値が不正 (None / 0 以下) なら何もしない (= 設定ミスで健全な行を黙らせない)。
    """
    if ws_live is not True or poll_healthy:
        return None
    limit = max_age_seconds if has_poll_history else (
        max_age_seconds_no_history if max_age_seconds_no_history is not None
        else max_age_seconds)
    if limit is None or limit <= 0:
        return None
    if session_age_seconds is not None and session_age_seconds <= limit:
        return None          # 若すぎて判定できない (証拠の不在を嘘の証拠にしない)
    ages = [a for a in (evidence_ages_seconds or ()) if a is not None]
    if any(a <= limit for a in ages):
        return None
    return WS_SUPPRESS_POLL_STALE if has_poll_history else WS_SUPPRESS_NO_EVIDENCE


def derive_state(declared_state, declared_at, live, now,
                 stale_after_seconds) -> str:
    """Project a work unit's canonical ``state`` (ms-159 / e-6243).

    The one place every value in ``ALL_STATES`` is decided. Pure: the
    impure liveness scan that produces ``live`` lives on the server; keeping the
    derivation pure lets every branch be pinned without a bus fixture (same
    discipline as ``derive_draining``).

    Authority model (作業単位状態モデル SPEC ``np2fSUqpE5LSIkOqHLuK`` 判断1/4 +
    slice SPEC ``Icb8zFtbnZZ1yXzMsLO6`` 方針1/4):

    Since ms-177 the two server-raised states divide cleanly: ``interrupted``
    means "it was working and its transport died" (not live), ``unknown`` means
    "it is live but has never said what it is doing". Neither is declarable.

    - **``terminated`` is terminal.** A session that reported SessionEnd stays
      ``terminated`` regardless of liveness or age — it legitimately stops
      emitting, so nothing flips it to ``unknown``.
    - **While LIVE, the latest declaration is authoritative — staleness does NOT
      apply.** The bridge heartbeats every few seconds; that live heartbeat
      continuously re-affirms the marker (the session would push a NEW marker if
      its state changed), so ``declared_at`` age is not evidence the state is
      wrong. This is the correction that lets a session parked in
      ``awaiting_human`` for hours stay visible at the top of the attention面
      (its ``declared_at`` freezes while it waits — no hook fires — but the
      heartbeat proves it is still genuinely waiting). Uniform staleness would
      instead hide exactly the long-waiting sessions this MS exists to surface.
      The one live exception: **no declaration at all ⇒ ``unknown``** (判断4:
      never silently assume ``running`` — the safe side toward human attention).
    - **Once NOT LIVE, staleness is what indicts the declaration.** With the
      heartbeat gone we can no longer confirm the state, so:
        * a *fresh* declaration is a very recent death — trust it through a short
          grace window (a just-crashed ``awaiting_human`` still reads
          ``awaiting_human`` for a moment, indistinguishable from a real pause);
        * a *stale* declaration ⇒ ``interrupted`` (中断) — transport gone AND the
          last report is old, so a lingering non-terminal state must be
          neutralized (判断4 固着 backstop: a dead session frozen in
          ``awaiting_human`` must not nag the inbox forever). ms-177 names this
          outcome instead of folding it into ``unknown``: it is precisely the
          accidental death (terminal closed / API error / context-limit exit) the
          ops room must show LOUDLY, because the human wants to resume it. Note
          this covers ``blocked`` too — a blocked session that then loses its
          transport also died without ending, so it takes the same branch rather
          than needing a second rule.
        * *no* declaration ⇒ ``terminated`` (no transport and nothing ever
          declared = gone). ms-177 SPEC 方針4 accepts that ``terminated`` stays a
          mixed bucket (clean exit + never-declared death) for now: separating
          those needs ``last_poll_at`` as a derivation input.

    So ``stale_after_seconds`` is a *post-death grace window*, not a general
    freshness clock — it is consulted only on the not-live path. (The precise
    live-stuck detector — a session that is live but silently stopped declaring
    because its hooks broke — is the deferred deadline-sweep / generic server
    tick, np2 判断6; this slice deliberately trusts a live declaration over
    catching that rarer case, because mis-hiding awaiting_human is the worse
    failure.)

    Args:
        declared_state: the session's last self-declared state, or a falsy /
            unrecognized value when it never declared one.
        declared_at: ISO-8601 timestamp of that declaration (``None`` if absent).
        live: transport liveness of the session (the ``live`` union used by the
            picker). Load-bearing: the primary axis here.
        now: current tz-aware ``datetime``.
        stale_after_seconds: post-death grace window (only consulted when
            ``live`` is false).

    Returns:
        One of ``ALL_STATES``: ``STATE_RUNNING`` / ``STATE_IDLE`` /
        ``STATE_AWAITING_HUMAN`` / ``STATE_BLOCKED`` / ``STATE_TERMINATED`` /
        ``STATE_INTERRUPTED`` / ``STATE_UNKNOWN``.
    """
    # 1. Terminal declaration is authoritative forever — never let age or a
    #    dropped transport flip an ended session to unknown.
    if declared_state == STATE_TERMINATED:
        return STATE_TERMINATED

    # 2. A recognized non-terminal declaration.
    if declared_state in DECLARABLE_STATES:  # non-terminal (terminated handled)
        if live:
            # Live heartbeat re-affirms the marker → trust it regardless of age.
            # This keeps a long-waiting awaiting_human at the top of attention.
            return declared_state
        # Not live: the heartbeat is gone, so staleness now decides.
        if not _declaration_is_stale(declared_at, now, stale_after_seconds):
            return declared_state          # very recent death: grace window
        # Gone + stale, and it HAD declared real work ⇒ 中断 (ms-177). This is
        # the 固着 backstop as before — the frozen awaiting_human stops nagging —
        # but it is now named for what it actually is instead of being dumped in
        # ``unknown``: a session that was working and died without ending.
        return STATE_INTERRUPTED

    # 3. No (or unrecognized) declaration ⇒ liveness fallback (判断4 safe side).
    #    live ⇒ up but unstated ⇒ unknown; not live ⇒ gone ⇒ terminated.
    if live:
        return STATE_UNKNOWN
    return STATE_TERMINATED
