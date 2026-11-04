"""ms-97 / e-2659 Phase 3 (AC18) — 共有 Trek メンバー判定の session 粒度試験。

**ms-185 e-7068 の変更**: この判定はもともと ``dm_gate`` (= 確認ゲートの判定器)
の bypass 根拠だったが、その規則は削除され、判定自体は ``lib/trek`` へ移った
(= 予算の最適化という別用途でまだ使われている)。以下の AC18 は **判定の正しさ**
として読む — 「在籍が証明できたら確認を省く」という結論は、もう続かない。


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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dm_gate  # noqa: E402
import trek as trek_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trek_phase_a(*, trek_id: str, members: list[dict]) -> dict:
    """Phase A trek doc shape (= meta.migration_phase = 'A').

    members[] entries are session_id keyed: each carries ``session_id`` and
    ``user_id`` (the latter is informational; the gate ignores it for the
    grain check in phase A+). Presence of ``session_id`` ⟺ joined (an invite
    placeholder has no session_id), so no separate ``joined_at`` is needed.
    """
    return {
        "trek_id": trek_id,
        "status": "active",
        "creator_actor": {
            "user_id": members[0].get("user_id") if members else "",
        },
        "members": members,
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
        "members": members,
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup(
        "uid-alice", "uid-bob", "sv-alice-1", "sv-bob-1",
    )
    assert matched is True
    assert trek_id == "tk-cross"


def test_phase_a_membership_no_longer_reaches_the_judge():
    """ms-185 e-7068 — session 粒度で在籍が証明できても、判定器には届かない。

    以前はこの組み合わせ (= 両 session_id が同じ active trek の members[] に
    在籍) が確認ゲートの bypass 根拠だった。判定器から規則ごと削除したので、
    **在籍の証明が立っていても** 別人宛の action 付き DM は確認を要する。

    下の在籍判定そのもの (= 移設先 ``lib/trek``) はこのファイルが測り続ける:
    予算の最適化という別の用途でまだ使われているため。**在籍が真であることと、
    確認が省かれることが、もう繋がっていない**ことをここで固定する。
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    # 在籍は真 (= 判定そのものは生きている)
    assert lookup("uid-alice", "uid-bob", "sv-a", "sv-b") == (True, "tk-e2e")
    # それでも確認ゲートは通らない
    should_gate, reason = dm_gate.should_gate_dm_action(
        sender_user_id="uid-alice",
        receiver_user_id="uid-bob",
        actions_authorized=["commit"],
    )
    assert should_gate is True
    assert reason == dm_gate.GATE_REASON_CROSS_USER_ACTION


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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
        lambda uid: treks_by_uid.get(uid, [])
    )
    matched, trek_id = lookup(
        "uid-alice", "uid-bob", "sv-alice-NOT-in-A", "sv-bob-1",
    )
    # Phase A+ fails (sender_sid mismatch) but pre-A matches user_id.
    assert matched is True
    assert trek_id == "tk-legacy"


def test_the_convenience_probe_is_gone_with_its_rule():
    """``dm_gate.shared_trek_member`` は規則と一緒に消えた (ms-185 e-7068)。

    AC18 当時この helper は「2 つの session_id が同じ active phase A+ trek に
    居るか」を 1 行で問うための便利関数で、**呼び出し元はテストだけ**だった
    (本番コードからの参照ゼロを実測して消した)。規則を消すときに helper だけ
    残すと、誰も呼ばない判定が 1 つ生き続ける。判定本体は ``lib/trek`` 側に
    移してあり、そちらは実際の呼び出し元 (予算の最適化) と一緒に生きている。
    """
    assert not hasattr(dm_gate, "shared_trek_member")
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
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
    lookup = trek_mod.build_shared_trek_lookup_from_lists(
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

    lookup = trek_mod.build_shared_trek_lookup_from_lists(_live_list)
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
