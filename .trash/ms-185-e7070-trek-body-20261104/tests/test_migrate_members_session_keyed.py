"""Tests for scripts/migrate_members_session_keyed.py (ms-97 / e-2658 AC34).

Migration / revert script は AC6 (= members[] session_id keyed cutover) の
不可逆破壊から既存 trek を守る最小単位。 ここでは pure mutator
(= migrate_trek / revert_trek) に対する green test を pin する。
CLI entrypoint は別ファイルで integration test 範囲。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from lib import trek  # noqa: E402

# scripts/ は package ではないので、 import path は file-name 直叩き。
import migrate_members_session_keyed as migrate_mod  # noqa: E402
import revert_members_session_keyed as revert_mod  # noqa: E402


def _trek_with_single_session() -> dict:
    """1 user, 1 session, leader として登録された fresh trek を返す。"""
    return trek.new_trek(
        title="Single session",
        creator_user_id="u-1", creator_email="leader@x",
        creator_session_id="sv-leader-1",
    )


def _trek_with_multi_session() -> dict:
    """1 user, 3 session (= うち 1 つが leader_session_id) の trek を作る。

    session_history に 3 entry 入れて、 leader の expand 後 1 leader + 2
    member に分解されることを観測する fixture。
    """
    t = trek.new_trek(
        title="Multi session",
        creator_user_id="u-1", creator_email="leader@x",
        creator_session_id="sv-leader-1",
    )
    # 同 user の別 session 2 つ (= fork / reconnect) を session_history に
    # 載せる。 ``upsert_session_history`` は idempotent なので追加できる。
    trek.upsert_session_history(
        t, session_id="sv-leader-2", user_id="u-1",
        email="leader@x", role_at_join="member",
        joined_at="2026-06-28T01:00:00Z",
    )
    trek.upsert_session_history(
        t, session_id="sv-leader-3", user_id="u-1",
        email="leader@x", role_at_join="member",
        joined_at="2026-06-28T02:00:00Z",
    )
    return t


# ---------------------------------------------------------------------------
# Basic case: 1 user / 1 session → 1 leader entry
# ---------------------------------------------------------------------------

def test_migrate_basic_single_session_becomes_single_leader_entry():
    t = _trek_with_single_session()
    migrate_mod.migrate_trek(t)
    assert len(t["members"]) == 1
    m = t["members"][0]
    assert m["session_id"] == "sv-leader-1"
    assert m["user_id"] == "u-1"
    assert m["role"] == "leader"
    assert m["email"] == "leader@x"


# ---------------------------------------------------------------------------
# Multi-session: leader user with 3 sessions → 1 leader + 2 member
# ---------------------------------------------------------------------------

def test_migrate_multi_session_expands_to_one_leader_plus_n_members():
    t = _trek_with_multi_session()
    migrate_mod.migrate_trek(t)
    by_role: dict[str, list[dict]] = {"leader": [], "member": []}
    for m in t["members"]:
        by_role[m["role"]].append(m)
    assert len(by_role["leader"]) == 1
    assert len(by_role["member"]) == 2
    # leader_session_id に一致する 1 つのみ leader、 他は member。
    assert by_role["leader"][0]["session_id"] == "sv-leader-1"
    member_sids = {m["session_id"] for m in by_role["member"]}
    assert member_sids == {"sv-leader-2", "sv-leader-3"}


# ---------------------------------------------------------------------------
# Backup is taken before mutation
# ---------------------------------------------------------------------------

def test_migrate_calls_backup_legacy_members():
    t = _trek_with_single_session()
    assert t["members_legacy_backup"] == []
    migrate_mod.migrate_trek(t)
    # backup には migration 直前の user_id keyed entries が入っている。
    assert len(t["members_legacy_backup"]) == 1
    backup_entry = t["members_legacy_backup"][0]
    assert backup_entry["user_id"] == "u-1"
    assert backup_entry["role"] == "leader"
    # backup には session_id field は無い (= user_id keyed のまま)。
    assert "session_id" not in backup_entry


# ---------------------------------------------------------------------------
# migration_phase is stamped
# ---------------------------------------------------------------------------

def test_migrate_stamps_meta_migration_phase_A():
    t = _trek_with_single_session()
    migrate_mod.migrate_trek(t)
    assert trek.get_migration_phase(t) == "A"
    assert t["meta"]["migration_phase"] == "A"


# ---------------------------------------------------------------------------
# Idempotency: refuses double migration
# ---------------------------------------------------------------------------

def test_migrate_refuses_double_migration():
    t = _trek_with_single_session()
    migrate_mod.migrate_trek(t)
    # 2 回目は phase == "A" なので ValueError で拒否される。
    with pytest.raises(ValueError, match="phase"):
        migrate_mod.migrate_trek(t)


# ---------------------------------------------------------------------------
# Reverse: revert restores original members[] and clears backup
# ---------------------------------------------------------------------------

def test_revert_restores_original_members_and_clears_backup():
    t = _trek_with_multi_session()
    original_members = [dict(m) for m in t["members"]]
    migrate_mod.migrate_trek(t)
    assert t["members"] != original_members  # migrated shape
    revert_mod.revert_trek(t)
    assert t["members"] == original_members
    assert t["members_legacy_backup"] == []
    assert trek.get_migration_phase(t) == "pre-A"


def test_revert_fails_when_no_backup():
    t = _trek_with_single_session()
    # backup field が空 (= まだ migrate されていない) なら復元元無し。
    with pytest.raises(ValueError):
        revert_mod.revert_trek(t)
