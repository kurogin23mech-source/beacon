"""ms-166 / e-6917 — the threshold dedup record must not split when ONE session
walks into a git worktree.

Pre-fix data flow (the bug, observed 2026-10-09 by a peer session):
  hook(S) in main cwd      → ./.claude/context-usage/S.json  {notified:[20,40]}
  hook(S) in worktree cwd  → ./.worktrees/X/.claude/.../S.json {notified:[]}
                             → "nothing notified yet" → 40 % note RE-FIRES
  hook(S) back in main cwd → the first copy, which never saw 60 → RE-FIRES

The dedup key is the Claude Code ``session_id``, which does not change when the
session changes directory — but the STORE was cwd-relative, so one session kept
two independent dedup lists. ``/beacon-session-fork`` makes worktree hopping
normal, so the heaviest users hit it first.

Post-fix: two facts, two homes.
  * "% for this terminal" stays per cwd (``.claude/context-usage/``) because
    channel/bus-context-usage.mjs reads it there to paint that terminal's badge.
  * "thresholds this SESSION was notified about" moves under
    ``git rev-parse --git-common-dir``, which is identical from every worktree
    of one repository. Outside git it falls back to the per-cwd directory.

These tests are deliberately hermetic: every one builds its own git repository
under ``tmp_path`` and passes an explicit ``cwd``. Nothing here reads the
developer's checkout, and no test shells out to the ``beacon`` CLI (a sibling
suite was red in CI for exactly that reason — the CLI exited "no project" before
reaching the behaviour under test).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beacon_cli.hooks import context_monitor as cm  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is required to build the worktree fixture",
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


@pytest.fixture()
def repo_and_worktree(tmp_path: Path):
    """A real repository plus a linked worktree — the shape that split state."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@example.com", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "f.txt").write_text("x", encoding="utf-8")
    _git("add", "f.txt", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "side", str(wt), cwd=main)
    return main, wt


# ---------------------------------------------------------------------------
# The fix: one session, one dedup record, regardless of which worktree it is in
# ---------------------------------------------------------------------------


def test_dedup_record_is_the_same_file_from_main_checkout_and_from_worktree(
    repo_and_worktree,
):
    main, wt = repo_and_worktree
    assert cm._dedup_path_for("S", cwd=main) == cm._dedup_path_for("S", cwd=wt)


def test_the_per_cwd_record_is_what_used_to_split(repo_and_worktree):
    """Contrast case: pins WHY the move was needed, so a revert is visible."""
    main, wt = repo_and_worktree
    assert (main / cm.STATE_DIR_REL) != (wt / cm.STATE_DIR_REL)


def test_threshold_does_not_refire_after_the_session_walks_into_a_worktree(
    repo_and_worktree, monkeypatch,
):
    """The behaviour the user actually feels: no second 40 % note request."""
    main, wt = repo_and_worktree
    monkeypatch.setenv("BEACON_PARENT_PID", str(os.getpid()))

    # Turn 1, in the main checkout: 20 and 40 have been notified.
    monkeypatch.chdir(main)
    cm._persist_state("S", [20, 40], context_pct=42)

    # Turn 2, same session, now inside the worktree.
    monkeypatch.chdir(wt)
    _, notified = cm._load_state(cm._dedup_path_for("S"), "S")
    assert notified == [20, 40], (
        "the session's notified list did not survive the move into the "
        "worktree — the 40 % threshold would be announced a second time"
    )
    triggered, _ = cm._classify_thresholds(42, notified)
    assert triggered is None, f"threshold {triggered} re-fired after the move"


def test_crossing_back_into_the_main_checkout_sees_the_worktree_turns(
    repo_and_worktree, monkeypatch,
):
    """The peer's 4th observation: the return trip re-fired 60 %."""
    main, wt = repo_and_worktree
    monkeypatch.setenv("BEACON_PARENT_PID", str(os.getpid()))

    monkeypatch.chdir(wt)
    cm._persist_state("S", [20, 40, 60], context_pct=61)

    monkeypatch.chdir(main)
    _, notified = cm._load_state(cm._dedup_path_for("S"), "S")
    assert notified == [20, 40, 60]
    assert cm._classify_thresholds(61, notified)[0] is None


