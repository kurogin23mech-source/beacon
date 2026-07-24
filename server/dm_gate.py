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


# ms-97 / e-2659 Phase 3 (G3) — shared_trek_lookup return contract.
#
# Pre-Phase-3 the lookup returned a plain ``bool``. AC19 needs the
# matched ``trek_id`` to land in the audit log so post-mortems can answer
# "which trek granted bypass?". The lookup now returns a 2-tuple
# ``(matched: bool, trek_id: str)`` — ``trek_id`` is the empty string
# when ``matched`` is False, or when the matched trek does not surface
# a ``trek_id`` field (= defensive, should not happen in practice).
#
# Callable signature alias for editor / mypy hint clarity.
SharedTrekLookup = Callable[[str, str], "tuple[bool, str]"]


def should_gate_dm_action(
    sender_user_id: str,
    receiver_user_id: str,
    actions_authorized: Iterable[str] | None,
    shared_trek_lookup: Callable[..., object],
    envelope_tier: str = "",
    envelope_issuer: str = "",
    sender_session_id: str = "",
    receiver_session_id: str = "",
) -> tuple[bool, str, str]:
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

    sender_session_id : str
        ms-97 / e-2659 Phase 3 (AC18) — session-grain bypass evidence.
        When the shared_trek_lookup is a session-grain (= phase A+) lookup,
        both session_ids must appear in the same trek's ``members[]`` for
        bypass. The lookup callable receives this in addition to the
        user_id pair so pre-A lookups can ignore it (= user_id grain) and
        phase A+ lookups can enforce the strict session-grain check.
    receiver_session_id : str
        Symmetric to ``sender_session_id``.

    Returns
    -------
    (should_gate, reason, trek_id) : tuple[bool, str, str]
        ``should_gate=True`` means the dispatcher must NOT act on the
        envelope; it must write a ``put_bus_event_approval`` sidecar
        with ``approval_status="pending"`` and wait for the receiver
        human's decision. ``False`` means the dispatcher proceeds
        as it did pre-ms-70.

        ``reason`` is one of the GATE_REASON_* constants above; the
        dispatcher records it in the audit log so post-hoc analysis
        can answer "why did this envelope skip the gate?".

        ``trek_id`` is the matched trek's id when ``reason`` is
        ``shared_trek_member`` (= AC19 audit log carries the bypass
        evidence). For every other reason it is the empty string.
    """
    # Rule 0 (ms-83 / e-1995): T1-system envelope signed by beacon-system
    # bypasses the gate. The server-mint authority is the structural
    # re-execution of the user's "Trek で進めて" pre-approval — the
    # receiver has already consented at trek-activation time. The verify
    # pipeline (envelope.verify) is what proves the signature + scope /
    # tier rules; this gate just defers to that result.
    if envelope_tier == "T1-system" and envelope_issuer == "beacon-system":
        return (False, GATE_REASON_T1_SYSTEM, "")

    # Rule 1: same user → always skip. A single human's sessions talking
    # to themselves never need cross-human approval, even when the DM
    # carries action implication.
    if sender_user_id and sender_user_id == receiver_user_id:
        return (False, GATE_REASON_SAME_USER, "")

    # Rule 3 (cheap before Rule 2): empty actions_authorized → pure info
    # sharing, no action implication. Skip the gate regardless of who
    # the parties are. Checking this before the shared_trek_lookup
    # avoids a Firestore round-trip for the common "just chat" path.
    actions_list = list(actions_authorized) if actions_authorized else []
    if not actions_list:
        return (False, GATE_REASON_NO_ACTIONS, "")

    # Rule 2: shared-Trek membership. Both users have already opted
    # into AI-to-AI action exchange within the scope of that Trek;
    # skip the gate.
    if sender_user_id and receiver_user_id:
        # ms-97 / e-2659 G3 — lookup now returns (matched, trek_id).
        # Phase A+ lookups also consult session_id grain. Back-compat
        # path: if the lookup returns a plain bool (= legacy test
        # injection), wrap it to the tuple shape.
        lookup_out = shared_trek_lookup(
            sender_user_id,
            receiver_user_id,
            sender_session_id,
            receiver_session_id,
        ) if _accepts_session_grain(shared_trek_lookup) else shared_trek_lookup(
            sender_user_id, receiver_user_id,
        )
        matched, matched_trek_id = _coerce_lookup_result(lookup_out)
        if matched:
            return (False, GATE_REASON_SHARED_TREK, matched_trek_id)

    # Cross-user + actions implied + no shared Trek → gate triggers.
    return (True, GATE_REASON_CROSS_USER_ACTION, "")


def _accepts_session_grain(lookup: Callable[..., object]) -> bool:
    """True if ``lookup`` accepts the 4-arg session-grain signature.

    ms-97 / e-2659 Phase 3 — backwards compat shim. Existing tests
    inject 2-arg ``(sender_uid, receiver_uid) -> bool`` callables; the
    new production path injects 4-arg
    ``(sender_uid, receiver_uid, sender_sid, receiver_sid) -> (bool, str)``.
    Detection is by parameter count so test code keeps working.
    """
    try:
        import inspect
        params = inspect.signature(lookup).parameters
        # functools.partial / lambda with *args → assume new shape so the
        # caller can pass session ids without breaking. Plain 2-arg lambdas
        # have exactly 2 POSITIONAL params.
        positional = [
            p for p in params.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()):
            return True
        return len(positional) >= 4
    except (TypeError, ValueError):
        # Builtins / C-level callables with no inspectable signature
        # default to legacy 2-arg shape (safer than crashing).
        return False


def _coerce_lookup_result(result: object) -> tuple[bool, str]:
    """Normalize lookup return into ``(matched, trek_id)``.

    The legacy lookup returns a plain ``bool``; the new lookup returns
    ``(bool, str)``. This shim accepts either so the gate stays
    backward compatible during the rollout.
    """
    if isinstance(result, tuple) and len(result) == 2:
        matched, trek_id = result
        return (bool(matched), str(trek_id or ""))
    return (bool(result), "")


def build_shared_trek_lookup_from_lists(
    list_treks_for_user: Callable[[str], list[dict]],
) -> Callable[..., tuple[bool, str]]:
    """Construct a ``shared_trek_lookup`` callback from a per-user trek
    list function.

    The caller supplies ``list_treks_for_user(user_id) -> list[dict]``
    (= the same shape returned by ``firestore_client.list_treks(actor_id=
    user_id)``). The returned callback fetches the sender's visible
    treks and walks each one's members list checking for receiver_uid.

    ms-97 / e-2659 Phase 3 (G3 + G4 + AC18) — the lookup is now
    session-grain aware:

      * Return shape is ``(matched: bool, trek_id: str)`` so the caller
        can record which trek granted the bypass into the audit log
        (= AC19).
      * For ``phase A+`` treks (= ``is_session_id_keyed`` true; members[]
        carries session_id field), BOTH ``sender_session_id`` and
        ``receiver_session_id`` must appear in the trek's members[]
        for bypass to apply (= strict session-grain check).
      * For ``pre-A`` treks (= legacy user_id keyed), the user_id grain
        check is preserved verbatim.
      * **G4 live check**: each invocation re-fetches the trek list via
        ``list_treks_for_user`` (= no caching), so a member removed
        between invocations stops granting bypass on the next call.

    Two design choices worth pinning:

      * **Active-only filtering is the caller's job.** This module does
        not know what "active" means for a project. ``list_treks``
        already hides archived by default; this lookup additionally
        skips halted + archived treks regardless of caller filter.
      * **Sender-side query is sufficient.** Trek membership is
        symmetric on Firestore (creator + members), so listing from
        sender's POV and checking receiver presence is equivalent to
        the reverse, with one fewer round-trip.
    """
    # Local import keeps this module import-cheap and avoids a circular
    # dependency at module-load time (server.dm_gate is imported by
    # server.app, lib.trek is imported by both).
    def _is_phase_a_plus(trek: dict) -> bool:
        try:
            from lib import trek as trek_mod
            return trek_mod.is_session_id_keyed(trek)
        except Exception:
            # If the trek module is unavailable, default to user_id
            # grain (= safer back-compat: never accidentally upgrade
            # a pre-A trek to strict session checking).
            return False

    def _lookup(
        sender_uid: str,
        receiver_uid: str,
        sender_sid: str = "",
        receiver_sid: str = "",
    ) -> tuple[bool, str]:
        if not sender_uid or not receiver_uid:
            return (False, "")
        # ms-97 / e-2659 G4 — live check. The caller-supplied
        # ``list_treks_for_user`` is invoked per call (= no caching),
        # so a member kicked between two DMs stops granting bypass.
        treks = list_treks_for_user(sender_uid) or []
        for t in treks:
            # ms-97 / e-2612 (AC32) — halt 中の trek は DM bypass の根拠に
            # ならない。 leader が Andon cord を引いた状態で「同じ Trek の
            # 仲間だから」 と autonomous cross-user action DM を通すのは
            # halt の意図と矛盾する。 該当 trek は跨ぐが他に halt 無しの
            # 共有 trek があればそちらで bypass 成立、 共有 trek が halt
            # しかなければ通常の cross-user budget gate に fallback する。
            if t.get("halt"):
                continue
            # ms-75 e-4116 follow-up (PR #491 parent review 1): only an
            # ACTIVE trek grants bypass. Previously only ``archived`` was
            # excluded, which let a ``planning`` trek (still being set up,
            # not yet a live pre-approved work scope) grant the bypass. A
            # non-active trek is not a consented AI-action scope, so it must
            # not short-circuit the cross-user gate / budget. This also
            # subsumes the old archived exclusion.
            if (t.get("status") or "") != "active":
                continue

            trek_id = t.get("trek_id") or ""
            members = t.get("members") or []
            creator_uid = (t.get("creator_actor") or {}).get("user_id") or ""

            if _is_phase_a_plus(t):
                # ms-97 / e-2659 Phase 3 AC18 — strict session-grain.
                # Both sender_sid AND receiver_sid must be present in
                # members[]. The user_id is informational (= same human
                # can have multiple sessions in the same trek; only the
                # listed ones grant bypass).
                #
                # e-4116 follow-up (parent review 1): a member only counts
                # once ``joined_at`` is set. An invited-but-not-joined entry
                # (session_id present, joined_at empty) has NOT accepted the
                # pre-approval scope, so it must not grant bypass.
                if not sender_sid or not receiver_sid:
                    # Missing session ids — cannot verify session-grain
                    # membership; fall through to next trek.
                    continue
                sender_in = False
                receiver_in = False
                for m in members:
                    if not m.get("joined_at"):
                        continue
                    msid = m.get("session_id") or ""
                    if msid and msid == sender_sid:
                        sender_in = True
                    if msid and msid == receiver_sid:
                        receiver_in = True
                    if sender_in and receiver_in:
                        break
                if sender_in and receiver_in:
                    return (True, trek_id)
                # Not in this trek — try next one.
                continue

            # pre-A trek: user_id grain (legacy behaviour preserved). The
            # creator is a joined member by construction (they open the
            # trek); non-creator members must carry ``joined_at`` to count
            # (e-4116 follow-up — parity with the phase-A joined_at rule and
            # the CLI ``_trek_member_matches`` check).
            if creator_uid and creator_uid == receiver_uid:
                return (True, trek_id)
            for m in members:
                if m.get("user_id") == receiver_uid and m.get("joined_at"):
                    return (True, trek_id)
        return (False, "")
    return _lookup


def shared_trek_member(
    *,
    sender_session_id: str,
    recipient_session_id: str,
    list_treks_for_user: Callable[[str], list[dict]] | None = None,
    sender_user_id: str = "",
    recipient_user_id: str = "",
) -> bool:
    """Strict session-grain membership probe — convenience helper.

    ms-97 / e-2659 Phase 3 AC18 — wraps ``build_shared_trek_lookup_from_lists``
    so test code (and the upstream Skill / CLI integrations) can ask
    "are these two session_ids registered in the same active phase A+
    trek?" without recreating the lookup shape.

    Returns True iff BOTH session ids appear in the members[] of at
    least one non-halted, non-archived, phase A+ trek that the sender
    user can see. Returns False for pre-A treks (= they have no
    session_id field to match on), for halted / archived treks, and
    for any input where either session_id is empty.

    When ``list_treks_for_user`` is omitted the helper returns False
    (= no oracle to consult). Real callers supply the db-backed list
    function; tests supply an in-memory dict.
    """
    if not sender_session_id or not recipient_session_id:
        return False
    if list_treks_for_user is None:
        return False
    lookup = build_shared_trek_lookup_from_lists(list_treks_for_user)
    matched, _trek_id = lookup(
        sender_user_id, recipient_user_id,
        sender_session_id, recipient_session_id,
    )
    return matched
