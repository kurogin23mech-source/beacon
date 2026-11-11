"""降格の理由ごとに、受信側 2 つが**正しい直し方**を案内することを固定する (ms-185 e-7077)。

**このファイルは一度、偽の安全だった**

初版は全 7 件が ``assert "..." in src`` というソース文字列検査だけだった。独立レビューが
実測したところ、``scripts/codex-inbox-hook.py`` の **どの理由をどのカウンタに積むかだけを
入れ替えても (トークンは全部残るので) 7/7 緑のまま**だった。配線が逆転すると
``non-system-envelope`` の event に「古い opt-in を外せ」と案内し、引退した channel の
event に「直せません」と案内する — 通知文と真因が入れ替わる。**このファイルが防ぐために
存在する失敗モードそのもの**を、このファイルが見逃していた。

さらに AX レビューが、初版の ``test_claude_hook_does_not_enumerate_reasons`` が
**事実と逆のことを裏付けていた**ことを見つけた。Claude 側は理由タグを event に代入は
するが、``_format_event`` が**それを読まずに固定文**
「— channel not in bus_auto_execute_channels」を出していた。``retired`` と
``non-system-envelope`` は **どちらも「channel は許可リストに在る」ときにしか起きない**
ので、この固定文は事実と正反対で、読んだ AI を真因と逆の回復ループに入れる。初版の
テストは「代入文がソースに在るか」だけを見て「Claude 側は追従不要」と誤って固定していた。

だから今は **振る舞いを測る**: 純粋関数を呼んでカウンタの値を見る / 整形済みの出力文字列を
見る。ソース文字列検査は「文言そのものの固定」にだけ残す。反転は
``scripts/invert-retired-provenance-guard.py`` が実測する。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import bus_delivery as bd  # noqa: E402

CODEX_HOOK = REPO / "scripts" / "codex-inbox-hook.py"
CLAUDE_HOOK = REPO / "bin" / "beacon-bus-inbox-hook.py"


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def codex():
    return _load(CODEX_HOOK, "codex_hook_e7077")


@pytest.fixture(scope="module")
def claude():
    return _load(CLAUDE_HOOK, "claude_hook_e7077")


# 4 つの理由タグ。空文字列 = 許可リスト漏れ (e-5803 で sentinel として固定)。
ALLOWLIST_MISS = ""
PROVENANCE = bd.DOWNGRADE_NON_SYSTEM_ENVELOPE
RETIRED = bd.DOWNGRADE_RETIRED_CHANNEL
UNKNOWN = "some-future-reason"


# ---------------------------------------------------------------------------
# Codex 側: 計数の配線を **値** で測る (ソース文字列ではなく)
# ---------------------------------------------------------------------------

class TestCodexTallyIsWiredToTheRightCounter:
    """理由 → カウンタの対応を 1 件ずつ実際に呼んで確かめる。

    配線を入れ替える変異 (トークンを一切変えずに積み先だけ逆転) が赤くなるのは、
    ここが値を見ているから。
    """

    @pytest.mark.parametrize("reason,expected_key", [
        (ALLOWLIST_MISS, "allowlist"),
        (PROVENANCE, "provenance"),
        (RETIRED, "retired"),
        (UNKNOWN, "other"),
    ])
    def test_each_reason_lands_in_its_own_counter(self, codex, reason, expected_key):
        # ms-185 e-7395: channel も渡す形になった。ここは **現役** channel を渡す
        # (引退 channel だと許可リスト漏れが retired_unapproved へ行くのが正しい
        # 挙動で、それは TestRetiredUnapprovedIsCountedApart が測る)。
        counts = codex.tally_downgrade_reasons([(reason, "dm")], bd=bd)
        assert counts[expected_key] == 1, (
            "理由 %r が %s に積まれていません: %r" % (reason, expected_key, counts)
        )
        # 他は 0 — 「どれかに入った」ではなく「正しいものだけに入った」を見る
        for key in ("allowlist", "retired_unapproved", "provenance", "retired",
                    "other"):
            if key != expected_key:
                assert counts[key] == 0, (
                    "理由 %r が %s にも積まれています (配線の取り違え): %r"
                    % (reason, key, counts)
                )

    def test_mixed_batch_is_counted_independently(self, codex):
        counts = codex.tally_downgrade_reasons(
            [(RETIRED, "trek-trigger"), (PROVENANCE, "operation-trigger"),
             (ALLOWLIST_MISS, "dm"), (RETIRED, "trek-trigger"),
             (UNKNOWN, "dm")], bd=bd)
        assert counts["retired"] == 2
        assert counts["provenance"] == 1
        assert counts["allowlist"] == 1
        assert counts["other"] == 1

    def test_unknown_reason_is_not_folded_into_allowlist_miss(self, codex):
        """未知のタグを許可リスト漏れに畳まない。

        畳むと「channel を opt-in してください」という**誤案内**になる。
        将来 5 つ目の理由が足されたとき、黙って誤分類されないことを見る。
        """
        counts = codex.tally_downgrade_reasons([(UNKNOWN, "dm")], bd=bd)
        assert counts["allowlist"] == 0
        assert counts["other"] == 1
        assert counts["other_reasons"] == {UNKNOWN}

    def test_empty_input_is_all_zero(self, codex):
        counts = codex.tally_downgrade_reasons([], bd=bd)
        assert all(counts[k] == 0 for k in
                   ("allowlist", "retired_unapproved", "provenance", "retired",
                    "other"))
        assert counts["other_reasons"] == set()
        assert counts["retired_unapproved_channels"] == set()


class TestCodexNoticeTextTellsTheRightFix:
    """通知文が正しい直し方を名指すことを測る。

    ms-185 e-7308: ここは以前**ソース文字列検査**だった (``"auto-execute remove
    --channel" in src``)。文言が hook の中に凍結コピーで在ったので、それで測れていた。
    委譲した今は hook のソースにその literal は無く、**返ってくる文字列**を見る形に
    変えている。ソース検査のままにすると、委譲した瞬間に赤くなるか、あるいは
    共有層が別のことを言い出しても緑のままになる (= どちらの向きにも間違う)。
    """

    def test_retired_notice_points_at_removing_the_stale_optin(self, codex):
        out = codex.render_retired_advice(bd=bd)
        assert "auto-execute remove --channel" in out, (
            "引退した channel の直し方として `beacon bus auto-execute remove "
            "--channel <名前>` を案内していません。opt-in し直す案内では直りません。"
        )

    def test_retired_notice_is_byte_equal_to_the_shared_layer(self, codex):
        """引退理由の文言が共有層と**等値** (部分一致ではない)。"""
        assert codex.render_retired_advice(bd=bd) == \
            bd.downgrade_advice(RETIRED, "")

    def test_codex_hook_follows_the_shared_source_for_retired(self, codex,
                                                              monkeypatch):
        """真値源を差し替えると Codex 側の引退案内も変わる (= 委譲している)。

        **直した穴 (ms-185 e-7308)**: この検査は証拠欠落の理由にしか無かった
        (``test_codex_hook_follows_the_shared_source``)。引退理由は hook の中の
        凍結コピーで、共有層を差し替えても出力が変わらないことを実測で確認した
        うえで委譲に直した。Claude 側には 4 理由すべての等値比較が在ったので、
        同じ非対称がこの理由だけ残っていた形。
        """
        sentinel = "__SHARED_LAYER_SENTINEL__"
        monkeypatch.setattr(bd, "downgrade_advice",
                            lambda reason, channel="": sentinel)
        assert codex.render_retired_advice(bd=bd) == sentinel, (
            "真値源を差し替えても Codex 側の引退案内が変わりません "
            "(= 独自コピーを持っている)。"
        )

    def test_codex_retired_advice_does_not_guess_without_the_shared_layer(
            self, codex):
        """lib が読めないとき、回復手段を推測で名乗らない (e-7077 の教訓)。"""
        out = codex.render_retired_advice(bd=None)
        assert "auto-execute remove" not in out
        assert "回復手段を提示できません" in out

    def test_each_counter_has_its_own_notice_block(self, codex):
        import re
        src = CODEX_HOOK.read_text(encoding="utf-8")
        for counter in ("downgraded_allowlist", "downgraded_provenance",
                        "downgraded_retired", "downgraded_other"):
            assert re.search(r"if\s+" + counter + r"\s*:", src), (
                counter + " に対応する通知の分岐がありません"
            )


# ---------------------------------------------------------------------------
# Claude 側: 整形済みの出力を **文字列として** 測る
# ---------------------------------------------------------------------------

class TestClaudeHookRendersPerReasonAdvice:
    """``_format_event`` の出力が理由ごとに変わることを、出力そのもので確かめる。

    初版はここを「代入文がソースに在るか」で見ていて、固定文が出続けていることを
    見逃した (AX レビューが misleading として指摘)。
    """

    def _render(self, claude, reason: str, channel: str = "trek-trigger") -> str:
        # ms-185 e-7071: channel を引数にした。既定は trek-trigger のままなので
        # 引退理由のテストはそのまま (trek-* は今まさに引退済みの実例)。署名欠落
        # (PROVENANCE) の理由は**現役のゲート channel でしか起きない**ので、
        # そちらは operation-trigger を渡す。
        ev = {
            "event_id": "ev-1", "channel": channel,
            "delivery": "propose-to-ai", "sender_session_id": "sv-x",
            "created_at": "2026-10-19T00:00:00Z", "payload": {"text": "x"},
            "_downgraded_from": "auto-execute", "_downgrade_reason": reason,
        }
        return claude._format_event(ev)

    def test_retired_output_points_at_removing_the_stale_optin(self, claude):
        out = self._render(claude, RETIRED)
        assert "auto-execute remove" in out, (
            "引退した channel の降格で「古い opt-in を外す」案内が出ていません。"
            "retired は channel が許可リストに**在る**ときしか起きないので、"
            "固定文「channel not in bus_auto_execute_channels」は事実と逆です。"
        )
        assert "not in bus_auto_execute_channels" not in out, (
            "retired の降格で「許可リストに無い」と案内しています = 事実と正反対。"
        )

    def test_provenance_output_says_it_cannot_be_fixed_by_opting_in(self, claude):
        # ms-185 e-7071: 現役のゲート channel (operation-trigger) で測る。
        # trek-* は引退したので、この理由でここに来ることはもう無い。
        out = self._render(claude, PROVENANCE, channel="operation-trigger")
        assert "operation approve" in out
        assert "not in bus_auto_execute_channels" not in out, (
            "署名欠落の降格で「許可リストに無い」と案内しています = 事実と逆 "
            "(channel は許可リストに在る)。"
        )

    def test_allowlist_miss_output_keeps_the_original_wording(self, claude):
        """許可リスト漏れの文言は従来どおり (= 既存の読み手を壊さない)。

        **channel を明示するようになった理由 (ms-185 e-7395)**: この試験は
        ``_render`` の既定 channel (``trek-trigger``) を使っていたが、それは
        **引退した channel** である。引退した channel が許可リストに無い場合の
        案内は e-7395 で「引退済みなので登録しないでください」に変わった
        (以前は現役 channel と同じ「人間が許可する前」で、人間が案内に従って
        登録すると次の通知で「外してください」と逆を言われる誤誘導だった)。

        この試験が守りたいのは「**許可リスト漏れ**の文言が従来どおり」であって
        「引退した channel の文言が従来どおり」ではない。だから引退していない
        channel を明示して渡す。引退側の案内は
        ``TestRetiredChannelAwaitingApprovalIsNotToldToOptIn`` が測る。
        """
        out = self._render(claude, ALLOWLIST_MISS, channel="dm")
        assert "not in bus_auto_execute_channels" in out

    def test_unknown_reason_is_surfaced_not_mislabelled(self, claude):
        out = self._render(claude, UNKNOWN)
        assert UNKNOWN in out, "未知の理由タグを出力に出していません"
        assert "not in bus_auto_execute_channels" not in out, (
            "未知の理由を許可リスト漏れとして案内しています = 誤案内。"
        )

    @pytest.mark.parametrize("channel", ["trek-trigger", "dm"])
    def test_all_four_reasons_render_differently(self, claude, channel):
        """4 つの理由が互いに違う文言になること —— **引退 / 現役 のどちらの
        channel でも**。

        ms-185 e-7395 で「理由タグ無し」の案内が channel 依存になったので、
        片方の channel だけで測ると、もう片方で区別が潰れても緑になる。
        """
        outs = [self._render(claude, r, channel=channel)
                for r in (ALLOWLIST_MISS, PROVENANCE, RETIRED, UNKNOWN)]
        assert len(set(outs)) == 4, (
            f"channel={channel} で 4 つの理由のうち同じ文言になっているものが "
            "あります = 区別できていない"
        )

    def test_no_downgrade_marker_means_no_advice_line(self, claude):
        """降格していない event には案内を付けない (= 既存の形を変えない)。"""
        ev = {"event_id": "ev-2", "channel": "dm", "delivery": "auto-execute",
              "sender_session_id": "sv-x", "created_at": "2026-10-19T00:00:00Z",
              "payload": {}}
        assert "downgraded from" not in claude._format_event(ev)

    def test_advice_survives_bus_delivery_being_unavailable(self, claude,
                                                            monkeypatch):
        """``bus_delivery`` が読めない環境でも落ちず、理由タグを生で出す。

        この hook の ``bd`` は遅延 import で **None になりうる** (fail-closed 設計)。
        案内を足したときモジュールスコープの ``bd`` を参照していて、この経路で
        ``NameError`` になっていた。知らない定数名で分岐したふりをするより、
        生のタグを見せる方が誤診を招かない。
        """
        monkeypatch.setattr(claude, "_import_bus_delivery", lambda: None)
        out = self._render(claude, RETIRED)
        assert RETIRED in out
        assert "not in bus_auto_execute_channels" not in out


# ---------------------------------------------------------------------------
# ms-185 e-7395: 引退した channel が **まだ許可されていない** とき、
#                「許可してください」と案内してはならない
# ---------------------------------------------------------------------------

class TestRetiredUnapprovedIsCountedApart:
    """Codex 側: 引退 + 未許可を、現役の承認待ちと **別のバケツ** に数えること。

    ms-185 e-7395: Codex の許可リスト漏れバンドは件数だけを出す凍結文だったので、
    引退した channel も「人間 opt-in 前」と案内していた。畳む側 (純粋関数) で
    分けないと、``main()`` に inline で分岐する形に戻り、配線の逆転をテストで
    捕まえられなくなる (e-7077 の保守性レビューが罰した形)。
    """

    def test_retired_channel_without_optin_does_not_land_in_allowlist(self, codex):
        counts = codex.tally_downgrade_reasons(
            [(ALLOWLIST_MISS, "trek-trigger")], bd=bd)
        assert counts["retired_unapproved"] == 1, counts
        assert counts["allowlist"] == 0, (
            "引退した channel の許可リスト漏れを allowlist に畳んでいます = "
            "「opt-in してください」という誤案内になります: %r" % (counts,)
        )
        assert counts["retired_unapproved_channels"] == {"trek-trigger"}

    def test_live_channel_without_optin_still_lands_in_allowlist(self, codex):
        counts = codex.tally_downgrade_reasons([(ALLOWLIST_MISS, "dm")], bd=bd)
        assert counts["allowlist"] == 1, counts
        assert counts["retired_unapproved"] == 0, counts

    def test_mixed_batch_splits_the_two_states(self, codex):
        counts = codex.tally_downgrade_reasons(
            [(ALLOWLIST_MISS, "trek-trigger"), (ALLOWLIST_MISS, "dm"),
             (ALLOWLIST_MISS, "trek-progress-check")], bd=bd)
        assert counts["retired_unapproved"] == 2, counts
        assert counts["allowlist"] == 1, counts
        assert counts["retired_unapproved_channels"] == \
            {"trek-trigger", "trek-progress-check"}

    def test_advice_lines_are_byte_equal_to_the_shared_layer(self, codex):
        """案内は共有層と **等値** (部分一致ではない)。"""
        chans = {"trek-trigger", "trek-task-review"}
        got = codex.render_retired_unapproved_advice_lines(chans, bd=bd)
        want = "".join(
            f"\n    - channel={ch}: {bd.downgrade_advice('', ch)}"
            for ch in sorted(chans))
        assert got == want, got

    def test_no_lib_does_not_invent_a_recovery(self, codex):
        """lib が読めない / channel 不明のときは回復手段を推測で名乗らない。"""
        for args in (({"trek-trigger"}, None), (set(), bd)):
            chans, mod = args
            out = codex.render_retired_unapproved_advice_lines(chans, bd=mod)
            assert "auto-execute" not in out, out
            assert "特定できない" in out, out


class TestRetiredChannelAwaitingApprovalIsNotToldToOptIn:
    """引退と「人間の承認待ち」を案内層で見分けることを測る。

    **なぜ案内層なのか**: 判定 (``classify_auto_execute``) は許可リストを先に見る
    順序が固定されており、そこで外れた時点で理由タグが空になる。順序は
    ``test_allowlist_miss_still_wins_over_retired`` が意図して固定している
    (空タグが Claude hook の分岐に効いているため、e-5803 のレビュー由来)。
    順序を変えると記録済みの決定を覆すので、区別は **案内を出す層で付ける**。
    判定側の docstring も「案内層でやるしかない」と明記している。

    **なぜ 1 箇所で足りるのか**: 案内の文面は
    ``lib/bus_delivery.downgrade_advice`` が唯一の所有者で、Claude 側と Codex 側の
    両 hook がこれを呼ぶ (``TestBothHooksShareOneAdviceSource`` がその委譲を
    差し替えで測っている)。両 hook に同じ分岐を書くと、#812 で罰された
    「同じ説明が 2 コピーで割れる」形を再生産する。だから直すのは共有層 1 箇所で、
    ここでは **両 hook の出力に届いているか** を端から端まで測る。
    """

    # 3 状態。e-7395 の受入条件 1〜3 に 1:1 で対応する。
    RETIRED_CH = "trek-trigger"      # 引退した channel の実例
    LIVE_CH = "dm"                   # 引退していない channel

    def test_retired_and_unapproved_says_retired_not_approve_me(self):
        """受入条件1: 引退 + 許可リスト空 → 「引退済み」の意味になる。"""
        delivery, downgraded, reason = bd.classify_auto_execute(
            {"channel": self.RETIRED_CH, "delivery": "auto-execute"},
            allowlist=frozenset())
        assert (delivery, downgraded) == ("propose-to-ai", "auto-execute")
        advice = bd.downgrade_advice(reason, self.RETIRED_CH)
        assert "引退" in advice, (
            "引退した channel が許可リストに無いとき、引退である旨を案内して "
            f"いません: {advice!r}"
        )
        assert advice != bd.downgrade_advice(reason, self.LIVE_CH), (
            "引退した channel と現役 channel で案内が同じです = 区別が付いて "
            "いません。人間はこれに従って `auto-execute add` で登録し、次の通知で "
            "「外してください」と逆を言われます"
        )

    def test_retired_and_approved_still_says_remove_the_stale_optin(self):
        """受入条件2: 引退 + 許可リストに在る → 従来どおり「外してください」。"""
        _d, _f, reason = bd.classify_auto_execute(
            {"channel": self.RETIRED_CH, "delivery": "auto-execute"},
            allowlist=frozenset({self.RETIRED_CH}))
        assert reason == RETIRED, reason
        advice = bd.downgrade_advice(reason, self.RETIRED_CH)
        assert "auto-execute remove" in advice, (
            "許可リストに残った引退 channel の直し方 (外す) が変わっています"
        )

    def test_live_channel_awaiting_approval_is_unchanged(self):
        """受入条件3: 現役 + 許可リスト空 → 従来どおり「人間が許可する前」。"""
        _d, _f, reason = bd.classify_auto_execute(
            {"channel": self.LIVE_CH, "delivery": "auto-execute"},
            allowlist=frozenset())
        assert bd.downgrade_advice(reason, self.LIVE_CH) == \
            "channel not in bus_auto_execute_channels (= 人間が許可する前)", (
            "引退していない channel の承認待ち案内が変わっています = 既存の "
            "読み手を壊しています"
        )

    def test_the_three_states_do_not_swap(self):
        """3 状態の案内が互いに入れ替わらない (受入条件4 の「入れ替わらない」)。

        1 件ずつ見ると、ある状態の文言を別の状態へコピーする変異を検知できない。
        3 つを同時に取って互いに異なることを見る。
        """
        states = {
            "retired+unapproved": (frozenset(), self.RETIRED_CH),
            "retired+approved": (frozenset({self.RETIRED_CH}), self.RETIRED_CH),
            "live+unapproved": (frozenset(), self.LIVE_CH),
        }
        advice = {}
        for name, (allowlist, ch) in states.items():
            _d, _f, reason = bd.classify_auto_execute(
                {"channel": ch, "delivery": "auto-execute"}, allowlist=allowlist)
            advice[name] = bd.downgrade_advice(reason, ch)
        assert len(set(advice.values())) == 3, (
            "3 状態のうち同じ案内になっているものがあります: "
            + repr(advice)
        )

    def test_retired_advice_does_not_invite_opting_in(self):
        """引退の案内が ``auto-execute add`` を **勧めて** いないこと。

        文面に `auto-execute add` という語が出ることは許す (「許可しても無駄だ」と
        説明するために名前を出すのは自然)。測るのは **勧誘していないこと** —
        つまり現役 channel の承認待ち案内と同じ文言になっていないこと。
        文面の言い回しそのものを機械で測ると、正当な否定形で偽の赤になる
        (このリポジトリが 4 周のレビューで実証した経路なので踏まない)。
        """
        _d, _f, reason = bd.classify_auto_execute(
            {"channel": self.RETIRED_CH, "delivery": "auto-execute"},
            allowlist=frozenset())
        retired_advice = bd.downgrade_advice(reason, self.RETIRED_CH)
        live_advice = bd.downgrade_advice(reason, self.LIVE_CH)
        assert retired_advice != live_advice
        assert "引退" in retired_advice and "引退" not in live_advice

    def test_both_hooks_surface_it(self, claude, codex):
        """受入条件1 の「両フックで実機確認」: 共有層の案内が両方に届く。

        文面の著者は共有層なので、ここで測るのは **運んでいるか** だけ
        (意味の正しさは機械で測らない — PR#842 の保守性レビューで学んだ区別)。
        """
        expected = bd.downgrade_advice(ALLOWLIST_MISS, self.RETIRED_CH)
        assert claude._downgrade_advice(ALLOWLIST_MISS, self.RETIRED_CH) == expected, (
            "Claude 側の受信フックが共有層の案内を運んでいません"
        )
        # Codex 側は専用の整形関数を通す。**等値** で測る (部分一致だと、今日の
        # 文言を凍結したコピーを埋め込んでも緑のままになる — #812 の judge が
        # 実測した経路)。
        line = codex.render_retired_unapproved_advice_lines(
            {self.RETIRED_CH}, bd=bd)
        assert line == f"\n    - channel={self.RETIRED_CH}: {expected}", (
            "Codex 側が共有層の案内を運んでいません: " + repr(line)
        )


class TestReasonTagsAreAllDistinct:
    def test_no_two_reason_tags_collide(self):
        tags = [PROVENANCE, RETIRED, ALLOWLIST_MISS]
        assert len(set(tags)) == len(tags), (
            "理由タグが衝突しています: " + repr(tags)
            + " — 消費側が理由を区別できず、誤った直し方を案内します。"
        )


# ---------------------------------------------------------------------------
# ms-185 / #812 レビュー: 案内は **channel ごと** に導かれる
# ---------------------------------------------------------------------------

class TestProvenanceAdviceIsChannelAware:
    """``non-system-envelope`` 1 つのタグが 2 つの別世界を指すことを測る。

    #812 が ``operation-trigger`` の証拠を「サーバ署名 (T1-system)」から
    「人間がその op_id を承認した封筒 (T2)」へ張り替えた。それ以降、同じ理由タグが
    **回復手段が在る世界** (operation-trigger) と **operator には直せない世界**
    (trek-*) の両方を指す。タグだけで分岐していた以前の実装は後者の文言を前者にも
    返し、``beacon operation approve <op-id>`` という実在する回復手段を隠して
    「直りません」と断定していた (独立レビュー 2 体が一致で high)。

    ここは**返ってくる文字列そのもの**を見る。「分岐が在るか」をソースで見ると、
    分岐の形を残したまま中身を入れ替える変異を捕まえられない。
    """

    def test_operation_trigger_names_the_approval_recovery(self):
        out = bd.downgrade_advice(PROVENANCE, bd.OPERATION_TRIGGER_CHANNEL)
        assert "beacon operation approve" in out, (
            "operation-trigger の降格で、実在する回復手段を名指していません。"
        )
        assert "T1-system" not in out, (
            "operation-trigger はサーバ署名を要求しません (#812 以前の語彙)。"
        )
        assert "直りません" not in out, (
            "回復手段が実在するのに「直らない」と断定しています。"
        )

    # ms-185 e-7071: test_trek_channel_says_the_operator_cannot_fix_it と
    # test_the_two_families_get_different_advice を落とした。
    #
    # どちらも「同じ理由タグが 2 つの別世界を指す」ことを、trek-* という
    # **operator には直せない側の実例**で測っていた。その一群は引退済みの集合へ
    # 移り (発行者が e-7070 で消えたため)、署名欠落の理由でここに来ることはもう
    # 無い。宣言が残っているのは operation-trigger だけなので、2 つを比べる形は
    # 成立しない。
    #
    # **守りたい契約は消えていない** — 助言は理由タグだけでなく channel から
    # 導かれなければならない (タグだけで分岐すると、片方の文言がもう片方にも出る)。
    # それを測るのは下の 2 件:
    #   * test_every_gated_channel_declares_a_recovery — 宣言した channel は
    #     述語と回復手段の両方を持ち、助言がその宣言と一致する
    #   * test_undeclared_channel_does_not_claim_a_recovery — 宣言の無い channel が
    #     宣言済みの回復手段を**受け継がない** (= タグだけで分岐していたら、ここで
    #     operation-trigger の文言が出てしまい赤くなる)
    # 宣言が 1 つに減っても、後者が「タグだけ分岐」を捕まえ続ける。

    def test_every_gated_channel_declares_a_recovery(self):
        """ゲートした channel は、述語と**回復手段の説明**の両方を必ず持つ。

        片方だけ在る状態 (= 以前そのものだった形) を構造的に作れなくする。
        """
        for channel, requirement in bd.PROVENANCE_REQUIREMENTS.items():
            assert callable(requirement.predicate), (
                f"{channel} の述語が呼び出せません")
            assert requirement.advice and requirement.advice.strip(), (
                f"{channel} は証拠を要求しているのに、operator への回復手段を "
                "宣言していません")
            assert bd.downgrade_advice(PROVENANCE, channel) == requirement.advice

    def test_undeclared_channel_does_not_claim_a_recovery(self):
        """要求を宣言していない channel に、回復手段を推測で名乗らない。

        知らないのに「直せます」「直りません」と言う方が、言わないより害が大きい
        (e-7077 の教訓そのもの)。
        """
        out = bd.downgrade_advice(PROVENANCE, "nobody-declares-me")
        assert "operation approve" not in out
        assert "auto-execute remove" not in out
        assert "nobody-declares-me" in out, "どの channel の話か示していません。"


class TestBothHooksShareOneAdviceSource:
    """2 つの受信 hook が、案内文を **共有層から取っている** ことを測る。

    欠陥が 2 箇所に在ったのは、同じ説明文が Claude 側と Codex 側へ別コピーで
    置かれていたから。説明の所有者を ``lib/bus_delivery.downgrade_advice`` 1 つに
    畳んだので、ここは「どちらかに固定文を書き戻す変異」で赤くならなければならない。

    **測り方を 1 度やり直している (= 偽の安全を踏んだ記録)**。最初はこう書いていた::

        assert claude._downgrade_advice(reason, ch) == bd.downgrade_advice(reason, ch)

    これは「**今日の文言と一致するか**」しか見ていない。片方の hook に *今日の文言と
    byte 一致する* 凍結コピーを埋め込む変異を当てたところ、**全件緑のまま**だった
    (= 独自コピーを持っているのに検知できない)。等値比較は、コピーが古くなった後で
    しか赤くならない — つまり drift が起きてからしか気付けず、守りたい性質
    (「割れない形になっている」) を測れていない。

    いまは **真値源を試験中だけ差し替えて、出力が追随するか** を見る。凍結コピーを
    持つ実装は差し替えに追随できないので、byte 一致でも必ず赤くなる。
    """

    SENTINEL = "SENTINEL-ADVICE-7ca91f (試験中だけ差し替えた値)"

    @pytest.mark.parametrize("channel", sorted(bd.GATED_PROVENANCE_CHANNELS))
    def test_claude_hook_follows_the_shared_source(self, claude, channel,
                                                   monkeypatch):
        """真値源を差し替えると Claude 側の出力も変わる (= 委譲している)。"""
        patched = dict(bd.PROVENANCE_REQUIREMENTS)
        patched[channel] = patched[channel]._replace(advice=self.SENTINEL)
        monkeypatch.setattr(bd, "PROVENANCE_REQUIREMENTS", patched)
        assert claude._downgrade_advice(PROVENANCE, channel) == self.SENTINEL, (
            "真値源を差し替えても Claude 側の案内が変わりません "
            "(= 独自コピーを持っている)。"
        )

    @pytest.mark.parametrize("channel", sorted(bd.GATED_PROVENANCE_CHANNELS))
    def test_codex_hook_follows_the_shared_source(self, codex, channel,
                                                  monkeypatch):
        """真値源を差し替えると Codex 側の出力も変わる (= 委譲している)。

        **直した穴 (独立 AX レビュー, medium)**: この検査は Claude 側にしか無かった。
        Codex 側は出力の部分一致 (``"beacon operation approve" in ctx``) だけだったので、
        judge が実測したとおり凍結コピーを埋め込んでも全テスト緑だった。
        """
        patched = dict(bd.PROVENANCE_REQUIREMENTS)
        patched[channel] = patched[channel]._replace(advice=self.SENTINEL)
        monkeypatch.setattr(bd, "PROVENANCE_REQUIREMENTS", patched)
        rendered = codex.render_provenance_advice_lines([channel], bd=bd)
        assert rendered == f"\n    - channel={channel}: {self.SENTINEL}", (
            "真値源を差し替えても Codex 側の案内が変わりません "
            "(= 独自コピーを持っているか、整形が割れている)。"
        )

    @pytest.mark.parametrize("channel", sorted(bd.GATED_PROVENANCE_CHANNELS))
    @pytest.mark.parametrize("reason", [ALLOWLIST_MISS, RETIRED, UNKNOWN])
    def test_claude_hook_does_not_keep_its_own_copy_of_the_other_reasons(
            self, claude, reason, channel):
        """証拠欠落以外の 3 理由も共有層と一致する (こちらは真値源が定数)。"""
        assert claude._downgrade_advice(reason, channel) == \
            bd.downgrade_advice(reason, channel)

    def test_codex_hook_does_not_guess_a_recovery_without_the_shared_layer(
            self, codex):
        """lib が読めない / channel 不明のとき、回復手段を推測で名乗らない。"""
        for channels, kw in (([], {"bd": bd}), (["operation-trigger"], {"bd": None})):
            out = codex.render_provenance_advice_lines(channels, **kw)
            assert "beacon operation approve" not in out
            assert "auto-execute remove" not in out
            assert "回復手段を提示できません" in out
