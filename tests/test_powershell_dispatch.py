"""PowerShell-native dispatch coverage (ms-44 e-695 / e-799 / e-800).

This module verifies that ``beacon_cli/dispatch.py`` correctly translates
argv into the same ``BEACON_*`` env-var contract that ``bin/beacon``
already uses to call ``lib/commands.py``. The goal is structural: a
Windows user who installs Beacon via ``pipx install beacon`` (no bash,
no ``bin/beacon``) should be able to run the Day-1 commands and have
``commands.py`` see the exact same payload it would receive from the
bash path.

Strategy
--------

We don't actually shell out to ``python3 lib/commands.py`` here — that
would conflate dispatch correctness with commands.py behavior, and the
existing test suite already covers commands.py. Instead we patch
``beacon_cli.dispatch.subprocess.call`` and capture:

* the ``cmd`` argv that would be passed to ``python3 commands.py``,
* the ``env`` dict that would be exported,

then assert the expected env keys are present with the expected values.

We also simulate the bash-less environment by patching ``_find_bash`` to
``None`` so we exercise the same code path a Windows PowerShell shell
would hit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

import pytest

# Source tree access
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from beacon_cli import dispatch, main as main_mod  # noqa: E402  (sys.path mutated)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_call(monkeypatch):
    """Patch ``subprocess.call`` inside dispatch and return the capture box.

    The returned dict has keys ``cmd`` (list[str]) and ``env`` (dict[str,
    str]). Tests inspect what dispatch *would have* invoked.
    """
    box: Dict[str, Any] = {"cmd": None, "env": None, "calls": []}

    def fake_call(cmd, env=None, **kwargs):
        box["cmd"] = list(cmd)
        box["env"] = dict(env) if env is not None else None
        box["calls"].append((list(cmd), dict(env) if env is not None else None))
        return 0

    monkeypatch.setattr(dispatch.subprocess, "call", fake_call)
    return box


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """Create a fake project dir with ``.beacon/project.json`` and chdir.

    Most Phase-1 commands run ``_ensure_project`` which checks for the
    file existence. We need a real file on disk for those.
    """
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "test", "milestones": []}'
    )
    monkeypatch.chdir(tmp_path)
    # Make BEACON_PROJECT_FILE explicit so tests don't rely on global env.
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    yield tmp_path


@pytest.fixture
def no_bash(monkeypatch):
    """Simulate a Windows PowerShell environment (no bash, no bin/beacon)."""
    monkeypatch.setattr(main_mod, "_find_bash", lambda: None)
    monkeypatch.setattr(main_mod, "_find_bin_beacon", lambda root: None)


# ---------------------------------------------------------------------------
# Top-level fast paths (work without bash AND without project)
# ---------------------------------------------------------------------------


def test_version_works_without_bash(capsys, no_bash):
    rc = main_mod.main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "beacon " in out  # "beacon 0.5.0"


def test_help_works_without_bash(capsys, no_bash):
    rc = main_mod.main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "beacon" in out.lower()
    assert "task" in out.lower()
    assert "milestone" in out.lower()


def test_bare_invocation_shows_help_without_bash(capsys, no_bash):
    """Bash users get tmux; PowerShell users get help (no tmux available)."""
    rc = main_mod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "beacon" in out.lower()


def test_unknown_command_returns_nonzero(capsys, no_bash):
    rc = main_mod.main(["this-is-not-a-command"])
    # argparse errors exit with code 2.
    assert rc != 0


# ---------------------------------------------------------------------------
# ensure_project guard — fires before dispatch when project missing
# ---------------------------------------------------------------------------


def test_status_without_project_fails(capsys, tmp_path, monkeypatch, no_bash, captured_call):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    rc = main_mod.main(["status"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "No .beacon/project.json" in out
    assert captured_call["cmd"] is None, "should not have called commands.py"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_json(project_dir, no_bash, captured_call):
    rc = main_mod.main(["status", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env is not None
    assert env["BEACON_JSON"] == "1"
    assert env["BEACON_ALL"] == ""
    assert env["BEACON_MS_FILTER"] == ""
    # commands.py subcommand should be milestone_list (matches bin/beacon)
    assert captured_call["cmd"][-1] == "milestone_list"


def test_status_with_multiple_ms_filters(project_dir, no_bash, captured_call):
    rc = main_mod.main(["status", "--ms", "ms-1", "--ms", "ms-2"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_FILTER"] == "ms-1,ms-2"


def test_status_all_flag(project_dir, no_bash, captured_call):
    rc = main_mod.main(["status", "--all"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ALL"] == "1"


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_text(project_dir, no_bash, captured_call):
    rc = main_mod.main(["summary", "new", "direction"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SUMMARY_TEXT"] == "new direction"
    assert captured_call["cmd"][-1] == "summary"


# ---------------------------------------------------------------------------
# milestone subcommands
# ---------------------------------------------------------------------------


def test_milestone_list(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "list"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "milestone_list"


def test_milestone_ls_alias(project_dir, no_bash, captured_call):
    rc = main_mod.main(["ms", "ls"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "milestone_list"


def test_milestone_start_requires_id(project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main(["milestone", "start"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Usage:" in out


def test_milestone_start_id(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "start", "ms-99"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-99"
    assert captured_call["cmd"][-1] == "milestone_start"


def test_milestone_add_full(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "milestone", "add", "My new MS",
        "-d", "2026-12-31",
        "--description", "desc",
        "--priority", "high",
        "--objective", "obj",
        "--ac", "ac",
        "--owner", "alice",
        "--assignee", "bob",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_TITLE"] == "My new MS"
    assert env["BEACON_TARGET_DATE"] == "2026-12-31"
    assert env["BEACON_DESCRIPTION"] == "desc"
    assert env["BEACON_PRIORITY"] == "high"
    assert env["BEACON_OBJECTIVE"] == "obj"
    assert env["BEACON_ACCEPTANCE_CRITERIA"] == "ac"
    assert env["BEACON_OWNER"] == "alice"
    assert env["BEACON_ASSIGNEE"] == "bob"
    assert captured_call["cmd"][-1] == "milestone_add"


def test_milestone_done(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "done", "ms-7", "--reason", "shipped"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-7"
    assert env["BEACON_REASON"] == "shipped"
    assert captured_call["cmd"][-1] == "milestone_done"


def test_milestone_observe_maps_to_update(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "observe", "ms-7", "-r", "watching"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-7"
    assert env["BEACON_STATUS"] == "observing"
    assert env["BEACON_REASON"] == "watching"
    assert captured_call["cmd"][-1] == "milestone_update"


def test_milestone_show(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "show", "ms-7", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-7"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "milestone_show"


def test_milestone_update_progress(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "milestone", "update", "ms-7", "-p", "75", "--status", "in_progress",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-7"
    assert env["BEACON_PROGRESS"] == "75"
    assert env["BEACON_STATUS"] == "in_progress"
    assert captured_call["cmd"][-1] == "milestone_update"


# ---------------------------------------------------------------------------
# task subcommands
# ---------------------------------------------------------------------------


def test_task_add_minimal(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "add", "do the thing", "-m", "ms-7"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_DESCRIPTION"] == "do the thing"
    assert env["BEACON_MS_ID"] == "ms-7"
    assert env["BEACON_TYPE"] == "task"
    # BEACON_DATE should be ISO YYYY-MM-DD.
    assert len(env["BEACON_DATE"].split("-")) == 3
    assert captured_call["cmd"][-1] == "task_add"


def test_task_add_with_priority_motivation_ac(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "task", "add", "do it",
        "-m", "ms-7",
        "--priority", "high",
        "--motivation", "because",
        "--ac", "must pass tests",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_PRIORITY"] == "high"
    assert env["BEACON_MOTIVATION"] == "because"
    assert env["BEACON_ACCEPTANCE_CRITERIA"] == "must pass tests"


def test_task_done(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "done", "e-123", "-r", "completed"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-123"
    assert env["BEACON_REASON"] == "completed"
    assert captured_call["cmd"][-1] == "task_done"


def test_task_done_requires_id(project_dir, no_bash, captured_call, capsys):
    rc = main_mod.main(["task", "done"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "Usage:" in out


def test_task_list_with_ms(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "list", "-m", "ms-7", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-7"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "task_list"


def test_task_update(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "task", "update", "e-1",
        "--description", "new desc",
        "--status", "todo",
        "--priority", "low",
        "--behavior", "deferred",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-1"
    assert env["BEACON_DESCRIPTION"] == "new desc"
    assert env["BEACON_STATUS"] == "todo"
    assert env["BEACON_PRIORITY"] == "low"
    assert env["BEACON_BEHAVIOR"] == "deferred"
    assert captured_call["cmd"][-1] == "task_update"


def test_task_show(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "show", "e-1", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-1"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "task_show"


def test_task_cancel(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "cancel", "e-1", "--reason", "dropped"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-1"
    assert env["BEACON_REASON"] == "dropped"
    assert captured_call["cmd"][-1] == "task_cancel"


def test_task_delete(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "delete", "e-1"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-1"
    assert captured_call["cmd"][-1] == "task_delete"


def test_task_detail(project_dir, no_bash, captured_call):
    rc = main_mod.main(["task", "detail", "e-1", "extra detail text"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-1"
    assert env["BEACON_DETAIL"] == "extra detail text"
    assert captured_call["cmd"][-1] == "task_detail"


# ---------------------------------------------------------------------------
# doc subcommands
# ---------------------------------------------------------------------------


def test_doc_add(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "doc", "add", "My Doc",
        "--scope", "spec",
        "--ms", "ms-7",
        "--id", "myslug",
        "--content", "body",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_TITLE"] == "My Doc"
    assert env["BEACON_SCOPE"] == "spec"
    assert env["BEACON_MS"] == "ms-7"
    assert env["BEACON_DOC_ID"] == "myslug"
    assert env["BEACON_CONTENT"] == "body"
    assert captured_call["cmd"][-1] == "doc_add"


def test_doc_list(project_dir, no_bash, captured_call):
    rc = main_mod.main(["doc", "list", "--scope", "core", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_SCOPE"] == "core"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "doc_list"


def test_doc_show(project_dir, no_bash, captured_call):
    rc = main_mod.main(["doc", "show", "doc-abc"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_DOC_ID"] == "doc-abc"
    assert captured_call["cmd"][-1] == "doc_show"


def test_doc_update(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "doc", "update", "doc-abc",
        "--title", "new title",
        "--content", "new body",
        "--scope", "memo",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_DOC_ID"] == "doc-abc"
    assert env["BEACON_TITLE"] == "new title"
    assert env["BEACON_CONTENT"] == "new body"
    assert env["BEACON_SCOPE"] == "memo"
    assert captured_call["cmd"][-1] == "doc_update"


def test_doc_delete(project_dir, no_bash, captured_call):
    rc = main_mod.main(["doc", "delete", "doc-abc"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_DOC_ID"] == "doc-abc"
    assert captured_call["cmd"][-1] == "doc_delete"


# ---------------------------------------------------------------------------
# note subcommands
# ---------------------------------------------------------------------------


def test_note_add(project_dir, no_bash, captured_call):
    rc = main_mod.main(["note", "remember this thing", "--context", "session"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_NOTE_TEXT"] == "remember this thing"
    assert env["BEACON_NOTE_CONTEXT"] == "session"
    assert captured_call["cmd"][-1] == "note_add"


def test_note_list(project_dir, no_bash, captured_call):
    rc = main_mod.main(["note", "list", "--json"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "note_list"
    env = captured_call["env"]
    assert env["BEACON_JSON"] == "1"


def test_note_clear(project_dir, no_bash, captured_call):
    rc = main_mod.main(["note", "clear"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "note_clear"


# ---------------------------------------------------------------------------
# trigger subcommands
# ---------------------------------------------------------------------------


def test_trigger_fire(project_dir, no_bash, captured_call):
    rc = main_mod.main(["trigger", "fire", "my_trigger", "hello there"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_TRIGGER_NAME"] == "my_trigger"
    assert env["BEACON_TRIGGER_MESSAGE"] == "hello there"
    assert captured_call["cmd"][-1] == "trigger_fire"


def test_trigger_check(project_dir, no_bash, captured_call):
    rc = main_mod.main(["trigger", "check"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "trigger_check"


def test_trigger_clear(project_dir, no_bash, captured_call):
    rc = main_mod.main(["trigger", "clear", "my_trigger"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_TRIGGER_NAME"] == "my_trigger"
    assert captured_call["cmd"][-1] == "trigger_clear"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_basic(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "search", "windows install",
        "--ms", "ms-44",
        "--type", "task",
        "--limit", "20",
        "--json",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_QUERY"] == "windows install"
    assert env["BEACON_MS_ID"] == "ms-44"
    assert env["BEACON_TYPE"] == "task"
    assert env["BEACON_LIMIT"] == "20"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "search"


# ---------------------------------------------------------------------------
# cycle / push / deploy
# ---------------------------------------------------------------------------


def test_cycle_status(project_dir, no_bash, captured_call):
    rc = main_mod.main(["cycle", "status", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "cycle_status"


def test_push_record(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "push", "record",
        "--from", "abc123",
        "--to", "def456",
        "--branch", "main",
        "--desc", "rolled out",
        "-m", "ms-7",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_FROM"] == "abc123"
    assert env["BEACON_TO"] == "def456"
    assert env["BEACON_BRANCH"] == "main"
    assert env["BEACON_DESCRIPTION"] == "rolled out"
    assert env["BEACON_MS"] == "ms-7"
    assert captured_call["cmd"][-1] == "push_record"


def test_push_list(project_dir, no_bash, captured_call):
    rc = main_mod.main(["push", "list", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "push_list"


def test_deploy_record(project_dir, no_bash, captured_call):
    rc = main_mod.main([
        "deploy", "record",
        "--revision", "r-1",
        "--semver", "v1.2.3",
        "--desc", "first deploy",
        "--env", "prod",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_REVISION"] == "r-1"
    assert env["BEACON_SEMVER"] == "v1.2.3"
    assert env["BEACON_DESCRIPTION"] == "first deploy"
    assert env["BEACON_ENVIRONMENT"] == "prod"
    assert captured_call["cmd"][-1] == "deploy_record"


def test_deploy_list(project_dir, no_bash, captured_call):
    rc = main_mod.main(["deploy", "list", "--env", "prod", "--json"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENVIRONMENT"] == "prod"
    assert env["BEACON_JSON"] == "1"
    assert captured_call["cmd"][-1] == "deploy_list"


# ---------------------------------------------------------------------------
# entry move / project / doctor
# ---------------------------------------------------------------------------


def test_entry_move(project_dir, no_bash, captured_call):
    rc = main_mod.main(["entry", "move", "e-1", "-m", "ms-9"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_ENTRY_ID"] == "e-1"
    assert env["BEACON_MS_ID"] == "ms-9"
    assert captured_call["cmd"][-1] == "entry_move"


def test_project_archive(project_dir, no_bash, captured_call):
    rc = main_mod.main(["project", "archive"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "project_archive"


def test_doctor(no_bash, captured_call, tmp_path, monkeypatch):
    # doctor does NOT require a project file (bin/beacon proves it).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    rc = main_mod.main(["doctor"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "doctor"


# ---------------------------------------------------------------------------
# init (Python fallback)
# ---------------------------------------------------------------------------


def test_init_requires_name_and_objective_non_interactive(
    no_bash, captured_call, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    # Force non-TTY so we don't try to prompt.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = main_mod.main(["init"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--name" in err and "--objective" in err
    assert captured_call["cmd"] is None  # never reached commands.py


def test_init_with_flags(no_bash, captured_call, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    rc = main_mod.main([
        "init",
        "--name", "myproj",
        "--objective", "do something useful",
        "--retro-day", "monday",
    ])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_NAME"] == "myproj"
    assert env["BEACON_OBJECTIVE"] == "do something useful"
    assert env["BEACON_RETRO_DAY"] == "monday"
    assert captured_call["cmd"][-1] == "init"


def test_init_in_home_dir_refused(no_bash, captured_call, tmp_path, monkeypatch, capsys):
    """Mirror bash guard: refuse to init in $HOME without explicit flags."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEACON_PROJECT_FILE", ".beacon/project.json")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = main_mod.main(["init"])
    # No flags supplied → refused with explanatory error.
    assert rc != 0
    captured_call["cmd"] is None


