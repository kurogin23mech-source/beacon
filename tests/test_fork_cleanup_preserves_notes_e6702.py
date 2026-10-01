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
