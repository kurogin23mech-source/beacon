"""ms-173 / e-6776 + 独立レビュー (AX-2) — 書き手の誤記を再発させない。

`last_heartbeat_at` を書くのは **CLI の session 解決** (lib/session の
get_or_mint_session_via_server が /api/me/heartbeat を叩く) の副産物。
**PostToolUse hook ではない。**

この誤記が散在していたせいで「hook が毎回書いている」と信じられ、throttle cache が
永久に当たって心拍が二度と出ない (= 健全性の「注目されているか」軸が構造的に死んでいる)
ことが長く見逃された。e-6776 で 2 箇所を訂正したが、独立レビュー (AX-2) が **同じ関数内に
もう 1 箇所、さらに別箇所に 1 箇所** 残っていることを指摘した。字面が残る限り同じ誤診が
再生産されるので、ガードで固定する。

却下した指摘 1 件: channel/bus.mjs の
「The previous heartbeat path (PostToolUse hook → `beacon session id` → upsert
last_active)」は **正確な履歴記述** (e-1318 以前の経路の説明) で、現在の書き手の誤記では
ない。だからこのガードは bus.mjs を対象にしない。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class TestNoPostToolUseMisattribution:
    """誤記の再発防止は **禁止語リストではなく正典への参照** で担保する。

    ms-173 独立レビュー 保守性 M-1: 初版はここに「3 ファイルの全行を走査し、1 行に
    PostToolUse と heartbeat が同時に出たら (訂正語を含まない限り) 落とす」という
    **散文に対する正規表現ガード** を置いていた。独立レビューはこれを、通過条件が
    「何を言っていいか」という曖昧な自然言語規則になっており、同じ 3 ファイルに
    『本当に正しい PostToolUse と heartbeat 両方の説明』を書いた次の AI が自分の変更と
    無関係に落ちる — と指摘した (medium)。採用して撤去し、下の
    :meth:`test_the_canonical_writer_is_documented_in_one_place` (= 書き手の事実を
    ``lib/session._cloud_mint_cache_hit`` の docstring 1 箇所に閉じ、他ファイルは
    そこを参照しているかだけを見る構造チェック) に一本化した。

    こちらの形の方が強い理由: 誤記が再発しうるのは「書き手の事実が散在して drift する」
    ときで、正典への参照が在れば次の書き手はそこを読む。散文の語の共起を禁じても、
    別の言い方で同じ誤記は書ける (= 緩いガードの偽の安全)。
    """

    def test_the_canonical_writer_is_documented_in_one_place(self):
        """書き手の正典が 1 箇所にあり、そこを指していること。"""
        sess = (REPO / "lib" / "session.py").read_text(encoding="utf-8")
        assert "_cloud_mint_cache_hit" in sess
        app = (REPO / "server" / "app.py").read_text(encoding="utf-8")
        assert "_cloud_mint_cache_hit" in app, (
            "server 側が書き手の正典を参照していない — 散在した説明が再び drift する")


class TestTimestampGlossary:
    """独立レビュー AX-1: 似た名前の時刻が 4 つあり書き手が違う。用語集を 1 箇所に置き、
    新設時に表の更新を強制する。"""

    #: session.json / session row に出る時刻フィールド。新設したら用語集も更新する。
    EXPECTED = (
        "last_cloud_heartbeat_sent_at",
        "last_active",
        "last_heartbeat_at",
        "last_poll_at",
    )

    def test_glossary_exists_and_covers_every_timestamp(self):
        sess = (REPO / "lib" / "session.py").read_text(encoding="utf-8")
        i = sess.index("セッション時刻フィールドの用語集")
        glossary = sess[i:i + 2000]
        missing = [f for f in self.EXPECTED if f not in glossary]
        assert not missing, (
            f"用語集に {missing} が載っていない — 書き手の違いが読めず e-6776 が再発する")

    def test_the_throttle_clock_name_says_it_was_sent(self):
        """名前だけで last_active と区別が付くこと (AX-1)。"""
        sess = (REPO / "lib" / "session.py").read_text(encoding="utf-8")
        assert "last_cloud_heartbeat_sent_at" in sess
        assert "last_cloud_heartbeat_at" not in sess.replace(
            "last_cloud_heartbeat_sent_at", ""), (
            "旧名が残っている — 近接同義語に戻っている")
