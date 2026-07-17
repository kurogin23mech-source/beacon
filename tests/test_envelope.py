"""Unit tests for the bus message envelope (ms-54 / e-1155 Phase 1).

Coverage targets:
  * canonical serialization → signature stable across field-order changes
  * each of the 9 verify steps (signature → tier → project → time → nonce →
    in_reply_to → chain_depth → action → disclosure) has at least one
    success and one failure test
  * T5 degradation rules
  * decide_delivery downgrade matrix
  * backward compat (missing envelope → T5-equivalent legacy path)
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import envelope as env_mod  # noqa: E402


PROJECT_ID = "proj-x"


@pytest.fixture
def nonce_store():
    return env_mod.InMemoryNonceStore()


@pytest.fixture
def parent_lookup():
    return env_mod.FunctionParentLookup(lambda _pid, _eid: None)


def _t1(**overrides):
    """Build a fresh, valid T1 envelope. Overrides update before signing."""
    base = dict(
        tier=env_mod.TIER_T1,
        issuer="user@example.com",
        project_id=PROJECT_ID,
        actions_authorized=["status_check"],
        data_class="free",
    )
    base.update(overrides)
    return env_mod.issue_envelope(**base)


# ---------------------------------------------------------------------------
# Canonical serialization + signature
# ---------------------------------------------------------------------------

def test_canonical_bytes_field_order_independent():
    a = {"a": 1, "b": 2, "signature": "ignored"}
    b = {"b": 2, "a": 1, "signature": "different"}
    assert env_mod.canonical_bytes(a) == env_mod.canonical_bytes(b)


def test_signature_round_trip():
    body = {
        "tier": "T1", "issuer": "u", "scope": None,
        "actions_authorized": ["x"], "data_class": "free",
        "issued_at": "2026-06-08T00:00:00.000000Z",
        "expires_at": "2026-06-08T01:00:00.000000Z",
        "project_id": PROJECT_ID, "nonce": "n1",
        "conversation_id": "c1", "in_reply_to": None, "chain_depth": 0,
        "tokens": None, "refill_policy": None,
    }
    body["signature"] = env_mod.sign(body)
    assert env_mod.verify_signature(body) is True


def test_tampered_envelope_fails_signature():
    e = _t1()
    e["tier"] = env_mod.TIER_T2  # post-sign tamper
    assert env_mod.verify_signature(e) is False


# ---------------------------------------------------------------------------
# ms-110 / e-3443: recipient_confirmed consent claim signed IN at mint time.
#
# Regression guard for the bug where the CLI grafted the consent claim onto the
# already-signed envelope (``envelope_obj = {**envelope_obj, key: claim}``).
# Adding a key after ``sign()`` invalidated the signature, so the receive-time
# verify degraded the DM to T5, and T5's ping-only payload schema then rejected
# the free-text ``text`` key → 403 ``envelope_verify_rejected`` on every
# --recipient-confirmed send. The fix mints the claim INTO the signed body.
# ---------------------------------------------------------------------------

_SAMPLE_CLAIM = {
    "kind": "recipient_confirmed",
    "confirmation_id": "rc-1700000000-abc123",
    "confirmed_by": {"user_id": "uid-alice", "email": "alice@example.com"},
    "confirmed_at": "2026-07-17T00:00:00Z",
    "recipient": {"session_id": "sv-bob", "user_id": "uid-bob",
                  "project_id": PROJECT_ID},
    "channel": "dm",
}


def test_consent_claim_is_covered_by_signature():
    """A claim passed to issue_envelope rides UNDER the signature (authentic)."""
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T1, issuer="alice@example.com", project_id=PROJECT_ID,
        actions_authorized=[], data_class="free", consent_claim=_SAMPLE_CLAIM,
    )
    # The claim is present as a top-level envelope field...
    assert e[env_mod.CONSENT_CLAIM_KEY] == _SAMPLE_CLAIM
    # ...and the signature is valid *with* the claim included (not grafted after).
    assert env_mod.verify_signature(e) is True
    # Tampering the claim after signing must break the signature (tamper-evident).
    tampered = dict(e)
    tampered[env_mod.CONSENT_CLAIM_KEY] = {
        **_SAMPLE_CLAIM,
        "recipient": {"session_id": "sv-carol", "user_id": "uid-carol"},
    }
    assert env_mod.verify_signature(tampered) is False


def test_confirmed_dm_verifies_as_t1_not_t5(nonce_store, parent_lookup):
    """The repro: a free-text DM carrying a signed consent claim must pass the
    full verify pipeline as T1 — NOT degrade to T5 (which would 403 the
    free-text ``text`` payload under T5's ping-only allowlist)."""
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T1, issuer="alice@example.com", project_id=PROJECT_ID,
        actions_authorized=[], data_class="free", consent_claim=_SAMPLE_CLAIM,
    )
    res = env_mod.verify(
        e, project_id=PROJECT_ID,
        # Free text — exactly the payload shape the T5 fallback rejects.
        payload={"text": "hello", "recipient_session_id": "sv-bob",
                 "source_project": PROJECT_ID},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is True, res.steps
    assert res.effective_tier == env_mod.TIER_T1
    # The claim survives verify intact for the downstream consent backstop to read.
    assert res.effective_tier != env_mod.TIER_T5


def test_grafting_claim_after_signing_breaks_verify(nonce_store, parent_lookup):
    """Documents the ORIGINAL bug: appending the claim AFTER sign() invalidates
    the signature → T5 degrade → the free-text payload is rejected. This is the
    behavior the fix eliminates; kept as a guard so the graft pattern can never
    silently return."""
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T1, issuer="alice@example.com", project_id=PROJECT_ID,
        actions_authorized=[], data_class="free",
    )
    # Reproduce the removed CLI line: graft onto the already-signed envelope.
    e_grafted = {**e, env_mod.CONSENT_CLAIM_KEY: _SAMPLE_CLAIM}
    assert env_mod.verify_signature(e_grafted) is False
    res = env_mod.verify(
        e_grafted, project_id=PROJECT_ID,
        payload={"text": "hello"}, requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.effective_tier == env_mod.TIER_T5
    # And the rejection is exactly the T5-payload-allowlist failure from the field.
    assert res.rejection_reason is not None
    assert "T5 payload" in res.rejection_reason


# ---------------------------------------------------------------------------
# Issuance discipline
# ---------------------------------------------------------------------------

def test_t1_must_have_no_scope():
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T1, issuer="u", project_id=PROJECT_ID,
            actions_authorized=[], scope="oops",
        )


