"""Unit tests for the ms-63 disclosure_contract + disclosure_gate.

Covers the foundation schema (e-1428 / e-1443) and the receive-side
disclosure gate (e-1430) plus the high-sensitivity T5 schema enforcement
(e-1431) and the T5 救済 paths (e-1432: in_reply_to chain → T3 and
query_type schema).

The test surface deliberately mirrors test_envelope.py: imports the same
server-side module, uses the same _t1() helper pattern, and pins the
documented behaviour against the SPEC acceptance criteria.

NOT tested here (separate suites):
  * persistence gate (e-1293) — covered by test_persistence_poisoning_defense
  * inbox-hook injection priority (e-1442 wiring) — covered by
    test_bus_inbox_hook
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import envelope as env_mod  # noqa: E402


PROJECT_ID = "proj-x"


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
# Schema foundation: default + normalize (e-1428 / e-1443)
# ---------------------------------------------------------------------------

def test_default_disclosure_contract_is_high_safe():
    """SPEC § 設計方針 2: default-safe = high sensitivity, schema-only T5."""
    contract = env_mod.default_disclosure_contract()
    assert contract["sensitivity"] == env_mod.SENSITIVITY_HIGH
    assert contract["t5_response_mode"] == env_mod.T5_RESPONSE_MODE_SCHEMA_ONLY
    assert contract["t5_free_text"] is False


def test_normalize_low_sensitivity_default_flips_to_free_mode():
    """A bare ``sensitivity=low`` policy implies t5_response_mode=free.

    This matches the OSS-friendly posture: low projects don't gain extra
    schema cap beyond Phase-1 ping shape unless they ask for it.
    """
    contract = env_mod.normalize_disclosure_contract({
        "sensitivity": "low",
    })
    assert contract["sensitivity"] == "low"
    assert contract["t5_response_mode"] == env_mod.T5_RESPONSE_MODE_FREE
    assert contract["t5_free_text"] is True


def test_normalize_unknown_sensitivity_falls_back_to_default():
    """Garbage in → safe default out (= fail-closed)."""
    contract = env_mod.normalize_disclosure_contract({
        "sensitivity": "lol",
        "t5_response_mode": "anything-goes",
    })
    assert contract["sensitivity"] == env_mod.SENSITIVITY_HIGH
    assert contract["t5_response_mode"] == env_mod.T5_RESPONSE_MODE_SCHEMA_ONLY


def test_normalize_drops_unknown_keys():
    """Forward-compat: future axes don't leak into the signed bytes."""
    contract = env_mod.normalize_disclosure_contract({
        "sensitivity": "high",
        "made_up_future_field": "ignored",
    })
    assert "made_up_future_field" not in contract


def test_normalize_explicit_t5_free_text_override():
    """Caller can opt-in to T5 free text on low projects."""
    contract = env_mod.normalize_disclosure_contract({
        "sensitivity": "low",
        "t5_free_text": False,
    })
    assert contract["sensitivity"] == "low"
    assert contract["t5_free_text"] is False


# ---------------------------------------------------------------------------
# issue_envelope burns the contract into the signed bytes (e-1429 / e-1443)
# ---------------------------------------------------------------------------

def test_issue_envelope_default_contract_present():
    """Acceptance § 1: every minted envelope carries a disclosure_contract."""
    env = _t1()
    assert "disclosure_contract" in env
    assert env["disclosure_contract"] == env_mod.default_disclosure_contract()


def test_issue_envelope_explicit_contract_burned_in():
    """Acceptance § 4: project_join mints carry the project's snapshot."""
    env = _t1(disclosure_policy={"sensitivity": "low"})
    assert env["disclosure_contract"]["sensitivity"] == "low"
    assert env["disclosure_contract"]["t5_free_text"] is True


def test_envelope_disclosure_contract_is_signed():
    """Tampering with the contract post-sign breaks the signature.

    This is the structural guarantee that the gate cannot be bypassed by
    stripping or downgrading the contract on the wire — the canonical
    bytes include it, so any mutation invalidates the HMAC.
    """
    env = _t1(disclosure_policy={"sensitivity": "high"})
    assert env_mod.verify_signature(env) is True
    # Tamper: downgrade to low.
    env["disclosure_contract"]["sensitivity"] = "low"
    assert env_mod.verify_signature(env) is False


def test_legacy_envelope_without_contract_treated_as_high():
    """Backward-compat: pre-ms-63 envelopes (no contract field) → safe default.

    The from_dict path accepts missing disclosure_contract (it's now
    optional). The gate then reads None and falls back to high-sensitivity.
    """
    legacy_payload = {
        "tier": "T1",
        "issuer": "u",
        "scope": None,
        "actions_authorized": [],
        "data_class": "free",
        "issued_at": "2026-06-08T00:00:00.000000Z",
        "expires_at": "2026-06-08T01:00:00.000000Z",
        "project_id": PROJECT_ID,
        "nonce": "n1",
        "conversation_id": "c1",
        "in_reply_to": None,
        "chain_depth": 0,
        # No disclosure_contract field at all.
    }
    legacy = env_mod.Envelope.from_dict(legacy_payload)
    assert legacy.disclosure_contract is None


