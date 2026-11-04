"""同じ「共有作業部屋の参加者か」判定が 2 箇所にあることを突き合わせる
(ms-185 e-7069 / 独立保守性レビューの medium)。

**なぜこのファイルが要るか**: 「送り手と受け手が同じ作業部屋の参加者か」を
判定するコードが 2 箇所にある。

  * ``lib/trek.build_shared_trek_lookup_from_lists`` — 正本。
    ``server/routers_treks.py`` がこれを直接呼ぶ。
  * ``lib/cmd_bus._trek_has_joined_member`` — CLI 側 (``_is_trek_internal_send``
    = 送信予算の迂回判定) の独自実装。同じ規則 (phase-A は session 粒度 /
    pre-A は user_id 粒度 / halt と非 active は除外) を書き直している。

ms-185 e-7068 で正本を ``server/dm_gate.py`` から ``lib/trek.py`` へ移したとき、
CLI 側は **docstring だけ**を「判定は lib/trek... と同じ」に書き換えて実装は
寄せなかった。両者を突き合わせる機械的な検査も無かったので、次に粒度の境界を
直す人が片方だけ直して「直した」と判断できる状態だった。しかも CLI 側は予算の
最適化なので、ズレても機能は壊れず、grep で気付かれるまで黙って drift する。

**実装を 1 本に寄せないのは意図**: この重複ごと e-7070 (Trek 本体の削除) で
消える予定なので、寄せ替えは捨てる作業になる。代わりに「消えるまでの間、
黙ってズレない」ことだけをここで固定する。e-7070 でこのファイルも一緒に消える。

測るのは 2 点:
  1. **phase-A (session 粒度) では両者が完全に一致する** — 現役の粒度なので
     ここがズレたら本物の drift。
  2. **pre-A (legacy の user_id 粒度) では CLI 側が正本より厳しい側に寄る** —
     正本は送り手側の一覧を引いている前提で受け手だけを照合するが、CLI 側は
     送り手も members[] に居ることを要求する。等価ではないので等価を主張せず、
     **「CLI 側が正本より緩くなることはない」** という向きを固定する
     (緩い側に転ぶと予算の迂回が広がる = 危険側)。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import pytest  # noqa: E402

import cmd_bus  # noqa: E402
import trek as trek_mod  # noqa: E402

SENDER_UID = "u-sender"
RECV_UID = "u-recv"
SENDER_SID = "sv-sender"
RECV_SID = "sv-recv"
# 第三者を creator にして、正本側の「creator == 受け手なら成立」特例を
# 判定から外す (その特例は members[] の照合とは別の軸なので、ここで混ぜると
# 何がズレたのか読めなくなる)。
OTHER_UID = "u-someone-else"


def _trek(members, *, phase_a: bool, status: str = "active", halt=None) -> dict:
    """phase-A / pre-A の trek を作る。

    フェーズは ``meta.migration_phase`` が決める (``get_migration_phase`` →
    ``is_session_id_keyed``)。members[] が session_id を持つかどうかでは**ない** —
    最初に書いたこの helper は members[] の形から推測しており、phase-A を
    意図した全ケースが実際には pre-A に落ちて 64/65 が「間違った理由で緑」に
    なっていた。フェーズの真値源を本番コードから取る (``set_migration_phase``)
    ので、フェーズの決め方が変わればこの helper も一緒に動く。
    """
    rows = []
    for m in members:
        row = {"user_id": m["uid"]}
        if phase_a:
            row["session_id"] = m.get("sid", "")
        rows.append(row)
    trek = {
        "trek_id": "trek-parity",
        "status": status,
        "members": rows,
        "creator_actor": {"user_id": OTHER_UID},
    }
    if phase_a:
        trek_mod.set_migration_phase(trek, "A")
    if halt is not None:
        trek["halt"] = halt
    return trek


def _canonical_verdict(trek: dict) -> bool:
    """正本の判定 (= server/routers_treks.py が呼んでいる形そのまま)。"""
    lookup = trek_mod.build_shared_trek_lookup_from_lists(lambda _uid: [trek])
    matched, _trek_id = lookup(SENDER_UID, RECV_UID, SENDER_SID, RECV_SID)
    return bool(matched)


def _cli_verdict(trek: dict) -> bool:
    """CLI 側の判定を、``_is_trek_internal_send`` と同じ順序で組み立てる。

    ネットワークに触る部分 (身元解決 / セッション一覧) は
    ``_is_trek_internal_send`` の責務なので、ここでは判定の芯だけを
    同じ合成順で呼ぶ。合成順を変えたらこの関数も直すことになる。
    """
    if (trek.get("status") or "") != "active":
        return False
    if trek.get("halt"):
        return False
    members = trek.get("members") or []
    phase_a = trek_mod.is_session_id_keyed(trek)
    am_member = cmd_bus._trek_has_joined_member(
        members, phase_a=phase_a, session_id=SENDER_SID, user_id=SENDER_UID)
    if not am_member:
        return False
    return cmd_bus._trek_has_joined_member(
        members, phase_a=phase_a, session_id=RECV_SID, user_id=RECV_UID)


# members[] の形を網羅する。phase-A では session_id の有無が「招待されただけ /
# 参加済み」の境界そのものなので、sid を落とした行 (= 招待の placeholder) を
# 必ず含める。
MEMBER_SHAPES = [
    pytest.param([], id="members-empty"),
    pytest.param([{"uid": SENDER_UID, "sid": SENDER_SID}], id="sender-only"),
    pytest.param([{"uid": RECV_UID, "sid": RECV_SID}], id="recipient-only"),
    pytest.param([{"uid": SENDER_UID, "sid": SENDER_SID},
                  {"uid": RECV_UID, "sid": RECV_SID}], id="both-joined"),
    pytest.param([{"uid": SENDER_UID, "sid": SENDER_SID},
                  {"uid": RECV_UID, "sid": ""}], id="recipient-invited-only"),
    pytest.param([{"uid": SENDER_UID, "sid": ""},
                  {"uid": RECV_UID, "sid": RECV_SID}], id="sender-invited-only"),
    pytest.param([{"uid": OTHER_UID, "sid": "sv-other"}], id="third-party-only"),
    pytest.param([{"uid": SENDER_UID, "sid": SENDER_SID},
                  {"uid": RECV_UID, "sid": RECV_SID},
                  {"uid": OTHER_UID, "sid": "sv-other"}], id="both-plus-third"),
]

STATE_SHAPES = [
    pytest.param("active", None, id="active"),
    pytest.param("active", "leader pulled the cord", id="active-but-halted"),
    pytest.param("planning", None, id="planning"),
    pytest.param("archived", None, id="archived"),
]


@pytest.mark.parametrize("members", MEMBER_SHAPES)
@pytest.mark.parametrize("status,halt", STATE_SHAPES)
def test_phase_a_rule_is_identical_in_both_implementations(members, status, halt):
    """現役の粒度 (session) では 2 実装が完全に一致する。

    片方だけ境界を直したらここが赤くなる = 黙った drift が起きない。
    """
    trek = _trek(members, phase_a=True, status=status, halt=halt)
    assert _cli_verdict(trek) == _canonical_verdict(trek), (
        f"phase-A の判定がズレた (members={members}, status={status}, "
        f"halt={halt}): CLI={_cli_verdict(trek)} / "
        f"正本={_canonical_verdict(trek)}。"
        f"lib/cmd_bus._trek_has_joined_member と "
        f"lib/trek.build_shared_trek_lookup_from_lists のどちらかだけを直すと "
        f"送信予算の迂回が静かにズレる。両方直すか、CLI 側を正本に寄せること。")


@pytest.mark.parametrize("members", MEMBER_SHAPES)
@pytest.mark.parametrize("status,halt", STATE_SHAPES)
def test_pre_a_cli_is_never_looser_than_the_canonical_rule(members, status, halt):
    """legacy の粒度では等価を主張せず、CLI が緩くならないことだけを固定する。

    正本は送り手側の一覧を引いている前提で **受け手だけ** を members[] と
    照合する。CLI 側は送り手も居ることを要求するので厳しい側。等価だと書くと
    嘘になるので、向き (CLI ⊆ 正本) を測る。緩い側に転ぶと予算の迂回が
    広がるので、そこが危険側。
    """
    trek = _trek(members, phase_a=False, status=status, halt=halt)
    cli = _cli_verdict(trek)
    canonical = _canonical_verdict(trek)
    assert not (cli and not canonical), (
        f"pre-A で CLI 側が正本より緩くなった (members={members}, "
        f"status={status}, halt={halt})。CLI={cli} / 正本={canonical}。"
        f"送信予算の迂回が正本の許す範囲を超える = 危険側のズレ。")


def test_the_two_implementations_are_both_still_here():
    """突き合わせる相手が居ることを確かめる (= この検査が空振りしない)。

    どちらかが e-7070 で消えたら、このファイルも一緒に消すのが正しい。
    消えたのに検査だけ残ると、何も測っていない緑になる。
    """
    assert hasattr(cmd_bus, "_trek_has_joined_member"), (
        "lib/cmd_bus._trek_has_joined_member が無い。e-7070 で消したなら "
        "このファイルも一緒に消すこと (何も測らない緑を残さない)。")
    assert hasattr(trek_mod, "build_shared_trek_lookup_from_lists"), (
        "lib/trek.build_shared_trek_lookup_from_lists が無い。e-7070 で "
        "消したならこのファイルも一緒に消すこと。")
