"""未判断の優先度を「低い」として数えないこと (ms-181 e-6999).

踏んだ穴
========
2026-10-15、ms-166 自身の目的達成レビューで測った: 未着手タスクが 16 件
(medium 5 / untriaged 11、highest/high は 0 件) 残っているのに、関門は
disposition を **1 件も要求せず**、承認は素通りする状態だった。関門が数える
区分が highest/high だけに狭まっていたため。

とくに ``untriaged`` の扱いが問題だった。これは ``core.UNTRIAGED_PRIORITY`` —
**重大度ではなく番号札** で、「機械が起票し、人がまだ重要度を判断していない」
を記録する。それを「数えない側」に置くことは、誰もしていない判断を黙って
下すことに等しい (= 「不明」を「低い」として読む)。同じ時刻に
untriaged-backlog トリガーはその同じ項目を債務として報告していたので、
システムの両半分が「数えるのか」について食い違っていた。

さらに judge が返した 16 件の disposition は **1 件も記録できなかった**
(「gated backlog ではない」として拒否された)。判断は実在したのに置き場が無く、
証拠本文に手で書いて逃がすことになった — 判断軌跡が落ちる形そのもの。

この試験は「広げた」ことだけでなく **広げすぎていない** ことも測る: 見た上で
低いと判断された medium/low/lowest は残余として数えない。判断済みで低いことと、
未判断であることの非対称が設計の要点。
"""

import os
import shutil
import subprocess
import sys

import pytest

import core
import transition_approval as ta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _task(priority, status="todo", tid="e-1"):
    return {"id": tid, "type": "task", "status": status,
            "meta": {"priority": priority}}


# ---------------------------------------------------------------------------
# 受入条件 1: which tiers are counted, stated — and untriaged is counted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("priority", ["highest", "high", "untriaged", ""])
def test_an_unjudged_or_important_tier_is_counted(priority):
    assert ta.is_backlog_gated(_task(priority)) is True, (
        f"priority={priority!r} の未着手タスクが数えられていません")


@pytest.mark.parametrize("priority", ["medium", "low", "lowest"])
def test_a_tier_judged_lower_is_residue_not_a_blocker(priority):
    """The other direction — without this, "gate everything" would pass above.

    attainment はアウトカム (AC) 軸で定義するので、見た上で低いと判断された
    作業を全部消化するまで閉じられない形にはしない。
    """
    assert ta.is_backlog_gated(_task(priority)) is False, (
        f"priority={priority!r} まで数えると、残余を消化するまで閉じられなくなります")


@pytest.mark.parametrize("status", ["done", "in_progress", "cancelled"])
def test_only_unstarted_work_is_counted(status):
    assert ta.is_backlog_gated(_task("untriaged", status=status)) is False


def test_every_known_tier_is_on_exactly_one_side():
    """数える / 数えない の 2 集合が、優先度の語彙を漏れなく二分すること。

    どちらにも載っていない区分があると、その区分は黙って数えられない側に
    落ちる — それが e-6999 で踏んだ状態 (untriaged がどちらの宣言にも無かった)。
    語彙は ``core`` が持つので、そちらから引いて突き合わせる。
    """
    gated = set(ta.BACKLOG_GATED_PRIORITIES)
    ungated = set(ta.BACKLOG_UNGATED_PRIORITIES)
    assert not (gated & ungated), f"両方に載っている区分: {gated & ungated}"

    vocabulary = set(core.VALID_PRIORITIES) | {core.UNTRIAGED_PRIORITY}
    unplaced = vocabulary - gated - ungated
    assert not unplaced, (
        f"どちらの宣言にも無い区分: {unplaced} — 黙って数えない側に落ちます")


def test_the_refusal_says_what_it_counted_and_what_it_did_not():
    """受入条件 1: 読み手が補集合を推測しなくて済むこと。"""
    import cmd_target
    msg = cmd_target._backlog_undisposed_message(
        "e-9", "ms-181", [_task("untriaged", tid="e-7124")])
    for tier in ta.BACKLOG_GATED_PRIORITIES:
        assert tier in msg, f"数える区分 {tier} が文面に出ていません"
    for tier in ta.BACKLOG_UNGATED_PRIORITIES:
        assert tier in msg, f"数えない区分 {tier} が文面に出ていません"
    assert "untriaged" in msg and "判断していない" in msg, msg


# ---------------------------------------------------------------------------
# 受入条件 2: a judge's determination always has somewhere to land
# ---------------------------------------------------------------------------

