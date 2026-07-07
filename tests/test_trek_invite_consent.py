"""Tests for AC15 (= Trek invite acceptance leader-candidate notice +
1 hop consent DM hook) — ms-97 / Phase 6.

Coverage:

1. ``accept_invitation`` stamps ``meta.leader_candidate_notice_shown_at``
   on the affected member entry (both phase A+ session-grain path and
   pre-A user-grain path).
2. ``build_leader_candidate_notice`` renders the canonical text with the
   trek title substituted.
3. ``emit_leader_succession_consent_dm`` posts to the candidate's bus
   with the contracted envelope + payload schema.
4. DM payload schema matches the Phase 7 (AC22) contract.
5. Stamp is idempotent — re-joining from the same session preserves the
   first-delivery timestamp.

Phase 6 only pins the contract; the AC22 succession algorithm itself
lands in Phase 7.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Stable envelope-signing secret so the T1-system envelope mint succeeds
# in tests (= same convention as test_server_high_risk_endpoint_enforce.py).
os.environ.setdefault("BEACON_ENVELOPE_SECRET", "test-envelope-secret-ms97p6")

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pre_a_trek():
    """A pre-A (= legacy user_id keyed) trek with creator + 1 invitee."""
    t = trek.new_trek(
        title="Phase 6 contract",
        creator_user_id="u-leader", creator_email="leader@x",
        creator_session_id="sv-leader",
    )
    trek.add_invitation(
        t, user_id="u-invitee", email="invitee@x",
        invited_by_user_id="u-leader",
    )
    return t


@pytest.fixture
def phase_a_trek():
    """A phase A trek (= session_id keyed) with creator + 1 invitee."""
    t = trek.new_trek(
        title="Phase 6 session-grain",
        creator_user_id="u-leader", creator_email="leader@x",
        creator_session_id="sv-leader",
    )
    trek.set_migration_phase(t, "A")
    t["members"][0]["session_id"] = "sv-leader"
    trek.add_invitation(
        t, user_id="u-invitee", email="invitee@x",
        invited_by_user_id="u-leader",
    )
    return t


# ---------------------------------------------------------------------------
# 1. lib.trek — accept_invitation stamps notice meta
# ---------------------------------------------------------------------------

def test_accept_invitation_stamps_notice_pre_a(pre_a_trek):
    """pre-A trek の accept_invitation で meta stamp が刻まれる。"""
    trek.accept_invitation(pre_a_trek, user_id="u-invitee")
    m = trek.find_member(pre_a_trek, "u-invitee")
    assert m is not None
    meta = m.get("meta") or {}
    stamp = meta.get("leader_candidate_notice_shown_at")
    assert stamp, (
        "AC15: accept_invitation must stamp "
        "meta.leader_candidate_notice_shown_at on the member entry"
    )
    # ISO-like timestamp (= utcnow_iso output)
    assert "T" in stamp


def test_accept_invitation_stamps_notice_phase_a(phase_a_trek):
    """phase A (session-grain) でも placeholder upgrade 時に stamp が刻まれる。"""
    trek.accept_invitation(
        phase_a_trek, user_id="u-invitee", session_id="sv-invitee-1",
    )
    m = trek.find_member(phase_a_trek, session_id="sv-invitee-1")
    assert m is not None
    meta = m.get("meta") or {}
    assert meta.get("leader_candidate_notice_shown_at"), (
        "AC15: phase A placeholder upgrade must also stamp the notice"
    )
    # Schema sanity — the upgraded entry keeps session_id + joined_at
    assert m["session_id"] == "sv-invitee-1"
    assert m["joined_at"]


def test_accept_invitation_stamp_idempotent(pre_a_trek):
    """同じ user の 2 回目 accept で stamp は最初の delivery を保持する。"""
    trek.accept_invitation(pre_a_trek, user_id="u-invitee")
    m = trek.find_member(pre_a_trek, "u-invitee")
    first = m["meta"]["leader_candidate_notice_shown_at"]
    # Second accept call (= idempotent retry) must not bump the stamp.
    trek.accept_invitation(pre_a_trek, user_id="u-invitee")
    m = trek.find_member(pre_a_trek, "u-invitee")
    assert m["meta"]["leader_candidate_notice_shown_at"] == first, (
        "AC15 audit: stamp must pin first-delivery time, not be "
        "overwritten on idempotent retry"
    )


# ---------------------------------------------------------------------------
# 2. lib.trek — build_leader_candidate_notice text contract
# ---------------------------------------------------------------------------

def test_build_leader_candidate_notice_includes_title(pre_a_trek):
    """notice 文面に Trek タイトルが埋め込まれる。"""
    text = trek.build_leader_candidate_notice(pre_a_trek)
    assert "Phase 6 contract" in text, (
        "AC15 notice must include the trek title so the invitee knows "
        "which trek the consent attaches to"
    )


def test_build_leader_candidate_notice_includes_succession_keywords():
    """notice 文面に succession / consent / escalate のキーワードが含まれる。"""
    t = trek.new_trek(
        title="T",
        creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    text = trek.build_leader_candidate_notice(t)
    # Concept keywords that pin the contract (= 説明責任)
    assert "leader 候補" in text
    assert "succession" in text
    assert "consent" in text or "確認 DM" in text
    assert "辞退" in text
    assert "escalate" in text


# ---------------------------------------------------------------------------
# 3. server — emit_leader_succession_consent_dm posts correct envelope
# ---------------------------------------------------------------------------

def _make_trek_with_scope(project_id: str = "proj-cand") -> dict:
    """Build a Trek with a single scope entry so the emitter can resolve a
    candidate home project without an explicit override."""
    t = trek.new_trek(
        title="Succession contract",
        creator_user_id="u-leader", creator_email="leader@x",
        creator_session_id="sv-leader-old",
    )
    t["scope"] = [{"project": project_id, "ref": "ms-1"}]
    return t


def test_emit_leader_succession_consent_dm_posts_to_candidate_bus():
    """helper が candidate home project に bus event を post する。"""
    import app as server_app  # noqa: E402 — server/app.py imported via sys.path

    t = _make_trek_with_scope("proj-cand")
    posted: list[tuple[str, dict]] = []

    def fake_append(project_id, bus_data):
        posted.append((project_id, bus_data))
        return "evt-fake-123"

    with patch.object(server_app.db, "append_bus_event", side_effect=fake_append):
        event_id = server_app.emit_leader_succession_consent_dm(
            t,
            candidate_session_id="sv-candidate",
            former_leader_session_id="sv-leader-old",
        )

    assert event_id == "evt-fake-123"
    assert len(posted) == 1
    target_project, bus_data = posted[0]
    assert target_project == "proj-cand", (
        "AC15: DM must be posted to the candidate's home project bus "
        "(= resolved from trek.scope[0] when no override given)"
    )
    assert bus_data["channel"] == "dm"
    assert bus_data["recipient_session_id"] == "sv-candidate"
    assert bus_data["delivery"] == "auto-execute"
    # T1-system envelope wraps the DM.
    env = bus_data.get("envelope") or {}
    assert env, "AC15: DM must carry a signed envelope"
    assert env.get("scope") == "trek:" + t["trek_id"]
    assert "trek.leader_succession_consent" in (env.get("actions_authorized") or [])


def test_emit_leader_succession_consent_dm_payload_schema():
    """DM payload schema が AC22 契約と一致する。"""
    import app as server_app  # noqa: E402 — server/app.py imported via sys.path

    t = _make_trek_with_scope()
    captured: dict = {}

    def fake_append(project_id, bus_data):
        captured["data"] = bus_data
        return "evt-x"

    with patch.object(server_app.db, "append_bus_event", side_effect=fake_append):
        server_app.emit_leader_succession_consent_dm(
            t,
            candidate_session_id="sv-cand-2",
            former_leader_session_id="sv-leader-old",
            deadline_seconds=1800,
        )

    payload = captured["data"]["payload"]
    # Required fields (= AC22 contract pins for Phase 7 wiring)
    assert payload["kind"] == "trek-leader-succession-consent"
    assert payload["trek_id"] == t["trek_id"]
    assert payload["deadline_seconds"] == 1800
    assert payload["former_leader_session_id"] == "sv-leader-old"
    assert payload["candidate_session_id"] == "sv-cand-2"


def test_emit_leader_succession_consent_dm_requires_candidate_session_id():
    """空 candidate_session_id では ValueError。"""
    import app as server_app  # noqa: E402 — server/app.py imported via sys.path

    t = _make_trek_with_scope()
    with pytest.raises(ValueError, match="candidate_session_id"):
        server_app.emit_leader_succession_consent_dm(
            t, candidate_session_id="",
        )


def test_emit_leader_succession_consent_dm_no_scope_returns_none():
    """scope 空 + project override も空なら post せず None を返す。"""
    import app as server_app  # noqa: E402 — server/app.py imported via sys.path

    t = trek.new_trek(
        title="No scope",
        creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    # Empty scope: emitter cannot resolve a target bus.
    t["scope"] = []

    called = {"count": 0}

    def fake_append(project_id, bus_data):
        called["count"] += 1
        return "evt-should-not-happen"

    with patch.object(server_app.db, "append_bus_event", side_effect=fake_append):
        result = server_app.emit_leader_succession_consent_dm(
            t, candidate_session_id="sv-cand",
        )
    assert result is None
    assert called["count"] == 0, (
        "AC15: when no target bus is resolvable, the helper must skip the "
        "post (= return None) rather than throwing or posting to an "
        "ambiguous default"
    )
