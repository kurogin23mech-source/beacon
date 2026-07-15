"""Sender-side cross-user DM consent gate — data model (ms-110 / e-3442).

Pins the sender-side mirror of ``server/dm_gate.py``: the discrimination
function that decides whether a human must confirm the recipient before a
DM leaves, and the ``recipient_confirmed`` consent-claim data model.

e-3442 acceptance criteria mapped to test sections:

  (AC1) ``classify_send_consent`` splits cross-user / same-user / Trek /
        Operation correctly (user_id comparison + carve-outs).
  (AC2) the consent claim schema records 誰が・いつ・どの宛先, and is
        **independent of the auto-execute ``actions_authorized`` grant**.
  (AC3) each case (cross-user / same-user / Trek / Operation / reply) is
        classified correctly by unit tests.

Plus ``evaluate_send`` — the combined accept/reject rule e-3443 / e-3445
share — and the recipient-match replay guard.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import dm_consent  # noqa: E402


# ---------------------------------------------------------------------------
# AC1 / AC3: classify_send_consent — the discrimination function
# ---------------------------------------------------------------------------

def test_cross_user_new_send_requires_consent():
    """Different humans, plain dm, new send → a human must confirm."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
    )
    assert required is True
    assert reason == dm_consent.CONSENT_REQUIRED_CROSS_USER


def test_same_user_skips_consent():
    """A user's own two sessions → no cross-human confirmation needed."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-alice",
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_SAME_USER


def test_shared_trek_skips_consent():
    """Cross-user but both in the same active Trek → pre-approved scope."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        shared_trek=True,
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_SHARED_TREK


def test_operation_envelope_skips_consent():
    """Operation (T2 scope) envelope is an already pre-approved path."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        operation_envelope=True,
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_OPERATION


def test_reply_skips_consent():
    """A reply's recipient is derived from the parent envelope → §2 lane."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        is_reply=True,
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_REPLY


@pytest.mark.parametrize(
    "channel",
    ["operation-trigger", "trek-progress-check", "trek-trigger",
     "trek-task-review", "trek-leader-digest", "trek-anything"],
)
def test_trek_scoped_channel_skips_consent(channel):
    """Operation / Trek channels are pre-approved scope (reuses dm_qualgate)."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        channel=channel,
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_TREK_SCOPE


# ---------------------------------------------------------------------------
# Fail-safe: unknown recipient identity → treat as cross-user
# ---------------------------------------------------------------------------

def test_unknown_recipient_user_id_requires_consent():
    """A send we cannot prove is same-user must be confirmed (fail-safe).

    This is exactly the accident shape: the primitive was called directly
    with no resolved recipient identity. It must NOT slip through as
    same-user just because the recipient uid is blank.
    """
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="",
    )
    assert required is True
    assert reason == dm_consent.CONSENT_REQUIRED_CROSS_USER


def test_both_user_ids_empty_requires_consent():
    """Both blank → cannot prove same-user → require confirmation."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="",
        recipient_user_id="",
    )
    assert required is True
    assert reason == dm_consent.CONSENT_REQUIRED_CROSS_USER


# ---------------------------------------------------------------------------
# Precedence: carve-outs must win even when recipient identity is unknown,
# so armed auto-reply / Trek协奏 / Operation autonomy do not regress (AC5).
# ---------------------------------------------------------------------------

def test_same_user_precedence_over_reply():
    """Same-user is the cheapest definitive skip; reported as same_user."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="uid-alice",
        is_reply=True,
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_SAME_USER


def test_reply_carveout_survives_unknown_recipient():
    """An armed auto-reply with no resolved recipient uid must still pass
    (recipient derived from parent) — otherwise ms-100 armed regresses."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="",
        is_reply=True,
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_REPLY


def test_trek_channel_carveout_survives_unknown_recipient():
    """Operation-trigger delivery with unresolved recipient uid still passes."""
    required, reason = dm_consent.classify_send_consent(
        sender_user_id="uid-alice",
        recipient_user_id="",
        channel="operation-trigger",
    )
    assert required is False
    assert reason == dm_consent.CONSENT_SKIP_TREK_SCOPE


# ---------------------------------------------------------------------------
# AC2: the recipient_confirmed consent claim schema
# ---------------------------------------------------------------------------

def test_build_claim_records_who_when_which():
    """誰が・いつ・どの宛先 — all three facts land in the claim."""
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        confirmed_by_email="alice@example.com",
        recipient_session_id="sv-bob",
        recipient_user_id="uid-bob",
        recipient_project_id="proj-bob",
        confirmation_id="rc-123-abc",
        confirmed_at="2026-07-15T00:00:00Z",
    )
    assert claim["kind"] == dm_consent.RECIPIENT_CONFIRMED_KIND
    assert claim["confirmation_id"] == "rc-123-abc"
    # 誰が
    assert claim["confirmed_by"] == {
        "user_id": "uid-alice", "email": "alice@example.com",
    }
    # いつ
    assert claim["confirmed_at"] == "2026-07-15T00:00:00Z"
    # どの宛先
    assert claim["recipient"] == {
        "session_id": "sv-bob", "user_id": "uid-bob", "project_id": "proj-bob",
    }


