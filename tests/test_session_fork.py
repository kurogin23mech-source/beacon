"""Unit tests for ``session.fork_workspace`` — the helper that backs
``beacon session fork`` (ms-67 / e-1549).

The helper does 4 things and the tests pin each:

  1. ``git worktree add <wt-path> -b <child-branch>`` runs with the right
     args.
  2. ``.beacon/cloud.json`` from the parent is copied into the child so
     the new bclaude there binds to the same Beacon project.
  3. ``beacon channel install`` runs in the child cwd so ``.mcp.json``
     gets generated. Failure here is non-fatal but recorded.
  4. ``.beacon/fork.json`` is written with parent ↔ child link data.

A fake runner is injected so no real ``git`` or ``beacon`` subprocess
fires during the test.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRunner:
    """Records (args, cwd) for every call and returns scripted exits.

    ``exits`` is a list of ``(rc, stdout, stderr)`` tuples consumed in order;
    if exhausted falls back to ``(0, "", "")`` so adding new internal calls
    in the helper doesn't immediately break tests.
    """

    def __init__(self, exits=None):
        self.calls = []
        self.exits = list(exits) if exits else []

    def __call__(self, args, cwd=None):
        self.calls.append((list(args), cwd))
        if self.exits:
            return self.exits.pop(0)
        return (0, "", "")


@pytest.fixture
def parent_repo(tmp_path: Path) -> Path:
    """Set up a fake parent repo with .beacon/cloud.json + project.json."""
    repo = tmp_path / "parent_repo"
    repo.mkdir()
    (repo / ".beacon").mkdir()
    (repo / ".beacon" / "cloud.json").write_text(
        json.dumps({"project_id": "fake-proj-abc", "api_url": "https://example.com"}),
        encoding="utf-8",
    )
    (repo / ".beacon" / "project.json").write_text(
        json.dumps({"name": "FakeProject", "milestones": []}),
        encoding="utf-8",
    )
    return repo


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

def test_fork_workspace_runs_git_worktree_add(parent_repo: Path):
    runner = FakeRunner()
    result = session.fork_workspace(
        "ms-99", "Fake target milestone",
        parent_session_id="sv-test-aaa",
        parent_branch="main",
        parent_repo_path=parent_repo,
        short_uuid="abc123",
        runner=runner,
    )
    git_call = runner.calls[0]
    args, cwd = git_call
    assert args[:3] == ["git", "worktree", "add"]
    assert args[-2] == "-b"
    assert args[-1] == "ms-99-fork-abc123"
    assert cwd == str(parent_repo.resolve())
    assert result["branch"] == "ms-99-fork-abc123"
    assert result["worktree_path"].endswith(".worktrees/ms-99-fork-abc123")


def test_fork_workspace_copies_cloud_json(parent_repo: Path):
    runner = FakeRunner()
    result = session.fork_workspace(
        "ms-12", "Another milestone",
        parent_session_id="sv-test-bbb",
        parent_branch="feat/x",
        parent_repo_path=parent_repo,
        short_uuid="deadbe",
        runner=runner,
    )
    child_cloud = Path(result["worktree_path"]) / ".beacon" / "cloud.json"
    assert child_cloud.exists()
    parsed = json.loads(child_cloud.read_text(encoding="utf-8"))
    assert parsed["project_id"] == "fake-proj-abc"


def test_fork_workspace_runs_channel_install_in_child_cwd(parent_repo: Path):
    runner = FakeRunner()
    result = session.fork_workspace(
        "ms-7", "Channel install target",
        parent_session_id="sv-test-ccc",
        parent_branch="main",
        parent_repo_path=parent_repo,
        short_uuid="cafe01",
        runner=runner,
    )
    # 2nd recorded call is `beacon channel install` in the child cwd
    args, cwd = runner.calls[1]
    assert args == ["beacon", "channel", "install"]
    assert cwd == result["worktree_path"]


def test_fork_workspace_writes_fork_json(parent_repo: Path):
    runner = FakeRunner()
    result = session.fork_workspace(
        "ms-42", "Fork manifest milestone",
        parent_session_id="sv-test-ddd",
        parent_branch="hotfix/y",
        parent_repo_path=parent_repo,
        short_uuid="0f0f0f",
        runner=runner,
    )
    fork_json = Path(result["worktree_path"]) / ".beacon" / "fork.json"
    assert fork_json.exists()
    record = json.loads(fork_json.read_text(encoding="utf-8"))
    assert record["parent_session_id"] == "sv-test-ddd"
    assert record["parent_branch"] == "hotfix/y"
    assert record["parent_repo_path"] == str(parent_repo.resolve())
    assert record["target_ms_id"] == "ms-42"
    assert record["target_ms_title"] == "Fork manifest milestone"
    assert record["child_branch"] == "ms-42-fork-0f0f0f"
    assert "created_at" in record and record["created_at"]
    assert record["channel_install"]["ok"] is True


def test_fork_workspace_channel_install_failure_recorded_but_not_fatal(parent_repo: Path):
    runner = FakeRunner(exits=[
        (0, "", ""),            # git worktree add ok
        (1, "", "no .mcp.json"), # channel install fails
    ])
    result = session.fork_workspace(
        "ms-3", "Channel install failure",
        parent_session_id="sv-test-eee",
        parent_branch="main",
        parent_repo_path=parent_repo,
        short_uuid="aaaaaa",
        runner=runner,
    )
    record = result["fork_record"]
    assert record["channel_install"]["ok"] is False
    assert "no .mcp.json" in record["channel_install"]["stderr"]


def test_fork_workspace_git_failure_raises(parent_repo: Path):
    runner = FakeRunner(exits=[
        (128, "", "fatal: already exists"),
    ])
    with pytest.raises(RuntimeError) as excinfo:
        session.fork_workspace(
            "ms-99", "Already-exists target",
            parent_session_id="sv-test-fff",
            parent_branch="main",
            parent_repo_path=parent_repo,
            short_uuid="bbbbbb",
            runner=runner,
        )
    assert "git worktree add failed" in str(excinfo.value)


def test_fork_workspace_handles_missing_cloud_json(tmp_path: Path):
    """Parent without cloud.json should still produce a worktree.

    (Some local-mode projects might not have it. The helper should not
    block fork in that case — child can be re-bound manually later.)
    """
    repo = tmp_path / "minimal_repo"
    repo.mkdir()
    (repo / ".beacon").mkdir()  # no cloud.json
    runner = FakeRunner()
    result = session.fork_workspace(
        "ms-5", "Local-mode milestone",
        parent_session_id="",
        parent_branch="main",
        parent_repo_path=repo,
        short_uuid="zerozr",
        runner=runner,
    )
    child_cloud = Path(result["worktree_path"]) / ".beacon" / "cloud.json"
    assert not child_cloud.exists()
    # fork.json still written
    assert (Path(result["worktree_path"]) / ".beacon" / "fork.json").exists()


def test_fork_workspace_symlinks_project_json(parent_repo: Path):
    """Child's .beacon/project.json must exist so the SessionStart hook's
    `test -f .beacon/project.json` check succeeds in the child cwd.

    This is the SessionStart hook integration fix found during the ms-67
    e-1554 dogfood. Without the symlink the global hook in
    ~/.claude/settings.json silently skips the auto /beacon-session-start
    injection and the child boots into an anonymous-feeling session.
    """
    runner = FakeRunner()
    result = session.fork_workspace(
        "ms-7", "Symlink target",
        parent_session_id="sv-x",
        parent_branch="main",
        parent_repo_path=parent_repo,
        short_uuid="symlnk",
        runner=runner,
    )
    child_project = Path(result["worktree_path"]) / ".beacon" / "project.json"
    assert child_project.exists()
    assert child_project.is_symlink()
    # symlink target should be the parent's project.json (compare resolved)
    assert child_project.resolve() == (parent_repo / ".beacon" / "project.json").resolve()


def test_fork_workspace_rejects_bad_ms_id(parent_repo: Path):
    with pytest.raises(ValueError):
        session.fork_workspace(
            "not-an-ms-id", "Should reject",
            parent_session_id="sv-x",
            parent_branch="main",
            parent_repo_path=parent_repo,
        )


def test_fork_workspace_generates_short_uuid_when_none(parent_repo: Path):
    """short_uuid=None should produce a fresh 6-hex-char id each call."""
    runner1 = FakeRunner()
    r1 = session.fork_workspace(
        "ms-1", "A",
        parent_session_id="sv-x",
        parent_branch="main",
        parent_repo_path=parent_repo,
        runner=runner1,
    )
    runner2 = FakeRunner()
    r2 = session.fork_workspace(
        "ms-1", "A",
        parent_session_id="sv-x",
        parent_branch="main",
        parent_repo_path=parent_repo,
        runner=runner2,
    )
    uuid1 = r1["branch"].rsplit("-", 1)[-1]
    uuid2 = r2["branch"].rsplit("-", 1)[-1]
    assert len(uuid1) == 6
    assert len(uuid2) == 6
    assert all(c in "0123456789abcdef" for c in uuid1)
    assert uuid1 != uuid2  # vanishingly unlikely to collide


# ---------------------------------------------------------------------------
# list_forks (e-1553)
# ---------------------------------------------------------------------------

def _make_worktree_with_fork(parent_root: Path, wt_name: str, record: dict) -> Path:
    """Helper: stage a worktree dir with .beacon/fork.json under parent_root."""
    wt = parent_root / ".worktrees" / wt_name
    (wt / ".beacon").mkdir(parents=True)
    (wt / ".beacon" / "fork.json").write_text(
        json.dumps(record), encoding="utf-8",
    )
    return wt


def test_list_forks_returns_only_fork_worktrees(parent_repo: Path):
    """list_forks ignores worktrees without .beacon/fork.json."""
    fork_wt = _make_worktree_with_fork(parent_repo, "ms-1-fork-aaaaaa", {
        "target_ms_id": "ms-1",
        "target_ms_title": "First",
        "child_branch": "ms-1-fork-aaaaaa",
        "parent_session_id": "sv-x",
        "parent_branch": "main",
        "created_at": "2026-06-12T03:00:00Z",
    })
    # also stage a non-fork worktree (no fork.json) — should be ignored
    plain_wt = parent_repo / ".worktrees" / "plain-branch"
    plain_wt.mkdir(parents=True)

    porcelain = (
        f"worktree {parent_repo}\nHEAD abc\nbranch main\n\n"
        f"worktree {fork_wt}\nHEAD def\nbranch ms-1-fork-aaaaaa\n\n"
        f"worktree {plain_wt}\nHEAD ghi\nbranch plain-branch\n\n"
    )
    runner = FakeRunner(exits=[(0, porcelain, "")])
    forks = session.list_forks(parent_repo, runner=runner)
    assert len(forks) == 1
    assert forks[0]["target_ms_id"] == "ms-1"
    assert forks[0]["child_branch"] == "ms-1-fork-aaaaaa"
    assert forks[0]["worktree_path"] == str(fork_wt)


def test_list_forks_empty_when_no_forks(parent_repo: Path):
    porcelain = f"worktree {parent_repo}\nHEAD abc\nbranch main\n\n"
    runner = FakeRunner(exits=[(0, porcelain, "")])
    assert session.list_forks(parent_repo, runner=runner) == []


def test_list_forks_skips_invalid_json(parent_repo: Path):
    """A corrupt fork.json shouldn't crash the listing."""
    wt = parent_repo / ".worktrees" / "ms-9-fork-xxxxxx"
    (wt / ".beacon").mkdir(parents=True)
    (wt / ".beacon" / "fork.json").write_text("not-json", encoding="utf-8")
    porcelain = (
        f"worktree {parent_repo}\nHEAD abc\nbranch main\n\n"
        f"worktree {wt}\nHEAD def\nbranch ms-9-fork-xxxxxx\n\n"
    )
    runner = FakeRunner(exits=[(0, porcelain, "")])
    # corrupted fork.json silently skipped, no exception
    assert session.list_forks(parent_repo, runner=runner) == []


def test_list_forks_git_failure_returns_empty(parent_repo: Path):
    """If git worktree list fails (e.g. not a repo), return empty list."""
    runner = FakeRunner(exits=[(128, "", "fatal: not a git repository")])
    assert session.list_forks(parent_repo, runner=runner) == []
