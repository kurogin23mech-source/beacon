"""Tests for transition prompts and done-MS re-open prompt (ms-81 e-1919).

Pins the SPEC §4 + §5 prompts:
- Adding a task to a done MS surfaces a re-open prompt (interactive) or
  a one-line warning (non-interactive). The Skill / hook surface is
  non-interactive; the warning is the audit trail and the add proceeds
  so a Skill report can flag it rather than silently fail.
- Phase transitions on an MS whose worktree directory still exists log
  a leftover-worktree warning. Non-interactive runs proceed without
  removing the worktree (= the operator can clean up at their leisure).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands
import cmd_milestone  # ms-127 e-4849: _prompt_close_leftover_worktree moved here
import core


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": [{"id": "ms-1", "title": "Sample", '
        '"status": "done", "progress": 100, "target_date": "", "entries": [], '
        '"commits": []}], "operations": []}'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_DESCRIPTION", "follow-up task")
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_TYPE", "task")
    # ms-126: priority is mandatory. This test exercises the done-MS re-open
    # prompt, not priority triage, so opt into the untriaged sentinel to keep
    # the add path unblocked.
    # ms-126 / e-4222 + philosophy fix (2026-07-29): the untriaged sentinel is a
    # *machine-only* capability. The refusal is no longer keyed only on
    # BEACON_SESSION_KIND=human — an undeclared *interactive* session (the
    # test_interactive_* cases below monkeypatch stdin.isatty→True) now counts as
    # a human and is refused. This fixture seeds data as a machine caller, so it
    # must DECLARE itself non-human explicitly (BEACON_SESSION_KIND=ai) rather
    # than rely on the unset default — which is exactly the recovery path the
    # refusal message advertises for automated callers at a terminal.
    monkeypatch.setenv("BEACON_ALLOW_UNTRIAGED", "1")
    monkeypatch.setenv("BEACON_SESSION_KIND", "ai")
    return tmp_path


class TestDoneMSReopenPrompt:
    def test_non_interactive_warns_and_proceeds(
        self, project_dir, monkeypatch, capsys
    ):
        # Skill / hook surface: warn, then add.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        commands.cmd_task_add()
        captured = capsys.readouterr()
        assert "[ms-81 re-open warning]" in captured.err
        assert "Re-open with" in captured.err
        # The task was added despite the warning (= 努力義務 surface).
        assert "Added task" in captured.out

    def test_interactive_abort_default(self, project_dir, monkeypatch, capsys):
        # Default response (= empty input) is to abort.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        with pytest.raises(SystemExit):
            commands.cmd_task_add()
        assert "aborted" in capsys.readouterr().err

    def test_interactive_observe_reopens(
        self, project_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "o")
        commands.cmd_task_add()
        # MS should be observing now, task added.
        data = commands.load_project()
        ms = next(m for m in data["milestones"] if m["id"] == "ms-1")
        assert ms["status"] == "observing"
        err = capsys.readouterr().err
        assert "re-opened ms-1 as observing" in err

    def test_interactive_active_reopens(
        self, project_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "a")
        # Suppress the actor-resolution subprocess in the occupation step
        # so the test stays deterministic on machines without git ancestors.
        monkeypatch.setenv("BEACON_NO_BRANCH", "1")
        monkeypatch.setenv("BEACON_NO_ASSIGNEE", "1")
        commands.cmd_task_add()
        data = commands.load_project()
        ms = next(m for m in data["milestones"] if m["id"] == "ms-1")
        assert ms["status"] == "in_progress"

    def test_interactive_skip_adds_without_reopen(
        self, project_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda _prompt: "s")
        commands.cmd_task_add()
        data = commands.load_project()
        ms = next(m for m in data["milestones"] if m["id"] == "ms-1")
        # Status stays done; task got added (= zombie risk, but the
        # operator opted in consciously by choosing "s").
        assert ms["status"] == "done"


class TestLeftoverWorktreePrompt:
    def test_no_worktree_no_warning(self, project_dir, monkeypatch, capsys):
        # Sanity: with no `.worktrees/<branch>/` directory, the helper is
        # a silent no-op.
        cmd_milestone._prompt_close_leftover_worktree("ms-1", "done")
        assert capsys.readouterr().err == ""

    def test_leftover_worktree_emits_warning(
        self, project_dir, monkeypatch, capsys
    ):
        # Synth a leftover worktree directory under the project root so the
        # helper's existence check trips.
        import branch as _branch
        ms_title = "Sample"
        branch_name = _branch.ms_branch_name("ms-1", ms_title)
        worktree_dir = project_dir / ".worktrees" / branch_name
        worktree_dir.mkdir(parents=True)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        cmd_milestone._prompt_close_leftover_worktree("ms-1", "done")
        err = capsys.readouterr().err
        assert "[ms-81 transition prompt]" in err
        assert str(worktree_dir) in err or branch_name in err
        assert "non-interactive" in err

    def test_unknown_ms_silent(self, project_dir, capsys):
        # Defensive: bad ms_id mustn't raise — the prompt is a UX surface,
        # never load-bearing.
        cmd_milestone._prompt_close_leftover_worktree("ms-99", "done")
        assert capsys.readouterr().err == ""
