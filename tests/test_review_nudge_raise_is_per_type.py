"""促しを **足す** 方向が種別ごとであることの固定 (ms-119)。

**何が起きていた** (独立 AX レビュー 7 周目, 2026-11-10): 促しは 2 方向で維持される
派生状態 (head が進んだら足す / もう止めていないなら下げる) だが、**下げる方向にしか
専用の試験が無かった**。足す側の判定は「この PR のトリガーが 1 件でも在れば何もしない」
という全か無かで、その結果:

    1 種別だけ実施済み (促しのファイルは消えている) + 別種別は未実施 (ファイルは
    残っている) の状態で head が進むと、**実施済み側の促しが二度と復活しない**。

関門は種別ごとに sha を比べるので両方を要求する。つまり同じ瞬間に「促しは 1 種別しか
出さない」「関門は 2 種別 owed」という食い違いが起きる。関門が止めるので即座のマージ
事故にはならないが、促しだけを見た AI は『あちらは済んでいる』と誤った作業計画を組む。
``BEACON_REVIEW_GATE_CI`` 未設定のリポジトリでは関門を経由しないマージ経路があるので、
この課題が名指している「促しの空リストを信じて直接マージへ進む」型に直結する。

**なぜ 7 周目まで残ったか**: 足す側の発火元 (``_auto_fire_pr_open_review_triggers_for_open_prs``)
は ``PYTEST_CURRENT_TEST`` で即 return するため、**試験から駆動できない形**だった。
判定を ``nudge_types_to_fire_for_pr`` に切り出して、ここで両方向を測れるようにした。
"""

import json

import pytest

import cmd_trigger
import commands_shared as cs
import review_spine as rs

HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEXT = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PR, REPO = "838", "o/r"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    tdir = tmp_path / "triggers"
    tdir.mkdir()
    monkeypatch.setattr(cs, "_get_triggers_dir", lambda: str(tdir))
    monkeypatch.setattr(cmd_trigger, "_get_triggers_dir", lambda: str(tdir))
    monkeypatch.setattr(cs, "_review_done_dir",
                        lambda: (str(tmp_path / "ledger"), True))
    return tdir


def _required():
    return list(rs.required_pr_open_review_types())


def _record(review_type, sha):
    cs.record_review_done_across_worktrees(
        review_type, PR, REPO, reviewed_sha=sha)


def _fired(tdir):
    """いま促しのファイルが在る種別の集合。"""
    out = set()
    for fn in tdir.iterdir():
        p = cs.parse_review_due_filename(fn.name)
        if p is not None and p[1] == PR and p[2] == REPO:
            out.add(p[0])
    return out


def _write_nudge(tdir, review_type):
    (tdir / cs.review_due_filename(review_type, PR, REPO)).write_text(
        json.dumps({"review": review_type, "pr_repo": REPO}), encoding="utf-8")


def test_a_type_reviewed_at_an_old_head_is_raised_again(_isolated):
    """**本体**: 片方が実施済みでも、head が進めばその種別の促しが復活する。

    修正前はここが空リストで、実施済み側は永久に復活しなかった。
    """
    required = _required()
    assert len(required) >= 2, "この試験は必須種別が 2 つ以上あることを前提にする"
    done_type, pending_type = required[0], required[1]

    # done_type は古い head に対して実施済み (= 促しのファイルは消えている)。
    _record(done_type, HEAD)
    # pending_type は未実施なので促しが残っている。
    _write_nudge(_isolated, pending_type)

    to_fire = cmd_trigger.nudge_types_to_fire_for_pr(
        PR, REPO, NEXT, {pending_type})

    assert to_fire is not None, "導出が使えずに旧経路へ退いた (前提が崩れている)"
    fired_types = {t for t, _label in to_fire}
    assert done_type in fired_types, (
        "古い head に対する実施しかないのに、その種別の促しが復活していない "
        "(全か無かの判定が残っている): %r" % (fired_types,))
    assert pending_type not in fired_types, (
        "まだ促しが残っている種別を二重に発火しようとしている: %r" % (fired_types,))


def test_nothing_is_raised_when_every_type_covers_the_current_head(_isolated):
    """**偽の赤**も測る: いまの head に対して全種別が実施済みなら何も足さない。"""
    for t in _required():
        _record(t, NEXT)
    to_fire = cmd_trigger.nudge_types_to_fire_for_pr(PR, REPO, NEXT, set())
    assert to_fire == [], (
        "いまの head に対して充足しているのに促しを足そうとしている: %r" % (to_fire,))


def test_a_fresh_pr_raises_every_required_type(_isolated):
    """促しもレビュー記録も無い PR は、必須種別すべてが対象になる。"""
    to_fire = cmd_trigger.nudge_types_to_fire_for_pr(PR, REPO, NEXT, set())
    assert to_fire is not None
    assert {t for t, _l in to_fire} == set(_required())


def test_an_unknown_head_still_raises(_isolated):
    """head を確かめられないときは **促しを出す側に倒す**。

    促しは止める力を持たないので、過剰に出て煩いのは実害が小さく、出ないのは
    事故に直結する (この非対称は既存の判断で、ここで固定する)。
    """
    for t in _required():
        _record(t, NEXT)
    to_fire = cmd_trigger.nudge_types_to_fire_for_pr(PR, REPO, "", set())
    assert to_fire, (
        "head 不明なのに促しを 1 件も足さなかった (沈黙する側に倒れている)")


def test_a_broken_derivation_falls_back_instead_of_going_silent(monkeypatch,
                                                                _isolated):
    """導出が壊れたら ``None`` を返して旧経路に退く (沈黙しない)。"""
    def _boom(*a, **k):
        raise RuntimeError("derivation unavailable")
    monkeypatch.setattr(cmd_trigger, "pr_review_obligations", _boom)
    assert cmd_trigger.nudge_types_to_fire_for_pr(PR, REPO, NEXT, set()) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
