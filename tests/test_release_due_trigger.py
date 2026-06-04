"""Unit tests for the release-due trigger (ms-52 e-958).

Threshold: feat >= 3 OR fix >= 5 since the last v* tag
(CORE doc rMlHx9n0LYFJ2kWIQELi).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def git_repo_with_beacon(monkeypatch):
    """Set up a temp git repo with .beacon/{project.json, triggers/} and a v* tag."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        beacon_dir = repo / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "triggers").mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "test", "milestones": [], "summary": ""}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")

        def git(*args, **kw):
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True,
                check=True, **kw,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t.t")
        git("config", "user.name", "t")
        git("config", "commit.gpgsign", "false")
        (repo / "README").write_text("seed", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "chore: seed")
        git("tag", "v0.1.0")

        yield repo, git


def _read_trigger(repo: Path) -> dict | None:
    p = repo / ".beacon" / "triggers" / "release-due.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _add_commit(repo: Path, git, msg: str):
    fname = msg.replace(":", "_").replace(" ", "_")[:40]
    (repo / fname).write_text("x", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", msg)


def _run_trigger_check(monkeypatch, repo: Path):
    # commands.py reads cwd indirectly through project_dir parent — set CWD too
    # for subprocess git invocations inside _auto_fire_release_due_trigger.
    monkeypatch.chdir(repo)
    # Re-import to pick up fresh module-level state (otherwise other tests may
    # have patched constants).
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands  # noqa: F401
    commands._auto_fire_release_due_trigger()


def test_no_tag_means_no_trigger(monkeypatch):
    """Fresh repo with no v* tag should not fire (no baseline)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        beacon_dir = repo / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "triggers").mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "t", "milestones": [], "summary": ""}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t.t")
        git("config", "user.name", "t")
        git("config", "commit.gpgsign", "false")
        (repo / "f").write_text("x", encoding="utf-8")
        git("add", ".")
        git("commit", "-q", "-m", "feat: x")
        # NO tag

        _run_trigger_check(monkeypatch, repo)
        assert _read_trigger(repo) is None


def test_below_threshold_does_not_fire(git_repo_with_beacon, monkeypatch):
    repo, git = git_repo_with_beacon
    # 2 feat + 4 fix — both below threshold (feat>=3 OR fix>=5).
    _add_commit(repo, git, "feat: a")
    _add_commit(repo, git, "feat: b")
    _add_commit(repo, git, "fix: c")
    _add_commit(repo, git, "fix: d")
    _add_commit(repo, git, "fix: e")
    _add_commit(repo, git, "fix: f")

    _run_trigger_check(monkeypatch, repo)
    assert _read_trigger(repo) is None


def test_feat_threshold_fires(git_repo_with_beacon, monkeypatch):
    repo, git = git_repo_with_beacon
    _add_commit(repo, git, "feat: a")
    _add_commit(repo, git, "feat(scope): b")
    _add_commit(repo, git, "feat: c")
    _add_commit(repo, git, "chore: irrelevant")

    _run_trigger_check(monkeypatch, repo)
    t = _read_trigger(repo)
    assert t is not None
    assert t["kind"] == "release-due"
    assert t["since_tag"] == "v0.1.0"
    assert t["feat_count"] == 3
    assert t["fix_count"] == 0
    assert t["commit_count"] == 4
    assert t["next_version_hint"].startswith("v")  # at least minor bump
    assert "v0.1.0" in t["message"]
    assert "feat 3" in t["message"]


def test_fix_threshold_fires(git_repo_with_beacon, monkeypatch):
    repo, git = git_repo_with_beacon
    for i in range(5):
        _add_commit(repo, git, f"fix: bug-{i}")

    _run_trigger_check(monkeypatch, repo)
    t = _read_trigger(repo)
    assert t is not None
    assert t["fix_count"] == 5
    assert "fix 5" in t["message"]


def test_new_tag_resets_created_at(git_repo_with_beacon, monkeypatch):
    """When a new tag appears after the trigger was first written, created_at
    must reset (the previous "since" period is over)."""
    repo, git = git_repo_with_beacon
    for i in range(3):
        _add_commit(repo, git, f"feat: a-{i}")
    _run_trigger_check(monkeypatch, repo)
    first = _read_trigger(repo)
    assert first is not None
    first_created = first["created_at"]
    first_since = first["since_tag"]

    # New release happens (tag advances).
    git("tag", "v0.2.0")
    # New commits after the new tag.
    for i in range(3):
        _add_commit(repo, git, f"feat: b-{i}")

    _run_trigger_check(monkeypatch, repo)
    second = _read_trigger(repo)
    assert second is not None
    assert second["since_tag"] != first_since
    assert second["since_tag"] == "v0.2.0"
    assert second["created_at"] != first_created  # reset


def test_same_tag_preserves_created_at(git_repo_with_beacon, monkeypatch):
    """If the trigger refreshes against the same since_tag (more commits
    piled up but no new release), created_at stays fixed so users see
    "pending since YYYY-MM-DD"."""
    repo, git = git_repo_with_beacon
    for i in range(3):
        _add_commit(repo, git, f"feat: a-{i}")
    _run_trigger_check(monkeypatch, repo)
    first = _read_trigger(repo)
    assert first is not None
    first_created = first["created_at"]

    # More commits, same tag (no release yet).
    _add_commit(repo, git, "feat: extra")
    _run_trigger_check(monkeypatch, repo)
    second = _read_trigger(repo)
    assert second is not None
    assert second["since_tag"] == first["since_tag"]
    assert second["created_at"] == first_created  # preserved
    assert second["feat_count"] == 4
    # refreshed_at advances (or at least is present); not asserting strict >
    # to avoid flakiness on sub-second runs.
    assert "refreshed_at" in second


def test_trigger_clears_when_counts_drop_below_threshold(git_repo_with_beacon, monkeypatch):
    """If commits get squashed / cancelled and counts drop below threshold,
    the stale trigger must be cleared (not left lingering)."""
    repo, git = git_repo_with_beacon
    for i in range(3):
        _add_commit(repo, git, f"feat: a-{i}")
    _run_trigger_check(monkeypatch, repo)
    assert _read_trigger(repo) is not None

    # Reset hard to the tag (simulates "squashed everything away").
    subprocess.run(["git", "reset", "--hard", "v0.1.0"], cwd=repo, check=True,
                   capture_output=True)
    _run_trigger_check(monkeypatch, repo)
    assert _read_trigger(repo) is None
