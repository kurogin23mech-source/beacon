"""ms-97 / e-2659 Phase 3 (AC18) — dm_gate session-grain authorization tests.

AC18: dm_gate (= cross-user DM 承認 judge) は phase A+ trek (= members[]
が session_id keyed に cutover 済) の bypass 判定で sender + recipient の
両 session_id が同 trek の members[] に在籍することを **両方** 確認する。

これで以下の構造保証が立つ:
  * 同 user の trek 未参加 session (= fork / 別 worktree で別 session_id
    が生えた状況) は無条件 bypass されず、 正規の cross-user gate を通る。
  * pre-A trek (= 旧 user_id keyed legacy) は従来通り user_id grain で
    判定する (= 既存 dogfood path は不変)。
  * G3 のため、 lookup の戻り値は ``(matched: bool, trek_id: str)`` の
    tuple であり、 bypass を granted した trek の id が audit log に
    propagate できる。
  * G4 のため、 lookup は呼ぶ度に list_treks_for_user を fresh fetch
    する (= cache 無し)。

Positive tests:
  * phase A+ trek で 2 つの session_id が members[] に居る → bypass。
  * pre-A trek で user_id 一致 → bypass (= legacy compat)。
  * phase A+ + phase pre-A 混在で phase A+ 側のみで bypass。

Negative tests:
  * phase A+ trek で sender_sid が members[] 不在 → no bypass。
  * phase A+ trek で recipient_sid が members[] 不在 → no bypass。
  * 同 user の別 session で trek 未参加 → no bypass (= AC18 核心)。
  * sender_sid / receiver_sid 空 + phase A+ → no bypass。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dm_gate  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_JOINED_AT = "2026-06-18T00:00:00.000000Z"


def _with_joined_at(members: list[dict]) -> list[dict]:
    """Default every member to *joined* unless it explicitly sets ``joined_at``.

    e-4116 follow-up (PR #491 parent review 1): the shared-Trek bypass now
    requires ``joined_at`` (an invited-but-not-joined session must NOT grant
    bypass). These fixtures represent real, joined members, which always carry
    ``joined_at`` in production — so inject the default here. A test that wants
    an invited-but-not-joined member sets ``joined_at`` to "" explicitly.
    """
    out = []
    for m in members:
        if "joined_at" not in m:
            m = {**m, "joined_at": _DEFAULT_JOINED_AT}
        out.append(m)
    return out


def _trek_phase_a(*, trek_id: str, members: list[dict]) -> dict:
    """Phase A trek doc shape (= meta.migration_phase = 'A').

    members[] entries are session_id keyed: each carries ``session_id`` and
    ``user_id`` (the latter is informational; the gate ignores it for the
    grain check in phase A+).
    """
    return {
        "trek_id": trek_id,
        "status": "active",
        "creator_actor": {
            "user_id": members[0].get("user_id") if members else "",
        },
        "members": _with_joined_at(members),
        "meta": {"migration_phase": "A"},
    }


def _trek_pre_a(*, trek_id: str, members: list[dict]) -> dict:
    """Pre-A (= legacy) trek doc shape — no meta.migration_phase set.

    members[] entries are user_id keyed; no session_id field present.
    """
    return {
        "trek_id": trek_id,
        "status": "active",
        "creator_actor": {
            "user_id": members[0].get("user_id") if members else "",
        },
        "members": _with_joined_at(members),
        # NB: meta intentionally absent so get_migration_phase returns
        # the default "pre-A".
    }


# ---------------------------------------------------------------------------
# Positive: phase A+ session-grain bypass
# ---------------------------------------------------------------------------

def test_phase_a_bypass_when_both_session_ids_in_members():
    """Phase A+ trek + sender_sid + recipient_sid both in members[] → bypass."""
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-cross",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-alice-1",
                     "role": "leader"},
                    {"user_id": "uid-bob", "session_id": "sv-bob-1",
                     "role": "member"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup(
        "uid-alice", "uid-bob", "sv-alice-1", "sv-bob-1",
    )
    assert matched is True
    assert trek_id == "tk-cross"


def test_phase_a_bypass_judge_full_pipeline():
    """End-to-end: should_gate_dm_action with phase A+ lookup.

    Confirms the 3-tuple return + audit-log-shaped trek_id propagation.
    """
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-e2e",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-a",
                     "role": "leader"},
                    {"user_id": "uid-bob", "session_id": "sv-b",
                     "role": "member"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    should_gate, reason, trek_id = dm_gate.should_gate_dm_action(
        sender_user_id="uid-alice",
        receiver_user_id="uid-bob",
        actions_authorized=["commit"],
        shared_trek_lookup=lookup,
        sender_session_id="sv-a",
        receiver_session_id="sv-b",
    )
    assert should_gate is False
    assert reason == dm_gate.GATE_REASON_SHARED_TREK
    assert trek_id == "tk-e2e"


def test_phase_pre_a_bypass_still_user_grain():
    """pre-A trek must keep legacy user_id grain bypass (back-compat)."""
    treks_by_uid = {
        "uid-alice": [
            _trek_pre_a(
                trek_id="tk-legacy",
                members=[
                    {"user_id": "uid-alice", "role": "leader"},
                    {"user_id": "uid-bob", "role": "member"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    # No session ids passed — pre-A grain still resolves bypass on user
    # ids alone.
    matched, trek_id = lookup("uid-alice", "uid-bob")
    assert matched is True
    assert trek_id == "tk-legacy"


def test_mixed_phase_a_and_pre_a_only_phase_a_grants_session_grain():
    """phase A+ + pre-A 混在の treks リストで session_grain bypass が
    phase A+ entry でのみ成立する。

    本ケース: phase A+ trek の members[] には sender_sid が居らず、
    pre-A trek の方には居る (= user_id grain で見れば共有)。 sender / recv
    の session id を渡しても、 phase A+ は session-grain mismatch で fall
    through し、 pre-A は user_id grain で成立する。
    """
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-phaseA",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-other",
                     "role": "leader"},
                    {"user_id": "uid-bob", "session_id": "sv-bob-1",
                     "role": "member"},
                ],
            ),
            _trek_pre_a(
                trek_id="tk-legacy",
                members=[
                    {"user_id": "uid-alice", "role": "leader"},
                    {"user_id": "uid-bob", "role": "member"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup(
        "uid-alice", "uid-bob", "sv-alice-NOT-in-A", "sv-bob-1",
    )
    # Phase A+ fails (sender_sid mismatch) but pre-A matches user_id.
    assert matched is True
    assert trek_id == "tk-legacy"


def test_shared_trek_member_helper_phase_a_path():
    """``dm_gate.shared_trek_member`` convenience helper for AC18 test
    surface (= the upstream Skill / CLI callers ask this question with
    pure session ids)."""
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-helper",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-1"},
                    {"user_id": "uid-bob", "session_id": "sv-2"},
                ],
            ),
        ],
    }
    assert dm_gate.shared_trek_member(
        sender_session_id="sv-1",
        recipient_session_id="sv-2",
        list_treks_for_user=lambda uid: treks_by_uid.get(uid, []),
        sender_user_id="uid-alice",
        recipient_user_id="uid-bob",
    ) is True


# ---------------------------------------------------------------------------
# Negative: phase A+ strict session-grain rejection
# ---------------------------------------------------------------------------

def test_phase_a_no_bypass_when_sender_session_absent():
    """Phase A+ trek で sender の session_id が members[] に居ない → no bypass。"""
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-x",
                members=[
                    # alice の正式 session は sv-alice-MAIN だが、 sender が
                    # 別 fork の sv-alice-FORK から DM を投げてくる状況。
                    {"user_id": "uid-alice", "session_id": "sv-alice-MAIN"},
                    {"user_id": "uid-bob", "session_id": "sv-bob"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, _ = lookup(
        "uid-alice", "uid-bob", "sv-alice-FORK", "sv-bob",
    )
    assert matched is False


def test_phase_a_no_bypass_when_recipient_session_absent():
    """Symmetric: recipient_sid が members[] 不在 → no bypass。"""
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-y",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-a"},
                    {"user_id": "uid-bob", "session_id": "sv-bob-MAIN"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, _ = lookup(
        "uid-alice", "uid-bob", "sv-a", "sv-bob-FORK",
    )
    assert matched is False


def test_phase_a_no_bypass_for_same_user_fork_session():
    """AC18 核心: 同 user の別 session (= trek 未登録) は bypass されない。

    シナリオ: alice が trek に sv-main で参加した。 並行で別 worktree から
    sv-fork を生やして bob に DM を投げる。 phase A+ では members[] に
    sv-fork が居ない限り、 sender_user_id は同 user でも bypass されない。
    (= ms-70 cross-user gate のフォールバックに乗る)
    """
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-z",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-main"},
                    {"user_id": "uid-bob", "session_id": "sv-bob"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup(
        "uid-alice", "uid-bob", "sv-fork", "sv-bob",
    )
    assert matched is False
    assert trek_id == ""


def test_phase_a_no_bypass_when_session_ids_blank():
    """Phase A+ で session id が空 → bypass 判定不能 → False。"""
    treks_by_uid = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-blank",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-a"},
                    {"user_id": "uid-bob", "session_id": "sv-b"},
                ],
            ),
        ],
    }
    lookup = dm_gate.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, _ = lookup("uid-alice", "uid-bob", "", "sv-b")
    assert matched is False
    matched2, _ = lookup("uid-alice", "uid-bob", "sv-a", "")
    assert matched2 is False


# ---------------------------------------------------------------------------
# G4: live check structural guarantee
# ---------------------------------------------------------------------------

def test_lookup_invokes_list_treks_for_user_fresh_each_call():
    """G4: lookup は呼ぶ度に list_treks_for_user を fresh fetch する。

    シナリオ: 1 回目は trek 共有 → bypass、 2 回目は trek 解除済 → no bypass
    という同 instance での 2 連続呼び出しで、 結果が変わることを観測する
    (= cache すると 2 回目も True になる病理を検出)。
    """
    call_count = {"n": 0}
    live_treks = {
        "uid-alice": [
            _trek_phase_a(
                trek_id="tk-live",
                members=[
                    {"user_id": "uid-alice", "session_id": "sv-a"},
                    {"user_id": "uid-bob", "session_id": "sv-b"},
                ],
            ),
        ],
    }

    def _live_list(uid: str) -> list[dict]:
        call_count["n"] += 1
        return live_treks.get(uid, [])

    lookup = dm_gate.build_shared_trek_lookup_from_lists(_live_list)
    matched_1, _ = lookup("uid-alice", "uid-bob", "sv-a", "sv-b")
    assert matched_1 is True

    # Now bob is removed from the trek between calls.
    live_treks["uid-alice"] = [
        _trek_phase_a(
            trek_id="tk-live",
            members=[
                {"user_id": "uid-alice", "session_id": "sv-a"},
                # bob removed
            ],
        ),
    ]
    matched_2, _ = lookup("uid-alice", "uid-bob", "sv-a", "sv-b")
    assert matched_2 is False
    # G4: list_treks was called at least once per invocation (= no cache).
    assert call_count["n"] >= 2
