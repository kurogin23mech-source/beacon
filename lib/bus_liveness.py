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

# The states a session may self-declare (方針1: 自己宣言が正)。``unknown`` は
# 含まない — 宣言由来では決して現れない (判断4)。
DECLARABLE_STATES = frozenset({
    STATE_RUNNING, STATE_IDLE, STATE_AWAITING_HUMAN, STATE_BLOCKED,
    STATE_TERMINATED,
})


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
    as stale (safe side: we would rather raise ``unknown`` for human attention
    than trust an undatable self-report). ``False`` only when the stamp parses AND
    is within the freshness window.
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


def derive_state(declared_state, declared_at, live, now,
                 stale_after_seconds) -> str:
    """Project a work unit's canonical ``state`` (ms-159 / e-6243).

    The one place the 5 canonical states + ``unknown`` are decided. Pure: the
    impure liveness scan that produces ``live`` lives on the server; keeping the
    derivation pure lets every branch be pinned without a bus fixture (same
    discipline as ``derive_draining``).

    Authority model (作業単位状態モデル SPEC ``np2fSUqpE5LSIkOqHLuK`` 判断1/4 +
    slice SPEC ``Icb8zFtbnZZ1yXzMsLO6`` 方針1/4):

    - **A fresh self-declaration is authoritative.** The session knows its own
      state best, so a recognized, in-window ``declared_state`` is returned as
      is (方針1). ``running`` / ``idle`` / ``awaiting_human`` / ``blocked``.
    - **``terminated`` is terminal.** A session that reported SessionEnd stays
      ``terminated`` regardless of liveness or age — it legitimately stops
      emitting, so staleness must not flip it to ``unknown``.
    - **A stale non-terminal declaration ⇒ ``unknown``.** The self-report can no
      longer be trusted; raising ``unknown`` (the attention-drawing side) both
      satisfies 方針1 ("live-but-silent ⇒ unknown") and neutralizes the 判断4
      固着 hazard (a dead session frozen in ``awaiting_human`` would otherwise
      nag the inbox forever).
    - **No declaration ⇒ fall back to liveness** (AC1: "宣言が無いときは liveness
      から fallback"). ``live`` is load-bearing HERE: a live-but-unstated session
      becomes ``unknown`` (判断4: never silently assume ``running`` — safe side
      toward human attention), while a not-live session with nothing ever
      declared becomes ``terminated`` (the coarse fallback: no transport and no
      state = gone).

    Args:
        declared_state: the session's last self-declared state, or a falsy /
            unrecognized value when it never declared one.
        declared_at: ISO-8601 timestamp of that declaration (``None`` if absent).
        live: transport liveness of the session (the ``live`` union used by the
            picker). Only consulted on the no-declaration fallback path.
        now: current tz-aware ``datetime``.
        stale_after_seconds: freshness window for a declaration.

    Returns:
        One of ``STATE_RUNNING`` / ``STATE_IDLE`` / ``STATE_AWAITING_HUMAN`` /
        ``STATE_BLOCKED`` / ``STATE_TERMINATED`` / ``STATE_UNKNOWN``.
    """
    # 1. Terminal declaration is authoritative forever — never let age or a
    #    dropped transport flip an ended session to unknown.
    if declared_state == STATE_TERMINATED:
        return STATE_TERMINATED

    # 2. A recognized non-terminal self-declaration is authoritative *while
    #    fresh* (方針1). Once stale, the report is untrustworthy ⇒ unknown
    #    (方針1 live-but-silent + 判断4 固着 backstop; not-live folds in here too
    #    — a lingering non-terminal state must be actively neutralized).
    if declared_state in DECLARABLE_STATES:  # non-terminal (terminated handled)
        if not _declaration_is_stale(declared_at, now, stale_after_seconds):
            return declared_state
        return STATE_UNKNOWN

    # 3. No (or unrecognized) declaration ⇒ liveness fallback (判断4 safe side).
    #    live ⇒ up but unstated ⇒ unknown; not live ⇒ gone ⇒ terminated.
    if live:
        return STATE_UNKNOWN
    return STATE_TERMINATED
