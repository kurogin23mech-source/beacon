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
