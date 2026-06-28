"""Red tests for AC18 (= dm_gate session-grain authorization) — ms-97 / e-2659.

AC18: dm_gate (= cross-user DM 承認 judge) が member 判定を session_id grain
で行うこと。 現状の `shared_trek_member` は user_id 一致だけを見るので、
同 user の別 session が無条件で bypass されている。 AC18 land 後は同 user
でも未登録 session は bypass されず、 trek の `members[]` (= session_id
keyed) に居る session_id のみ bypass される。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.mark.xfail(reason="AC18 not yet implemented (= dm_gate still user_id grain)")
def test_dm_gate_bypass_only_for_registered_session_id():
    """member.session_id 一致した DM のみ bypass、 同 user の別 sid は denied。"""
    import dm_gate  # noqa: E402

    # Phase 3: dm_gate.shared_trek_member は (sender_session_id,
    # recipient_session_id) を受け取り、 trek.members[] 内に両者の
    # session_id が居る時のみ True を返す。 現状は user_id grain なので
    # 同 user 別 sid でも True が返ってしまうことを観測する。
    sender_sid = "sv-leader-1"
    other_sid = "sv-other-1"  # 同 user の別 session、 trek 未登録
    result = dm_gate.shared_trek_member(  # type: ignore[attr-defined]
        sender_session_id=sender_sid,
        recipient_session_id=other_sid,
    )
    assert result is False


@pytest.mark.xfail(reason="AC18 not yet implemented (= judge signature still user_id keyed)")
def test_dm_gate_judge_carries_session_id_in_decision():
    """judge() が返す decision に session_id field が含まれる契約。"""
    # Phase 3 で dm_gate.judge_dm の返り値 dict が `sender_session_id`
    # と `recipient_session_id` を必ず持つ (= audit log に session grain
    # で追跡可能にする)。
    assert False, "AC18 implementation pending"
