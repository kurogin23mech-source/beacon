"""Qualitative gate for outbound autonomous replies — Python side (ms-93 e-3340).

This is the faithful Python mirror of ``channel/bus-qualgate.mjs``. The JS gate
sits in the Claude Code MCP reply tool (bus.mjs); this one sits in the CLI
``beacon bus send`` reply path (lib/commands.cmd_bus_send), which is how a
**Codex** armed session sends its replies (codex_receive_loop.py builds a
``beacon bus send`` argv). Without this, e-3308's qualitative safety (hold
外部宛 / 機密 / action付き autonomous replies for the human) was Claude-only —
Codex armed could autonomously DM another project unchecked. The budget gate
(量的) is already mirrored on both sides (lib/commands._bus_budget_consume_one
↔ channel/bus-budget.mjs); this brings the qualitative gate to parity.

The two implementations MUST agree; ``tests/test_dm_qualgate_parity.py`` drives
both with the same inputs and pins identical classification + hold decisions
(same two-impl-pin pattern as tests/test_bus_budget_asymmetry.py).

Keep this file and ``channel/bus-qualgate.mjs`` in lockstep. Background:
ms-100 SPEC ``PVaNf6HYFjucgBS3lkQF`` ("armed の本質") and the Hybrid decision
(通常会話 = allowed / 外部宛・機密・action = held).
"""
from __future__ import annotations

# Trek-scoped / operation channels: a shared pre-approved working scope. Sends
# on these are covered by the Trek's blanket consent, so they are never treated
# as "external" even cross-project. Mirrors bus-qualgate.mjs TREK_SCOPED_CHANNELS.
TREK_SCOPED_CHANNELS = frozenset({
    "operation-trigger",
    "trek-progress-check",
    "trek-trigger",
    "trek-task-review",
    "trek-leader-digest",
})

CAT_NORMAL = "normal_chat"
CAT_EXTERNAL = "external"
CAT_ACTION = "action"
CAT_CONFIDENTIAL = "confidential"


def is_trek_scoped_channel(channel: str) -> bool:
    c = str(channel or "")
    return c.startswith("trek-") or c in TREK_SCOPED_CHANNELS


def _same_user(sender_user_id: str, recipient_user_id: str) -> bool:
    """True iff both identities are present and equal (case-insensitive).

    The identity is the user's email (= how user identity surfaces everywhere:
    the server directory keys on ``actor.email``, and same-user cross-project
    is what the server dm_gate already permits). Blank on either side → not a
    known same-user match, so the caller keeps the conservative 外部 default.
    """
    su = str(sender_user_id or "").strip().lower()
    ru = str(recipient_user_id or "").strip().lower()
    return bool(su) and su == ru


def classify_outbound_reply(
    channel: str = "",
    sender_project_id: str = "",
    recipient_project_id: str = "",
    actions_authorized=None,
    confidential: bool = False,
    sender_user_id: str = "",
    recipient_user_id: str = "",
) -> str:
    """Classify an outbound reply. Mirrors bus-qualgate.mjs classifyOutboundReply.

    - confidential wins (only when an explicit signal is provided; no content
      sniffing — that is ms-63 disclosure's job).
    - authorised actions -> action.
    - Trek-scoped channel -> normal_chat (pre-approved scope, even cross-project).
    - plain DM to a different project by a DIFFERENT user -> external.
    - plain DM to a different project by the SAME user -> normal_chat (e-3566):
      the server dm_gate already permits same-user cross-project, so the client
      must not over-block it as 外部宛. Identity is the sender/recipient email.
    - otherwise -> normal_chat.
    """
    if confidential:
        return CAT_CONFIDENTIAL
    if actions_authorized and len(list(actions_authorized)) > 0:
        return CAT_ACTION
    if is_trek_scoped_channel(channel):
        return CAT_NORMAL
    if (
        recipient_project_id and sender_project_id
        and recipient_project_id != sender_project_id
    ):
        # e-3566: same user across their own projects is not "external".
        if _same_user(sender_user_id, recipient_user_id):
            return CAT_NORMAL
        return CAT_EXTERNAL
    return CAT_NORMAL


def evaluate_outbound_qual_gate(classification: str, armed: bool) -> dict:
    """Return {hold, category, reason}. Mirrors bus-qualgate.mjs.

    - not armed -> never hold (a human-driven send is its own approval).
    - normal_chat -> allowed (B芯).
    - external/action/confidential -> HOLD for the human (A gate).
    """
    if not armed:
        return {"hold": False, "category": classification, "reason": "not_armed"}
    if classification == CAT_NORMAL:
        return {"hold": False, "category": classification, "reason": CAT_NORMAL}
    return {
        "hold": True,
        "category": classification,
        "reason": f"{classification}_needs_human",
    }


def qual_hold_message(category: str) -> str:
    """Human-facing refusal text; mirrors bus-qualgate.mjs qualHoldMessage."""
    if category == CAT_EXTERNAL:
        return (
            "reply held: this is an autonomous cross-project message to another "
            "project's bus. armed mode holds 外部宛 (external) sends for you to "
            "review. Send it yourself (e.g. via /beacon-dm-send) if intended, or "
            "share a Trek so the scope is pre-approved."
        )
    if category == CAT_CONFIDENTIAL:
        return (
            "reply held: flagged confidential. armed mode holds 機密 "
            "(confidential) sends for you to review before it leaves this session."
        )
    if category == CAT_ACTION:
        return (
            "reply held: this reply carries an authorised action. armed mode "
            "holds action付き sends for your approval (a text ping would not be "
            "held)."
        )
    return "reply held for human review."
