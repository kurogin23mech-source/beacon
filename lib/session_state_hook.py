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


def build_state_marker(event_name, now_iso) -> Optional[dict]:
    """Build the ``.beacon/session-state.json`` marker payload for an event.

    Returns ``None`` when the event declares nothing (see
    ``event_to_declared_state``) so the caller writes nothing. Otherwise returns
    ``{declared_state, declared_at, source_event}`` — ``declared_at`` is what the
    server's ``derive_state`` uses to judge staleness, so it travels with the
    state, never separately.
    """
    state = event_to_declared_state(event_name)
    if state is None:
        return None
    return {
        "declared_state": state,
        "declared_at": now_iso,
        "source_event": str(event_name),
    }
