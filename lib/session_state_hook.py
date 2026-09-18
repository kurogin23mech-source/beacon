"""ms-159 / e-6244 — map a Claude Code lifecycle hook event to a declared state.

The 統合オペレーションUI needs each session to *declare* its own execution state
so a human never has to open a terminal to learn "is this one waiting on me?".
The declaration source is Claude Code's lifecycle hooks (slice SPEC
``Icb8zFtbnZZ1yXzMsLO6`` 方針2 — the 要石): each hook fires the instant the
session changes footing, so mapping the event to a canonical state gives a
precise, active signal (not a passive heartbeat guess).

This module is the pure mapping only. The impure parts live elsewhere:
``bin/beacon-state-hook.py`` reads the hook stdin and writes the marker file;
``channel/bus.mjs`` piggybacks the marker onto the existing heartbeat PUT
(方針4 — no new send path); the server projects the final ``state`` via
``bus_liveness.derive_state`` (e-6245). Keeping the map pure lets the
event→state contract be pinned without a harness or a bus.

Only the four transitions the slice owns are wired (方針2 table):

    UserPromptSubmit / PreToolUse / PostToolUse → running        (人/AI が駆動中)
    Notification                                → awaiting_human  (入力/許可待ち)
    Stop                                        → idle            (ターン終了)
    SessionEnd                                  → terminated      (終了)

``blocked`` has no hook trigger in this slice (its declaration path is deferred,
slice SPEC スコープ「やらない」) and ``unknown`` is never self-declared — the
server infers it from absence (方針1). Any other / unknown event maps to
``None`` = "no declaration", so an unrecognized hook never fabricates a state.
"""
from __future__ import annotations

from typing import Optional

import bus_liveness

# Claude Code lifecycle hook event name → canonical declared state.
# Sourced 1:1 from slice SPEC 方針2. A session drives through tool calls and
# prompts (all ``running``); the human-facing transitions are the precise ones.
_EVENT_STATE_MAP = {
    "UserPromptSubmit": bus_liveness.STATE_RUNNING,
    "PreToolUse": bus_liveness.STATE_RUNNING,
    "PostToolUse": bus_liveness.STATE_RUNNING,
    "Notification": bus_liveness.STATE_AWAITING_HUMAN,
    "Stop": bus_liveness.STATE_IDLE,
    "SessionEnd": bus_liveness.STATE_TERMINATED,
}


def event_to_declared_state(event_name) -> Optional[str]:
    """Return the canonical state a hook event declares, or ``None``.

    ``None`` means "this event declares nothing" — an unrecognized or empty
    event must never invent a state (the marker writer then leaves the last
    declaration untouched rather than clobbering it with a guess).
    """
    if not event_name:
        return None
    return _EVENT_STATE_MAP.get(str(event_name))


# --- Notification の 2 系統判別 (ms-173 PR#758 QA finding, 2026-09-18) ---------
#
# Claude Code の Notification hook は 2 種類を同じ event で運ぶ:
#   (1) 許可要求 "Claude needs your permission to use X" — 本当に応答を要する待ち
#   (2) アイドル通知 "Claude is waiting for your input" — ターン終了後 60 秒放置で
#       発火する「プロンプトに座っているだけ」の合図
# 従来は両方を無条件に awaiting_human へ写したため、放置セッションが運用室で
# 「確認待ち」(橙) に過剰発火した (人間 user が実機観測)。(2) は下の規則で分ける。
#
# 判別は message の部分一致 (case-insensitive)。harness の英文が変わると (2) を
# 見逃す方向に外れる = 過剰発火側 (旧挙動) へ戻るだけで、確認待ちの取りこぼしは
# 起きない (fail-safe の向きを保守側に固定)。
_IDLE_NOTIFICATION_MARKER = "waiting for your input"

#: 選択肢提示ツール。PreToolUse でこのツールが立った瞬間が「応答を要する待ち」の
#: 確定的な宣言点 (Notification のアイドル通知と違い曖昧さが無い)。
ASK_TOOL_NAME = "AskUserQuestion"


def is_idle_notification(message) -> bool:
    """True iff the Notification message is the idle nudge ("waiting for your
    input") — プロンプトに座っているだけで、応答を要する待ちではない。"""
    return _IDLE_NOTIFICATION_MARKER in str(message or "").lower()