# ---------------------------------------------------------------------------
# What must NOT change: the bridge's per-cwd record, and non-git behaviour
# ---------------------------------------------------------------------------


def test_per_cwd_record_is_still_written_so_the_bridge_keeps_its_badge(
    repo_and_worktree, monkeypatch,
):
    main, _ = repo_and_worktree
    monkeypatch.setenv("BEACON_PARENT_PID", str(os.getpid()))
    monkeypatch.chdir(main)
    cm._persist_state("S", [20], context_pct=23)

    own = main / cm.STATE_DIR_REL / "S.json"
    assert own.is_file(), (
        "channel/bus-context-usage.mjs reads this path in the cwd it was "
        "started in; moving the dedup record must not take it away"
    )
    rec = json.loads(own.read_text(encoding="utf-8"))
    assert rec["context_pct"] == 23
    assert rec["session_id"] == "S"


def test_outside_a_git_repository_it_falls_back_to_the_per_cwd_directory(
    tmp_path: Path,
):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert cm._worktree_shared_base(plain) is None
    assert cm._dedup_path_for("S", cwd=plain) == Path(cm.STATE_DIR_REL) / "S.json"


def test_a_relative_git_common_dir_is_resolved_against_the_cwd_we_asked_in(
    repo_and_worktree,
):
    """git answers ".git" from the main checkout; an unresolved relative path
    would make the record land wherever the process happens to be."""
    main, _ = repo_and_worktree
    base = cm._worktree_shared_base(main)
    assert base is not None and base.is_absolute()
    # Absolute is not enough: resolving ".git" against the PROCESS cwd instead
    # of the cwd we asked about also yields an absolute path — just the wrong
    # one. Pin the identity.
    assert base == (main / ".git").resolve(), (
        f"the shared base {base} is not this repository's common dir"
    )


# ---------------------------------------------------------------------------
# Pruning: must work where _pid_alive must not run (Windows)
# ---------------------------------------------------------------------------


def _write_record(path: Path, **fields) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def _stamp(days_ago: float) -> str:
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_a_week_old_record_is_pruned_without_any_liveness_probe(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(
        cm, "_pid_alive",
        lambda pid: pytest.fail("_pid_alive must never run on a non-POSIX host"),
    )
    keep = tmp_path / "keep.json"
    _write_record(keep, session_id="me", updated_at=_stamp(0))
    old = tmp_path / "old.json"
    _write_record(old, session_id="gone", pids=[1], updated_at=_stamp(30))

    cm._prune_stale_state_files(tmp_path, keep=keep, posix=False)

    assert not old.exists(), "abandoned record survived on a non-POSIX host"
    assert keep.exists()


def test_a_recent_record_is_left_alone_on_a_host_with_no_probe(
    tmp_path: Path, monkeypatch,
):
    keep = tmp_path / "keep.json"
    _write_record(keep, session_id="me", updated_at=_stamp(0))
    live = tmp_path / "live.json"
    _write_record(live, session_id="other", pids=[1], updated_at=_stamp(0.01))

    cm._prune_stale_state_files(tmp_path, keep=keep, posix=False)

    assert live.exists(), (
        "a record refreshed moments ago was deleted — that resets a live "
        "session's dedup state and re-fires its thresholds"
    )


def test_an_unjudgeable_record_is_left_alone(tmp_path: Path):
    keep = tmp_path / "keep.json"
    _write_record(keep, session_id="me", updated_at=_stamp(0))
    mystery = tmp_path / "mystery.json"
    _write_record(mystery, session_id="?")  # no pids, no updated_at

    cm._prune_stale_state_files(tmp_path, keep=keep, posix=False)

    assert mystery.exists()


def test_on_posix_a_dead_pid_still_decides(tmp_path: Path, monkeypatch):
    """The exact POSIX path must not be weakened into "wait a week"."""
    if os.name != "posix":
        pytest.skip("POSIX-only branch")
    monkeypatch.setattr(cm, "_pid_alive", lambda pid: False)
    keep = tmp_path / "keep.json"
    _write_record(keep, session_id="me", updated_at=_stamp(0))
    dead = tmp_path / "dead.json"
    _write_record(dead, session_id="gone", pids=[424242], updated_at=_stamp(0))

    cm._prune_stale_state_files(tmp_path, keep=keep)

    assert not dead.exists()


# ---------------------------------------------------------------------------
# The upgrade itself must not re-fire (the one-time seed), driven through the
# hook's real entry point — a pure-helper test would not have caught a caller
# that forgets to seed.
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, input_tokens: int) -> None:
    line = {"message": {"role": "assistant", "model": "claude-opus-4-8",
                        "usage": {"input_tokens": input_tokens}}}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")


