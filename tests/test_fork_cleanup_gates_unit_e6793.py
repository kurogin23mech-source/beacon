"""fork の片付けの 4 ゲートを、git も file system も触らずに測る (ms-166 e-6793)。

なぜ要るか
----------
``cmd_session_fork_cleanup`` は 1 関数 (約 270 行) の中で判定と実行を一手に行っていた
ため、``tests/test_fork_cleanup_preserves_notes_e6702.py`` の全ケースが「tmp_path に実
git リポジトリを init し、worktree を add し、fork.json / session.json /
session_notes.jsonl を書き、CLI を subprocess で起動する」フルセットアップを経由していた。
**ゲートの真偽表だけを見る軽量な単体テストが 1 つも無かった** (PR #770 の独立保守性
レビュー)。

実害は「未取り込みのときの文言を 1 語直したい」程度の変更でも、実 git 操作を伴う統合
テスト以外に検証手段が無いこと。この file がその穴を埋める — ここは **判定だけ** を見る
(配線が正しいかは既存の統合テスト群が担う、という責務分界)。

実行時間がそのまま目的の達成度になる (統合テスト 30 件で約 11 秒、ここは 1 秒未満)。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import fork_cleanup as fc  # noqa: E402

THRESHOLD = 15 * 60


def _facts(*, origin=True, merge=fc.MERGED, merge_err="",
           dirty=fc.CLEAN, files=(), dirty_err="") -> fc.GitFacts:
    return fc.GitFacts(origin, merge, merge_err, dirty, tuple(files), dirty_err)


def _record(**kw) -> dict:
    base = {"child_branch": "ms-1-fork-abc", "worktree_path": "/tmp/wt",
            "idle_seconds": 9999, "unpromoted_notes": 0,
            "notes_path": "/tmp/wt/.beacon/session_notes.jsonl",
            "own_session_id": "sv-x"}
    base.update(kw)
    return base


def _ok_backup(record, branch, n):
    return fc.BackupResult("/tmp/backup.jsonl", "")


def _run(record=None, *, force=False, facts=None, backup=_ok_backup):
    return fc.evaluate(record or _record(), force=force,
                       idle_threshold=THRESHOLD,
                       facts=facts or _facts(), backup=backup)


# --- 通る形 (= 全ゲート通過) --------------------------------------------------

def test_a_merged_idle_clean_fork_with_no_notes_is_removable():
    v = _run()
    assert v["refusals"] == [], v


# --- gate 1: 取り込み確認 ----------------------------------------------------

def test_an_unmerged_branch_is_refused():
    v = _run(facts=_facts(merge=fc.NOT_MERGED))
    assert any("取り込まれていません" in b for b in v["refusals"]), v


def test_a_missing_origin_main_says_so_instead_of_not_merged():
    """「未取り込み」と「比較できない」を同じ文言で報せない。

    混ぜると、人は決して成立しない「取り込まれるのを待つ」ループに入る
    (AX レビュー PR#770)。
    """
    v = _run(facts=_facts(origin=False))
    msg = " ".join(v["refusals"])
    assert "origin/main が見つかりません" in msg, v
    assert "取り込まれていません" not in msg, (
        "比較不能を未取り込みと報せています: " + msg)


def test_an_unknown_merge_result_is_refused_with_the_reason():
    v = _run(facts=_facts(merge=fc.MERGE_UNKNOWN, merge_err="fatal: bad rev"))
    msg = " ".join(v["refusals"])
    assert "取り込み確認に失敗" in msg and "fatal: bad rev" in msg, v


def test_a_fork_with_no_branch_is_refused_not_skipped():
    """child_branch が空の fork.json が取り込み確認を飛ばさないこと。

    ここは以前 ``if branch:`` で、メタデータ欠落という入口から、この verb が防ぐ
    はずの risk が素通りしていた (AX レビュー PR#770)。
    """
    v = _run(_record(child_branch=""), facts=_facts(merge=fc.NOT_MERGED))
    assert any("branch 情報が読めません" in b for b in v["refusals"]), v


# --- gate 1b: 未コミットの作業 -----------------------------------------------

def test_uncommitted_work_is_named_before_it_would_be_destroyed():
    """何が消えるかを名前で出すこと。

    以前は削除の段で git が exit 128 を返し、コードが **あらゆる失敗に対して**
    --force で再試行していたので、*未取り込み* を理由に --force を渡した人が
    気づかないまま未コミットの作業も消していた (2026-10-01 に実リポジトリで再現)。
    """
    v = _run(facts=_facts(dirty=fc.DIRTY, files=("a.py", "b.py")))
    msg = " ".join(v["refusals"])
    assert "未コミットの変更が 2 件" in msg, v
    assert "a.py" in msg and "b.py" in msg, "対象を名前で出していない: " + msg


def test_many_dirty_files_are_truncated_but_counted_exactly():
    v = _run(facts=_facts(dirty=fc.DIRTY,
                          files=tuple(f"f{i}.py" for i in range(9))))
    msg = " ".join(v["refusals"])
    assert "9 件" in msg, "件数が正確でない: " + msg
    assert "ほか" in msg, "列挙を打ち切っていない: " + msg


def test_an_unknown_dirty_state_is_refused():
    v = _run(facts=_facts(dirty=fc.DIRTY_UNKNOWN, dirty_err="not a worktree"))
    msg = " ".join(v["refusals"])
    assert "確認できませんでした" in msg and "not a worktree" in msg, v


# --- gate 2: 作業中かどうか --------------------------------------------------

def test_a_recently_active_fork_is_refused():
    v = _run(_record(idle_seconds=60))
    assert any("作業されています" in b for b in v["refusals"]), v


def test_an_unknown_idle_is_refused_because_unknown_is_not_free():
    v = _run(_record(idle_seconds=None))
    msg = " ".join(v["refusals"])
    assert "判定できません" in msg, v
    assert "『判定できない』は『空いている』ではありません" in msg, (
        "不明を空きと扱わない理由を出していない: " + msg)


def test_the_idle_threshold_boundary(monkeypatch):
    """閾値そのものは通し、1 秒でも手前なら止める (境界で落ちない)。"""
    assert _run(_record(idle_seconds=THRESHOLD))["refusals"] == []
    assert _run(_record(idle_seconds=THRESHOLD - 1))["refusals"] != []


# --- gate 3: メモの退避 (hard_blocker) ---------------------------------------

def test_notes_are_backed_up_before_deletion():
    v = _run(_record(unpromoted_notes=3))
    assert v["refusals"] == [], v
    assert v["backup_path"] == "/tmp/backup.jsonl", v


def test_a_failed_backup_is_a_hard_blocker_that_force_cannot_override():
    """**ここが --force と hard_blocker を分けている理由。**

    以前は 1 つの list を共有していたので --force が退避失敗のゲートも通り抜け、
    コメント・help・Skill が揃って「--force でも上書きできない」と約束していたのに
    実装がそうなっていなかった。コードが提供しない保証を文書が主張するのは、保証が
    無いより悪い — 人が --force に手を伸ばすのは、まさに復旧が要る場面だから。
    """
    def failing(record, branch, n):
        return fc.BackupResult("", "No space left on device")

    v = _run(_record(unpromoted_notes=3), backup=failing)
    assert any("退避に失敗" in b for b in v["hard_blockers"]), v
    # force でも通らないこと
    v2 = _run(_record(unpromoted_notes=3), backup=failing, force=True)
    assert v2["refusals"], "--force が hard_blocker を通り抜けています: " + repr(v2)
    assert any("退避に失敗" in b for b in v2["refusals"]), v2


def test_an_unreadable_notes_count_is_a_hard_blocker():
    """読めないことは空であることではない。

    件数が「ファイル無し」と「読めない」を 0 に潰していたので、一過性の IO エラーで
    退避の段が丸ごと飛ばされ、メモが消えていた (保守性レビュー PR#770)。
    """
    v = _run(_record(unpromoted_notes=None))
    assert any("0 件と確定できない" in b for b in v["hard_blockers"]), v
    v2 = _run(_record(unpromoted_notes=None), force=True)
    assert v2["refusals"], "--force が通り抜けています"


def test_a_missing_backup_port_is_treated_as_failure_not_as_no_notes():
    """退避の口が配線されていない場合を「退避不要」に倒さないこと。

    倒すと、呼び出し側の配線漏れが「メモごと削除してよい」に化ける。
    """
    v = fc.evaluate(_record(unpromoted_notes=2), force=True,
                    idle_threshold=THRESHOLD, facts=_facts(), backup=None)
    assert v["refusals"], "配線漏れが削除を許しています: " + repr(v)


# --- force の射程 ------------------------------------------------------------

def test_force_overrides_soft_blockers_only():
    """--force は「人が承知で受け入れられる risk」だけを上書きする。"""
    soft = _run(_record(idle_seconds=60), facts=_facts(merge=fc.NOT_MERGED))
    assert soft["refusals"], "soft blocker が出ていない"
    forced = _run(_record(idle_seconds=60), facts=_facts(merge=fc.NOT_MERGED),
                  force=True)
    assert forced["refusals"] == [], (
        "--force が soft blocker を上書きしていない: " + repr(forced))
    # ただし blockers 自体は消えない (報告には残る)
    assert forced["blockers"], "上書きしても理由の記録は残すこと"


# --- 事実への翻訳 (collect_git_facts) ----------------------------------------

def test_the_exit_code_meanings_are_translated_here_not_in_the_gates():
    """``--is-ancestor`` の終了コードの意味を、判定側に持ち込まないこと。

    1 = 祖先でない / 128 = 比較できない の区別は git の約束。これを判定が知って
    いると、判定が git に結合して真偽表で試験できなくなる。
    """
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def runner(argv, cwd):
        if argv[:2] == ["git", "rev-parse"]:
            return R(0)
        if "--is-ancestor" in argv:
            return R(1)                      # 祖先でない
        return R(0, "")                      # status: clean
    f = fc.collect_git_facts("/repo", "/wt", "br", runner=runner)
    assert f.merge_state == fc.NOT_MERGED, f

    def runner128(argv, cwd):
        if argv[:2] == ["git", "rev-parse"]:
            return R(0)
        if "--is-ancestor" in argv:
            return R(128, err="fatal: no upstream")
        return R(0, "")
    f = fc.collect_git_facts("/repo", "/wt", "br", runner=runner128)
    assert f.merge_state == fc.MERGE_UNKNOWN and "no upstream" in f.merge_error, f

    def runner_dirty(argv, cwd):
        if argv[:2] == ["git", "rev-parse"]:
            return R(0)
        if "--is-ancestor" in argv:
            return R(0)
        return R(0, " M a.py\n?? b.py\n")
    f = fc.collect_git_facts("/repo", "/wt", "br", runner=runner_dirty)
    assert f.dirty_state == fc.DIRTY and f.dirty_files == ("a.py", "b.py"), f


def test_the_gates_never_touch_git_or_the_filesystem():
    """判定側に副作用の口が無いこと (= 軽量に試せる、という目的そのもの)。"""
    import ast
    src = open(os.path.join(os.path.dirname(__file__), "..", "lib",
                            "fork_cleanup.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    ev = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "evaluate")
    called = {n.func.id for n in ast.walk(ev)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    forbidden = {"open", "makedirs", "copyfile", "run"}
    assert not (called & forbidden), (
        "evaluate が副作用の口を直接叩いています: " + repr(sorted(called & forbidden)))
    assert not [n for n in ast.walk(ev) if isinstance(n, ast.Import)
                or isinstance(n, ast.ImportFrom)], (
        "evaluate が import を抱えています (副作用の持ち込み口になります)")
