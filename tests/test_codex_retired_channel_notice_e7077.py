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
        counts = codex.tally_downgrade_reasons([reason], bd=bd)
        assert counts[expected_key] == 1, (
            "理由 %r が %s に積まれていません: %r" % (reason, expected_key, counts)
        )
        # 他の 3 つは 0 — 「どれかに入った」ではなく「正しいものだけに入った」を見る
        for key in ("allowlist", "provenance", "retired", "other"):
            if key != expected_key:
                assert counts[key] == 0, (
                    "理由 %r が %s にも積まれています (配線の取り違え): %r"
                    % (reason, key, counts)
                )

    def test_mixed_batch_is_counted_independently(self, codex):
        counts = codex.tally_downgrade_reasons(
            [RETIRED, PROVENANCE, ALLOWLIST_MISS, RETIRED, UNKNOWN], bd=bd)
        assert counts["retired"] == 2
        assert counts["provenance"] == 1
        assert counts["allowlist"] == 1
        assert counts["other"] == 1

    def test_unknown_reason_is_not_folded_into_allowlist_miss(self, codex):
        """未知のタグを許可リスト漏れに畳まない。

        畳むと「channel を opt-in してください」という**誤案内**になる。
        将来 5 つ目の理由が足されたとき、黙って誤分類されないことを見る。
        """
        counts = codex.tally_downgrade_reasons([UNKNOWN], bd=bd)
        assert counts["allowlist"] == 0
        assert counts["other"] == 1
        assert counts["other_reasons"] == {UNKNOWN}

    def test_empty_input_is_all_zero(self, codex):
        counts = codex.tally_downgrade_reasons([], bd=bd)
        assert all(counts[k] == 0 for k in
                   ("allowlist", "provenance", "retired", "other"))
        assert counts["other_reasons"] == set()


class TestCodexNoticeTextTellsTheRightFix:
    """通知文の文言を固定する (ここはソース文字列検査でよい — 文言そのものが対象)。"""

    def test_retired_notice_points_at_removing_the_stale_optin(self, codex):
        src = CODEX_HOOK.read_text(encoding="utf-8")
        assert "auto-execute remove --channel" in src, (
            "引退した channel の直し方として `beacon bus auto-execute remove "
            "--channel <名前>` を案内していません。opt-in し直す案内では直りません。"
        )

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

    def _render(self, claude, reason: str) -> str:
        ev = {
            "event_id": "ev-1", "channel": "trek-trigger",
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
        out = self._render(claude, PROVENANCE)
        assert "T1-system" in out
        assert "not in bus_auto_execute_channels" not in out, (
            "署名欠落の降格で「許可リストに無い」と案内しています = 事実と逆 "
            "(channel は許可リストに在る)。"
        )

    def test_allowlist_miss_output_keeps_the_original_wording(self, claude):
        """許可リスト漏れの文言は従来どおり (= 既存の読み手を壊さない)。"""
        out = self._render(claude, ALLOWLIST_MISS)
        assert "not in bus_auto_execute_channels" in out

    def test_unknown_reason_is_surfaced_not_mislabelled(self, claude):
        out = self._render(claude, UNKNOWN)
        assert UNKNOWN in out, "未知の理由タグを出力に出していません"
        assert "not in bus_auto_execute_channels" not in out, (
            "未知の理由を許可リスト漏れとして案内しています = 誤案内。"
        )

    def test_all_four_reasons_render_differently(self, claude):
        outs = [self._render(claude, r)
                for r in (ALLOWLIST_MISS, PROVENANCE, RETIRED, UNKNOWN)]
        assert len(set(outs)) == 4, (
            "4 つの理由のうち同じ文言になっているものがあります = 区別できていない"
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

    def test_trek_channel_says_the_operator_cannot_fix_it(self):
        out = bd.downgrade_advice(PROVENANCE, "trek-trigger")
        assert "T1-system" in out, "trek-* が要求するサーバ署名を説明していません。"
        assert "直りません" in out, (
            "trek-* の証拠は Trek スケジューラだけが発行するので、operator には "
            "直せない旨を伝える必要があります。"
        )
        assert "operation approve" not in out, (
            "trek-* に operation の回復手段を案内しています = 誤案内。"
        )

    def test_the_two_families_get_different_advice(self):
        assert (bd.downgrade_advice(PROVENANCE, bd.OPERATION_TRIGGER_CHANNEL)
                != bd.downgrade_advice(PROVENANCE, "trek-trigger")), (
            "同じ理由タグでも channel で要求が違うのに、同じ案内を返しています。"
        )

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
    """2 つの受信 hook が **同じ (理由, channel) に同じ案内** を返す。

    欠陥が 2 箇所に在ったのは、同じ説明文が Claude 側と Codex 側へ別コピーで
    置かれていたから。レビューは Claude 側しか見ておらず、Codex 側は誰も見て
    いなかった。説明の所有者を ``lib/bus_delivery.downgrade_advice`` 1 つに
    畳んだので、このテストは**どちらかに固定文を書き戻す変異**で赤くなる。
    """

    @pytest.mark.parametrize("channel", sorted(bd.SYSTEM_PROVENANCE_CHANNELS))
    @pytest.mark.parametrize("reason", [ALLOWLIST_MISS, PROVENANCE, RETIRED, UNKNOWN])
    def test_claude_hook_delegates_instead_of_keeping_a_copy(
            self, claude, reason, channel):
        assert claude._downgrade_advice(reason, channel) == \
            bd.downgrade_advice(reason, channel), (
            "Claude 側の受信 hook が独自の説明文を持っています "
            "(= 共有層と割れる形に戻っている)。"
        )