def test_t2_requires_scope():
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
            actions_authorized=["x"],
        )


def test_wildcards_rejected_for_t1():
    # T1 = human explicit signature. Strict enumeration — wildcards smuggle
    # scope under a per-message human approval.
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T1, issuer="u", project_id=PROJECT_ID,
            actions_authorized=["deploy:*"],
        )


def test_last_segment_wildcards_allowed_for_t2():
    # ms-60 § 設計方針 4 — T2 (Operation scope) allows last-segment wildcards
    # because the SPEC author documents the subscope contract in the approve
    # flow. Middle / verb wildcards remain forbidden (see test below).
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op-1", project_id=PROJECT_ID,
        scope="op-1",
        actions_authorized=["extract:profile:*", "task done:e-*"],
    )
    assert env["tier"] == env_mod.TIER_T2
    assert env["actions_authorized"] == ["extract:profile:*", "task done:e-*"]


def test_middle_wildcards_rejected_for_t2():
    # Carve-out is strict: only the last segment may contain `*`.
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T2, issuer="op-1", project_id=PROJECT_ID,
            scope="op-1",
            actions_authorized=["*:profile:daily"],
        )
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T2, issuer="op-1", project_id=PROJECT_ID,
            scope="op-1",
            actions_authorized=["extract:*:user"],
        )


