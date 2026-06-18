"""Tests for T1-system envelope (ms-83 / e-1995).

T1-system is a server-mint envelope tier introduced by ms-83 to let the
Beacon backend's periodic scheduler fire "next, please" progress-check
DMs into a Trek's claimed session as if the user had explicitly signed
them. The user's pre-approval happens once at Trek activation time
("Trek で進めて"); the server re-issues the equivalent T1 permission on
each tick within that Trek's scope.

Coverage:

  (1) issuance discipline (scope shape / issuer / requires trek_id)
  (2) verify happy path with T1-system envelope
  (3) expired T1-system → degrade
  (4) scope mismatch / malformed scope → verify rejects
  (5) signature tampering (bad signature) → verify rejects
  (6) issuer spoof (= caller mints with tier=T1-system but issuer is not
      "beacon-system") → issuance rejected at module boundary
  (7) high-risk action (deploy) cannot ride a T1-system envelope
  (8) dm_gate recognises T1-system as bypass
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import envelope as env_mod  # noqa: E402
import dm_gate as dm_gate_mod  # noqa: E402


PROJECT_ID = "proj-x"
TREK_ID = "tk-deadbeef"


@pytest.fixture
def nonce_store():
    return env_mod.InMemoryNonceStore()


@pytest.fixture
def parent_lookup():
    return env_mod.FunctionParentLookup(lambda _pid, _eid: None)


def _t1_system(**overrides):
    """Build a fresh, valid T1-system envelope. Overrides update before signing."""
    kwargs = dict(
        project_id=PROJECT_ID,
        trek_id=TREK_ID,
        actions_authorized=["dm.send"],
    )
    kwargs.update(overrides)
    return env_mod.issue_t1_system_envelope(**kwargs)


# ---------------------------------------------------------------------------
# (1) Issuance discipline
# ---------------------------------------------------------------------------

def test_issue_t1_system_happy_path():
    env = _t1_system()
    assert env["tier"] == env_mod.TIER_T1_SYSTEM
    assert env["issuer"] == env_mod.T1_SYSTEM_ISSUER
    assert env["scope"] == f"trek:{TREK_ID}"
    assert env["actions_authorized"] == ["dm.send"]
    assert env["project_id"] == PROJECT_ID
    assert env["signature"]  # non-empty
    assert env_mod.verify_signature(env)


def test_issue_t1_system_requires_trek_id():
    with pytest.raises(ValueError):
        env_mod.issue_t1_system_envelope(
            project_id=PROJECT_ID,
            trek_id="",
            actions_authorized=["dm.send"],
        )


def test_issue_t1_system_via_issue_envelope_rejects_other_issuer():
    """A caller cannot directly mint tier=T1-system claiming to be a
    different issuer. The dedicated helper forces the canonical issuer,
    and the underlying issue_envelope enforces the rule too so callers
    bypassing the helper still fail at the module boundary."""
    with pytest.raises(ValueError):
        env_mod.issue_envelope(
            tier=env_mod.TIER_T1_SYSTEM,
            issuer="user@example.com",  # wrong issuer
            project_id=PROJECT_ID,
            actions_authorized=["dm.send"],
            scope=f"trek:{TREK_ID}",
        )


def test_issue_t1_system_via_issue_envelope_requires_trek_scope():
    """T1-system rejects scope shapes other than ``trek:<id>``."""
    for bad_scope in [None, "", "op:op-1", "trek:"]:
        with pytest.raises(ValueError):
            env_mod.issue_envelope(
                tier=env_mod.TIER_T1_SYSTEM,
                issuer=env_mod.T1_SYSTEM_ISSUER,
                project_id=PROJECT_ID,
                actions_authorized=["dm.send"],
                scope=bad_scope,
            )


# ---------------------------------------------------------------------------
# (2) Verify happy path
# ---------------------------------------------------------------------------

def test_verify_t1_system_happy_path(nonce_store, parent_lookup):
    env = _t1_system()
    result = env_mod.verify(
        env,
        project_id=PROJECT_ID,
        payload={"text": "next, please"},
        requested_action="dm.send",
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    assert result.passed is True
    assert result.effective_tier == env_mod.TIER_T1_SYSTEM


# ---------------------------------------------------------------------------
# (3) Time window
# ---------------------------------------------------------------------------

def test_verify_t1_system_expired_envelope(nonce_store, parent_lookup):
    env = _t1_system(ttl_seconds=1)
    # Sleep past expiry — tiny TTL keeps the test fast.
    import time
    time.sleep(1.2)
    result = env_mod.verify(
        env,
        project_id=PROJECT_ID,
        payload={"text": "next, please"},
        requested_action="dm.send",
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    assert result.passed is False
    assert result.effective_tier == env_mod.TIER_T5


# ---------------------------------------------------------------------------
# (4) Scope / project mismatch
# ---------------------------------------------------------------------------

def test_verify_t1_system_project_mismatch(nonce_store, parent_lookup):
    env = _t1_system()
    result = env_mod.verify(
        env,
        project_id="proj-other",  # receiver is a different project
        payload={"text": "x"},
        requested_action="dm.send",
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# (5) Signature tampering
# ---------------------------------------------------------------------------

def test_verify_t1_system_tampered_signature_rejected(
    nonce_store, parent_lookup
):
    env = _t1_system()
    env["actions_authorized"] = ["deploy"]  # post-sign tamper
    result = env_mod.verify(
        env,
        project_id=PROJECT_ID,
        payload={"text": "x"},
        requested_action="deploy",
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# (6) Issuer spoof — caller hand-crafts a body with the right issuer but
# bad signature (= can't fake the HMAC without the secret)
# ---------------------------------------------------------------------------

def test_verify_t1_system_unsigned_body_rejected(nonce_store, parent_lookup):
    """Hand-craft a T1-system body without signing it (= empty signature).

    Mirrors the path where an attacker tries to inject a payload that
    claims tier=T1-system without access to the server secret. Step 1
    (= verify_signature) rejects.
    """
    body = {
        "tier": env_mod.TIER_T1_SYSTEM,
        "issuer": env_mod.T1_SYSTEM_ISSUER,
        "scope": f"trek:{TREK_ID}",
        "actions_authorized": ["dm.send"],
        "data_class": "free",
        "issued_at": "2026-06-18T00:00:00.000000Z",
        "expires_at": "2099-06-18T00:00:00.000000Z",
        "project_id": PROJECT_ID,
        "nonce": "n1",
        "conversation_id": "c1",
        "in_reply_to": None,
        "chain_depth": 0,
        "disclosure_contract": env_mod.default_disclosure_contract(),
        "tokens": None,
        "refill_policy": None,
        "signature": "AAAA",  # bogus
    }
    result = env_mod.verify(
        body,
        project_id=PROJECT_ID,
        payload={"text": "x"},
        requested_action="dm.send",
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# (7) High-risk action gate — deploy / project.delete cannot ride T1-system
# ---------------------------------------------------------------------------

def test_t1_system_cannot_request_deploy(nonce_store, parent_lookup):
    """Even if a SPEC enumerates deploy in actions_authorized, the verify
    step blocks high-risk actions for non-T1 tiers (= Trek's deploy /
    release は user 介入境界, CORE doc b1XOKXQeC0JXaKkO0CRt)."""
    env = _t1_system(actions_authorized=["deploy"])
    result = env_mod.verify(
        env,
        project_id=PROJECT_ID,
        payload={"text": "x"},
        requested_action="deploy",
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    assert result.passed is False


# ---------------------------------------------------------------------------
# (8) dm_gate recognises T1-system as a bypass
# ---------------------------------------------------------------------------

def _never(_a, _b):
    return False


def test_dm_gate_t1_system_bypass_even_cross_user():
    """Even with cross-user + actions + no shared Trek, a T1-system
    envelope signals "user pre-approved at trek activation" and the
    cross-user human gate steps aside."""
    should_gate, reason = dm_gate_mod.should_gate_dm_action(
        sender_user_id="u-alice",
        receiver_user_id="u-bob",
        actions_authorized=["dm.send"],
        shared_trek_lookup=_never,
        envelope_tier="T1-system",
        envelope_issuer="beacon-system",
    )
    assert should_gate is False
    assert reason == dm_gate_mod.GATE_REASON_T1_SYSTEM


def test_dm_gate_t1_system_issuer_must_be_beacon_system():
    """Spoofed tier (= claiming T1-system with the wrong issuer) does
    NOT bypass the gate. This is belt + braces alongside the envelope
    verify pipeline."""
    should_gate, reason = dm_gate_mod.should_gate_dm_action(
        sender_user_id="u-alice",
        receiver_user_id="u-bob",
        actions_authorized=["dm.send"],
        shared_trek_lookup=_never,
        envelope_tier="T1-system",
        envelope_issuer="user@example.com",  # wrong
    )
    assert should_gate is True
    assert reason == dm_gate_mod.GATE_REASON_CROSS_USER_ACTION


def test_dm_gate_backward_compat_default_args():
    """Existing callers that don't pass envelope_tier / issuer must still
    behave as before (= no regression for ms-70 ms-76 paths)."""
    should_gate, reason = dm_gate_mod.should_gate_dm_action(
        sender_user_id="u-alice",
        receiver_user_id="u-bob",
        actions_authorized=["dm.send"],
        shared_trek_lookup=_never,
    )
    assert should_gate is True
    assert reason == dm_gate_mod.GATE_REASON_CROSS_USER_ACTION
