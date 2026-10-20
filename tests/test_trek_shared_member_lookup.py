"""共有 Trek メンバー判定の単体試験 — ms-185 e-7068 で ``server/dm_gate.py`` から
``lib/trek.py`` へ移設した判定を、移設前と同じ条件で測り続ける。

**なぜ残すのか**: この判定はもともと「送信者と受信者が同じ Trek の参加者なら
人間の確認を省く」規則の裏付けだった。その規則は ms-185 で削除されたので、
人間の確認ゲートからは誰もこれを呼んでいない。それでも判定自体は Trek の 1 経路
(``GET /api/system/trek-internal-send`` = 同じ Trek の参加者同士の返信は自動返信の
予算を消費しない、という client 側の最適化) がまだ使っているので、**移設しただけ
では試験が宙に浮く**。移設先の module を指して同じ条件を測り続ける。

この経路と呼び出し側 (``channel/bus.mjs``) は e-7080 で一緒に落ちる。そのとき
この判定とこのファイルも一緒に消える。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import trek as trek_mod  # noqa: E402

# ---------------------------------------------------------------------------
# AC (e): build_shared_trek_lookup_from_lists walks creator + members
# ---------------------------------------------------------------------------

def test_shared_trek_lookup_detects_receiver_in_members_list():
    """Receiver is one of the trek's listed members → lookup returns True."""
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-aaa",
                "creator_actor": {"user_id": "user-alice"},
                "status": "active",
                "members": [
                    {"user_id": "user-alice", "role": "leader",
                     "joined_at": "2026-06-18T00:00:00Z"},
                    {"user_id": "user-bob", "role": "member",
                     "joined_at": "2026-06-18T00:00:00Z"},
                ],
            }
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup("user-alice", "user-bob")
    assert matched is True
    assert trek_id == "tk-aaa"
    # Receiver NOT in the trek → False.
    matched_neg, _ = lookup("user-alice", "user-charlie")
    assert matched_neg is False


def test_shared_trek_lookup_detects_receiver_as_creator():
    """Receiver is the trek's creator (= role=leader stamped via creator_actor)
    → lookup returns True even if the members list does not duplicate them."""
    # Sender is a member of a trek created by receiver.
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-bbb",
                "creator_actor": {"user_id": "user-bob"},  # receiver
                "status": "active",
                "members": [
                    {"user_id": "user-alice", "role": "member",
                     "joined_at": "2026-06-18T00:00:00Z"},
                ],
            }
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup("user-alice", "user-bob")
    assert matched is True
    assert trek_id == "tk-bbb"


def test_shared_trek_lookup_skips_halted_trek():
    """ms-97 / e-2612 (AC32) — halted treks must not grant DM bypass.

    Leader pulled the Andon cord → autonomous cross-user action exchange
    must fall back to the normal cross-user gate. Even if sender +
    receiver share the trek's members[], the halt flag suspends the
    bypass for the duration of the halt.
    """
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-halted",
                "creator_actor": {"user_id": "user-alice"},
                "members": [
                    {"user_id": "user-alice", "role": "leader"},
                    {"user_id": "user-bob", "role": "member"},
                ],
                "halt": {"issued_by_session_id": "sv-alice"},
                "status": "active",
            }
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    # Halt set → no bypass even though both users are in members[].
    matched, _ = lookup("user-alice", "user-bob")
    assert matched is False


def test_shared_trek_lookup_skips_archived_trek():
    """Archived treks no longer grant DM bypass (defence in depth alongside
    AC32 halt skip). Caller-side filter already hides archived by default
    but the lookup itself must not depend on the filter being applied."""
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-archived",
                "creator_actor": {"user_id": "user-alice"},
                "members": [
                    {"user_id": "user-alice"},
                    {"user_id": "user-bob"},
                ],
                "status": "archived",
            }
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, _ = lookup("user-alice", "user-bob")
    assert matched is False


def test_shared_trek_lookup_multi_trek_only_one_active_still_grants_bypass():
    """ms-97 / e-2612 (AC32) — if the user shares multiple treks and at
    least one is non-halted + non-archived, bypass still applies. Halt
    only suspends bypass for the specific halted trek, not blanket.
    """
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-halted",
                "creator_actor": {"user_id": "user-alice"},
                "members": [
                    {"user_id": "user-alice"},
                    {"user_id": "user-bob"},
                ],
                "halt": {"issued_by_session_id": "sv-alice"},
                "status": "active",
            },
            {
                "trek_id": "tk-active",
                "creator_actor": {"user_id": "user-alice"},
                "members": [
                    {"user_id": "user-alice",
                     "joined_at": "2026-06-18T00:00:00Z"},
                    {"user_id": "user-bob",
                     "joined_at": "2026-06-18T00:00:00Z"},
                ],
                "status": "active",
            },
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    # tk-active grants bypass despite tk-halted being halted.
    matched, trek_id = lookup("user-alice", "user-bob")
    assert matched is True
    assert trek_id == "tk-active"


def test_shared_trek_lookup_skips_planning_trek():
    """e-4116 follow-up (PR #491 parent review 1): a non-active (e.g.
    ``planning``) trek must NOT grant bypass — it is not yet a live
    pre-approved work scope. Previously only ``archived`` was excluded."""
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-planning",
                "creator_actor": {"user_id": "user-alice"},
                "status": "planning",
                "members": [
                    {"user_id": "user-alice",
                     "joined_at": "2026-06-18T00:00:00Z"},
                    {"user_id": "user-bob",
                     "joined_at": "2026-06-18T00:00:00Z"},
                ],
            }
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, _ = lookup("user-alice", "user-bob")
    assert matched is False


def test_shared_trek_lookup_skips_invited_not_joined_member_phase_a():
    """e-4116 follow-up (PR #491 parent re-review): an invited-but-not-joined
    member does NOT grant bypass — and the session_id grain is what enforces it.

    Per ``trek.accept_invitation``, an invite placeholder carries NEITHER a
    session_id NOR a joined_at; joining writes both together. So a phase-A
    invite placeholder (no session_id) simply never matches the requester's
    session_id, and the bypass is denied structurally — no separate joined_at
    check needed. Here user-bob was invited (placeholder, no session_id) but
    the DM targets session sv-bob, which is not in members[]."""
    treks_by_uid = {
        "user-alice": [
            {
                "trek_id": "tk-invited",
                "creator_actor": {"user_id": "user-alice"},
                "status": "active",
                "meta": {"migration_phase": "A"},
                "members": [
                    {"user_id": "user-alice", "session_id": "sv-alice",
                     "role": "leader", "joined_at": "2026-06-18T00:00:00Z"},
                    # user-bob invited but never joined: no session_id, joined_at "".
                    {"user_id": "user-bob", "role": "member",
                     "invited_at": "2026-06-18T00:00:00Z", "joined_at": ""},
                ],
            }
        ],
    }
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, _ = lookup("user-alice", "user-bob", "sv-alice", "sv-bob")
    assert matched is False


def test_shared_trek_lookup_empty_user_ids_return_false():
    """Blank sender or receiver → cannot be in any shared Trek.

    The dispatcher passes blank strings when session→user resolution
    fails. The lookup must not crash and must return False so the
    judge falls through to the no_actions or cross-user-action path.
    """
    lookup = trek_mod.build_shared_trek_lookup_from_lists(lambda uid: [])
    assert lookup("", "user-bob")[0] is False
    assert lookup("user-alice", "")[0] is False
    assert lookup("", "")[0] is False