def test_phase2_fields_always_null():
    e = _t1()
    assert e["tokens"] is None
    assert e["refill_policy"] is None


# ---------------------------------------------------------------------------
# 9-step verify
# ---------------------------------------------------------------------------

def test_verify_happy_path_t1(nonce_store, parent_lookup):
    e = _t1(actions_authorized=["status_check"])
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"text": "ok"},
        requested_action="status_check",
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is True, res.steps
    assert res.effective_tier == env_mod.TIER_T1


def test_verify_step1_bad_signature(nonce_store, parent_lookup):
    e = _t1()
    e["signature"] = "AAAA"
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.effective_tier == env_mod.TIER_T5


def test_verify_step3_project_mismatch(nonce_store, parent_lookup):
    e = _t1()
    res = env_mod.verify(
        e, project_id="other-project", payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert "project_match" in res.steps


def test_verify_step3_cross_project_freetext_dm_soft_degrades(nonce_store, parent_lookup):
    # e-3567: a free-text DM to *another* project mismatches project_id (the
    # sender can only mint against their own project). It must NOT 403 — it
    # soft-degrades to T5 (flows as notify-user) like the legacy path, so the
    # downstream ms-70 dm_gate can handle cross-user approval.
    e = _t1()  # project_id = PROJECT_ID
    res = env_mod.verify(
        e, project_id="other-project",
        payload={"text": "hey, quick question about the deploy"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.effective_tier == env_mod.TIER_T5
    # No rejection_reason → app.py does not 403 (the whole point of e-3567).
    assert res.rejection_reason is None
    assert "cross_project_dm_soft_degrade" in res.steps


def test_verify_step3_cross_project_action_still_rejected(nonce_store, parent_lookup):
    # The relaxation is free-text only: a cross-project envelope that requests
    # an ACTION still hard-rejects (no cross-project action smuggling).
    e = _t1()
    res = env_mod.verify(
        e, project_id="other-project",
        payload={"text": "run it"},
        requested_action="status_check",
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.effective_tier == env_mod.TIER_T5
    assert res.rejection_reason is not None  # → 403


def test_verify_signature_fail_freetext_still_rejected(nonce_store, parent_lookup):
    # Guard: the relaxation is scoped to project_id mismatch only. A *different*
    # verify failure (tampered signature) on free text must still hard-reject —
    # a forged envelope is structurally suspicious, not a benign cross-project DM.
    e = _t1()
    e["issuer"] = "attacker@evil.example"  # mutate after signing → sig invalid
    res = env_mod.verify(
        e, project_id=PROJECT_ID,
        payload={"text": "free text"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.rejection_reason is not None  # → 403, unchanged by e-3567


def test_verify_step4_expired(nonce_store, parent_lookup):
    e = _t1(ttl_seconds=1)
    # Force "now" forward past the expiry.
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s", now=future,
    )
    assert res.passed is False
    # Ping-shape payload + no action → soft degrade. The breadcrumb lives in
    # the per-step trail (audit-visible) rather than rejection_reason
    # (HTTP-403 trigger). See _degrade_to_t5 docstring for the split.
    assert "expired" in res.steps.get("time_window", "")
    assert res.effective_tier == env_mod.TIER_T5


def test_verify_step5_replay_blocked(nonce_store, parent_lookup):
    e = _t1()
    first = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert first.passed is True
    # Same nonce a second time → replay.
    second = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert second.passed is False


def test_verify_step6_in_reply_to_mismatch(nonce_store):
    e = _t1(in_reply_to="parent-eid", chain_depth=1)
    # Parent has recipient_session_id = "alice", sender claims "eve".
    lookup = env_mod.FunctionParentLookup(
        lambda _pid, _eid: {"payload": {"recipient_session_id": "alice"}}
    )
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=lookup,
        sender_session_id="eve",
    )
    assert res.passed is False
    assert "in_reply_to" in res.steps


def test_verify_step7_chain_depth_exceeded(nonce_store, parent_lookup):
    e = _t1(chain_depth=999)
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert "chain_depth" in res.steps


def test_issue_envelope_rejects_negative_chain_depth():
    # Server must never mint a chain_depth < 0 envelope. The verify-side
    # check is the second line of defense; this is the first.
    with pytest.raises(ValueError, match="chain_depth"):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T1, issuer="u", project_id=PROJECT_ID,
            actions_authorized=["status_check"], chain_depth=-1,
        )


def test_verify_step7_rejects_negative_chain_depth(nonce_store, parent_lookup):
    # Handcraft an envelope with chain_depth=-1 (issue_envelope refuses to
    # mint one, so we build the dict directly and sign it with the server
    # secret to reach verify's step 7). Previously the check was only
    # `depth > limit`, so -1 slipped through. Tightened invariant rejects it.
    issued_at = env_mod._now_iso()
    expires_at = env_mod._plus_seconds_iso(3600)
    body = {
        "tier": env_mod.TIER_T1,
        "issuer": "u",
        "scope": None,
        "actions_authorized": ["status_check"],
        "data_class": "free",
        "issued_at": issued_at,
        "expires_at": expires_at,
        "project_id": PROJECT_ID,
        "nonce": "nonce-negdepth-1",
        "conversation_id": "c-negdepth",
        "in_reply_to": None,
        "chain_depth": -1,
        "tokens": None,
        "refill_policy": None,
    }
    body["signature"] = env_mod.sign(body)
    # Sanity check: signature itself is valid, so verify must reach step 7.
    assert env_mod.verify_signature(body) is True

    res = env_mod.verify(
        body, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert "chain_depth" in res.steps
    assert "-1" in res.steps["chain_depth"]
    assert res.effective_tier == env_mod.TIER_T5


def test_verify_step8_action_not_authorized(nonce_store, parent_lookup):
    e = _t1(actions_authorized=["status_check"])
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"text": "x"},
        requested_action="deploy",
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.rejection_reason and "action" in res.rejection_reason.lower()


def test_verify_step8_high_risk_requires_t1(nonce_store, parent_lookup):
    # T2 envelope tries to authorize a high-risk action.
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        scope="dangerous-op", actions_authorized=["deploy"],
    )
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"text": "x"},
        requested_action="deploy",
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert "high-risk" in (res.rejection_reason or "")


def test_verify_step9_t5_free_text_rejected(nonce_store, parent_lookup):
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T5, issuer="ai", project_id=PROJECT_ID,
        actions_authorized=[],
    )
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"text": "free prose here"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    # T5 with free text payload → reject (text is not in T5_PING_KEYS).
    assert res.effective_tier == env_mod.TIER_T5


