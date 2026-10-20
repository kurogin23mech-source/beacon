"""Cross-user DM action authorization gate (ms-70 / e-1713).

This module hosts the **pure judge function** that decides whether a bus
event carrying an action-bearing envelope must be held for receiver-side
human approval, or may pass through immediately.

Background
----------
ms-70 introduces a cross-user DM (= direct message) action authorization
gate. Before ms-70, every bus event that arrived at the receiver was
dispatched (= delivered + acted on, subject to tier rules) without asking
the receiver's human. Same-machine same-user single-developer dogfood
made that safe in practice. The moment a second human enters the picture
— e.g. user A's AI session DMs an action envelope to user B's session —
the receiver's human has had no chance to say "yes, do that on my behalf".
e-1713 closes that gap with a structural gate.

Design (= SPEC 設計方針 5)
--------------------------
Three orthogonal facts decide the gate outcome:

1. **Same user** (sender_user_id == receiver_user_id):
   Always skip the gate. A user's own sessions talking to each other
   need no inter-human approval; this preserves the dogfood path
   without changes.
2. **Action implication** (envelope.actions_authorized is non-empty):
   If the envelope authorises no actions (= pure information-sharing
   DM, e.g. a plain free-text chat message), there is nothing to
   approve. Skip the gate.

The gate triggers **only** when (1) says "different humans" AND (2)
says "this envelope carries action implication". In every other case
the dispatcher acts as it did before ms-70.

ms-185 e-7068: 共有 Trek メンバーなら確認を省く規則 (旧 設計方針 2) を
**判定ごと削除した**。Trek を畳む (ms-185) と規則の根拠が消えるため、残すと
「誰もセットしない判定」になる (ms-166 で 236 項目かけて掃除した形)。判定が
常に偽を返す形でも残していない。向きは fail-closed — 消すと「必ず人間の確認を
要求する」側に倒れるので、安全側である。

Tier coverage
-------------
The gate is keyed on ``actions_authorized`` non-emptiness, not on the
specific tier. This deliberately covers **both** T2 auto-execute and
T3 propose-to-AI envelopes whenever they declare authorised actions.
A T2 envelope's auto-execute pass would obviously bypass the receiver
without consent; a T3 envelope's "propose to AI" can still result in
the AI calling a tool, so cross-user T3 with actions also needs the
human gate. Pure T5 short-pings have empty actions and skip the gate.

Pure-function contract
----------------------
``should_gate_dm_action`` reads no globals and writes no state and
performs no IO: it takes the facts as arguments and returns a tuple
``(should_gate: bool, reason: str)`` — the reason string is used by
the dispatcher for audit logging.
"""

from __future__ import annotations

from typing import Iterable


# Public reason codes — stable strings used by the audit log + tests.
GATE_REASON_SAME_USER = "same_user"
GATE_REASON_NO_ACTIONS = "no_actions_authorized"
GATE_REASON_CROSS_USER_ACTION = "cross_user_action_pending_approval"
# ms-83 / e-1995: T1-system envelopes minted by the Beacon server itself
# bypass the cross-user human gate. The server-signed envelope IS the
# structural re-issuance of the user's "Trek で進めて" pre-approval.
GATE_REASON_T1_SYSTEM = "t1_system_envelope"


def should_gate_dm_action(
    sender_user_id: str,
    receiver_user_id: str,
    actions_authorized: Iterable[str] | None,
    envelope_tier: str = "",
    envelope_issuer: str = "",
) -> tuple[bool, str]:
    """Return (should_gate, reason) for one bus envelope.

    Parameters
    ----------
    sender_user_id : str
        The user_id of the human that owns the sending session. Empty
        string is treated as "unknown sender"; the gate plays it safe
        by behaving like a cross-user gate-candidate (= still subject
        to the no-actions check below).
    receiver_user_id : str
        The user_id of the human that owns the receiving session.
        Empty string is treated symmetrically with sender_user_id.
    actions_authorized : Iterable[str] | None
        The envelope's ``actions_authorized`` field. ``None`` or empty
        list / tuple means "pure information sharing, no action
        implication" — gate skipped.

    Returns
    -------
    (should_gate, reason) : tuple[bool, str]
        ``should_gate=True`` means the dispatcher must NOT act on the
        envelope; it must write a ``put_bus_event_approval`` sidecar
        with ``approval_status="pending"`` and wait for the receiver
        human's decision. ``False`` means the dispatcher proceeds
        as it did pre-ms-70.

        ``reason`` is one of the GATE_REASON_* constants above; the
        dispatcher records it in the audit log so post-hoc analysis
        can answer "why did this envelope skip the gate?".
    """
    # Rule 0 (ms-83 / e-1995): T1-system envelope signed by beacon-system
    # bypasses the gate. The server-mint authority is the structural
    # re-execution of the user's "Trek で進めて" pre-approval — the
    # receiver has already consented at trek-activation time. The verify
    # pipeline (envelope.verify) is what proves the signature + scope /
    # tier rules; this gate just defers to that result.
    if envelope_tier == "T1-system" and envelope_issuer == "beacon-system":
        return (False, GATE_REASON_T1_SYSTEM)

    # Rule 1: same user → always skip. A single human's sessions talking
    # to themselves never need cross-human approval, even when the DM
    # carries action implication.
    if sender_user_id and sender_user_id == receiver_user_id:
        return (False, GATE_REASON_SAME_USER)

    # Rule 2: empty actions_authorized → pure info sharing, no action
    # implication. Skip the gate regardless of who the parties are.
    actions_list = list(actions_authorized) if actions_authorized else []
    if not actions_list:
        return (False, GATE_REASON_NO_ACTIONS)

    # Cross-user + actions implied → gate triggers.
    #
    # ms-185 e-7068: ここに「送信者と受信者が同じ Trek の参加者なら確認を省く」
    # 規則が在った。Trek を畳むので判定ごと削除してある。**代わりに常に偽を返す
    # 判定を残していない** — 誰もセットしない判定が残ると、配線は在るのに動かない
    # 機能を 1 つ作ることになる。削除の向きは fail-closed (= 必ず人間の確認を
    # 要求する側) なので、残骸より安全。
    return (True, GATE_REASON_CROSS_USER_ACTION)