# ---------------------------------------------------------------------------
# log — uses git, so we patch subprocess.check_output too
# ---------------------------------------------------------------------------


def test_log_normal(project_dir, no_bash, captured_call, monkeypatch):
    """log should read HEAD via git and pass commit metadata as env vars."""
    git_responses = {
        ("git", "rev-parse", "--is-inside-work-tree"): b"true\n",
        ("git", "rev-parse", "--short", "HEAD"): "abc1234",
        ("git", "log", "-1", "--pretty=%s", "HEAD"): "feat: do thing",
        ("git", "log", "-1", "--pretty=%ci", "HEAD"): "2026-06-02 12:00:00 +0900",
        (
            "git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
        ): "src/foo.py\n",
    }

    def fake_check_output(args, **kwargs):
        key = tuple(args)
        if key not in git_responses:
            raise KeyError(f"unmocked git call: {args}")
        resp = git_responses[key]
        # Honor text=True like real subprocess
        if isinstance(resp, bytes) and kwargs.get("text"):
            return resp.decode()
        if isinstance(resp, str) and not kwargs.get("text"):
            return resp.encode()
        return resp

    monkeypatch.setattr(dispatch.subprocess, "check_output", fake_check_output)

    rc = main_mod.main(["log", "--summary", "manual override"])
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_HASH"] == "abc1234"
    assert env["BEACON_MESSAGE"] == "feat: do thing"
    assert env["BEACON_DATE"] == "2026-06-02"
    assert env["BEACON_SUMMARY"] == "manual override"
    assert captured_call["cmd"][-1] == "log"