def _project_with_approval(priorities):
    """A project whose milestone carries one task per given priority, plus a
    pending approval entry id to hang dispositions on."""
    tasks = [_task(p, tid=f"e-{i + 1}") for i, p in enumerate(priorities)]
    data = {
        "name": "t", "profession": "dev",
        "milestones": [{
            "id": "ms-1", "title": "m", "status": "in_progress",
            "progress": 50, "entries": list(tasks),
        }],
    }
    eid = core.target_transition_approval_add(
        data, "ms-1", old_state="in_progress", new_state="done",
        intent="probe", actor="tester")
    return data, eid


def test_a_disposition_can_be_recorded_for_an_ungated_task():
    """判定は実在したのに置き場が無い、を無くす (medium は数えないが記録はできる)。"""
    data, eid = _project_with_approval(["medium"])
    core.target_transition_approval_attach_disposition(
        data, eid, task_id="e-1", verdict="superseded",
        reason="要件変更で不要", source="independent-judge:probe", actor="tester")
    recorded = ta.disposition_map(core.find_entry(data, eid)[2])
    assert "e-1" in recorded
    assert recorded["e-1"]["verdict"] == "superseded"


def test_recording_an_ungated_disposition_does_not_make_it_block():
    """記録できる範囲を広げても、塞ぐ範囲は広げない。

    関門は gated な部分集合しか読まない。ここが崩れると「記録できる = 塞ぐ」に
    なり、残余を消化するまで閉じられなくなる。
    """
    data, _eid = _project_with_approval(["medium"])
    target = data["milestones"][0]
    assert core.unstarted_priority_tasks(target) == []


def test_a_stray_task_id_is_still_refused():
    """広げたことで、存在しない id が関門を偽って満たせるようになっていないこと。"""
    data, eid = _project_with_approval(["high"])
    with pytest.raises(ValueError) as exc:
        core.target_transition_approval_attach_disposition(
            data, eid, task_id="e-nope", verdict="done", reason="",
            source="probe", actor="tester")
    assert "is not a task of" in str(exc.value)


# ---------------------------------------------------------------------------
# 受入条件 4: inversion — restore the narrow set and watch a test go red
# ---------------------------------------------------------------------------

def test_narrowing_the_gate_back_to_highest_high_goes_red(tmp_path):
    """untriaged を数えない状態に戻したら赤くなることを実走で確かめる。

    影の**コピー**を壊す (並列実行で他の worker に書き換え途中のファイルを
    見せないため — scripts/check-tests-dont-mutate-repo.py の要求)。
    """
    tree = tmp_path / "tree"
    for sub in ("lib", "tests"):
        shutil.copytree(os.path.join(REPO, sub), tree / sub,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    target = tree / "lib" / "transition_approval.py"
    src = target.read_text(encoding="utf-8")
    anchor = 'BACKLOG_GATED_PRIORITIES = ("highest", "high", "untriaged")'
    assert src.count(anchor) == 1, (
        "宣言が動きました — この反転を消さずに追従させてください "
        "(消すと受入条件 4 が測られなくなります)")
    target.write_text(
        src.replace(anchor, 'BACKLOG_GATED_PRIORITIES = ("highest", "high")', 1),
        encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:randomly",
         "-p", "no:cacheprovider",
         "tests/test_attainment_backlog_tiers_ms181.py",
         "-k", "unjudged_or_important or exactly_one_side"],
        cwd=str(tree), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc")})
    assert proc.returncode != 0, (
        "関門を highest/high に戻しても試験が緑のままです = 数える区分を"
        "測れていません:\n" + proc.stdout[-2000:])


def test_widening_the_gate_to_everything_also_goes_red(tmp_path):
    """逆向きも測る: medium まで数える形にしたら赤くなること。

    片側だけの反転だと「全部数える」実装でも両方の試験が通ってしまう。
    """
    tree = tmp_path / "tree"
    for sub in ("lib", "tests"):
        shutil.copytree(os.path.join(REPO, sub), tree / sub,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    target = tree / "lib" / "transition_approval.py"
    src = target.read_text(encoding="utf-8")
    anchor = 'BACKLOG_GATED_PRIORITIES = ("highest", "high", "untriaged")'
    target.write_text(
        src.replace(
            anchor,
            'BACKLOG_GATED_PRIORITIES = ("highest", "high", "untriaged", '
            '"medium", "low", "lowest")', 1),
        encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:randomly",
         "-p", "no:cacheprovider",
         "tests/test_attainment_backlog_tiers_ms181.py",
         "-k", "residue_not_a_blocker or exactly_one_side"],
        cwd=str(tree), capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc2")})
    assert proc.returncode != 0, (
        "medium まで数える形にしても試験が緑のままです:\n" + proc.stdout[-2000:])