def test_verify_step9_t5_short_ping_ok(nonce_store, parent_lookup):
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T5, issuer="ai", project_id=PROJECT_ID,
        actions_authorized=[],
    )
    res = env_mod.verify(
        e, project_id=PROJECT_ID, payload={"ping": "ok", "status": "awake"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is True
    assert res.effective_tier == env_mod.TIER_T5


# ---------------------------------------------------------------------------
# Backward compat (missing envelope → T5-equivalent)
# ---------------------------------------------------------------------------

def test_missing_envelope_ping_ok(nonce_store, parent_lookup):
    res = env_mod.verify(
        None, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is True
    assert res.effective_tier == env_mod.TIER_T5


def test_missing_envelope_action_rejected(nonce_store, parent_lookup):
    res = env_mod.verify(
        None, project_id=PROJECT_ID, payload={"ping": "ok"},
        requested_action="deploy",
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False
    assert res.rejection_reason and "legacy" in res.rejection_reason.lower()


def test_missing_envelope_free_text_rejected(nonce_store, parent_lookup):
    res = env_mod.verify(
        None, project_id=PROJECT_ID, payload={"text": "hello world"},
        requested_action=None,
        nonce_store=nonce_store, parent_lookup=parent_lookup,
        sender_session_id="s",
    )
    assert res.passed is False


# ---------------------------------------------------------------------------
# decide_delivery downgrade matrix
# ---------------------------------------------------------------------------

def test_decide_delivery_t1_auto_execute_passes_through():
    e = _t1(actions_authorized=["deploy"])
    out = env_mod.decide_delivery(
        envelope=e, effective_tier=env_mod.TIER_T1,
        requested_action="deploy", requested_delivery="auto-execute",
    )
    assert out == "auto-execute"


def test_decide_delivery_t2_out_of_scope_downgrades():
    e = env_mod.issue_envelope(
        tier=env_mod.TIER_T2, issuer="op", project_id=PROJECT_ID,
        scope="status", actions_authorized=["status_check"],
    )
    out = env_mod.decide_delivery(
        envelope=e, effective_tier=env_mod.TIER_T2,
        requested_action="adhoc_action",
        requested_delivery="auto-execute",
    )
    assert out == "propose-to-ai"


def test_decide_delivery_t3_action_request_downgrades():
    e = _t1()  # we just need any signed envelope; verify decides tier
    out = env_mod.decide_delivery(
        envelope=e, effective_tier=env_mod.TIER_T3,
        requested_action="x", requested_delivery="auto-execute",
    )
    assert out == "propose-to-ai"


def test_decide_delivery_t5_freetext_caps_at_notify():
    """Free-text payload (= not ping-shape) under T5 caps at notify-user-only
    so the AI never auto-ingests free prose from an unsigned source."""
    e = _t1()
    out = env_mod.decide_delivery(
        envelope=e, effective_tier=env_mod.TIER_T5,
        requested_action=None, requested_delivery="propose-to-ai",
        t5_payload_conforms=False,
    )
    assert out == "notify-user-only"


def test_decide_delivery_t5_pingshape_keeps_propose():
    """Ping-shape payload is safe to surface as propose-to-ai (bounded
    disclosure surface) — only auto-execute is denied."""
    e = _t1()
    out = env_mod.decide_delivery(
        envelope=e, effective_tier=env_mod.TIER_T5,
        requested_action=None, requested_delivery="propose-to-ai",
        t5_payload_conforms=True,
    )
    assert out == "propose-to-ai"


def test_decide_delivery_legacy_never_auto_execute():
    """Legacy (no envelope) treated as T5; auto-execute always downgrades."""
    out = env_mod.decide_delivery(
        envelope=None, effective_tier=env_mod.TIER_T5,
        requested_action=None, requested_delivery="auto-execute",
        t5_payload_conforms=True,
    )
    # Auto-execute under T5 + ping-shape → propose-to-ai (one step down).
    assert out == "propose-to-ai"


def test_decide_delivery_legacy_freetext_caps_at_notify():
    """Legacy + free-text → notify-user-only, even if caller asked for
    propose-to-ai (the AI seeing unsigned free prose is the exact
    prompt-injection vector e-1155 defends against)."""
    out = env_mod.decide_delivery(
        envelope=None, effective_tier=env_mod.TIER_T5,
        requested_action=None, requested_delivery="propose-to-ai",
        t5_payload_conforms=False,
    )
    assert out == "notify-user-only"


# ---------------------------------------------------------------------------
# T5 short-ping schema validator
# ---------------------------------------------------------------------------

def test_t5_payload_long_string_rejected():
    err = env_mod.validate_t5_payload({"status": "x" * 100})
    assert err is not None


def test_t5_payload_disallowed_key_rejected():
    err = env_mod.validate_t5_payload({"command": "run"})
    assert err is not None


def test_t5_payload_nested_rejected():
    err = env_mod.validate_t5_payload({"status": {"nested": "bad"}})
    assert err is not None
