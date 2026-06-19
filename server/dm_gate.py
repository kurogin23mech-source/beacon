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
2. **Shared Trek membership** (= 共有作業領域メンバー):
   When sender and receiver are both members of the same active Trek
   (= 缶詰の作業部屋 / 自律的計画的タスク実行の作業空間), the receiver
   has already opted in to AI action exchange within that scope. Skip
   the gate.
3. **Action implication** (envelope.actions_authorized is non-empty):
   If the envelope authorises no actions (= pure information-sharing
   DM, e.g. a plain free-text chat message), there is nothing to
   approve. Skip the gate.

The gate triggers **only** when (1) AND (2) BOTH say "different humans
without a shared Trek bridge" AND (3) says "this envelope carries
action implication". In every other case the dispatcher acts as it did
before ms-70 — fully backward compatible.

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
``should_gate_dm_action`` reads no globals and writes no state. It
takes the three facts as arguments + a small lookup callable for
shared-Trek membership (so the caller injects the Firestore / DynamoDB
access path that suits its layer). Returns a tuple
``(should_gate: bool, reason: str)`` — the reason string is used by
the dispatcher for audit logging.
"""

from __future__ import annotations

from typing import Callable, Iterable


# Public reason codes — stable strings used by the audit log + tests.
GATE_REASON_SAME_USER = "same_user"
GATE_REASON_SHARED_TREK = "shared_trek_member"
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
    shared_trek_lookup: Callable[[str, str], bool],
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
        to the no-actions and shared-Trek checks below).
    receiver_user_id : str
        The user_id of the human that owns the receiving session.
        Empty string is treated symmetrically with sender_user_id.
    actions_authorized : Iterable[str] | None
        The envelope's ``actions_authorized`` field. ``None`` or empty
        list / tuple means "pure information sharing, no action
        implication" — gate skipped.
    shared_trek_lookup : Callable[[str, str], bool]
        Callback ``(sender_uid, receiver_uid) -> bool`` answering
        "are both users members of the same active Trek?". The caller
        injects the backend lookup (Firestore list_treks for the
        cloud path, in-memory dict for tests). Not called if a cheaper
        rule (same_user / no_actions) already decides.

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

    # Rule 3 (cheap before Rule 2): empty actions_authorized → pure info
    # sharing, no action implication. Skip the gate regardless of who
    # the parties are. Checking this before the shared_trek_lookup
    # avoids a Firestore round-trip for the common "just chat" path.
    actions_list = list(actions_authorized) if actions_authorized else []
    if not actions_list:
        return (False, GATE_REASON_NO_ACTIONS)

    # Rule 2: shared-Trek membership. Both users have already opted
    # into AI-to-AI action exchange within the scope of that Trek;
    # skip the gate.
    if sender_user_id and receiver_user_id and shared_trek_lookup(
        sender_user_id, receiver_user_id
    ):
        return (False, GATE_REASON_SHARED_TREK)

    # Cross-user + actions implied + no shared Trek → gate triggers.
    return (True, GATE_REASON_CROSS_USER_ACTION)


def build_shared_trek_lookup_from_lists(
    list_treks_for_user: Callable[[str], list[dict]],
) -> Callable[[str, str], bool]:
    """Construct a ``shared_trek_lookup`` callback from a per-user trek
    list function.

    The caller supplies ``list_treks_for_user(user_id) -> list[dict]``
    (= the same shape returned by ``firestore_client.list_treks(actor_id=
    user_id)``). The returned callback fetches the sender's visible
    treks and walks each one's members list checking for receiver_uid.

    Two design choices worth pinning:

      * **Active-only filtering is the caller's job.** This module does
        not know what "active" means for a project (Trek status is
        ``planning / active / paused / archived``; the SPEC says the
        gate should cover an "active Trek", but the production
        ``list_treks`` already hides archived by default and an
        archived Trek is the only status that clearly shouldn't grant
        AI-action consent. Callers that want a stricter status filter
        can preprocess the list).
      * **Sender-side query is sufficient.** Trek membership is
        symmetric on Firestore (creator + members), so listing from
        sender's POV and checking receiver presence is equivalent to
        the reverse, with one fewer round-trip.
    """
    def _lookup(sender_uid: str, receiver_uid: str) -> bool:
        if not sender_uid or not receiver_uid:
            return False
        treks = list_treks_for_user(sender_uid) or []
        for t in treks:
            if (t.get("creator_actor") or {}).get("user_id") == receiver_uid:
                return True
            for m in (t.get("members") or []):
                if m.get("user_id") == receiver_uid:
                    return True
        return False
    return _lookup
