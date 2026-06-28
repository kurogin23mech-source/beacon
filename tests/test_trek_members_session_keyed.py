"""Red tests for AC6 (= members[] session_id keyed cutover) — ms-97 / e-2659.

これらの test は AC6 実装が Phase 1 で land した時に green に変わる契約。
Phase 0 段階 (= scaffolding のみ) では `xfail` でマークし、 TDD signal を
保持する。 NEW write path (= add_invitation / accept_invitation / leader
transfer) が session_id keyed member entry を吐くようになった瞬間に
xfail → xpass 反転で実装の land を検知する。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


@pytest.fixture
def fresh_trek():
    return trek.new_trek(
        title="Session-keyed contract",
        creator_user_id="u-1", creator_email="leader@x",
        creator_session_id="sv-leader-1",
    )


@pytest.mark.xfail(reason="AC6 not yet implemented (= write path still user_id keyed)")
def test_new_trek_seeds_session_id_keyed_leader_member(fresh_trek):
    """new_trek が leader member を session_id keyed で吐くこと。"""
    leader = fresh_trek["members"][0]
    assert leader["session_id"] == "sv-leader-1"
    assert leader["role"] == "leader"


@pytest.mark.xfail(reason="AC6 not yet implemented (= accept_invitation still user_id keyed)")
def test_accept_invitation_emits_session_id_keyed_member():
    """accept_invitation 経由の member entry が session_id field を持つこと。

    AC6 が land した時、 同 user_id の複数 session が独立 member entry を
    持つことが期待値 (= 同 user 別 session の状態管理 granularity が
    session_id grain に揃う)。
    """
    t = trek.new_trek(
        title="Invitee", creator_user_id="u-1",
        creator_email="leader@x", creator_session_id="sv-leader-1",
    )
    trek.add_invitation(
        t, inviter_user_id="u-1",
        invitee_user_id="u-2", invitee_email="invitee@x",
    )
    trek.accept_invitation(
        t, invitee_user_id="u-2", session_id="sv-invitee-1",
    )
    invitee_entries = [
        m for m in t["members"] if m.get("user_id") == "u-2"
    ]
    assert len(invitee_entries) == 1
    assert invitee_entries[0].get("session_id") == "sv-invitee-1"
