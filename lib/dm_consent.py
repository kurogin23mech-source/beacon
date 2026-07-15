"""Sender-side cross-user DM consent gate — data model (ms-110 / e-3442).

This is the **sender-side mirror** of ``server/dm_gate.py`` (ms-70,
receiver side). ms-70 protects the human who *receives* a cross-user
action DM (their AI must not act without their approval). ms-110 closes
the symmetric hole on the *sending* side: an AI must not fire a DM at a
**different human** without a human having confirmed the recipient.

The asymmetry this module closes
--------------------------------
2026-07-15 a session DMed the wrong human. Two failures stacked: (F1)
the send bypassed ``/beacon-dm-send`` and hit the primitive directly
(``beacon bus send`` / MCP ``reply``), skipping the Skill's checks; (F2)
nobody confirmed the recipient before the message left. ms-70 guards the
receiver; nothing guarded the sender. This module is the missing half.

What lives here (= e-3442, the shared foundation both ① and ② build on):

  * ``classify_send_consent`` — the pure discrimination function. Given
    resolved facts about a send (sender/recipient user_id, channel,
    reply-vs-new, Trek / Operation carve-outs) it answers "does a human
    need to confirm this recipient before it goes out?". It mirrors the
    same-user / shared-Trek axes of ``dm_gate.should_gate_dm_action`` so
    the two sides stay in lock-step.

  * the ``recipient_confirmed`` **consent claim** — build / parse /
    match helpers for the claim that records *who* confirmed *which*
    recipient *when*. This claim is deliberately **independent of the
    envelope's ``actions_authorized``** capability grant (ms-60 / e-1155):
    that grant says "the receiver may run these side-effects"; this claim
    says "a human confirmed the send target". Different axis, different
    key, different kind.

  * ``evaluate_send`` — the combined decision (allow / deny + reason)
    that the server backstop (② e-3443) and ``/beacon-dm-send`` (④ e-3445)
    both lean on, so the accept/reject rule is defined exactly once.

What does NOT live here:
  * HTTP enforcement / ``--no-envelope``封じ (= ② server backstop, e-3443)
  * the PreToolUse hook that denies primitive直叩き (= ① hook, e-3444)
  * token / claim issuance UX in the Skill (= ④, e-3445)

Design方針 (SPEC ``FZcvJ5ivhLu0UkEtw7Ew`` §1–§4):
  §1 cross-user だけ厳格化 — same-user / Trek / Operation は素通り。
  §2 reply は宛先が親 envelope から導出されるので new-send だけに課す。
  §4 consent claim は auto-execute 権限 claim と混ぜない (意味の混線回避)。
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Trek-scoped / operation channel reuse (single source of truth = dm_qualgate)
# ---------------------------------------------------------------------------
#
# ``dm_qualgate.is_trek_scoped_channel`` already classifies the pre-approved
# channels (operation-trigger / trek-*). Reuse it so the sender-consent
# carve-out never drifts from the qualitative gate. Dual import supports both
# contexts: bare when lib is on sys.path (CLI / tests), package-qualified when
# the server imports lib as a package.
try:  # pragma: no cover - import shim, both branches exercised across contexts
    import dm_qualgate as _qualgate
except ImportError:  # pragma: no cover
    from lib import dm_qualgate as _qualgate


def _is_trek_scoped_channel(channel: str) -> bool:
    return _qualgate.is_trek_scoped_channel(channel)


# ---------------------------------------------------------------------------
# Verdict reason codes (stable strings — audit log + tests depend on these,
# mirroring the GATE_REASON_* convention in server/dm_gate.py).
# ---------------------------------------------------------------------------

# consent NOT required (send may proceed without a fresh recipient confirm):
CONSENT_SKIP_SAME_USER = "same_user"
CONSENT_SKIP_UNRESOLVED = "unresolved_identity"
CONSENT_SKIP_TREK_SCOPE = "trek_scoped_channel"
CONSENT_SKIP_NON_DM = "non_dm_channel"
CONSENT_SKIP_OPERATION = "operation_envelope"
CONSENT_SKIP_REPLY = "reply_derived_recipient"
CONSENT_SKIP_SHARED_TREK = "shared_trek_member"

# consent REQUIRED (a human must confirm the recipient first):
CONSENT_REQUIRED_CROSS_USER = "cross_user_new_send"


# ---------------------------------------------------------------------------
# Discrimination: does this send need a human recipient-confirmation?
# ---------------------------------------------------------------------------

def classify_send_consent(
    *,
    sender_user_id: str,
    recipient_user_id: str,
    channel: str = "dm",
    is_reply: bool = False,
    operation_envelope: bool = False,
    shared_trek: bool = False,
) -> tuple[bool, str]:
    """Return ``(consent_required, reason)`` for one outbound send.

    Pure function — reads no globals, writes no state. The caller resolves
    the facts (user_ids via ``_resolve_bus_event_user_ids`` server-side or
    ``_resolve_creator_identity`` + recipient lookup client-side; Trek
    membership via ``dm_gate.build_shared_trek_lookup_from_lists``) and
    passes them in, exactly like ``dm_gate.should_gate_dm_action``.

    Parameters
    ----------
    sender_user_id : str
        user_id (= JWT ``sub``) of the human owning the sending session.
        Empty = "unknown sender"; the function plays it safe and cannot
        prove same-user, so it falls through toward requiring consent.
    recipient_user_id : str
        user_id of the human owning the recipient session. Empty =
        "unknown recipient". e-3492: an unresolved recipient does NOT
        require consent — we cannot prove it is a different human, and
        requiring it there false-positived every same-user cross-project
        send (whose sender resolves to "" against the post-target
        registry). Consent is required only for a *proven* cross-user pair.
    channel : str
        Bus channel. Only the ``dm`` channel carries person-directed
        messages that can misfire to the wrong human; broadcast /
        coordination channels (claim-signal, ops, test-*) have no single
        recipient and skip consent. Trek-scoped / operation channels
        (operation-trigger, trek-*) are a pre-approved scope and also skip
        (§1).
    is_reply : bool
        True when this send replies to a received DM (has ``in_reply_to``).
        The recipient is derived from the parent envelope, so it cannot
        misfire to a different human — skip consent (§2).
    operation_envelope : bool
        True when the send rides an Operation (T2 scope) envelope — an
        already pre-approved autonomous path — skip consent (§1).
    shared_trek : bool
        True when sender and recipient are both members of the same active
        Trek (resolved via the same lookup ms-70 uses). Skip consent (§1).

    Returns
    -------
    (consent_required, reason) : tuple[bool, str]
        ``consent_required=True`` means a ``recipient_confirmed`` claim
        from a human is required before the send is allowed. ``reason`` is
        one of the ``CONSENT_*`` constants above.

    Precedence
    ----------
    Carve-outs are checked *before* the cross-user default so that armed
    auto-reply, Trek协奏, and Operation autonomy keep working (SPEC AC5 —
    no regression) even when recipient user_id resolution fails. Same-user
    is the cheapest definitive skip and is checked first, matching
    ``dm_gate`` ordering.
    """
    # Rule 1: same user → never needs cross-human confirmation. A single
    # human's own sessions talking to each other is the dogfood path.
    if sender_user_id and sender_user_id == recipient_user_id:
        return (False, CONSENT_SKIP_SAME_USER)

    # Rule 2: Trek-scoped / operation channel → pre-approved working scope.
    if _is_trek_scoped_channel(channel):
        return (False, CONSENT_SKIP_TREK_SCOPE)

    # Rule 2b: only the ``dm`` channel is person-directed. Broadcast /
    # coordination channels (claim-signal / ops / test-*) have no single
    # recipient to misfire at, so they are outside this gate's scope. The
    # accident this MS closes is a wrong-human *DM*; keeping the gate to dm
    # avoids rejecting cross-user broadcasts (= ms-55 claim coordination).
    if str(channel or "") != "dm":
        return (False, CONSENT_SKIP_NON_DM)

    # Rule 3: Operation (T2 scope) envelope → pre-approved autonomous path.
    if operation_envelope:
        return (False, CONSENT_SKIP_OPERATION)

    # Rule 4: reply → recipient derived from the parent envelope, so it
    # cannot be aimed at a different human. Lower-friction lane (§2).
    if is_reply:
        return (False, CONSENT_SKIP_REPLY)

    # Rule 5: shared Trek membership → both humans already opted into
    # AI-to-AI exchange in that scope. Mirrors dm_gate Rule 2.
    if shared_trek:
        return (False, CONSENT_SKIP_SHARED_TREK)

    # Rule 6 (e-3492 P1 fix): only a PROVEN cross-user pair may require
    # consent — both identities resolved AND different. If either side is
    # unresolved we cannot prove the recipient is a different human, and
    # blocking here broke the same-user cross-project handoff (a session
    # sending to its own user's other project, whose session is not in the
    # post-target registry, so its user_id resolves to ""). Err toward
    # allowing; the real accident (a resolvable *different* user, e.g. posting
    # to their project where their session IS registered) still fires below.
    # This reverses the earlier fail-safe: "cannot prove same-user" no longer
    # means "block", because that false-positived every cross-project self-send.
    if not (sender_user_id and recipient_user_id):
        return (False, CONSENT_SKIP_UNRESOLVED)

    # Default: a proven cross-user new-send (both ids resolved and different)
    # with no carve-out → a human must confirm the target.
    return (True, CONSENT_REQUIRED_CROSS_USER)


# ---------------------------------------------------------------------------
# The recipient_confirmed consent claim (independent of actions_authorized)
# ---------------------------------------------------------------------------

#: Discriminator for the consent claim. Distinct from the envelope's
#: ``tier`` / ``actions_authorized`` fields so the "human confirmed the
#: recipient" fact never gets conflated with the "receiver may run these
#: side-effects" capability grant (SPEC §4).
RECIPIENT_CONFIRMED_KIND = "recipient_confirmed"

#: Envelope key the claim rides under. e-3443 (server backstop) reads this
#: key; ② must NOT reuse ``actions_authorized``.
CONSENT_CLAIM_KEY = "recipient_confirmed"


def mint_confirmation_id() -> str:
    """Stable, sortable, collision-resistant confirmation id.

    Format ``rc-<unix ts>-<6 hex>``. Mirrors ``claims.mint_claim_id`` so
    the id sorts by issue time. The hook (①, e-3444) uses this as the
    one-time token that proves the Skill (④) minted the claim after a
    human confirmation.
    """
    return f"rc-{int(time.time())}-{secrets.token_hex(3)}"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_recipient_confirmed_claim(
    *,
    confirmed_by_user_id: str,
    confirmed_by_email: str = "",
    recipient_session_id: str = "",
    recipient_user_id: str = "",
    recipient_project_id: str = "",
    channel: str = "dm",
    confirmation_id: Optional[str] = None,
    confirmed_at: Optional[str] = None,
) -> dict:
    """Build a validated ``recipient_confirmed`` consent claim.

    Records *who* confirmed (``confirmed_by``), *when* (``confirmed_at``),
    and *which recipient* (``recipient``) — the three facts SPEC AC "誰が・
    いつ・どの宛先を確認したか" requires. Contains **no** capability grant;
    it is orthogonal to ``actions_authorized``.

    Raises
    ------
    ValueError
        If ``confirmed_by_user_id`` is blank (an unattributable
        confirmation is not a confirmation) or if no recipient identifier
        (session_id or user_id) is supplied (the claim must pin a target
        so ``claim_matches_recipient`` can verify it against the actual
        send).
    """
    if not confirmed_by_user_id:
        raise ValueError("confirmed_by_user_id is required")
    if not (recipient_session_id or recipient_user_id):
        raise ValueError(
            "at least one of recipient_session_id / recipient_user_id "
            "is required (the claim must pin a target)"
        )
    return {
        "kind": RECIPIENT_CONFIRMED_KIND,
        "confirmation_id": confirmation_id or mint_confirmation_id(),
        "confirmed_by": {
            "user_id": confirmed_by_user_id,
            "email": confirmed_by_email or "",
        },
        "confirmed_at": confirmed_at or _utcnow_iso(),
        "recipient": {
            "session_id": recipient_session_id or "",
            "user_id": recipient_user_id or "",
            "project_id": recipient_project_id or "",
        },
        "channel": channel or "dm",
    }


def parse_recipient_confirmed_claim(obj: object) -> Optional[dict]:
    """Normalize a raw object into a consent-claim record, or None.

    Forgiving parse (same rule as ``claims.parse_claim_event``): wrong
    kind, non-dict, or missing required fields → None rather than raising,
    so a malformed / absent claim simply reads as "no valid confirmation".
    """
    if not isinstance(obj, dict):
        return None
    if obj.get("kind") != RECIPIENT_CONFIRMED_KIND:
        return None
    confirmed_by = obj.get("confirmed_by") or {}
    if not isinstance(confirmed_by, dict):
        return None
    by_uid = str(confirmed_by.get("user_id") or "")
    if not by_uid:
        return None
    recipient = obj.get("recipient") or {}
    if not isinstance(recipient, dict):
        return None
    r_sid = str(recipient.get("session_id") or "")
    r_uid = str(recipient.get("user_id") or "")
    if not (r_sid or r_uid):
        return None
    cid = str(obj.get("confirmation_id") or "")
    if not cid:
        return None
    return {
        "kind": RECIPIENT_CONFIRMED_KIND,
        "confirmation_id": cid,
        "confirmed_by": {
            "user_id": by_uid,
            "email": str(confirmed_by.get("email") or ""),
        },
        "confirmed_at": str(obj.get("confirmed_at") or ""),
        "recipient": {
            "session_id": r_sid,
            "user_id": r_uid,
            "project_id": str(recipient.get("project_id") or ""),
        },
        "channel": str(obj.get("channel") or "dm"),
    }


def claim_matches_recipient(
    claim: object,
    *,
    recipient_session_id: str = "",
    recipient_user_id: str = "",
) -> bool:
    """True iff ``claim`` confirms the *actual* send target.

    A confirmation for recipient X must not authorise a send to recipient
    Y (else the hook token could be replayed against a different target).
    At least one of the supplied identifiers must match the pinned one in
    the claim. Comparison is on whichever identifier the claim carries;
    a claim pinning only ``session_id`` is matched on ``session_id`` etc.
    """
    parsed = parse_recipient_confirmed_claim(claim)
    if parsed is None:
        return False
    pinned = parsed["recipient"]
    if pinned["session_id"] and recipient_session_id:
        if pinned["session_id"] == recipient_session_id:
            return True
    if pinned["user_id"] and recipient_user_id:
        if pinned["user_id"] == recipient_user_id:
            return True
    return False


# ---------------------------------------------------------------------------
# Combined decision — the single accept/reject rule (② + ④ both call this)
# ---------------------------------------------------------------------------

# evaluate_send reason codes (in addition to the CONSENT_* verdicts above):
SEND_ALLOW_CONFIRMED = "cross_user_confirmed"
SEND_DENY_MISSING_CONFIRMATION = "cross_user_missing_confirmation"
SEND_DENY_RECIPIENT_MISMATCH = "cross_user_confirmation_recipient_mismatch"


def evaluate_send(
    *,
    sender_user_id: str,
    recipient_user_id: str,
    recipient_session_id: str = "",
    channel: str = "dm",
    is_reply: bool = False,
    operation_envelope: bool = False,
    shared_trek: bool = False,
    consent_claim: object = None,
) -> dict:
    """Decide whether a send is allowed, and why.

    Combines ``classify_send_consent`` with consent-claim verification so
    the server backstop (e-3443) and ``/beacon-dm-send`` (e-3445) share one
    rule. Deliberately **does not** consult ``actions_authorized``: consent
    is about the send *target*, not the capability grant (SPEC §4). A
    cross-user send is denied for a missing recipient confirmation even if
    it carries no actions, and allowed with one even if it carries many.

    Returns
    -------
    dict with keys:
      ``allow`` (bool), ``consent_required`` (bool), ``reason`` (str).
    """
    consent_required, verdict = classify_send_consent(
        sender_user_id=sender_user_id,
        recipient_user_id=recipient_user_id,
        channel=channel,
        is_reply=is_reply,
        operation_envelope=operation_envelope,
        shared_trek=shared_trek,
    )
    if not consent_required:
        return {"allow": True, "consent_required": False, "reason": verdict}

    parsed = parse_recipient_confirmed_claim(consent_claim)
    if parsed is None:
        return {
            "allow": False,
            "consent_required": True,
            "reason": SEND_DENY_MISSING_CONFIRMATION,
        }
    if not claim_matches_recipient(
        parsed,
        recipient_session_id=recipient_session_id,
        recipient_user_id=recipient_user_id,
    ):
        return {
            "allow": False,
            "consent_required": True,
            "reason": SEND_DENY_RECIPIENT_MISMATCH,
        }
    return {
        "allow": True,
        "consent_required": True,
        "reason": SEND_ALLOW_CONFIRMED,
    }


__all__ = [
    "CONSENT_SKIP_SAME_USER",
    "CONSENT_SKIP_UNRESOLVED",
    "CONSENT_SKIP_TREK_SCOPE",
    "CONSENT_SKIP_NON_DM",
    "CONSENT_SKIP_OPERATION",
    "CONSENT_SKIP_REPLY",
    "CONSENT_SKIP_SHARED_TREK",
    "CONSENT_REQUIRED_CROSS_USER",
    "RECIPIENT_CONFIRMED_KIND",
    "CONSENT_CLAIM_KEY",
    "SEND_ALLOW_CONFIRMED",
    "SEND_DENY_MISSING_CONFIRMATION",
    "SEND_DENY_RECIPIENT_MISMATCH",
    "classify_send_consent",
    "mint_confirmation_id",
    "build_recipient_confirmed_claim",
    "parse_recipient_confirmed_claim",
    "claim_matches_recipient",
    "evaluate_send",
]
