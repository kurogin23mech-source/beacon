"""Fork cleanup never destroys a fork's handoff notes (ms-178 e-6702 / e-6703).

`git worktree remove` deletes `.beacon/` along with the worktree, so a fork's
`session_notes.jsonl` died with it — no backup, no warning, no count. ms-178
hardened `note clear` on both CLI frontends, but cleanup is a THIRD writer to the
same state and bypassed both guards. Observed 2026-09-29: a fork was cleaned up
mid-session and three handoff notes holding review adjudications were lost from
BOTH stores (local file gone with the worktree; the cloud copies were absent too).

The enabling condition (e-6703) was that the picker listed every fork without
regard for whether a session was still working in one — documented in
skills/beacon-session-merge-back.md as deliberate future work — so a parallel
session could pull the ground out from under a live one.

Pinned here:
  - the listing carries the facts a caller needs to REFUSE (unpromoted note
    count, how long ago the fork was worked in), not just display fields;
  - cleanup snapshots notes OUTSIDE the worktree before deleting anything, and
    refuses when it cannot — the same "no backup ⇒ no delete" ordering as
    `note clear` (e-6656);
  - cleanup refuses on an unmerged branch, on a live fork, and when liveness is
    UNKNOWN ("no evidence of life" is not "evidence of no life");
  - the guard lives in the tool layer, so a caller cannot skip it. A guard in a
    Skill prompt is a request, not a constraint.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import session as session_mod  # noqa: E402


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one fork worktree, so `git worktree list` is honest."""
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f.txt").write_text("hi\n", encoding="utf-8")
    # mirror the real repo: .beacon/ is gitignored, so `git worktree remove`
    # does not see the fork's own state as untracked work
    (root / ".gitignore").write_text(".beacon/\n.worktrees/\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    # a fork worktree with fork.json, as /beacon-session-fork creates
    # bin/beacon's ensure_project needs a project marker at the repo root
    (root / ".beacon").mkdir(exist_ok=True)
    (root / ".beacon" / "project.json").write_text(
        json.dumps({"name": "t", "milestones": []}), encoding="utf-8")
    # a real `origin` so origin/main resolves: gate 1 now distinguishes
    # "cannot compare" from "not an ancestor", and the tests must exercise both
    _git("remote", "add", "origin", str(root), cwd=root)
    _git("update-ref", "refs/remotes/origin/main", "main", cwd=root)
    wt = root / ".worktrees" / "ms-9-fork-abc"
    _git("worktree", "add", "-q", str(wt), "-b", "ms-9-fork-abc", cwd=root)
    beacon = wt / ".beacon"
    beacon.mkdir(parents=True, exist_ok=True)
    (beacon / "fork.json").write_text(json.dumps({
        "target_ms_id": "ms-9", "target_ms_title": "t",
        "child_branch": "ms-9-fork-abc",
        "parent_session_id": "sv-parent", "parent_branch": "main",
        "created_at": "2026-09-29T00:00:00Z",
    }), encoding="utf-8")
    return root, wt


def _set_notes(wt, n):
    (wt / ".beacon" / "session_notes.jsonl").write_text(
        "".join(json.dumps({"ts": f"2026-09-29T0{i}:00:00+0900",
                            "text": f"note {i}"}) + "\n" for i in range(n)),
        encoding="utf-8")


def _set_activity(wt, seconds_ago):
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(seconds=seconds_ago))
    (wt / ".beacon" / "session.json").write_text(json.dumps({
        "session_id": "sv-child",
        "last_active": stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }), encoding="utf-8")


def _cleanup(root, wt, *extra, env=None):
    e = dict(os.environ)
    e.pop("BEACON_FORK_CLEANUP_FORCE", None)
    e["BEACON_FORK_PATH"] = str(wt)
    e["BEACON_JSON"] = "1"
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(ROOT / "lib" / "commands.py"), "session_fork_cleanup",
         *extra],
        cwd=str(root), capture_output=True, text=True, env=e)


# --- the listing must carry refusal-grade facts -----------------------------

def test_listing_reports_unpromoted_note_count(repo):
    root, wt = repo
    _set_notes(wt, 3)
    fk = session_mod.list_forks(root)[0]
    assert fk["unpromoted_notes"] == 3
    assert fk["notes_path"].endswith("session_notes.jsonl")


def test_listing_reports_how_long_ago_it_was_worked_in(repo):
    root, wt = repo
    _set_activity(wt, 30)
    fk = session_mod.list_forks(root)[0]
    assert fk["idle_seconds"] is not None and fk["idle_seconds"] < 120