def test_log_skips_beacon_only_commits(project_dir, no_bash, captured_call, monkeypatch, capsys):
    """The infinite-loop guard from bash must be preserved in Python."""
    git_responses = {
        ("git", "rev-parse", "--is-inside-work-tree"): b"true\n",
        ("git", "rev-parse", "--short", "HEAD"): "deadbee",
        ("git", "log", "-1", "--pretty=%s", "HEAD"): "chore: beacon update",
        ("git", "log", "-1", "--pretty=%ci", "HEAD"): "2026-06-02 12:00:00 +0900",
        (
            "git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD",
        ): ".beacon/project.json\n.beacon/changelog.jsonl\n",
    }

    def fake_check_output(args, **kwargs):
        resp = git_responses[tuple(args)]
        if isinstance(resp, bytes) and kwargs.get("text"):
            return resp.decode()
        return resp

    monkeypatch.setattr(dispatch.subprocess, "check_output", fake_check_output)

    rc = main_mod.main(["log"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Skipped" in out
    assert captured_call["cmd"] is None  # never reached commands.py


# ---------------------------------------------------------------------------
# Env var contract — equivalence to bash for spot-checked commands
# ---------------------------------------------------------------------------


# This is the canonical mapping from CLI flags to BEACON_* env vars that
# bin/beacon establishes. The Python dispatch must remain bit-compatible
# with this so commands.py keeps working unchanged.
#
# Each entry: (argv_excluding_top_level, expected_subcmd_name, expected_env)
_EQUIVALENCE_CASES: List[Tuple[List[str], str, Dict[str, str]]] = [
    (
        ["status", "--json", "--ms", "ms-1", "--ms", "ms-2"],
        "milestone_list",
        {"BEACON_JSON": "1", "BEACON_MS_FILTER": "ms-1,ms-2"},
    ),
    (
        ["task", "add", "x", "-m", "ms-7", "--priority", "high"],
        "task_add",
        {
            "BEACON_DESCRIPTION": "x",
            "BEACON_MS_ID": "ms-7",
            "BEACON_PRIORITY": "high",
            "BEACON_TYPE": "task",
        },
    ),
    (
        ["task", "done", "e-9", "-r", "shipped"],
        "task_done",
        {"BEACON_ENTRY_ID": "e-9", "BEACON_REASON": "shipped"},
    ),
    (
        ["milestone", "observe", "ms-1", "-r", "watching"],
        "milestone_update",
        {"BEACON_MS_ID": "ms-1", "BEACON_STATUS": "observing", "BEACON_REASON": "watching"},
    ),
    (
        ["doc", "add", "T", "--scope", "spec", "--ms", "ms-7"],
        "doc_add",
        {
            "BEACON_TITLE": "T",
            "BEACON_SCOPE": "spec",
            "BEACON_MS": "ms-7",
        },
    ),
    (
        ["summary", "hello", "world"],
        "summary",
        {"BEACON_SUMMARY_TEXT": "hello world"},
    ),
    (
        ["note", "remember", "--context", "ctx"],
        "note_add",
        {"BEACON_NOTE_TEXT": "remember", "BEACON_NOTE_CONTEXT": "ctx"},
    ),
    (
        ["trigger", "fire", "tname", "tmsg"],
        "trigger_fire",
        {"BEACON_TRIGGER_NAME": "tname", "BEACON_TRIGGER_MESSAGE": "tmsg"},
    ),
    (
        ["search", "q", "--ms", "ms-44", "--scope", "spec"],
        "search",
        {"BEACON_QUERY": "q", "BEACON_MS_ID": "ms-44", "BEACON_SCOPE": "spec"},
    ),
]


@pytest.mark.parametrize("argv, subcmd, env_subset", _EQUIVALENCE_CASES)
def test_env_var_contract_matches_bash(
    project_dir, no_bash, captured_call, argv, subcmd, env_subset
):
    rc = main_mod.main(argv)
    assert rc == 0, f"argv {argv!r} returned non-zero: {rc}"
    assert captured_call["cmd"][-1] == subcmd, (
        f"argv {argv!r}: dispatch called commands.py with "
        f"{captured_call['cmd'][-1]!r}, expected {subcmd!r}"
    )
    env = captured_call["env"]
    for k, v in env_subset.items():
        assert env.get(k) == v, (
            f"argv {argv!r}: env[{k!r}] was {env.get(k)!r}, expected {v!r}"
        )


# ---------------------------------------------------------------------------
# PYTHONPATH and BEACON_DIR plumbing
# ---------------------------------------------------------------------------


def test_dispatch_sets_beacon_dir(project_dir, no_bash, captured_call):
    """BEACON_DIR must be set so commands.py can find its sibling modules."""
    rc = main_mod.main(["status"])
    assert rc == 0
    env = captured_call["env"]
    assert env.get("BEACON_DIR")
    assert Path(env["BEACON_DIR"]).exists()


def test_dispatch_sets_pythonpath(project_dir, no_bash, captured_call):
    """PYTHONPATH must point at the lib/ dir so `from store import ...` works."""
    rc = main_mod.main(["status"])
    assert rc == 0
    env = captured_call["env"]
    pp = env.get("PYTHONPATH", "")
    parts = pp.split(os.pathsep) if pp else []
    assert any(
        Path(part).name in ("lib", "_bundled_lib") for part in parts if part
    ), f"PYTHONPATH={pp!r} doesn't include lib/ or _bundled_lib/"


def test_dispatch_invokes_python_executable(project_dir, no_bash, captured_call):
    rc = main_mod.main(["status"])
    assert rc == 0
    # cmd[0] is the python executable, cmd[1] is commands.py, cmd[2] is the subcmd
    assert captured_call["cmd"][0] == sys.executable
    assert captured_call["cmd"][1].endswith("commands.py")
    assert captured_call["cmd"][2] == "milestone_list"


# ---------------------------------------------------------------------------
# milestone graph / workspace / workspace-cleanup (e-842)
#
# These power /beacon-dispatch. They exist in commands.py and the legacy
# dispatch table but were missing from the PowerShell-native dispatcher, so
# /beacon-dispatch was unusable on Windows/OSS. Verify they now route.
# ---------------------------------------------------------------------------


def test_milestone_graph_json(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "graph", "--json"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "milestone_graph"
    assert captured_call["env"]["BEACON_JSON"] == "1"


def test_milestone_ms_alias_graph(project_dir, no_bash, captured_call):
    rc = main_mod.main(["ms", "graph"])
    assert rc == 0
    assert captured_call["cmd"][-1] == "milestone_graph"
    assert captured_call["env"]["BEACON_JSON"] == ""


def test_milestone_workspace_routes_with_executor(project_dir, no_bash, captured_call):
    rc = main_mod.main(
        ["milestone", "workspace", "ms-1", "--executor", "ai", "--json"]
    )
    assert rc == 0
    assert captured_call["cmd"][-1] == "milestone_workspace"
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-1"
    assert env["BEACON_EXECUTOR"] == "ai"
    assert env["BEACON_JSON"] == "1"


def test_milestone_workspace_dir_and_no_git(project_dir, no_bash, captured_call):
    rc = main_mod.main(
        ["milestone", "workspace", "ms-3", "--dir", "/tmp/ws", "--no-git", "--clear"]
    )
    assert rc == 0
    env = captured_call["env"]
    assert env["BEACON_WORKSPACE"] == "/tmp/ws"
    assert env["BEACON_NO_GIT"] == "1"
    assert env["BEACON_CLEAR"] == "1"


def test_milestone_workspace_requires_ms_id(project_dir, no_bash, captured_call):
    rc = main_mod.main(["milestone", "workspace"])
    assert rc == 1
    assert captured_call["cmd"] is None  # never reached commands.py


def test_milestone_workspace_cleanup_routes(project_dir, no_bash, captured_call):
    rc = main_mod.main(
        ["milestone", "workspace-cleanup", "ms-2", "--merge-to", "main"]
    )
    assert rc == 0
    assert captured_call["cmd"][-1] == "milestone_workspace_cleanup"
    env = captured_call["env"]
    assert env["BEACON_MS_ID"] == "ms-2"
    assert env["BEACON_MERGE_TO"] == "main"
