"""Tests for the rollback boundary helper (ms-55 e-1647).

Three layers under test:

  1. inspect_repo_state — uses a tmp_path real git repo so the
     subprocess calls exercise the actual git binary.
  2. plan_rollback — pure function, exercised with hand-built RepoState
     objects so the planner logic is decoupled from git behavior.
  3. execute_rollback — integration paths that mutate the tmp_path
     repo and verify the resulting state.

The CLI is tested separately in test_rollback_cli.py.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import rollback  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — minimal tmp_path git repo factories.
# ---------------------------------------------------------------------------

def _run(cwd, *args, env=None):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True,
        env=env,
    )


def _init_repo(tmp_path):
    """Initialize a brand-new repo with a single committed file."""
    cwd = str(tmp_path)
    _run(cwd, "git", "init", "-q", "-b", "main")
    _run(cwd, "git", "config", "user.email", "test@example.com")
    _run(cwd, "git", "config", "user.name", "Test")
    p = tmp_path / "README.md"
    p.write_text("hello\n")
    _run(cwd, "git", "add", "README.md")
    _run(cwd, "git", "commit", "-q", "-m", "initial")
    return cwd


def _set_upstream(cwd, tmp_path):
    """Add a bare "remote" and push so HEAD has an upstream branch."""
    bare = tmp_path.parent / "remote.git"
    if bare.exists():
        # pytest tmp_path is unique per test; this branch is just defensive.
        import shutil
        shutil.rmtree(bare)
    _run(cwd, "git", "init", "-q", "--bare", str(bare))
    _run(cwd, "git", "remote", "add", "origin", str(bare))
    _run(cwd, "git", "push", "-q", "-u", "origin", "main")


def _make_commit(cwd, filename, content="x\n"):
    p = os.path.join(cwd, filename)
    with open(p, "w") as f:
        f.write(content)
    _run(cwd, "git", "add", filename)
    _run(cwd, "git", "commit", "-q", "-m", f"add {filename}")


# ---------------------------------------------------------------------------
# inspect_repo_state
# ---------------------------------------------------------------------------

def test_inspect_outside_repo_returns_empty(tmp_path):
    """Not a git repo → empty RepoState, no exception."""
    state = rollback.inspect_repo_state(cwd=str(tmp_path))
    assert state.head_hash == ""
    assert state.branch == ""
    assert not state.working_tree_dirty
    assert state.local_commits_ahead == 0


def test_inspect_clean_repo(tmp_path):
    cwd = _init_repo(tmp_path)
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.head_hash != ""
    assert state.branch == "main"
    assert not state.working_tree_dirty
    assert state.local_commits_ahead == 1  # initial commit, no upstream
    assert state.upstream_branch == ""


def test_inspect_dirty_working_tree(tmp_path):
    cwd = _init_repo(tmp_path)
    (tmp_path / "wip.txt").write_text("uncommitted\n")
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.working_tree_dirty
    assert "wip.txt" in state.working_tree_files


def test_inspect_with_upstream_no_ahead(tmp_path):
    cwd = _init_repo(tmp_path)
    _set_upstream(cwd, tmp_path)
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.upstream_branch.endswith("origin/main")
    assert state.local_commits_ahead == 0


def test_inspect_with_upstream_some_ahead(tmp_path):
    cwd = _init_repo(tmp_path)
    _set_upstream(cwd, tmp_path)
    _make_commit(cwd, "a.txt")
    _make_commit(cwd, "b.txt")
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.local_commits_ahead == 2


# ---------------------------------------------------------------------------
# plan_rollback — pure-function tests
# ---------------------------------------------------------------------------

def _state(*, dirty=False, files=None, ahead=0, upstream="origin/main"):
    return rollback.RepoState(
        working_tree_dirty=dirty,
        working_tree_files=list(files or []),
        local_commits_ahead=ahead,
        upstream_branch=upstream,
        head_hash="abc1234",
        branch="main",
    )


def test_plan_empty_when_clean():
    p = rollback.plan_rollback(_state())
    assert not p.stash_working_tree
    assert p.reset_commits == 0
    assert not p.pushed_warning
    assert p.compensation_options == []


def test_plan_dirty_tree_only():
    p = rollback.plan_rollback(_state(dirty=True, files=["a", "b"]))
    assert p.stash_working_tree
    assert p.reset_commits == 0


def test_plan_local_commits_undone_by_default_with_upstream():
    """commits=0 + upstream configured → undo HEAD..@{u}, capped."""
    p = rollback.plan_rollback(_state(ahead=3, upstream="origin/main"))
    assert p.reset_commits == 3


def test_plan_no_upstream_defaults_to_no_reset():
    """commits=0 + no upstream → we can't distinguish 'local-only' from
    'every commit ever', so refuse to auto-reset. Stash still runs."""
    p = rollback.plan_rollback(_state(ahead=3, upstream="", dirty=True))
    assert p.reset_commits == 0
    assert p.stash_working_tree


def test_plan_local_commits_explicit_request():
    p = rollback.plan_rollback(_state(ahead=3), commits=2)
    assert p.reset_commits == 2
    assert not p.pushed_warning


def test_plan_commits_request_exceeds_local_warns():
    """Asking to undo 5 when only 2 are local-only → 2 auto + report
    about the surplus."""
    p = rollback.plan_rollback(_state(ahead=2), commits=5)
    assert p.reset_commits == 2  # auto half capped at what's local
    assert p.pushed_warning
    assert any("revert PR" in o for o in p.compensation_options)
    assert any("3" in o for o in p.compensation_options)  # surplus count


def test_plan_caps_to_max_auto_commits():
    p = rollback.plan_rollback(
        _state(ahead=10), commits=0, max_auto_commits=5,
    )
    assert p.reset_commits == 5
    assert any(
        "5 additional local commit" in o or "additional local" in o
        for o in p.compensation_options
    )


def test_plan_caps_explicit_request_too():
    p = rollback.plan_rollback(
        _state(ahead=10), commits=8, max_auto_commits=5,
    )
    assert p.reset_commits == 5
    # Both messages: cap warning AND no pushed warning (8 <= 10 ahead).
    assert not p.pushed_warning
    assert any("auto cap" in o for o in p.compensation_options)


def test_plan_negative_commits_treated_as_zero():
    p = rollback.plan_rollback(_state(ahead=3, upstream="origin/main"),
                                commits=-2)
    # commits=0 path → undo all local-only, no cap hit (3 < default 5)
    assert p.reset_commits == 3


def test_plan_carries_reason_into_report():
    p = rollback.plan_rollback(_state(dirty=True), reason="op-2 over-fire")
    report = rollback.render_report(p)
    assert "op-2 over-fire" in report
    assert "git stash" in report


def test_render_report_nothing_section_when_clean():
    p = rollback.plan_rollback(_state())
    report = rollback.render_report(p)
    assert "nothing" in report.lower()


def test_render_report_lists_compensation_options():
    p = rollback.plan_rollback(_state(ahead=2), commits=5)
    report = rollback.render_report(p)
    assert "Report-only items" in report
    assert "revert PR" in report
    assert "force" in report  # the pushed_warning message


# ---------------------------------------------------------------------------
# execute_rollback — integration with real git
# ---------------------------------------------------------------------------

def test_execute_stash_only(tmp_path):
    cwd = _init_repo(tmp_path)
    (tmp_path / "wip.txt").write_text("uncommitted\n")
    state = rollback.inspect_repo_state(cwd=cwd)
    plan = rollback.plan_rollback(state, reason="halt")
    assert plan.stash_working_tree
    result = rollback.execute_rollback(plan, cwd=cwd)
    assert result.stashed
    assert result.errors == []
    # Verify the working tree is now clean.
    state_after = rollback.inspect_repo_state(cwd=cwd)
    assert not state_after.working_tree_dirty
    # Verify the stash carries the reason in its message.
    r = subprocess.run(
        ["git", "stash", "list"], cwd=cwd,
        capture_output=True, text=True,
    )
    assert "halt" in r.stdout


def test_execute_reset_undoes_commits(tmp_path):
    cwd = _init_repo(tmp_path)
    _set_upstream(cwd, tmp_path)
    _make_commit(cwd, "a.txt")
    _make_commit(cwd, "b.txt")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd,
        capture_output=True, text=True,
    ).stdout.strip()
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.local_commits_ahead == 2
    plan = rollback.plan_rollback(state, commits=2)
    result = rollback.execute_rollback(plan, cwd=cwd)
    assert result.reset_commits == 2
    assert result.errors == []
    # HEAD has moved back.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd,
        capture_output=True, text=True,
    ).stdout.strip()
    assert head_after != head_before
    # The content is still on disk because reset --soft preserves it.
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").exists()
    # And the un-pushed count is back to 0.
    state_after = rollback.inspect_repo_state(cwd=cwd)
    assert state_after.local_commits_ahead == 0


def test_execute_combined_stash_and_reset(tmp_path):
    cwd = _init_repo(tmp_path)
    _set_upstream(cwd, tmp_path)
    _make_commit(cwd, "a.txt")
    (tmp_path / "wip.txt").write_text("dirty\n")
    state = rollback.inspect_repo_state(cwd=cwd)
    plan = rollback.plan_rollback(state, commits=1, reason="stop signal")
    result = rollback.execute_rollback(plan, cwd=cwd)
    assert result.stashed
    assert result.reset_commits == 1
    assert result.errors == []


def test_execute_clean_repo_no_ops(tmp_path):
    cwd = _init_repo(tmp_path)
    _set_upstream(cwd, tmp_path)
    state = rollback.inspect_repo_state(cwd=cwd)
    plan = rollback.plan_rollback(state)
    result = rollback.execute_rollback(plan, cwd=cwd)
    assert not result.stashed
    assert result.reset_commits == 0
    assert result.errors == []


# ---------------------------------------------------------------------------
# Top-level rollback() helper — dry_run vs execute
# ---------------------------------------------------------------------------

def test_rollback_dry_run_does_not_mutate(tmp_path):
    cwd = _init_repo(tmp_path)
    (tmp_path / "wip.txt").write_text("dirty\n")
    plan, result, report = rollback.rollback(
        cwd=cwd, dry_run=True, reason="rehearsal",
    )
    assert result is None
    assert plan.stash_working_tree
    assert "rehearsal" in report
    # Working tree is still dirty.
    state_after = rollback.inspect_repo_state(cwd=cwd)
    assert state_after.working_tree_dirty


def test_rollback_execute_mutates(tmp_path):
    cwd = _init_repo(tmp_path)
    (tmp_path / "wip.txt").write_text("dirty\n")
    plan, result, report = rollback.rollback(
        cwd=cwd, dry_run=False, reason="real",
    )
    assert result is not None
    assert result.stashed
    state_after = rollback.inspect_repo_state(cwd=cwd)
    assert not state_after.working_tree_dirty


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_detached_head_branch_is_empty(tmp_path):
    cwd = _init_repo(tmp_path)
    _make_commit(cwd, "a.txt")
    # Detach.
    h = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=cwd,
        capture_output=True, text=True,
    ).stdout.strip()
    _run(cwd, "git", "-c", "advice.detachedHead=false", "checkout", "-q", h)
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.branch == ""  # detached
    assert state.head_hash != ""


def test_plan_pushed_warning_when_no_upstream_and_request_exceeds(tmp_path):
    """No upstream → local_commits_ahead counts from root. Asking for more
    than that still triggers the pushed_warning because the request
    is asking to go past what we know to be local."""
    # 2 commits total, no upstream → ahead == 2.
    cwd = _init_repo(tmp_path)
    _make_commit(cwd, "a.txt")
    state = rollback.inspect_repo_state(cwd=cwd)
    assert state.upstream_branch == ""
    assert state.local_commits_ahead == 2
    p = rollback.plan_rollback(state, commits=3)
    assert p.pushed_warning
    assert any("no upstream" in o or "(no upstream)" in o
               for o in p.compensation_options)