def test_unknown_liveness_is_none_not_zero(repo):
    """None means UNKNOWN. Returning 0 would read as "active" and returning a big
    number as "idle" — both invent a fact. Absence must stay absent."""
    root, wt = repo
    assert session_mod.list_forks(root)[0]["idle_seconds"] is None


# --- no backup ⇒ no delete --------------------------------------------------

def test_notes_are_snapshotted_outside_the_worktree_before_removal(repo):
    """A backup inside the directory being deleted is not a backup."""
    root, wt = repo
    _set_notes(wt, 4)
    _set_activity(wt, 99999)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"], r.stdout + r.stderr
    backup = Path(out["notes_backup"])
    assert backup.exists(), out
    assert str(root / ".beacon" / "fork-notes-backup") in str(backup)
    assert not str(wt) in str(backup), "backup was placed inside the deleted tree"
    assert len([l for l in backup.read_text(encoding="utf-8").splitlines() if l.strip()]) == 4
    assert not wt.exists(), "worktree not removed"


def test_refuses_when_the_snapshot_cannot_be_written(repo, monkeypatch):
    """If the notes cannot be backed up, nothing is deleted."""
    root, wt = repo
    _set_notes(wt, 2)
    _set_activity(wt, 99999)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    # make the backup directory un-creatable by putting a FILE at its path
    (root / ".beacon").mkdir(exist_ok=True)
    (root / ".beacon" / "fork-notes-backup").write_text("x", encoding="utf-8")
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is False, r.stdout
    assert any("退避" in b for b in out["blockers"]), out
    assert wt.exists(), "worktree was removed despite an unwritable backup"


# --- liveness / merge gates -------------------------------------------------

def test_refuses_a_fork_that_is_still_being_worked_in(repo):
    root, wt = repo
    _set_notes(wt, 5)
    _set_activity(wt, 10)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is False
    assert any("作業されています" in b for b in out["blockers"]), out
    assert wt.exists()
    assert (wt / ".beacon" / "session_notes.jsonl").exists(), "notes destroyed"


def test_refuses_when_liveness_is_unknown(repo):
    """"Cannot tell" must not resolve to "safe to delete"."""
    root, wt = repo
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _cleanup(root, wt)  # no session.json written
    out = json.loads(r.stdout)
    assert out["removed"] is False
    assert any("判定できません" in b for b in out["blockers"]), out
    assert wt.exists()


def test_refuses_an_unmerged_branch(repo):
    """Removing an unmerged fork discards commits."""
    root, wt = repo
    _set_activity(wt, 99999)
    (wt / "new.txt").write_text("work\n", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "unmerged work", cwd=wt)
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is False
    assert any("取り込まれていません" in b for b in out["blockers"]), out
    assert wt.exists()


def test_force_overrides_but_still_takes_the_backup(repo):
    """--force is an override of the REFUSAL, not of the backup. Even when the
    operator insists, the notes are preserved — otherwise --force silently
    becomes "destroy the notes"."""
    root, wt = repo
    _set_notes(wt, 3)
    _set_activity(wt, 10)  # live → would refuse
    r = _cleanup(root, wt, env={"BEACON_FORK_CLEANUP_FORCE": "1"})
    out = json.loads(r.stdout)
    assert out["forced"] is True
    backup = Path(out["notes_backup"])
    assert backup.exists(), "forced cleanup skipped the backup: " + r.stdout
    assert len([l for l in backup.read_text(encoding="utf-8").splitlines()
                if l.strip()]) == 3


def test_a_non_fork_path_is_refused(repo):
    root, _ = repo
    r = _cleanup(root, root / ".worktrees" / "not-a-fork")
    assert r.returncode == 1
    assert "not an active fork" in r.stderr


def test_the_gate_is_reachable_only_through_the_tool(repo):
    """The deletion must be owned by the CLI, not by Skill markdown. If the Skill
    still calls `git worktree remove` directly, the guard can be skipped."""
    skill = ROOT / "skills" / "beacon-session-merge-back.md"
    body = skill.read_text(encoding="utf-8")
    assert "session fork cleanup" in body, (
        "the merge-back Skill does not route through the guarded verb")


# --- both CLI frontends route through the same guard ------------------------

