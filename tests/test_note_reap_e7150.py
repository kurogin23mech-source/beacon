"""終了したセッションのメモだけを刈る (ms-178 / e-7150).

``note clear`` は「自分が書いたメモ」しか対象にしない (ms-160 e-6714)。その結果、
メモを片付けられる唯一の当事者はそのセッション自身であり、セッションは自分より
長生きしない。クリアせずに終わったセッション (落ちた / 殺された / e-7150 で id が
入れ替わって自分の分を見つけられなかった) のメモは、``--include-other-sessions``
以外のどの口からも届かなくなる。そしてその旗は **いま動いているセッションの
引き継ぎメモまで** 持っていく。だから安全な手は常に「放置」で、放置され続けた
(2026-10-25 時点で 180 件・16 セッション分、うち 97% が孤児)。

ここで固定する最重要の性質は「刈ってよい範囲」ではなく **刈ってはいけない範囲** —
生きているセッションのメモが孤児に分類されないこと、そして生存を確かめられない
ときは刈らずに止まること。生きているセッションの引き継ぎメモを食う reaper は、
reaper が無い状態より悪い。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import note_reap  # noqa: E402

NOTES = [
    {"ts": "t1", "text": "live session's handoff", "session_id": "sv-LIVE"},
    {"ts": "t2", "text": "ended session", "session_id": "sv-DEAD1"},
    {"ts": "t3", "text": "ended session again", "session_id": "sv-DEAD1"},
    {"ts": "t4", "text": "another ended", "session_id": "sv-DEAD2"},
    {"ts": "t5", "text": "legacy untagged"},
]


# --- 生存集合の組み立て ------------------------------------------------------

def test_live_set_is_a_union_of_every_source():
    """どの源が挙げたセッションも生存側に入る (= 片方だけ見て孤児にしない)。"""
    live, refusal = note_reap.resolve_live_ids(
        current_sid="sv-ME",
        session_json_sid="sv-FILE",
        bridge_sids=["sv-BRIDGE"],
        server_sids=["sv-SERVER"],
        cloud_mode=True,
    )
    assert refusal == ""
    assert live == {"sv-ME", "sv-FILE", "sv-BRIDGE", "sv-SERVER"}


def test_session_json_sid_is_kept_not_reaped():
    """session.json の id は「自分の」とは限らないが、だからこそ残す側。

    共有 cwd では session.json は最後に mint したセッションの id を持つ。
    それは他人のものかもしれない = 生きている誰かのものなので、削除側に
    入れてはならない。
    """
    live, _ = note_reap.resolve_live_ids(
        current_sid="sv-ME", session_json_sid="sv-SOMEONE_ELSE", cloud_mode=False)
    assert "sv-SOMEONE_ELSE" in live


def test_cloud_mode_refuses_when_server_unreadable():
    """cloud では、生存一覧が取れないなら刈らない。

    cloud プロジェクトには、このマシンから数えられないセッション (別マシン /
    別 worktree) が居る。一覧が欠けたまま進むと、それらが全部「孤児」に
    なる。止まる方が安い (後で再実行すればよい)。
    """
    live, refusal = note_reap.resolve_live_ids(
        current_sid="sv-ME", server_error="connection refused", cloud_mode=True)
    assert refusal == note_reap.REFUSE_SERVER_UNREADABLE
    assert live == set()


def test_cloud_mode_refuses_when_server_list_is_none():
    """「取りに行っていない」(None) も「取れなかった」と同じ扱い。

    空リスト (= 本当に 0 件) と区別する。None を空リストとして扱うと、
    一度も問い合わせていないのに「生きているセッションは無い」と読める。
    """
    live, refusal = note_reap.resolve_live_ids(
        current_sid="sv-ME", server_sids=None, cloud_mode=True)
    assert refusal == note_reap.REFUSE_SERVER_UNREADABLE


def test_cloud_mode_accepts_a_genuinely_empty_server_list():
    """サーバが「0 件」と答えたのは答えなので、進んでよい。"""
    live, refusal = note_reap.resolve_live_ids(
        current_sid="sv-ME", server_sids=[], cloud_mode=True)
    assert refusal == ""
    assert live == {"sv-ME"}


def test_local_mode_does_not_need_the_server():
    """local プロジェクトは手元の源で数え切れるので、サーバ不在でも進む。"""
    live, refusal = note_reap.resolve_live_ids(
        current_sid="sv-ME", bridge_sids=["sv-B"],
        server_sids=None, cloud_mode=False)
    assert refusal == ""
    assert live == {"sv-ME", "sv-B"}


def test_blank_ids_do_not_enter_the_live_set():
    """空文字が生存集合に入ると、未記録メモが全部「生存」になって刈れなくなる。"""
    live, _ = note_reap.resolve_live_ids(
        current_sid="", session_json_sid="   ", bridge_sids=["", None],
        cloud_mode=False)
    assert live == set()


# --- 分類 --------------------------------------------------------------------

def test_classify_splits_by_writer_liveness():
    v = note_reap.classify(NOTES, {"sv-LIVE"})
    assert [n["text"] for n in v["kept"]] == ["live session's handoff"]
    assert len(v["orphans"]) == 3
    assert len(v["untagged"]) == 1
    assert v["by_session"] == {"sv-DEAD1": 2, "sv-DEAD2": 1}


def test_untagged_is_kept_by_default():
    """セッション未記録のメモは、既定では消さない。

    消せない理由は「終わった証拠が無い」こと。分かっているのは「誰のものか
    名乗っていない」だけで、それは終了の証拠ではない。情報の欠落を根拠に
    削除しない。
    """
    v = note_reap.classify(NOTES, {"sv-LIVE"})
    assert all("untagged" not in n["text"] for n in v["doomed"])

    v_opt_in = note_reap.classify(NOTES, {"sv-LIVE"}, include_untagged=True)
    assert any("untagged" in n["text"] for n in v_opt_in["doomed"])


def test_age_is_not_the_criterion():
    """古くても生きているセッションのものなら残る (「古い」≠「終わった」)。"""
    old_but_live = [{"ts": "2020-01-01T00:00:00+00:00", "text": "ancient",
                     "session_id": "sv-LIVE"}]
    v = note_reap.classify(old_but_live, {"sv-LIVE"})
    assert v["doomed"] == []
    assert len(v["kept"]) == 1


def test_predicate_agrees_with_classification():
    """実際に削除する述語と、操作者に見せた分類が食い違わない。

    見せた内訳と消す対象がずれるのが最も危ない壊れ方 (操作者は内訳を見て
    同意する)。全件で突き合わせる。
    """
    for include_untagged in (False, True):
        v = note_reap.classify(NOTES, {"sv-LIVE"},
                               include_untagged=include_untagged)
        doomed_keys = {(n.get("ts"), n.get("text")) for n in v["doomed"]}
        by_predicate = {
            (n.get("ts"), n.get("text")) for n in NOTES
            if note_reap.is_doomed(n, {"sv-LIVE"},
                                   include_untagged=include_untagged)
        }
        assert doomed_keys == by_predicate, include_untagged


def test_empty_live_set_still_keeps_untagged_by_default():
    """生存 0 件でも、未記録メモは既定で残る (既定の安全側が崩れない)。"""
    v = note_reap.classify(NOTES, set())
    assert len(v["orphans"]) == 4
    assert len(v["doomed"]) == 4
    assert len(v["untagged"]) == 1
