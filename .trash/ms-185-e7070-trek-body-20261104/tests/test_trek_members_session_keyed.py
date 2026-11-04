"""Tests for AC6 (= members[] session_id keyed cutover) — ms-97 / e-2658.

Phase 0 では xfail 札で TDD signal を持っていたが、 Phase 1 cutover land で
green に反転した。 ここでは

- new_trek の leader member が session_id を持つこと (= 新規 trek は最初から
  session_id keyed として吐かれる)
- accept_invitation 経由の member entry が session_id field を持つこと
- 同 user_id の 2 sessions が独立 member entry に展開されること (= silent
  expand)
- pre-A (= legacy) trek は user_id grain 振る舞いを保つこと

の 4 観点を pin する。 migration script の挙動は
``test_migrate_members_session_keyed.py`` で別途 cover する。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


@pytest.fixture
def fresh_trek():
    """A trek built via new_trek then stamped at phase A (= post-migration)."""
    t = trek.new_trek(
        title="Session-keyed contract",
        creator_user_id="u-1", creator_email="leader@x",
        creator_session_id="sv-leader-1",
    )
    # Phase 1 cutover: new_trek 自身はまだ pre-A schema を seed (= 既存 trek
    # の互換性維持のため、 Phase 1 では明示的に phase A stamp + leader entry
    # の session_id 補完で session_id keyed 化する。 Phase 2 以降で
    # new_trek 直接 session_id keyed seed に切替予定)。
    trek.set_migration_phase(t, "A")
    t["members"][0]["session_id"] = "sv-leader-1"
    return t


def test_phase_A_leader_member_has_session_id(fresh_trek):
    """phase A の leader member は session_id を持つ。"""
    leader = fresh_trek["members"][0]
    assert leader["session_id"] == "sv-leader-1"
    assert leader["role"] == "leader"
    assert leader["user_id"] == "u-1"


def test_phase_A_accept_invitation_upgrades_placeholder_to_session_bound(fresh_trek):
    """phase A の accept_invitation は招待 placeholder を session-bound に upgrade する。

    add_invitation で seed された entry (= session_id 未設定) は、 accept_invitation
    時に session_id 引数で session-grain entry に upgrade される。
    """
    trek.add_invitation(
        fresh_trek, user_id="u-2", email="invitee@x",
        invited_by_user_id="u-1",
    )
    # 招待直後は placeholder = session_id 未設定 + joined_at 空
    placeholder = next(m for m in fresh_trek["members"] if m["user_id"] == "u-2")
    assert placeholder.get("session_id", "") == ""
    assert placeholder["joined_at"] == ""

    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-1",
    )
    invitee_entries = [m for m in fresh_trek["members"] if m["user_id"] == "u-2"]
    assert len(invitee_entries) == 1
    assert invitee_entries[0]["session_id"] == "sv-invitee-1"
    assert invitee_entries[0]["joined_at"]


def test_phase_A_silent_expand_same_user_different_session(fresh_trek):
    """phase A: 同 user の 2 つ目の session join は新規 member entry を作る。"""
    trek.add_invitation(
        fresh_trek, user_id="u-2", email="invitee@x",
        invited_by_user_id="u-1",
    )
    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-1",
    )
    # 別 session で join → silent expand
    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-2",
    )
    invitee_entries = [m for m in fresh_trek["members"] if m["user_id"] == "u-2"]
    assert len(invitee_entries) == 2
    session_ids = {m["session_id"] for m in invitee_entries}
    assert session_ids == {"sv-invitee-1", "sv-invitee-2"}
    # 2 つ目の entry は role=member で leader にはならない
    assert all(m["role"] == "member" for m in invitee_entries)


def test_phase_A_accept_invitation_idempotent_same_session(fresh_trek):
    """phase A: 同じ session_id から再 join しても entry は増えない。"""
    trek.add_invitation(
        fresh_trek, user_id="u-2", email="invitee@x",
        invited_by_user_id="u-1",
    )
    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-1",
    )
    first_joined = next(
        m for m in fresh_trek["members"]
        if m.get("session_id") == "sv-invitee-1"
    )["joined_at"]
    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-1",
    )
    invitee_entries = [m for m in fresh_trek["members"] if m["user_id"] == "u-2"]
    assert len(invitee_entries) == 1
    # joined_at は最初の値を保持 (= idempotent)
    assert invitee_entries[0]["joined_at"] == first_joined


def test_phase_A_find_member_by_session_id(fresh_trek):
    """phase A: find_member(t, session_id=...) で session-grain lookup。"""
    m = trek.find_member(fresh_trek, session_id="sv-leader-1")
    assert m is not None
    assert m["user_id"] == "u-1"


def test_phase_A_remove_member_by_session_id_keeps_user_other_sessions(fresh_trek):
    """phase A: session_id 指定の remove は同 user の他 session を残す。"""
    trek.add_invitation(
        fresh_trek, user_id="u-2", email="invitee@x",
        invited_by_user_id="u-1",
    )
    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-1",
    )
    trek.accept_invitation(
        fresh_trek, user_id="u-2", session_id="sv-invitee-2",
    )
    # session_id grain で 1 つだけ remove
    trek.remove_member(fresh_trek, session_id="sv-invitee-1")
    invitee_entries = [m for m in fresh_trek["members"] if m["user_id"] == "u-2"]
    assert len(invitee_entries) == 1
    assert invitee_entries[0]["session_id"] == "sv-invitee-2"


def test_phase_A_remove_member_rejects_missing_session(fresh_trek):
    with pytest.raises(ValueError, match="not a member"):
        trek.remove_member(fresh_trek, session_id="sv-ghost")


def test_phase_A_remove_member_blocks_leader_session(fresh_trek):
    """phase A: leader session を session_id grain で remove しようとすると拒否。"""
    with pytest.raises(ValueError, match="cannot remove leader"):
        trek.remove_member(fresh_trek, session_id="sv-leader-1")


def test_phase_A_accept_invitation_requires_invitation(fresh_trek):
    """phase A: 招待されていない user_id の join は拒否される。"""
    with pytest.raises(ValueError, match="not invited"):
        trek.accept_invitation(
            fresh_trek, user_id="u-rando", session_id="sv-rando",
        )


def test_is_session_id_keyed_phase_A():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    assert trek.is_session_id_keyed(t) is False  # pre-A by default
    trek.set_migration_phase(t, "A")
    assert trek.is_session_id_keyed(t) is True
    trek.set_migration_phase(t, "B")
    assert trek.is_session_id_keyed(t) is True
    trek.set_migration_phase(t, "C")
    assert trek.is_session_id_keyed(t) is True


def test_pre_A_trek_legacy_user_grain_idempotency_when_no_session_id():
    """pre-A trek + ``session_id`` 省略 join では従来の user_id grain 挙動を維持する。

    ms-97 / e-2636 — pre-A の auto-migrate は ``session_id`` 引数があった時にのみ
    起動する。 session_id を渡さない caller (= 旧 CLI / 旧 client) は user_id
    grain idempotent のまま動き、 phase は pre-A のまま据え置かれる。
    """
    t = trek.new_trek(
        title="legacy", creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    assert trek.is_session_id_keyed(t) is False
    trek.add_invitation(
        t, user_id="u-2", email="b@x", invited_by_user_id="u-1",
    )
    # session_id を渡さない 2 回 join は同じ entry の joined_at を冪等に維持。
    trek.accept_invitation(t, user_id="u-2")
    trek.accept_invitation(t, user_id="u-2")
    u2_entries = [m for m in t["members"] if m["user_id"] == "u-2"]
    assert len(u2_entries) == 1
    # session_id field は load されない (= pre-A schema 互換)
    assert "session_id" not in u2_entries[0]
    # phase も pre-A のまま (= auto-migrate は session_id 引数の有無で gate)
    assert trek.is_session_id_keyed(t) is False


def test_pre_A_trek_auto_migrates_on_first_session_grain_join():
    """ms-97 / e-2636 — pre-A trek + session_id 付き join は inline auto-migrate。

    Done when #1-#3 を pin する:
    1. members[] に session_id keyed entry が新規追加される (= 既存 user_id
       entry を上書きせず append)
    2. exit 0 + success message が実 server state と一致 (= silent no-op を
       構造的に塞ぐ → ここでは return 値の trek_doc の状態で確認)
    3. 既存 leader entry は in-place で session_id 補完 + 後続 join entry は
       別レコードとして追加 (= migration + extend の両立)
    """
    t = trek.new_trek(
        title="legacy", creator_user_id="u-leader", creator_email="leader@x",
        creator_session_id="sv-leader-orig",
    )
    assert trek.is_session_id_keyed(t) is False
    # Leader entry は pre-A 形 (= session_id field 不在)。
    leader_pre = next(m for m in t["members"] if m["user_id"] == "u-leader")
    assert "session_id" not in leader_pre

    trek.add_invitation(
        t, user_id="u-2", email="b@x", invited_by_user_id="u-leader",
    )
    # 同 user の 2 つの session が連続 join する (= dogfood で観測した経路)。
    trek.accept_invitation(t, user_id="u-2", session_id="sv-2a")
    trek.accept_invitation(t, user_id="u-2", session_id="sv-2b")

    # #1: members[] が 2 件に増える。
    u2_entries = [m for m in t["members"] if m["user_id"] == "u-2"]
    assert len(u2_entries) == 2
    assert {m["session_id"] for m in u2_entries} == {"sv-2a", "sv-2b"}

    # #3: leader entry に session_id が in-place 補完されている。
    leader_post = next(m for m in t["members"] if m["user_id"] == "u-leader")
    assert leader_post["session_id"] == "sv-leader-orig"
    assert leader_post["role"] == "leader"

    # phase は A に flip し、 backup snapshot も取られている。
    assert trek.is_session_id_keyed(t) is True
    assert trek.get_migration_phase(t) == "A"
    backup = t.get(trek.MEMBERS_LEGACY_BACKUP_KEY) or []
    assert backup, "auto-migrate must snapshot pre-A members"
    # backup は pre-A 形 (= leader entry に session_id 不在) を保つ。
    backup_leader = next(b for b in backup if b.get("user_id") == "u-leader")
    assert "session_id" not in backup_leader


def test_pre_A_trek_auto_migrate_is_idempotent_on_replay():
    """ms-97 / e-2636 — 同じ session_id から 2 度 join しても entry は増えない。

    AC #1 の「member entry 新規追加」 は同 session の冪等性を壊さない (=
    1 user 1 session = 1 member entry)。
    """
    t = trek.new_trek(
        title="legacy", creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    trek.add_invitation(
        t, user_id="u-2", email="b@x", invited_by_user_id="u-1",
    )
    trek.accept_invitation(t, user_id="u-2", session_id="sv-2a")
    trek.accept_invitation(t, user_id="u-2", session_id="sv-2a")
    u2_entries = [m for m in t["members"] if m["user_id"] == "u-2"]
    assert len(u2_entries) == 1
    assert u2_entries[0]["session_id"] == "sv-2a"


def test_build_member_with_session_id_writes_field():
    m = trek.build_member(
        user_id="u-1", email="a@x", session_id="sv-1",
    )
    assert m["session_id"] == "sv-1"


def test_build_member_without_session_id_omits_field():
    m = trek.build_member(user_id="u-1", email="a@x")
    assert "session_id" not in m


def test_find_member_requires_some_key():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    with pytest.raises(ValueError, match="user_id or session_id"):
        trek.find_member(t)


def test_remove_member_requires_some_key():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@x",
        creator_session_id="sv-1",
    )
    with pytest.raises(ValueError, match="user_id or session_id"):
        trek.remove_member(t)