def _dispatch_cleanup(root, wt, *extra):
    """Drive beacon_cli/dispatch.py (the Windows/Codex frontend) in-process."""
    code = (
        "import sys, pathlib\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from beacon_cli.dispatch import dispatch\n"
        f"sys.exit(dispatch(pathlib.Path({str(ROOT)!r}), "
        f"['session','fork','cleanup',{str(wt)!r},'--json',*{list(extra)!r}]))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "lib")])
    env.pop("BEACON_FORK_CLEANUP_FORCE", None)
    return subprocess.run([sys.executable, "-c", code], cwd=str(root),
                          capture_output=True, text=True, env=env)


def test_second_frontend_also_refuses_a_live_fork(repo):
    """A guard wired on one frontend is a guard the other walks around."""
    root, wt = repo
    _set_notes(wt, 2)
    _set_activity(wt, 5)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _dispatch_cleanup(root, wt)
    assert r.returncode == 1, r.stdout + r.stderr
    assert wt.exists(), "dispatch.py removed a live fork"
    assert (wt / ".beacon" / "session_notes.jsonl").exists()


def test_second_frontend_can_still_clean_up_a_finished_fork(repo):
    """...and must not merely fail: a merged, idle fork is removed, notes saved."""
    root, wt = repo
    _set_notes(wt, 2)
    _set_activity(wt, 99999)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _dispatch_cleanup(root, wt)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["removed"] and Path(out["notes_backup"]).exists()
    assert not wt.exists()


# --- independent review findings (PR #770) ---------------------------------
#
# The reviews found four holes in the gates above. The worst was that --force
# sailed past the backup-failure gate while the code comment, the help entry AND
# the Skill all promised it could not — documentation asserting a guarantee the
# code did not provide, on the one path where recovery matters most. The existing
# force test passed only because the backup SUCCEEDED in it; force + a failing
# backup had no coverage at all, which is how the hole survived.

def _block_backup_dir(root):
    """Make the backup directory impossible to create (a file sits at its path)."""
    (root / ".beacon").mkdir(exist_ok=True)
    (root / ".beacon" / "fork-notes-backup").write_text("x", encoding="utf-8")


def test_force_cannot_override_a_failed_backup(repo):
    """--force overrides risks a human may accept; it must NOT override "we
    cannot preserve the data". Otherwise --force silently means "destroy"."""
    root, wt = repo
    _set_notes(wt, 3)
    _set_activity(wt, 10)
    _block_backup_dir(root)
    r = _cleanup(root, wt, env={"BEACON_FORK_CLEANUP_FORCE": "1"})
    out = json.loads(r.stdout)
    assert out["removed"] is False, "--force deleted a fork whose notes could not be saved"
    assert out["backup_failed"] is True, out
    assert wt.exists()
    assert (wt / ".beacon" / "session_notes.jsonl").exists(), "notes destroyed"
    assert r.returncode == 1


def test_unreadable_note_count_blocks_deletion(repo, monkeypatch):
    """"Could not read the notes" must not be treated as "there are none".
    The count feeds the decision to skip the backup, so an I/O error there used
    to delete the notes."""
    root, wt = repo
    _set_activity(wt, 99999)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    notes = wt / ".beacon" / "session_notes.jsonl"
    notes.write_text("x\n", encoding="utf-8")
    notes.chmod(0o000)  # exists but unreadable
    try:
        r = _cleanup(root, wt, env={"BEACON_FORK_CLEANUP_FORCE": "1"})
        out = json.loads(r.stdout)
        assert out["removed"] is False, "an unreadable note file was treated as empty"
        assert any("件数を読めませんでした" in b for b in out["blockers"]), out
        assert wt.exists()
    finally:
        notes.chmod(0o644)


def test_absent_notes_still_allow_cleanup(repo):
    """The other side: genuinely absent notes must NOT block (or every finished
    fork becomes un-cleanable). 0 and UNKNOWN must stay distinguishable."""
    root, wt = repo
    _set_activity(wt, 99999)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is True, r.stdout + r.stderr
    assert out["unpromoted_notes"] == 0
    assert out["notes_backup"] == ""


def test_missing_branch_metadata_refuses_instead_of_skipping_the_merge_check(repo):
    """gate 1 used to be `if branch:` — missing metadata skipped the merge check
    entirely, so the very risk this verb exists to prevent slipped through."""
    root, wt = repo
    _set_activity(wt, 99999)
    fj = wt / ".beacon" / "fork.json"
    rec = json.loads(fj.read_text(encoding="utf-8"))
    rec["child_branch"] = ""
    fj.write_text(json.dumps(rec), encoding="utf-8")
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is False
    assert any("branch 情報が読めません" in b for b in out["blockers"]), out
    assert wt.exists()


def test_missing_origin_main_is_not_reported_as_unmerged(repo):
    """`--is-ancestor` exits non-zero both for "not an ancestor" and "cannot
    compare". Calling the second one "not merged yet" sends the operator into a
    wait-for-merge loop that can never succeed."""
    root, wt = repo
    _set_activity(wt, 99999)
    _git("update-ref", "-d", "refs/remotes/origin/main", cwd=root)
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is False
    assert any("origin/main が見つかりません" in b for b in out["blockers"]), out
    assert not any("取り込まれていません" in b for b in out["blockers"]), (
        "an unresolvable ref was misreported as an unmerged branch: " + str(out))


def test_blocker_message_states_the_liveness_threshold(repo):
    """A refusal the operator cannot act on is half a refusal: say how long to
    wait, since the threshold is configurable and otherwise invisible."""
    root, wt = repo
    _set_activity(wt, 10)
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert any("閾値" in b for b in out["blockers"]), out


def test_display_and_gate_share_one_threshold(repo, monkeypatch):
    """The listing's "⚠ 作業中" cutoff and the gate's cutoff must be the same
    number, or the listing contradicts the refusal."""
    import importlib
    sys.path.insert(0, str(ROOT / "lib"))
    sess = importlib.import_module("session")
    monkeypatch.setenv("BEACON_FORK_IDLE_THRESHOLD_S", "60")
    assert sess.fork_idle_threshold_seconds() == 60.0
    src = (ROOT / "lib" / "cmd_session.py").read_text(encoding="utf-8")
    assert "idle < 300" not in src, (
        "the display path still hardcodes 300 instead of sharing the threshold")


def test_one_iso_parser_is_shared(repo):
    """`_is_fresh` and `_fork_idle_seconds` must not each carry their own copy of
    the timestamp-parsing edge cases."""
    src = (ROOT / "lib" / "session.py").read_text(encoding="utf-8")
    assert src.count('replace("Z", "+00:00")') == 1, (
        "more than one place parses the ISO stamp; a fix to one will miss the other")


def test_bash_frontend_rejects_a_stray_positional(repo):
    """The two frontends must agree on an unexpected extra argument: argparse
    rejects it, so bash must too (it used to drop it in silence).

    Runs inside the fixture's project, not the repo checkout: `.beacon/` is
    gitignored, so a developer's working tree has one and CI does not. Depending
    on that made this pass locally and fail in CI on bin/beacon's
    "no Beacon project found" path, which never reaches the argument loop.
    """
    import shutil as _sh
    bash = _sh.which("bash")
    if bash is None:
        pytest.skip("bash required")
    root, _ = repo
    r = subprocess.run([bash, str(ROOT / "bin" / "beacon"), "session", "fork",
                        "cleanup", "/tmp/a", "/tmp/b"],
                       capture_output=True, text=True, cwd=str(root))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "unexpected argument" in r.stderr, r.stderr


def test_force_flag_parity_across_frontends(repo):
    """`--force` must be parsed from argv on BOTH frontends.

    The existing force tests inject BEACON_FORK_CLEANUP_FORCE directly, so neither
    frontend's argv parsing is exercised — a typo in the bash `case` arm or a
    renamed argparse dest would pass CI. This drives the real flag through both.
    REQUIRED_FLAG_PARITY cannot cover it (nested verb, not a cmd_* function), so
    this test is the guard, per the precedent in check-cli-help-drift.py.
    """
    import shutil as _sh
    bash = _sh.which("bash")
    root, wt = repo
    _set_notes(wt, 2)
    _set_activity(wt, 5)  # live → refused unless --force is really seen

    # bash frontend
    if bash is not None:
        r = subprocess.run([bash, str(ROOT / "bin" / "beacon"), "session", "fork",
                            "cleanup", str(wt), "--force", "--json"],
                           cwd=str(root), capture_output=True, text=True)
        assert '"forced": true' in r.stdout, (
            "bash frontend did not pass --force through: " + r.stdout + r.stderr)

    # python frontend (fresh fork, the first call removed the worktree)
    r2 = _dispatch_cleanup(root, wt, "--force")
    combined = r2.stdout + r2.stderr
    assert '"forced": true' in combined or "not an active fork" in combined, (
        "dispatch.py did not pass --force through: " + combined)


# --- parent review of PR #770 (ms-166 e-6780 / e-6781 / e-6782) -------------
# Three defects the fork's own tests could not see, because each one lives in
# the gap between what the code does and what it TELLS the caller it did.


def test_refusal_says_where_the_backup_went(repo):
    """e-6780: a refused cleanup still snapshots — say so in the human output.

    The snapshot runs BEFORE the refusal check, so a refused call leaves a
    backup on disk. That was reported only in --json, which is how the ms-160
    fork's notes came to survive by accident (2026-10-01) rather than by design:
    the operator is told the fork cannot be removed and is NOT told that the
    notes they were worried about are already safe.
    """
    root, wt = repo
    _set_notes(wt, 3)
    _set_activity(wt, 10)  # live ⇒ refused
    e = dict(os.environ)
    e.pop("BEACON_FORK_CLEANUP_FORCE", None)
    e["BEACON_FORK_PATH"] = str(wt)
    e.pop("BEACON_JSON", None)  # human-readable path
    r = subprocess.run(
        [sys.executable, str(ROOT / "lib" / "commands.py"), "session_fork_cleanup"],
        cwd=str(root), capture_output=True, text=True, env=e)
    assert r.returncode == 1, r.stdout + r.stderr
    backups = list((root / ".beacon" / "fork-notes-backup").glob("*.jsonl"))
    assert backups, "a refused cleanup must still have taken the snapshot"
    assert str(backups[0]) in r.stderr, (
        "the human-readable refusal must name the backup path:\n" + r.stderr)


def test_removed_reports_the_worktree_not_overall_success(repo):
    """e-6781: worktree gone + branch left must not report removed=false.

    `removed` used to be `not errors`, so forcing past the unmerged-branch
    blocker (where `git branch -d` is guaranteed to refuse) reported
    removed=false while the directory was already gone. A caller reading that
    retries, and the retry cannot find the fork any more — the real leftover,
    the branch, becomes unreachable.
    """
    root, wt = repo
    # make the branch unmerged so gate 1 blocks and `git branch -d` will refuse
    (wt / "new.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "fork work", cwd=wt)
    _set_activity(wt, 99999)  # idle ⇒ only the unmerged gate blocks
    r = _cleanup(root, wt, env={"BEACON_FORK_CLEANUP_FORCE": "1"})
    out = json.loads(r.stdout)
    assert not Path(wt).exists(), "the worktree should be gone"
    assert out["removed"] is True, (
        "removed must describe the worktree, which WAS removed: " + r.stdout)
    assert out["branch_removed"] is False, r.stdout
    assert out["errors"], "the surviving branch must still be reported"
    assert out["branch"] == "ms-9-fork-abc"


def test_force_is_rejected_outside_cleanup_on_both_frontends(repo):
    """e-6782: --force belongs to cleanup alone, on BOTH frontends.

    PR #770 put --force on the shared `fork` subparser, so `fork list --force`
    parsed fine on the Python frontend while bin/beacon's `list` arm refused it
    — the two frontends disagreeing, which is the class of defect the cleanup
    verb exists to close.
    """
    root, _wt = repo
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "lib")])
    for argv in (["session", "fork", "list", "--force"],
                 ["session", "fork", "ms-9", "--force"]):
        code = (
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from beacon_cli.dispatch import dispatch\n"
            f"sys.exit(dispatch(pathlib.Path({str(ROOT)!r}), {argv!r}))\n"
        )
        py = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                            capture_output=True, text=True, env=env)
        assert py.returncode != 0, (
            f"python frontend must refuse {argv}: {py.stdout}{py.stderr}")
        assert "--force" in (py.stdout + py.stderr)

        sh = subprocess.run([str(ROOT / "bin" / "beacon"), *argv],
                            cwd=str(root), capture_output=True, text=True)
        assert sh.returncode != 0, (
            f"bash frontend must refuse {argv}: {sh.stdout}{sh.stderr}")