# ---------------------------------------------------------------------------
# disclosure_gate_check — schema reply (= safest surface)
# ---------------------------------------------------------------------------

def test_gate_schema_reply_always_permitted_under_high():
    env = _t1(disclosure_policy={"sensitivity": "high"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="schema", reply_payload={"ack": True},
    )
    assert verdict.permit is True


def test_gate_schema_reply_with_task_id_permitted():
    """Acceptance § 5: task_id reference is allowed in the high schema."""
    env = _t1(disclosure_policy={"sensitivity": "high"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="schema",
        reply_payload={"busy": False, "task_id": "e-1430"},
    )
    assert verdict.permit is True


def test_gate_schema_reply_with_free_text_field_refused():
    """A reply with non-allowlist keys is refused under the high schema."""
    env = _t1(disclosure_policy={"sensitivity": "high"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="schema",
        reply_payload={"answer": "the customer's email is x@y.com"},
    )
    assert verdict.permit is False
    assert verdict.rewrite_to == {"ack": True}


def test_gate_schema_reply_string_too_long_refused():
    """T5_PING_VALUE_MAX_LEN bounds the answer surface."""
    env = _t1(disclosure_policy={"sensitivity": "high"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="schema",
        reply_payload={"task_id": "x" * 100},
    )
    assert verdict.permit is False


# ---------------------------------------------------------------------------
# disclosure_gate_check — free-text reply
# ---------------------------------------------------------------------------

def test_gate_free_reply_refused_on_high_sensitivity():
    """Acceptance § 5: high-sensitivity refuses free-text outright."""
    env = _t1(disclosure_policy={"sensitivity": "high"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="free",
        reply_payload={"text": "here are the deploy credentials..."},
    )
    assert verdict.permit is False
    assert "high-sensitivity" in verdict.reason
    assert verdict.rewrite_to == {"ack": True}


def test_gate_free_reply_permitted_on_low_sensitivity():
    """Beacon-OSS posture: low-sensitivity + default t5_free_text=True → permit."""
    env = _t1(disclosure_policy={"sensitivity": "low"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="free",
        reply_payload={"text": "PR#66 was merged at 14:30 UTC"},
    )
    assert verdict.permit is True


def test_gate_free_reply_refused_when_t5_free_text_disabled():
    """Low + explicit t5_free_text=False → refuse (caller opted in to schema)."""
    env = _t1(disclosure_policy={
        "sensitivity": "low",
        "t5_free_text": False,
    })
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="free",
        reply_payload={"text": "..."},
    )
    assert verdict.permit is False


def test_gate_free_reply_refused_when_response_mode_schema_only():
    """Low + t5_response_mode=schema-only → refuse free even with free_text=True."""
    env = _t1(disclosure_policy={
        "sensitivity": "low",
        "t5_response_mode": "schema-only",
        "t5_free_text": True,
    })
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="free",
        reply_payload={"text": "..."},
    )
    assert verdict.permit is False


# ---------------------------------------------------------------------------
# disclosure_gate_check — query reply (T5 救済方向 B, e-1432)
# ---------------------------------------------------------------------------

def test_gate_query_reply_permitted_on_low_sensitivity():
    """TrailNode → Beacon "PR#66 どうなった?" pattern survives on low projects."""
    env = _t1(disclosure_policy={"sensitivity": "low"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="query",
        reply_payload={"question": "Did PR#66 land?", "ref": "PR-66"},
    )
    assert verdict.permit is True


def test_gate_query_reply_refused_on_high_sensitivity():
    """Even a question carries context — refuse on high."""
    env = _t1(disclosure_policy={"sensitivity": "high"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="query",
        reply_payload={"question": "Is the customer X opted in?"},
    )
    assert verdict.permit is False
    assert verdict.rewrite_to == {"ack": True}


def test_gate_query_reply_too_long_question_refused():
    """T5_QUERY_VALUE_MAX_LEN bounds the question surface."""
    env = _t1(disclosure_policy={"sensitivity": "low"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="query",
        reply_payload={"question": "x" * 300},
    )
    assert verdict.permit is False


def test_gate_query_reply_with_unknown_key_refused():
    env = _t1(disclosure_policy={"sensitivity": "low"})
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="query",
        reply_payload={"question": "ok", "secret": "leak"},
    )
    assert verdict.permit is False


# ---------------------------------------------------------------------------
# disclosure_gate_check — fail-closed defaults
# ---------------------------------------------------------------------------

def test_gate_none_envelope_treated_as_high():
    """Legacy / unsigned inbound → default-safe (high-sensitivity refuse)."""
    verdict = env_mod.disclosure_gate_check(
        None, reply_kind="free",
        reply_payload={"text": "anything"},
    )
    assert verdict.permit is False
    # High-sensitivity refusal kicks in (= default contract is high).
    assert "high-sensitivity" in verdict.reason


def test_gate_unknown_reply_kind_refused():
    env = _t1()
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="hallucinated-kind",
        reply_payload={"ack": True},
    )
    assert verdict.permit is False


def test_gate_envelope_with_missing_contract_field_treated_as_high():
    """Envelope dict with no disclosure_contract key → safe default applied.

    Forward-compat path: a legacy unsigned-but-present envelope (= dict
    without the field) should not let the gate fall open.
    """
    env = {
        "tier": "T1",
        # No disclosure_contract key at all.
    }
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="free",
        reply_payload={"text": "..."},
    )
    assert verdict.permit is False


# ---------------------------------------------------------------------------
# effective_tier_for_disclosure (T5 救済方向 A, e-1432)
# ---------------------------------------------------------------------------

def test_effective_tier_t5_with_in_reply_to_promotes_to_t3():
    """SPEC § 設計方針 5 方向 A: alive reply chain → T3 treatment."""
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T5,
        issuer="ai@beacon",
        project_id=PROJECT_ID,
        actions_authorized=[],
        in_reply_to="evt-parent-123",
        disclosure_policy={"sensitivity": "high"},
    )
    assert env_mod.effective_tier_for_disclosure(env) == env_mod.TIER_T3


def test_effective_tier_t5_without_in_reply_to_stays_t5():
    """Spontaneous T5 (no parent) is still T5 — = autonomous-send risk surface."""
    env = _t1()
    # Build a manual T5 with no in_reply_to.
    env_t5 = env_mod.issue_envelope(
        tier=env_mod.TIER_T5,
        issuer="ai@beacon",
        project_id=PROJECT_ID,
        actions_authorized=[],
    )
    assert env_mod.effective_tier_for_disclosure(env_t5) == env_mod.TIER_T5


def test_effective_tier_other_tiers_passthrough():
    """T1/T2/T3 are unaffected by the in_reply_to promotion logic."""
    env_t1 = _t1()
    assert env_mod.effective_tier_for_disclosure(env_t1) == env_mod.TIER_T1


def test_effective_tier_none_envelope_defaults_to_t5():
    assert env_mod.effective_tier_for_disclosure(None) == env_mod.TIER_T5


# ---------------------------------------------------------------------------
# T5 救済方向 A applied to disclosure_gate: query reply opens on high
# project when the inbound is a reply chain.
# ---------------------------------------------------------------------------

def test_gate_query_reply_permitted_on_high_when_in_reply_chain():
    """The TrailNode → Beacon "PR#66 どうなった?" pattern works even on high.

    Reasoning: the inbound carries in_reply_to → T3 effective tier; the
    receiver is replying to an existing thread, not unilaterally exposing
    new context. The query schema itself caps payload to a question +
    optional reference id, so even on a high project the disclosure
    surface stays bounded.
    """
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T5,
        issuer="ai@trailnode",
        project_id=PROJECT_ID,
        actions_authorized=[],
        in_reply_to="evt-parent-456",
        disclosure_policy={"sensitivity": "high"},
    )
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="query",
        reply_payload={"question": "Did PR#66 land?", "ref": "PR-66"},
    )
    assert verdict.permit is True


def test_gate_query_reply_refused_on_high_without_in_reply_chain():
    """No reply chain → still refuse query on high (autonomous = risk)."""
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T5,
        issuer="ai@trailnode",
        project_id=PROJECT_ID,
        actions_authorized=[],
        # No in_reply_to.
        disclosure_policy={"sensitivity": "high"},
    )
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="query",
        reply_payload={"question": "Did PR#66 land?"},
    )
    assert verdict.permit is False


