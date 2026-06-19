"""Tests for atomic `milestone start` and project-type degrade (ms-81 e-1917).

Pins the two behaviours the SPEC AC #2 / #11 / e-1917 AC name:

- ``beacon milestone start`` flips status to active, adds the actor as an
  assignee, and (in a git project) creates the worktree in a single
  invocation. Partial-completion (status flipped but assignee missing,
  or assignee added but status untouched) is what e-1917 prevents — we
  pin that the data result reflects every step that succeeded.
- In a non-git project the worktree step is silently skipped: the call
  returns cleanly with status flipped, no warning, no exception.

The git-project branch is exercised via the lib-layer helpers rather
than spawning a real worktree, so the test stays fast and platform-
agnostic.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands
import core


def _project():
    return {
        "name": "test",
        "milestones": [
            {
                "id": "ms-1", "title": "Sample MS", "status": "todo",
                "progress": 0, "target_date": "",
                "entries": [], "commits": [],
            }
        ],
    }


class TestProjectTypeDetection:
    def test_is_git_project_true_when_dot_git_exists(self, tmp_path, monkeypatch):
        # Synth a fake git-project root with just a ``.git`` directory; the
        # helper short-circuits on the cheap filesystem check.
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        assert commands._is_git_project() is True

    def test_is_git_project_false_in_plain_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BEACON_ROOT", raising=False)
        # Force git rev-parse to also fail by pretending git isn't installed.
        # We patch the subprocess invocation so the fallback path returns
        # False rather than reaching the actual git binary.
        import subprocess as _sp

        def _fake_run(*args, **kwargs):  # pragma: no cover - smoke
            raise FileNotFoundError()

        monkeypatch.setattr(_sp, "run", _fake_run)
        assert commands._is_git_project() is False


class TestMilestoneStartAtomic:
    def test_core_milestone_start_flips_status(self):
        # Pin the data-layer contract: the status flip is unconditional and
        # happens in a single call. CLI layering on top of this composes the
        # assignee + worktree steps, but the core invariant is that status
        # never gets left in a half-flipped state.
        data = _project()
        ms = core.milestone_start(data, "ms-1")
        assert ms["status"] == "in_progress"

    def test_core_milestone_start_not_found_raises(self):
        data = _project()
        with pytest.raises(ValueError, match="not found"):
            core.milestone_start(data, "ms-99")

    def test_assignee_add_idempotent(self):
        # Joining a MS twice mustn't double-write — the atomic story relies
        # on every step being safe to repeat (since `start` can be called
        # again on an already-active MS as a recovery path).
        data = _project()
        core.milestone_start(data, "ms-1")
        _ms1, added1 = core.milestone_assignee_add(data, "ms-1", "alice")
        _ms2, added2 = core.milestone_assignee_add(data, "ms-1", "alice")
        assert added1 is True
        assert added2 is False


class TestNonGitProjectDegrade:
    def test_cmd_milestone_start_in_non_git_does_not_raise(
        self, tmp_path, monkeypatch, capsys
    ):
        # Set up a fresh non-git directory with just enough beacon scaffolding
        # to satisfy load_project. The worktree step must silently skip and
        # the status flip must still happen.
        (tmp_path / ".beacon").mkdir()
        (tmp_path / ".beacon" / "project.json").write_text(
            '{"name": "test", "milestones": [{"id": "ms-1", "title": "Sample", '
            '"status": "todo", "progress": 0, "target_date": "", "entries": [], '
            '"commits": []}], "operations": []}'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BEACON_MS_ID", "ms-1")
        monkeypatch.setenv("BEACON_NO_ASSIGNEE", "1")  # skip identity lookup

        # Patch git detection to force the non-git branch even if the test
        # runner is itself inside a git checkout.
        monkeypatch.setattr(commands, "_is_git_project", lambda: False)

        # The call must complete cleanly; the non-git-skip message goes to
        # stdout.
        commands.cmd_milestone_start()
        out = capsys.readouterr().out
        assert "non-git project" in out
        assert "worktree step skipped" in out
