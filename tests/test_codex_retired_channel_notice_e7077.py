"""引退した channel の降格を、受信側 hook が正しい直し方で案内することを固定する
(ms-185 e-7077)。

**なぜ消費側のテストが別に要るのか**

``lib/bus_delivery.py`` に理由タグを 1 つ足しても、それを読む側が追従していなければ
「足った」だけで「全域に効いて」はいない。この件の消費側は 2 つある:

  * ``bin/beacon-bus-inbox-hook.py`` (Claude) — 理由タグをそのまま event に載せるだけの
    汎用経路なので、新しい理由も素通りで運ばれる。
  * ``scripts/codex-inbox-hook.py`` (Codex) — **理由ごとに違う直し方を案内する**ために
    分岐している。足す前は 2-way で、``else`` が「allowlist-miss」を意味していた。
    新しい理由を足しただけだと、引退した channel の降格が **「channel を opt-in して
    ください」と誤案内される** — 古い opt-in が残っていることが問題なのに、その逆を
    勧めることになる。

このファイルは Codex 側の分岐が 4 つの理由を区別し、引退の案内が
``bus auto-execute remove`` を指していることを見る。
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import bus_delivery as bd  # noqa: E402

HOOK = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "codex-inbox-hook.py"
CLAUDE_HOOK = (pathlib.Path(__file__).resolve().parent.parent
               / "bin" / "beacon-bus-inbox-hook.py")


def _src() -> str:
    return HOOK.read_text(encoding="utf-8")


class TestCodexHookDistinguishesAllFourReasons:
    def test_retired_reason_has_its_own_branch(self):
        """引退の理由が独立した分岐を持つ (= allowlist-miss に畳まれていない)。"""
        src = _src()
        assert "DOWNGRADE_RETIRED_CHANNEL" in src, (
            "Codex hook が引退の理由タグを見ていません。足す前の 2-way 分岐のままだと "
            "else が allowlist-miss を意味するので、引退した channel の降格が "
            "「channel を opt-in してください」と誤案内されます (ms-185 e-7077)。"
        )
        assert "downgraded_retired" in src

    def test_allowlist_miss_is_matched_on_the_empty_reason_not_on_else(self):
        """allowlist-miss を ``else`` ではなく「理由タグが空」で判定している。

        ``else`` で拾うと、将来 5 つ目の理由を足したときにまた誤案内が復活する。
        空文字列は allowlist-miss の sentinel として e-5803 review で固定されている。
        """
        src = _src()
        assert re.search(r"elif\s+not\s+reason\s*:", src), (
            "allowlist-miss を else で拾っています。理由タグが空であることで判定して、"
            "未知の理由は別に数えてください (= 新しい理由が黙って誤分類されない形)。"
        )

    def test_unrecognised_reason_is_counted_separately(self):
        """知らない理由タグを allowlist-miss に混ぜず、別に数えて開示する。"""
        src = _src()
        assert "downgraded_other" in src, (
            "知らない理由タグを別に数えていません。bus_delivery 側に理由が足されて "
            "この hook が追従していないとき、黙って誤分類されます。"
        )

    def test_notice_for_retired_tells_the_operator_to_remove_the_stale_optin(self):
        """引退の案内が「古い opt-in を外せ」を指している。

        opt-in し直す案内は逆効果 (古い opt-in が残っていることが問題)。
        """
        src = _src()
        assert "auto-execute remove --channel" in src, (
            "引退した channel の直し方として `beacon bus auto-execute remove "
            "--channel <名前>` を案内していません。opt-in し直す案内では直りません。"
        )

    def test_each_reason_gets_a_distinct_notice(self):
        """4 つの数え口がそれぞれ別の通知文を持つ (= 1 つの文に畳まれていない)。"""
        src = _src()
        for counter in ("downgraded_allowlist", "downgraded_provenance",
                        "downgraded_retired", "downgraded_other"):
            assert re.search(r"if\s+" + counter + r"\s*:", src), (
                counter + " に対応する通知の分岐がありません"
            )


class TestClaudeHookCarriesAnyReasonThrough:
    def test_claude_hook_does_not_enumerate_reasons(self):
        """Claude 側は理由を列挙せず、truthy ならそのまま載せる汎用経路のまま。

        こちらは理由を増やしても追従不要な形なので、**その形が維持されている**ことを
        固定する (列挙に変わると、新しい理由が落ちる側に回る)。
        """
        src = CLAUDE_HOOK.read_text(encoding="utf-8")
        assert 'ev["_downgrade_reason"] = downgrade_reason' in src, (
            "Claude hook が理由タグをそのまま載せる形でなくなっています。理由ごとの "
            "列挙に変えるなら、新しい理由を足すたびに両 hook を直す義務が増えます。"
        )


class TestReasonTagsAreAllDistinct:
    def test_no_two_reason_tags_collide(self):
        """理由タグが衝突していない (= 消費側の分岐が成立する前提)。"""
        tags = [bd.DOWNGRADE_NON_SYSTEM_ENVELOPE, bd.DOWNGRADE_RETIRED_CHANNEL, ""]
        assert len(set(tags)) == len(tags), (
            "理由タグが衝突しています: " + repr(tags)
            + " — 消費側が理由を区別できず、誤った直し方を案内します。"
        )