@pytest.fixture()
def repo_project(repo_and_worktree, monkeypatch):
    """A cwd that is BOTH a git repository and a beacon project.

    Both halves are needed: ``_main_impl`` returns early without
    ``.beacon/project.json``, and the dedup path only leaves the per-cwd
    directory when git can answer. Every outward side effect is stubbed.
    """
    main, _ = repo_and_worktree
    (main / ".beacon").mkdir(exist_ok=True)
    (main / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8"
    )
    monkeypatch.chdir(main)
    monkeypatch.delenv("BEACON_CONTEXT_MONITOR_DRY_RUN", raising=False)
    monkeypatch.delenv("BEACON_CONTEXT_LIMIT", raising=False)
    monkeypatch.setattr(cm, "_which_beacon", lambda: None)
    monkeypatch.setattr(cm, "_build_recent_commits_text", lambda cwd: "")
    monkeypatch.setattr(cm, "_build_pending_tasks_text", lambda beacon, cwd: "")
    return main


def _run_hook(payload: dict, monkeypatch):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    rc = cm.main()
    return rc, out.getvalue()


def test_the_upgrade_itself_does_not_refire_an_already_notified_threshold(
    repo_project, monkeypatch,
):
    """A session already running when this fix lands holds its notified list
    only in the per-cwd record. If the hook ignored it, the first turn after the
    upgrade would announce 20 % a second time — the very symptom being fixed."""
    main = repo_project
    per_cwd = main / cm.STATE_DIR_REL / "S.json"
    per_cwd.parent.mkdir(parents=True, exist_ok=True)
    per_cwd.write_text(
        json.dumps({"session_id": "S", "notified_thresholds": [20]}),
        encoding="utf-8",
    )
    assert not cm._dedup_path_for("S").exists(), "fixture must start pre-upgrade"

    t = main / "t.jsonl"
    _write_transcript(t, 230_000)  # 23 % of 1M — past 20, short of 40

    rc, out = _run_hook({"session_id": "S", "transcript_path": str(t)}, monkeypatch)

    assert rc == 0
    assert not out.strip(), (
        "the first turn after the upgrade re-announced a threshold the session "
        f"had already been notified about (output: {out.strip()[:200]})"
    )
    _, carried = cm._load_state(cm._dedup_path_for("S"), "S")
    assert carried == [20], f"the dedup record was not seeded: {carried}"


def test_a_genuinely_new_threshold_still_fires_after_the_upgrade(
    repo_project, monkeypatch,
):
    """The seed must not be a blanket "stay quiet" — guards against fixing the
    re-fire by simply never notifying again."""
    main = repo_project
    per_cwd = main / cm.STATE_DIR_REL / "S.json"
    per_cwd.parent.mkdir(parents=True, exist_ok=True)
    per_cwd.write_text(
        json.dumps({"session_id": "S", "notified_thresholds": [20]}),
        encoding="utf-8",
    )
    t = main / "t.jsonl"
    _write_transcript(t, 430_000)  # 43 % — 40 has NOT been notified yet

    rc, out = _run_hook({"session_id": "S", "transcript_path": str(t)}, monkeypatch)

    assert rc == 0
    assert out.strip(), "40 % was never announced, so the seed silenced too much"


