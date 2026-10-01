"""ms-173 / e-6775 — state_detail は state に属する。両者を必ず一緒に動かす。

実害 (2026-10-01 実測 6 行): `state=running` なのに
`state_detail="Claude needs your permission"` がぶら下がった行が本番に 6 件あった。
session row は merge 保存なので、宣言が awaiting_human → running に変わっても client が
state_detail を省くと **古い待機内容が永久に残る**。消費側 (lib/working_target) は待ち状態
でしか detail を読まないため画面には出にくいが、行の中身は嘘になっており、detail を読む
consumer が増えた瞬間に表に出る。

固定する契約:

  * 待ちでない状態 (running / idle / terminated …) を宣言したら detail は空に揃える。
  * 待ち状態 (awaiting_human / blocked) の detail はそのまま通す (空なら空、でっち上げない)。
  * **宣言が無い heartbeat は何も触らない** (merge の既存値を保つ)。ここを空で上書きすると
    宣言を運ばない heartbeat が本物の待機内容を消してしまう。
  * 待ち状態の集合は lib/bus_liveness が唯一の定義 (消費側は別名参照のみ)。
"""

from __future__ import annotations

import bus_liveness
import working_target


class TestCanonicalSet:
    def test_wait_states_are_awaiting_human_and_blocked(self):
        assert bus_liveness.WAIT_DETAIL_STATES == frozenset(
            {bus_liveness.STATE_AWAITING_HUMAN, bus_liveness.STATE_BLOCKED})

    def test_consumer_reuses_the_canonical_set_not_a_copy(self):
        """複製を持つと、片方だけ直したときに静かに食い違う。"""
        assert working_target._WAIT_DETAIL_STATES is bus_liveness.WAIT_DETAIL_STATES

    def test_consumer_does_not_redefine_the_set(self):
        from pathlib import Path
        src = (Path(working_target.__file__)).read_text(encoding="utf-8")
        assert "frozenset({\n    bus_liveness.STATE_AWAITING_HUMAN" not in src, (
            "消費側が待ち状態の集合を再定義している (定義が 2 箇所になる)")

    def test_predicate_matches_the_set(self):
        for s in bus_liveness.ALL_STATES:
            assert bus_liveness.state_carries_wait_detail(s) == (
                s in bus_liveness.WAIT_DETAIL_STATES)


class TestDetailForDeclaration:
    def test_non_wait_state_clears_the_stale_detail(self):
        """e-6775 の本体: running の宣言は残骸を空で上書きする。"""
        assert bus_liveness.state_detail_for_declaration(
            "running", "Claude needs your permission") == ""

    def test_every_non_wait_state_clears(self):
        for s in sorted(bus_liveness.ALL_STATES - bus_liveness.WAIT_DETAIL_STATES):
            assert bus_liveness.state_detail_for_declaration(s, "残骸") == "", s

    def test_wait_state_detail_passes_through(self):
        assert bus_liveness.state_detail_for_declaration(
            "awaiting_human", "Claude needs your permission") \
            == "Claude needs your permission"
        assert bus_liveness.state_detail_for_declaration(
            "blocked", "外部要因で停止") == "外部要因で停止"

    def test_wait_state_without_detail_stays_empty(self):
        """待ち理由が分からないときは空。でっち上げない。"""
        assert bus_liveness.state_detail_for_declaration("awaiting_human", None) == ""
        assert bus_liveness.state_detail_for_declaration("awaiting_human", "") == ""

    def test_no_declaration_touches_nothing(self):
        """宣言を運ばない heartbeat で空上書きすると、本物の待機内容が消える。
        ここが None でなくなったら、待ち理由が毎 poll で消える退行になる。"""
        for missing in (None, "", 0):
            assert bus_liveness.state_detail_for_declaration(missing, "本物の待ち") is None


class TestServerWiring:
    """判定が正しくても保存口が呼んでいなければ行は直らない。"""

    @classmethod
    def setup_class(cls):
        from pathlib import Path
        cls.src = (Path(bus_liveness.__file__).resolve().parents[1]
                   / "server" / "routers_projects.py").read_text(encoding="utf-8")

    def test_upsert_applies_the_invariant(self):
        """呼んでいるだけでなく、**結果を payload に書いている** ことまで見る。

        呼び出しの存在だけを見ると、戻り値を捨てる変更 (= 判定は走るが行は直らない) を
        素通りさせる。実際このセッションで同型の事故を起こしている: wrapper の rename 時に
        純関数を引数なしで呼ぶ形が残り、ガードが常に false を返して孤児の退場が発火しなく
        なった (channel/bus.mjs, 独立レビュー 保守性 M-1 の作業中)。呼び出しの存在ではなく
        結果の行き先を固定する。
        """
        assert "bus_liveness.state_detail_for_declaration(" in self.src, (
            "保存口が不変条件を適用していない = 古い待機内容が残り続ける")
        i = self.src.index("bus_liveness.state_detail_for_declaration(")
        block = self.src[i:i + 300]
        assert 'payload["state_detail"] = ' in block, (
            "判定結果を payload に書いていない = 判定が走るだけで行は直らない")

    def test_applied_inside_the_session_upsert(self):
        """session log 側ではなく session row の upsert に入っていること。"""
        i = self.src.index("def upsert_session(")
        j = self.src.index("def upsert_session_log(")
        assert i < self.src.index("bus_liveness.state_detail_for_declaration(") < j

    def test_none_result_does_not_write(self):
        """None (= 宣言なし) のとき payload に触らないガードがあること。"""
        i = self.src.index("bus_liveness.state_detail_for_declaration(")
        block = self.src[i:i + 300]
        assert "is not None" in block, (
            "宣言なしでも payload を書いている = 本物の待機内容を消す")
