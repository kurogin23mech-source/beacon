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


def test_wildcards_rejected():
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T1, issuer="u", project_id=PROJECT_ID,
            actions_authorized=["deploy:*"],
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
    assert "expired" in (res.rejection_reason or "")


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


def test_decide_delivery_t5_caps_at_notify():
    e = _t1()
    out = env_mod.decide_delivery(
        envelope=e, effective_tier=env_mod.TIER_T5,
        requested_action=None, requested_delivery="auto-execute",
    )
    assert out == "notify-user-only"


def test_decide_delivery_legacy_never_auto_execute():
    out = env_mod.decide_delivery(
        envelope=None, effective_tier=env_mod.TIER_T5,
        requested_action=None, requested_delivery="auto-execute",
    )
    assert out == "propose-to-ai"


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