def test_sandboxing_state_dir_contains_every_write(
    repo_and_worktree, monkeypatch, tmp_path,
):
    """A caller that redirects the records must not have one escape into the
    repository's git directory. Before the fix the dedup base resolved itself,
    so `state_dir=` looked like a complete sandbox and was not one (AX review of
    PR #789)."""
    main, _ = repo_and_worktree
    monkeypatch.setenv("BEACON_PARENT_PID", str(os.getpid()))
    monkeypatch.chdir(main)
    sandbox = tmp_path / "sandbox"
    legacy = tmp_path / "sandbox" / "legacy.json"

    cm._persist_state("S", [20], context_pct=23,
                      state_dir=sandbox, legacy_path=legacy)

    escaped = list((main / ".git").rglob("*.json"))
    assert not escaped, f"a write escaped the sandbox into .git: {escaped}"
    assert (sandbox / "S.json").is_file()


# --- PR #789 の独立レビュー由来 (全 5 件のうち、答えに依存しない 4 件) ---------
#
# 再実行した独立レビュー (AX / 保守性、同じ差分) が、前回のレビューが出さなかった
# 5 件を返した。以下はそのうち「docstring が主張しているのに実装が守っていない」型を
# 機械で測る形に落としたもの。prose は advisory だが、ここで測れば prose が規則になる。


def test_sandboxing_state_dir_alone_contains_every_write(tmp_path, monkeypatch):
    """``state_dir`` だけで 3 つの書き込み**全部**が中に収まること。

    保守性レビューが実測で見つけた穴: write (2) の dedup は state_dir に落ちる形に
    直っていたが、write (3) の legacy だけ素の相対パスのままで、docstring は
    「全ての書き込みを閉じ込める」と主張していた。state_dir だけを渡すと
    ``.claude/context-usage-state.json`` がプロセスの cwd に残る = 隔離したはずの
    テストが開発者の作業フォルダを汚す形 (スイート自身のガードが捕まえる対象そのもの)。

    **守る状態への書き手を 1 つ塞いで「全部閉じた」と称した**のは、この repo で
    3 回踏んだ型なので、ここでは「外に 1 件も出ない」を全走査で測る。
    """
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    sandbox = work / "sandbox"
    cm._persist_state("S-sandbox", [20], context_pct=23, state_dir=sandbox)

    inside = {p for p in sandbox.rglob("*") if p.is_file()}
    assert inside, "サンドボックスの中に何も書かれていない (= 測れていない)"
    outside = [p for p in work.rglob("*")
               if p.is_file() and sandbox not in p.parents and p != sandbox]
    assert outside == [], (
        "state_dir だけ渡したのに外へ書き込みが逃げた: "
        + repr([str(p.relative_to(work)) for p in outside]))


def test_the_exact_dir_override_is_used_verbatim(tmp_path):
    """上書きは**そのまま**使い、計算された base と違って子フォルダを足さないこと。

    AX レビュー: 旧名 ``dedup_dir`` は「同じ種類の base を差し替えただけ」と読めて、
    次の呼び出し元が ``DEDUP_DIR_NAME`` の子を期待する。名前と docstring で契約を
    予告し、その契約をここで固定する。
    """
    got = cm._dedup_path_for("S", tmp_path)
    assert got == tmp_path / "S.json", got
    assert cm.DEDUP_DIR_NAME not in str(got), (
        "上書きに子フォルダが足されている — 契約と違う")