def ask_question_detail(tool_input) -> str:
    """AskUserQuestion の tool_input から待機内容 (先頭の質問文) を引く。

    形は harness 依存 ({questions: [{question: ...}]}) なので best-effort — 引けな
    ければ空を返し、呼び出し側は detail 無しの awaiting_human として扱う (でっち
    上げない、ms-173 方針2)。"""
    try:
        questions = (tool_input or {}).get("questions") or []
        q = str((questions[0] or {}).get("question") or "").strip()
        return q[:200]
    except Exception:
        return ""


def build_state_marker(event_name, now_iso, prev_marker=None, *, detail="",
                       tool_name="") -> Optional[dict]:
    """Build the ``.beacon/session-state.json`` marker payload for an event.

    Returns ``None`` when the event declares nothing (see
    ``event_to_declared_state``) so the caller writes nothing. Otherwise returns
    ``{declared_state, declared_at, state_since, source_event}`` (+ optional
    ``state_detail``).

    Two timestamps, deliberately distinct (ms-159 e-6245):

    * ``declared_at`` — WHEN the state was last declared. Advances on every fire.
      The server uses it to judge staleness (only once not-live — see
      ``bus_liveness.derive_state``).
    * ``state_since`` — WHEN the session ENTERED this state. Preserved across
      re-declarations of the SAME state, reset only on a transition. This is what
      the attention面 sorts by ("how long has it been waiting"): a session that
      re-fires ``running`` on every tool call, or an ``awaiting_human`` whose
      Notification re-fires, must not keep resetting its "waiting since" clock.

    ``detail`` (ms-159 / e-6488) is the wait detail — WHAT the human is being
    asked (e.g. the Notification message "Claude needs your permission to use
    Bash"). It is attached as ``state_detail`` ONLY for ``awaiting_human`` (the
    one state whose activity is the wait content, 判断3), and ONLY when non-empty:
    an unknown wait reason stays absent rather than fabricated (ms-173 方針2). For
    every other state the detail is dropped (a ``running`` marker has no wait
    detail). The consumer (``lib/working_target.derive_activity``) shows this on
    the ``awaiting_human`` row and renders empty when it is absent.

    ``prev_marker`` is the previously-written marker (or ``None`` on first
    write). When the incoming state equals the previous ``declared_state``, the
    prior ``state_since`` is carried forward; otherwise this is a transition and
    ``state_since`` becomes ``now_iso``.
    """
    state = event_to_declared_state(event_name)
    if state is None:
        return None
    # PreToolUse(AskUserQuestion) = 選択肢の提示そのもの → 応答を要する待ちを
    # 確定的に宣言する (Notification のアイドル通知に頼らない、PR#758 QA)。
    if (str(event_name) == "PreToolUse"
            and str(tool_name or "") == ASK_TOOL_NAME):
        state = bus_liveness.STATE_AWAITING_HUMAN
    # Notification のアイドル通知は awaiting_human に写さない (過剰発火の真因):
    #   - 直前の宣言が awaiting_human (許可要求 / AskUserQuestion 提示) なら
    #     何も宣言しない = その待ちを clobber しない (60 秒放置で必ず後追い発火
    #     するのがこの通知なので、上書きすると本物の確認待ちが毎回消える)。
    #   - それ以外は idle として宣言 (Stop を取り逃した crash 経路でも、放置
    #     セッションが running のまま凍らない保険)。
    if str(event_name) == "Notification" and is_idle_notification(detail):
        prev_state = (prev_marker or {}).get("declared_state") \
            if isinstance(prev_marker, dict) else None
        if prev_state == bus_liveness.STATE_AWAITING_HUMAN:
            return None
        state = bus_liveness.STATE_IDLE
        detail = ""  # アイドル文言は待機内容ではない (でっち上げ防止)
    state_since = now_iso
    if isinstance(prev_marker, dict) and prev_marker.get("declared_state") == state:
        state_since = prev_marker.get("state_since") or now_iso
    marker = {
        "declared_state": state,
        "declared_at": now_iso,
        "state_since": state_since,
        "source_event": str(event_name),
    }
    clean_detail = detail.strip() if isinstance(detail, str) else ""
    if state == bus_liveness.STATE_AWAITING_HUMAN and clean_detail:
        marker["state_detail"] = clean_detail
    return marker