# --- independent review of PR #770 (AX-1 / AX-2 / maintainability-2) ---------
# Three defects two context-free judges found that neither the fork's tests nor
# the parent's first pass caught. AX-1 and AX-2 are both "the thing this verb
# exists to prevent, recreated one line away from it".


def test_uncommitted_work_is_a_gate_and_names_the_files(repo):
    """AX-2: the merge check only sees committed history.

    Gate 1 asks `git merge-base --is-ancestor`, which cannot see work that was
    never committed. Removal used to discover it the worst possible way: plain
    `git worktree remove` fails with exit 128 ("contains modified or untracked
    files, use --force to delete it") and the code retried with --force on ANY
    failure — so an operator who passed --force for the *unmerged branch* reason
    silently also lost their uncommitted work, with git's own error text
    recommending exactly that. Discover it as a gate, and name the files.
    """
    root, wt = repo
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    _set_activity(wt, 99999)  # idle, merged ⇒ only the dirty gate should block
    (wt / "wip.txt").write_text("work in progress\n", encoding="utf-8")
    r = _cleanup(root, wt)
    out = json.loads(r.stdout)
    assert out["removed"] is False, r.stdout
    assert wt.exists(), "a worktree with uncommitted work must survive"
    assert (wt / "wip.txt").exists()
    joined = " ".join(out["blockers"])
    assert "未コミット" in joined, joined
    assert "wip.txt" in joined, "the refusal must name what would be destroyed: " + joined