def test_an_old_posix_record_without_pids_is_pruned_not_left_alone():
    """POSIX で pids が無い記録は「放置」ではなく年齢で掃除されること。

    AX レビュー: docstring は「どちらの判定もできない記録 (POSIX で pids 無し /
    それ以外で updated_at が読めない) は放置」と読め、POSIX + pids 無しが永久に
    安全だと誤読させていた。実際は非 POSIX と同じ 7 日の時計で消える。

    放置されるのは **両方**の判定ができない 1 ケースだけ、という実挙動を固定する。
    """
    old = (datetime.utcnow() - timedelta(days=cm.ABANDONED_AFTER_DAYS + 1)
           ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cm._record_is_abandoned({"updated_at": old}, posix=True) is True
    assert cm._record_is_abandoned({"pids": [], "updated_at": old},
                                   posix=True) is True
    # 放置される唯一のケース: pids の判定も updated_at の判定もできない
    assert cm._record_is_abandoned({}, posix=True) is False
    assert cm._record_is_abandoned({"updated_at": "not-a-date"},
                                   posix=True) is False


def test_every_worktree_fallback_branch_is_logged(tmp_path, capsys,
                                                  monkeypatch):
    """per-cwd への後退を**どの分岐でも**黙ってやらないこと。

    AX レビュー: git 呼び出しが一度こけただけで dedup が per-cwd に落ち、worktree を
    移った回に限って節目が再発火するのに、痕跡が残らない。分裂していた従来は常に
    再現したが、こちらは間欠的で診断できない = 「never worse」は正確でなかった。

    **3 分岐すべてを駆動する。** 最初に書いた版は repo 外 (= returncode != 0) しか
    駆動しておらず、例外分岐のログを消しても緑のままだった (反転 N4 で判明)。
    「ログを足した」と「どの後退でも残る」は別で、弱い側が実効になる。
    """
    class _Res:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    # 分岐 1: git 呼び出し自体が失敗 (OSError / timeout)
    def _raise(*a, **k):
        raise OSError("git missing")
    monkeypatch.setattr(cm.subprocess, "run", _raise)
    assert cm._worktree_shared_base(tmp_path) is None
    err = capsys.readouterr().err
    assert "worktree-shared base unavailable" in err and "per-cwd" in err, (
        "例外分岐の後退が記録されていない: " + repr(err))

    # 分岐 2: repo 外 (非 0 終了)
    monkeypatch.setattr(cm.subprocess, "run",
                        lambda *a, **k: _Res(128, "", "not a git repository"))
    assert cm._worktree_shared_base(tmp_path) is None
    err = capsys.readouterr().err
    assert "worktree-shared base unavailable" in err and "per-cwd" in err, (
        "非 0 終了の後退が記録されていない: " + repr(err))

    # 分岐 3: 成功したのに何も返ってこない
    monkeypatch.setattr(cm.subprocess, "run", lambda *a, **k: _Res(0, "  \n"))
    assert cm._worktree_shared_base(tmp_path) is None
    err = capsys.readouterr().err
    assert "worktree-shared base unavailable" in err and "per-cwd" in err, (
        "空応答の後退が記録されていない: " + repr(err))


def test_the_fallback_log_distinguishes_outside_repo_from_a_failed_call(
        tmp_path, capsys, monkeypatch):
    """「repo 外」(想定内) と「git 呼び出しが失敗」(想定外) を書き分けること。

    どちらも同じ文面だと、grep しても間欠的な異常を想定内のノイズから選り分けられない。
    """
    class _Res:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    monkeypatch.setattr(cm.subprocess, "run",
                        lambda *a, **k: _Res(128, "", "not a git repository"))
    cm._worktree_shared_base(tmp_path)
    outside = capsys.readouterr().err

    def _raise(*a, **k):
        raise OSError("git missing")
    monkeypatch.setattr(cm.subprocess, "run", _raise)
    cm._worktree_shared_base(tmp_path)
    failed = capsys.readouterr().err

    assert outside != failed, (
        "repo 外と呼び出し失敗が同じ文面 — 診断時に選り分けられない:\n"
        + repr(outside))
    assert "exited 128" in outside, outside
    assert "OSError" in failed, failed