def test_gate_free_reply_still_refused_on_high_even_with_in_reply_chain():
    """Reply-chain promotion does NOT open the free-text surface on high.

    The NDA metaphor (SPEC § 設計方針 4) doesn't bend just because a
    conversation is alive — high-sensitivity projects clamp free text
    unconditionally, regardless of the inbound tier.
    """
    env = env_mod.issue_envelope(
        tier=env_mod.TIER_T5,
        issuer="ai@trailnode",
        project_id=PROJECT_ID,
        actions_authorized=[],
        in_reply_to="evt-parent-789",
        disclosure_policy={"sensitivity": "high"},
    )
    verdict = env_mod.disclosure_gate_check(
        env, reply_kind="free",
        reply_payload={"text": "Yes, PR#66 was merged at 14:30 UTC by John"},
    )
    assert verdict.permit is False


# ---------------------------------------------------------------------------
# Verdict object serialization (audit log shape)
# ---------------------------------------------------------------------------

def test_verdict_to_audit_dict_round_trip():
    verdict = env_mod.disclosure_gate_check(
        _t1(disclosure_policy={"sensitivity": "high"}),
        reply_kind="free",
        reply_payload={"text": "..."},
    )
    audit = verdict.to_audit_dict()
    assert audit["permit"] is False
    assert audit["rewrite_to"] == {"ack": True}
    assert isinstance(audit["reason"], str)
