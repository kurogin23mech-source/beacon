"""ms-97 / e-2659 Phase 3 — AC19 audit log trek_id propagation tests.

AC19: cross-user DM action 経路で shared_trek_member bypass が成立した
ときの audit log 行に以下 2 つの構造保証を入れる:

  * ``dm_gate.bypass = True`` (= 「bypass しました」 の明示フラグ)
  * ``dm_gate.trek_id = <matched_trek_id>`` (= どの trek が bypass の
    根拠になったかを後追い可能にする)

ms-70 では reason 文字列のみ audit に載せていたので post-mortem で
「どの trek の同意で通したのか」 を辿れなかった。 G3 (= lookup の
``(matched, trek_id)`` tuple 化) と組で AC19 を成立させる。

Positive tests:
  * cross-user DM + phase A+ shared trek → audit に bypass=True + trek_id。
  * cross-user DM + pre-A shared trek → audit に bypass=True + trek_id
    (= legacy path も同じ audit shape を取る、 G3 の return contract が
    全 phase で揃う構造保証)。

Negative tests:
  * same_user 経由 → bypass=False (= shared_trek_member ではない)。
  * no_actions 経由 → bypass=False。
  * cross-user + 共有 trek 無し (= gate fires) → bypass=False, trek_id 空。

End-to-end test for the audit record shape goes through
`should_gate_dm_action` directly + a fake list_treks; the FastAPI
integration assertion lives in `test_dm_gate.py` (= existing dispatcher
sidecar pin).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dm_gate  # noqa: E402


def _phase_a_trek(*, trek_id: str, alice_sid: str, bob_sid: str) -> dict:
    return {
        "trek_id": trek_id,
        "status": "active",
        "creator_actor": {"user_id": "uid-alice"},
        "members": [
            {"user_id": "uid-alice", "session_id": alice_sid,
             "role": "leader"},
            {"user_id": "uid-bob", "session_id": bob_sid,
             "role": "member"},
        ],
        "meta": {"migration_phase": "A"},
    }


def _pre_a_trek(*, trek_id: str) -> dict:
    return {
        "trek_id": trek_id,
        "status": "active",
        "creator_actor": {"user_id": "uid-alice"},
        "members": [
            {"user_id": "uid-alice", "role": "leader"},
            {"user_id": "uid-bob", "role": "member"},
        ],
        # no meta.migration_phase → defaults to pre-A.
    }


def _simulate_audit_dm_gate_field(
    *,
    sender_uid: str,
    receiver_uid: str,
    sender_sid: str,
    receiver_sid: str,
    actions: list[str],
    list_treks_for_user,
) -> dict:
    """Replay the server/app.py audit_record["dm_gate"] block.

    Mirrors lines 5622-5635 of server/app.py exactly so the test pins the
    end-to-end shape without spinning up the FastAPI client.
    """
    gate_lookup = dm_gate.build_shared_trek_lookup_from_lists(
        list_treks_for_user,
    )
    should_gate, gate_reason, gate_trek_id = dm_gate.should_gate_dm_action(
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        actions_authorized=actions,
        shared_trek_lookup=gate_lookup,
        sender_session_id=sender_sid,
        receiver_session_id=receiver_sid,
    )
    return {
        "should_gate": should_gate,
        "reason": gate_reason,
        "sender_user_id": sender_uid,
        "receiver_user_id": receiver_uid,
        "bypass": (
            (not should_gate)
            and gate_reason == dm_gate.GATE_REASON_SHARED_TREK
        ),
        "trek_id": gate_trek_id,
    }


# ---------------------------------------------------------------------------
# Positive: phase A+ shared trek → audit has bypass=True + trek_id
# ---------------------------------------------------------------------------

def test_audit_log_phase_a_bypass_carries_trek_id():
    """Phase A+ shared trek bypass → audit log has bypass=True + matched id."""
    treks_by_uid = {
        "uid-alice": [
            _phase_a_trek(
                trek_id="tk-PHASE-A-AUDIT",
                alice_sid="sv-a",
                bob_sid="sv-b",
            ),
        ],
    }
    audit = _simulate_audit_dm_gate_field(
        sender_uid="uid-alice",
        receiver_uid="uid-bob",
        sender_sid="sv-a",
        receiver_sid="sv-b",
        actions=["commit"],
        list_treks_for_user=lambda uid: treks_by_uid.get(uid, []),
    )
    assert audit["should_gate"] is False
    assert audit["reason"] == dm_gate.GATE_REASON_SHARED_TREK
    assert audit["bypass"] is True
    assert audit["trek_id"] == "tk-PHASE-A-AUDIT"


def test_audit_log_pre_a_bypass_carries_trek_id():
    """Pre-A trek bypass (= user_id grain) → audit log still records trek_id."""
    treks_by_uid = {
        "uid-alice": [_pre_a_trek(trek_id="tk-PRE-A-AUDIT")],
    }
    audit = _simulate_audit_dm_gate_field(
        sender_uid="uid-alice",
        receiver_uid="uid-bob",
        sender_sid="",
        receiver_sid="",
        actions=["commit"],
        list_treks_for_user=lambda uid: treks_by_uid.get(uid, []),
    )
    assert audit["should_gate"] is False
    assert audit["reason"] == dm_gate.GATE_REASON_SHARED_TREK
    assert audit["bypass"] is True
    assert audit["trek_id"] == "tk-PRE-A-AUDIT"


# ---------------------------------------------------------------------------
# Negative: non-shared-trek skips → bypass=False, trek_id empty
# ---------------------------------------------------------------------------

def test_audit_log_same_user_skip_has_no_bypass_flag():
    """Same-user skip is NOT bypass — bypass=False, trek_id empty."""
    audit = _simulate_audit_dm_gate_field(
        sender_uid="uid-alice",
        receiver_uid="uid-alice",
        sender_sid="sv-a",
        receiver_sid="sv-a2",
        actions=["commit"],
        list_treks_for_user=lambda uid: [],
    )
    assert audit["reason"] == dm_gate.GATE_REASON_SAME_USER
    assert audit["bypass"] is False
    assert audit["trek_id"] == ""


def test_audit_log_no_actions_skip_has_no_bypass_flag():
    """no_actions skip is NOT bypass."""
    audit = _simulate_audit_dm_gate_field(
        sender_uid="uid-alice",
        receiver_uid="uid-bob",
        sender_sid="sv-a",
        receiver_sid="sv-b",
        actions=[],
        list_treks_for_user=lambda uid: [],
    )
    assert audit["reason"] == dm_gate.GATE_REASON_NO_ACTIONS
    assert audit["bypass"] is False
    assert audit["trek_id"] == ""


def test_audit_log_cross_user_gate_fires_has_empty_trek_id():
    """Cross-user + no shared trek → gate fires, bypass=False, trek_id empty."""
    audit = _simulate_audit_dm_gate_field(
        sender_uid="uid-alice",
        receiver_uid="uid-bob",
        sender_sid="sv-a",
        receiver_sid="sv-b",
        actions=["commit"],
        list_treks_for_user=lambda uid: [],  # no trek
    )
    assert audit["should_gate"] is True
    assert audit["reason"] == dm_gate.GATE_REASON_CROSS_USER_ACTION
    assert audit["bypass"] is False
    assert audit["trek_id"] == ""


# ---------------------------------------------------------------------------
# G3: tuple return contract pinned at the audit-shape boundary
# ---------------------------------------------------------------------------

def test_audit_record_always_has_trek_id_field_present():
    """G3 contract: trek_id field is always present (string, possibly empty).

    Post-mortem analytics depend on the field always existing — KeyError
    on `audit['dm_gate']['trek_id']` should be impossible regardless of
    which gate branch fired.
    """
    cases = [
        # (sender_uid, receiver_uid, sender_sid, receiver_sid, actions,
        #  treks)
        ("u1", "u1", "s1", "s2", ["x"], {}),       # same_user
        ("u1", "u2", "s1", "s2", [], {}),          # no_actions
        ("u1", "u2", "s1", "s2", ["x"], {}),       # cross-user gate
        ("u1", "u2", "s1", "s2", ["x"], {           # bypass
            "u1": [_phase_a_trek(
                trek_id="tk-z", alice_sid="s1", bob_sid="s2",
            )]
        }),
    ]
    for s_uid, r_uid, s_sid, r_sid, actions, treks in cases:
        audit = _simulate_audit_dm_gate_field(
            sender_uid=s_uid,
            receiver_uid=r_uid,
            sender_sid=s_sid,
            receiver_sid=r_sid,
            actions=actions,
            list_treks_for_user=lambda uid, _t=treks: _t.get(uid, []),
        )
        assert "trek_id" in audit
        assert isinstance(audit["trek_id"], str)
        assert "bypass" in audit
        assert isinstance(audit["bypass"], bool)


def test_t1_system_bypass_has_no_trek_id_in_audit():
    """T1-system envelope bypass (= server-mint, ms-83 / e-1995) is NOT a
    shared_trek_member bypass — it's a separate authority. trek_id must
    stay empty so post-mortem queries don't conflate the two paths.
    """
    should_gate, reason, trek_id = dm_gate.should_gate_dm_action(
        sender_user_id="uid-system",
        receiver_user_id="uid-alice",
        actions_authorized=["trek.progress_check"],
        shared_trek_lookup=lambda *_a, **_kw: (True, "tk-irrelevant"),
        envelope_tier="T1-system",
        envelope_issuer="beacon-system",
    )
    assert should_gate is False
    assert reason == dm_gate.GATE_REASON_T1_SYSTEM
    assert trek_id == ""
