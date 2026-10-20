"""引退した「サーバが発行する channel」の古い opt-in が、署名なしの自動実行を通さないことを固定する
(ms-185 e-7077)。

**守っている穴 (2026-10-19 に実測で再現)**

自動実行の判定 ``lib/bus_delivery.classify_auto_execute`` は 2 つを独立に見る:

  1. その channel をプロジェクトが明示的に opt-in したか (``bus_auto_execute_channels``)
  2. その channel が「人間が許可した証拠」を要求する側で、届いた event にその証拠が在るか
     (``SYSTEM_PROVENANCE_CHANNELS`` + ``has_required_provenance``)

     ms-185 e-7078 で、要求する証拠は channel ごとに違う形になった
     (``PROVENANCE_REQUIREMENTS``)。operation-trigger が求めるのは「人間が
     ``beacon operation approve`` で承認した封筒 (T2) が、いま発火する op_id を
     名指している」こと。かつては Trek だけが発行できるサーバ署名を求めており、
     Operation は一度もそれを満たせていなかった。

2 の検査は **1 に入っている channel のうち、サーバ発行と宣言されたものだけ**に掛かる。これは
意図的な設計で、「人間が opt-in した独自 channel は署名なしでも信頼する」(= opt-in そのものが
人間の同意) を表す。``test_bus_delivery.py::test_non_provenance_channel_kept_without_envelope``
が固定している。

ところがこの設計は、**channel を ``SYSTEM_PROVENANCE_CHANNELS`` から外したのに、どこかの
プロジェクトの許可リストにその channel の古い opt-in が残っている**とき破れる。その channel は
「署名が必要」から「opt-in だけで信頼」へ**黙って階級が変わり**、署名の無い
``delivery=auto-execute`` が実行される。

実測 (ms-185 で Trek を畳む準備中に発見):

    許可リスト = [operation-trigger, trek-trigger, ...]   ← _arm_for_trek が書いた古い opt-in
    provenance から trek-* を外す                          ← 畳む作業でやること
    → classify_auto_execute({channel: trek-trigger, delivery: auto-execute}, 署名なし)
      = ('auto-execute', '', '')                           ← 無確認で実行される

だから provenance channel の引退は **両側の操作**になる:

  * コード側 = ``RETIRED_PROVENANCE_CHANNELS`` に移す (許可リストに在っても降格し続ける)
  * データ側 = 各プロジェクトの許可リストから古い opt-in を外す

このファイルはコード側を固定する。**名前を ``SYSTEM_PROVENANCE_CHANNELS`` から消すだけで、
``RETIRED_PROVENANCE_CHANNELS`` に足さなければ赤くなる** — それがこのガードの要点で、
``scripts/invert-retired-provenance-guard.py`` が「壊したら本当に赤くなるか」を実測する。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import bus_delivery as bd  # noqa: E402

# サーバが発行する channel の「族」。ここに挙げた名前は、SYSTEM_PROVENANCE_CHANNELS か
# RETIRED_PROVENANCE_CHANNELS の **どちらかに必ず**居なければならない。
#
# なぜ手で列挙するのか: 「現在の SYSTEM_PROVENANCE_CHANNELS の中身」を期待値にすると、
# 名前を消したときに期待値も一緒に消えてテストが素通りする (= 自分を検証できないガードに
# なる)。族の名前は仕様であってコードの写しではないので、ここに固定して置く。
#
# 新しい gated channel を増やしたら、ここにも足す (足し忘れると
# test_every_known_gated_channel_is_classified が赤くなるので気付ける)。
# ms-185 e-7078: "server-minted" was renamed to "gated" in this comment because
# operation-trigger is no longer gated on a SERVER signature — its proof is the
# envelope a human approved. The family membership rule is unchanged: a gated name
# must sit in exactly one of the two sets.
KNOWN_GATED_CHANNELS = frozenset({
    "operation-trigger",
    "trek-trigger",
    "trek-progress-check",
    "trek-task-review",
    "trek-leader-digest",
})

_T1 = {"tier": "T1-system", "issuer": "beacon-system"}

# ms-185 e-7078: the proof operation-trigger now demands — the T2 envelope a human
# minted at ``beacon operation approve``, scoped to the op_id being fired.
_OP = {"tier": "T2", "issuer": "someone@example.com", "scope": "op-3"}


def _evt(channel: str, envelope=None) -> dict:
    # payload carries op_id so an operation-trigger event can satisfy its
    # requirement (the approval must name the Operation being fired).
    ev = {"channel": channel, "delivery": "auto-execute",
          "payload": {"op_id": "op-3"}}
    if envelope is not None:
        ev["envelope"] = envelope
    return ev


class TestClassificationCoverage:
    def test_every_known_gated_channel_is_classified(self):
        """族の各 channel が「現役」か「引退済み」のどちらかに居る。

        ``SYSTEM_PROVENANCE_CHANNELS`` から名前を消しただけ (= 引退先に足し忘れ) だと
        ここで赤くなる。これが「畳む作業で署名なし自動実行の窓を開ける」のを止める
        唯一の機械的な歯止め。
        """
        classified = bd.SYSTEM_PROVENANCE_CHANNELS | bd.RETIRED_PROVENANCE_CHANNELS
        unclassified = KNOWN_GATED_CHANNELS - classified
        assert not unclassified, (
            "gated channel "
            + ", ".join(sorted(unclassified))
            + " が、現役 (SYSTEM_PROVENANCE_CHANNELS) にも引退済み "
            "(RETIRED_PROVENANCE_CHANNELS) にも居ません。provenance から名前を消すなら "
            "引退済みへ移してください。消すだけだと、その channel の古い opt-in が "
            "残っているプロジェクトで、署名の無い自動実行が通ります (ms-185 e-7077)。"
        )

    def test_the_two_sets_do_not_overlap(self):
        """現役と引退済みが同時に成立することはない (= 判定の順序に依らせない)。"""
        both = bd.SYSTEM_PROVENANCE_CHANNELS & bd.RETIRED_PROVENANCE_CHANNELS
        assert not both, (
            "channel " + ", ".join(sorted(both))
            + " が現役と引退済みの両方に居ます。どちらか一方にしてください。"
        )


class TestRetiredChannelIsDowngraded:
    def test_retired_channel_downgraded_even_when_allowlisted(self):
        """引退済み channel は、許可リストに在っても降格する。

        これが穴を塞ぐ本体。古い opt-in が残ったプロジェクトでも fail-closed のまま。
        """
        delivery, downgraded_from, reason = bd.classify_auto_execute(
            _evt("trek-trigger"),
            allowlist=["operation-trigger", "trek-trigger"],
            provenance_channels=frozenset({"operation-trigger"}),
            retired_channels=frozenset({"trek-trigger"}),
        )
        assert delivery == bd.PROPOSE_TO_AI
        assert downgraded_from == bd.AUTO_EXECUTE
        assert reason == bd.DOWNGRADE_RETIRED_CHANNEL

    def test_retired_channel_downgraded_even_with_a_t1_system_envelope(self):
        """署名が在っても降格する。

        引退した channel にサーバが署名を出すことはもう無いので、署名付きで届いたら
        それ自体が異常 (= 古い event の再生か偽装)。署名の有無で救わない。
        """
        _d, _f, reason = bd.classify_auto_execute(
            _evt("trek-trigger", envelope=_T1),
            allowlist=["trek-trigger"],
            provenance_channels=frozenset({"operation-trigger"}),
            retired_channels=frozenset({"trek-trigger"}),
        )
        assert reason == bd.DOWNGRADE_RETIRED_CHANNEL

    def test_retired_wins_over_provenance_when_a_caller_passes_both(self):
        """呼び出し元が重なった集合を渡しても、理由は「引退済み」になる。

        既定の 2 集合は ``test_the_two_sets_do_not_overlap`` が重ならないことを保証する
        ので、**既定だけを見ている限り判定の順序は観測できない**。観測できるのは
        呼び出し元が重なった集合を渡したときで、そこで順序が逆だと理由タグが
        ``non-system-envelope`` になる。どちらも降格はするので安全側だが、
        **消費側が案内する直し方が変わる** — 「直せません」と言うべきところで
        「古い opt-in を外してください」と言えなくなる (その逆も)。

        だから順序は仕様であり、ここで固定する。この性質を持たせるまで、順序を
        入れ替える反転が赤くならなかった (= テストが自分で思っていたより弱かった)。
        """
        _d, _f, reason = bd.classify_auto_execute(
            _evt("trek-trigger"),
            allowlist=["trek-trigger"],
            provenance_channels=frozenset({"trek-trigger"}),   # 重なっている
            retired_channels=frozenset({"trek-trigger"}),
        )
        assert reason == bd.DOWNGRADE_RETIRED_CHANNEL, (
            "引退の判定は provenance の判定より先に効かなければならない。順序が逆だと "
            "理由が non-system-envelope になり、消費側が「直せません」と案内して "
            "古い opt-in を外すべきことが伝わらない。"
        )

    def test_allowlist_miss_still_wins_over_retired(self):
        """許可リストに無ければ、従来どおり理由タグ無しの降格 (= 既存の形を変えない)。

        allowlist-miss の理由タグが空文字列であることは、Claude hook が
        ``_downgrade_reason`` を載せるかどうかの分岐に効いている (e-5803 review)。
        引退の判定を足したことでこの形が変わっていないことを見る。
        """
        _d, _f, reason = bd.classify_auto_execute(
            _evt("trek-trigger"),
            allowlist=[],
            retired_channels=frozenset({"trek-trigger"}),
        )
        assert reason == "", "allowlist-miss の理由タグは空文字列のままでなければならない"


class TestNothingElseChanged:
    """引退の判定を足したことで、既存の 3 つの経路が変わっていないことを見る。"""

    def test_authorized_live_provenance_channel_still_kept(self):
        # ms-185 e-7078: the accepted proof is the human-approved Operation
        # envelope, not the server-minted T1-system one (Trek was its only
        # issuer). Asserting the T1-system shape here is what made this file
        # green while production downgraded every operation trigger.
        assert bd.classify_auto_execute(
            _evt("operation-trigger", envelope=_OP),
            allowlist=["operation-trigger"],
        ) == (bd.AUTO_EXECUTE, "", "")

    def test_unsigned_live_provenance_channel_still_downgraded(self):
        _d, _f, reason = bd.classify_auto_execute(
            _evt("operation-trigger"), allowlist=["operation-trigger"])
        assert reason == bd.DOWNGRADE_NON_SYSTEM_ENVELOPE

    def test_opted_in_custom_channel_still_kept_without_a_signature(self):
        """独自 channel の opt-in を黙って狭めていない。

        「人間が opt-in した channel は署名なしでも信頼する」は意図的な設計
        (test_bus_delivery.py が固定)。引退リストは既定で空なので、この経路は
        引退の判定を足しても変わらない。
        """
        assert bd.classify_auto_execute(
            _evt("dm"), allowlist=["dm"],
        ) == (bd.AUTO_EXECUTE, "", "")

    def test_non_auto_execute_event_untouched(self):
        assert bd.classify_auto_execute(
            {"channel": "dm", "delivery": "propose-to-ai"}, allowlist=["dm"],
        ) == ("propose-to-ai", "", "")


class TestReasonTagIsConsumable:
    def test_reason_tag_is_exported_and_distinct(self):
        """理由タグが公開されていて、既存の 2 つと区別できる。

        消費側 (bin/beacon-bus-inbox-hook.py / scripts/codex-inbox-hook.py) が
        理由ごとに違う直し方を案内するので、タグが衝突していると誤った案内になる。
        """
        assert "DOWNGRADE_RETIRED_CHANNEL" in bd.__all__
        assert bd.DOWNGRADE_RETIRED_CHANNEL
        assert bd.DOWNGRADE_RETIRED_CHANNEL != bd.DOWNGRADE_NON_SYSTEM_ENVELOPE
        assert bd.DOWNGRADE_RETIRED_CHANNEL != ""