def test_claim_is_independent_of_actions_authorized():
    """SPEC §4: the consent claim carries NO capability grant. It must not
    contain ``actions_authorized`` / ``tier`` — those belong to the
    auto-execute envelope, a different axis."""
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_user_id="uid-bob",
    )
    assert "actions_authorized" not in claim
    assert "tier" not in claim
    assert "scope" not in claim
    # And the key it rides under on the envelope is distinct.
    assert dm_consent.CONSENT_CLAIM_KEY != "actions_authorized"


def test_build_claim_requires_confirmer():
    """An unattributable confirmation is not a confirmation."""
    with pytest.raises(ValueError):
        dm_consent.build_recipient_confirmed_claim(
            confirmed_by_user_id="",
            recipient_user_id="uid-bob",
        )


def test_build_claim_requires_a_recipient_identifier():
    """The claim must pin a target so it can be matched against the send."""
    with pytest.raises(ValueError):
        dm_consent.build_recipient_confirmed_claim(
            confirmed_by_user_id="uid-alice",
        )


def test_mint_confirmation_id_shape():
    cid = dm_consent.mint_confirmation_id()
    assert cid.startswith("rc-")
    assert len(cid.split("-")) == 3


def test_parse_round_trips_a_built_claim():
    built = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_session_id="sv-bob",
    )
    parsed = dm_consent.parse_recipient_confirmed_claim(built)
    assert parsed is not None
    assert parsed["confirmed_by"]["user_id"] == "uid-alice"
    assert parsed["recipient"]["session_id"] == "sv-bob"


@pytest.mark.parametrize("bad", [
    None,
    "not-a-dict",
    {},
    {"kind": "something_else", "confirmed_by": {"user_id": "x"},
     "recipient": {"user_id": "y"}, "confirmation_id": "rc-1"},
    {"kind": "recipient_confirmed", "confirmed_by": {},
     "recipient": {"user_id": "y"}, "confirmation_id": "rc-1"},  # no confirmer
    {"kind": "recipient_confirmed", "confirmed_by": {"user_id": "x"},
     "recipient": {}, "confirmation_id": "rc-1"},  # no recipient pin
    {"kind": "recipient_confirmed", "confirmed_by": {"user_id": "x"},
     "recipient": {"user_id": "y"}},  # no confirmation_id
])
def test_parse_rejects_malformed(bad):
    assert dm_consent.parse_recipient_confirmed_claim(bad) is None


# ---------------------------------------------------------------------------
# Recipient-match replay guard
# ---------------------------------------------------------------------------

def test_claim_matches_pinned_session_id():
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_session_id="sv-bob",
    )
    assert dm_consent.claim_matches_recipient(
        claim, recipient_session_id="sv-bob") is True


def test_claim_does_not_match_different_recipient():
    """A confirmation for Bob must not authorise a send to Carol."""
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_session_id="sv-bob",
        recipient_user_id="uid-bob",
    )
    assert dm_consent.claim_matches_recipient(
        claim, recipient_session_id="sv-carol",
        recipient_user_id="uid-carol") is False


def test_claim_matches_on_user_id_when_session_absent():
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_user_id="uid-bob",
    )
    assert dm_consent.claim_matches_recipient(
        claim, recipient_user_id="uid-bob") is True


# ---------------------------------------------------------------------------
# evaluate_send: the combined accept/reject rule (② e-3443 / ④ e-3445 share)
# ---------------------------------------------------------------------------

def test_evaluate_allows_same_user_without_claim():
    out = dm_consent.evaluate_send(
        sender_user_id="uid-alice",
        recipient_user_id="uid-alice",
    )
    assert out["allow"] is True
    assert out["consent_required"] is False
    assert out["reason"] == dm_consent.CONSENT_SKIP_SAME_USER


def test_evaluate_denies_cross_user_without_claim():
    out = dm_consent.evaluate_send(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        recipient_session_id="sv-bob",
    )
    assert out["allow"] is False
    assert out["consent_required"] is True
    assert out["reason"] == dm_consent.SEND_DENY_MISSING_CONFIRMATION


def test_evaluate_allows_cross_user_with_matching_claim():
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_session_id="sv-bob",
        recipient_user_id="uid-bob",
    )
    out = dm_consent.evaluate_send(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        recipient_session_id="sv-bob",
        consent_claim=claim,
    )
    assert out["allow"] is True
    assert out["reason"] == dm_consent.SEND_ALLOW_CONFIRMED


def test_evaluate_denies_cross_user_with_mismatched_claim():
    """Replay guard: a claim confirming Bob cannot authorise a send to Carol."""
    claim = dm_consent.build_recipient_confirmed_claim(
        confirmed_by_user_id="uid-alice",
        recipient_session_id="sv-bob",
        recipient_user_id="uid-bob",
    )
    out = dm_consent.evaluate_send(
        sender_user_id="uid-alice",
        recipient_user_id="uid-carol",
        recipient_session_id="sv-carol",
        consent_claim=claim,
    )
    assert out["allow"] is False
    assert out["reason"] == dm_consent.SEND_DENY_RECIPIENT_MISMATCH


def test_evaluate_consent_ignores_actions_authorized():
    """SPEC §4: consent is about the target, not the capability grant.

    A cross-user send is denied for missing confirmation regardless of
    whether it carries actions — evaluate_send takes no actions argument,
    so this asserts the decision cannot depend on it.
    """
    out = dm_consent.evaluate_send(
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
        recipient_session_id="sv-bob",
        consent_claim=None,
    )
    # No claim → denied, whatever the (absent) action grant would be.
    assert out["allow"] is False