def test_force_may_discard_uncommitted_work_only_after_disclosure(repo):
    """AX-2 (other half): --force stays able to override, informedly.

    Losing one's own WIP is a risk a human can knowingly accept — unlike the
    notes snapshot, which is a hard_blocker. What was wrong was that the loss
    happened without ever being named. With the gate in place the refusal lists
    the files first, so passing --force is a choice rather than a surprise.
    """
    root, wt = repo
    _git("checkout", "-q", "main", cwd=root)
    _git("branch", "-f", "--no-track", "origin/main", "ms-9-fork-abc", cwd=root)
    _set_activity(wt, 99999)
    (wt / "wip.txt").write_text("work in progress\n", encoding="utf-8")
    r = _cleanup(root, wt, env={"BEACON_FORK_CLEANUP_FORCE": "1"})
    out = json.loads(r.stdout)
    assert out["removed"] is True, r.stdout
    assert not Path(wt).exists()


def test_stray_second_positional_is_refused_outside_cleanup(repo):
    """AX-1: `fork_path` lives on the shared subparser, so it leaked.

    Before the cleanup verb existed, `beacon session fork ms-9 typo-arg` failed
    with "unrecognized arguments". Adding `fork_path` to the shared `fork`
    subparser made argparse bind the stray token and exit 0 with nobody reading
    it — the same silent swallow the cleanup verb was added to close.
    """
    root, _wt = repo
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "lib")])
    for argv in (["session", "fork", "ms-9", "typo-arg"],
                 ["session", "fork", "list", "typo-arg"]):
        code = (
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from beacon_cli.dispatch import dispatch\n"
            f"sys.exit(dispatch(pathlib.Path({str(ROOT)!r}), {argv!r}))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                           capture_output=True, text=True, env=env)
        assert r.returncode != 0, (
            f"a stray second argument must be refused for {argv}: "
            f"{r.stdout}{r.stderr}")
        assert "typo-arg" in (r.stdout + r.stderr), (
            "the refusal must quote the token it rejected: " + r.stdout + r.stderr)


def test_own_session_id_does_not_claim_a_fallback_that_cannot_fire():
    """maintainability-2: `session-state.json` never carries a session id.

    The reader used to try it as a second source, but that file's schema is
    {declared_state, declared_at, state_since, source_event, state_detail?} —
    written by session_state_hook.build_state_marker, which puts no id in it. A
    docstring promising a fallback the code cannot take sends the next reader
    debugging the wrong layer. Pinned from the producer side so that if someone
    later DOES add an id to the marker, this test says the reader may change too.
    """
    import session_state_hook
    marker = session_state_hook.build_state_marker(
        "PreToolUse", "2026-10-01T00:00:00Z")
    assert "session_id" not in marker and "sid" not in marker, (
        "the state marker now carries an id — _fork_own_session_id may read it: "
        + repr(marker))
    # Look at what the function EXECUTES, not what it talks about: the comment
    # explaining why the fallback was removed names the file on purpose, and a
    # raw substring check would call that a violation (the guard has to fail on
    # the real defect, not on its own explanation).
    src = (ROOT / "lib" / "session.py").read_text(encoding="utf-8")
    fn = src.split("def _fork_own_session_id", 1)[1].split("\ndef ", 1)[0]
    code_lines = [ln for ln in fn.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    body = "\n".join(code_lines)
    assert "session-state.json" not in body, (
        "_fork_own_session_id must not read a file that cannot carry the id:\n"
        + body)
