"""CLI tests for `beacon rollback` (ms-55 e-1647).

The pure-logic tests in test_rollback.py cover plan / execute / report.
This file exercises the env-var entry point exposed by cmd_rollback —
making sure the bash dispatcher wiring (= the BEACON_ROLLBACK_* env
vars) lines up with the Python side.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


def _run(cwd, *args):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True,
    )


@pytest.fixture
def repo(tmp_path):
    cwd = str(tmp_path)
    _run(cwd, "git", "init", "-q", "-b", "main")
    _run(cwd, "git", "config", "user.email", "test@example.com")
    _run(cwd, "git", "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("hi\n")
    _run(cwd, "git", "add", "README.md")
    _run(cwd, "git", "commit", "-q", "-m", "initial")
    return cwd


def _clear_env(monkeypatch):
    for k in (
        "BEACON_ROLLBACK_COMMITS",
        "BEACON_ROLLBACK_REASON",
        "BEACON_ROLLBACK_DRY_RUN",
        "BEACON_ROLLBACK_CWD",
        "BEACON_ROLLBACK_NO_RECORD",
        "BEACON_JSON",
    ):
        monkeypatch.delenv(k, raising=False)
    # ms-55 e-1727: the rollback history record path calls
    # operations.apply_operation, which needs either a real local
    # project.json or the firestore_client mock. CLI mechanics tests
    # don't need the trail, so opt out by default; tests that *do*
    # care about the record (= test_cli_records_history_on_execute)
    # override BEACON_ROLLBACK_NO_RECORD explicitly.
    monkeypatch.setenv("BEACON_ROLLBACK_NO_RECORD", "1")


def test_cli_clean_repo_says_nothing(monkeypatch, capsys, repo):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    commands.cmd_rollback()
    out = capsys.readouterr().out
    assert "nothing" in out.lower()


def test_cli_dirty_dry_run_does_not_mutate(monkeypatch, capsys, repo, tmp_path):
    _clear_env(monkeypatch)
    (tmp_path / "wip.txt").write_text("dirty\n")
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    monkeypatch.setenv("BEACON_ROLLBACK_DRY_RUN", "1")
    monkeypatch.setenv("BEACON_ROLLBACK_REASON", "rehearsal")
    commands.cmd_rollback()
    out = capsys.readouterr().out
    assert "rehearsal" in out
    assert "dry run" in out
    # Still dirty.
    s = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        capture_output=True, text=True,
    )
    assert "wip.txt" in s.stdout


def test_cli_dirty_execute_stashes(monkeypatch, capsys, repo, tmp_path):
    _clear_env(monkeypatch)
    (tmp_path / "wip.txt").write_text("dirty\n")
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    monkeypatch.setenv("BEACON_ROLLBACK_REASON", "halt")
    commands.cmd_rollback()
    out = capsys.readouterr().out
    assert "Executed" in out
    assert "stashed" in out
    # Working tree should be clean now.
    s = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo,
        capture_output=True, text=True,
    )
    assert s.stdout.strip() == ""
    # And the stash carries the reason.
    sl = subprocess.run(
        ["git", "stash", "list"], cwd=repo,
        capture_output=True, text=True,
    )
    assert "halt" in sl.stdout


def test_cli_json_mode(monkeypatch, capsys, repo, tmp_path):
    _clear_env(monkeypatch)
    (tmp_path / "wip.txt").write_text("dirty\n")
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    monkeypatch.setenv("BEACON_ROLLBACK_DRY_RUN", "1")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_rollback()
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["dry_run"] is True
    assert parsed["plan"]["stash_working_tree"] is True
    assert "result" not in parsed  # dry_run → no result key


def test_cli_invalid_commits_errors(monkeypatch, capsys, repo):
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    monkeypatch.setenv("BEACON_ROLLBACK_COMMITS", "not-a-number")
    with pytest.raises(SystemExit) as exc:
        commands.cmd_rollback()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "integer" in err


def test_cli_records_history_on_execute(monkeypatch, capsys, repo, tmp_path):
    """ms-55 e-1727: a non-dry-run rollback that actually mutated the tree
    writes a save entry to the active MS so `beacon search "rollback"`
    can find it later."""
    _clear_env(monkeypatch)
    # Override the default opt-out from _clear_env — this test wants
    # the record path exercised end-to-end.
    monkeypatch.delenv("BEACON_ROLLBACK_NO_RECORD", raising=False)

    # Set up a real local project.json so apply_operation has somewhere
    # to write. Place it OUTSIDE the repo dir — otherwise the dirty
    # working-tree stash sweeps project.json up with it (= it's an
    # untracked file from git's POV).
    project_dir = tmp_path.parent / f"{tmp_path.name}-proj"
    project_dir.mkdir(exist_ok=True)
    project_file = project_dir / "project.json"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")

    import core
    project_data = {
        "name": "test-rollback-record",
        "schema_version": "1",
        "summary": "",
        "milestones": [
            {
                "id": "ms-1",
                "title": "Active MS",
                "status": "in_progress",
                "progress": 0,
                "target_date": "",
                "entries": [],
            }
        ],
    }
    project_file.write_text(json.dumps(project_data))

    # Make a dirty working tree so the rollback actually has work to do.
    (tmp_path / "wip.txt").write_text("dirty\n")
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    monkeypatch.setenv("BEACON_ROLLBACK_REASON", "test rollback for record")

    commands.cmd_rollback()
    captured = capsys.readouterr()
    out = captured.out
    err = captured.err

    # Surface assertions: report tells the user the trail was recorded.
    assert "Executed" in out
    assert "stashed" in out
    assert "Recorded:" in out, f"stdout=\n{out}\nstderr=\n{err}"
    # The recorded entry should appear in the project file with
    # type=save + source=rollback.
    saved = json.loads(project_file.read_text())
    entries = saved["milestones"][0]["entries"]
    rollback_entries = [
        e for e in entries
        if e.get("type") == "save"
        and e.get("meta", {}).get("source") == "rollback"
    ]
    assert len(rollback_entries) == 1, entries
    desc = rollback_entries[0]["description"]
    # AC (2) — description carries the reason + commit hash + working
    # tree change summary.
    assert "test rollback for record" in desc
    assert "stashed" in desc
    assert "working-tree" in desc
    # AC: the entry meta.hash points at the HEAD we rolled back from.
    assert rollback_entries[0]["meta"].get("hash")


def test_cli_records_skipped_for_noop(monkeypatch, capsys, repo, tmp_path):
    """No-op rollback (clean tree, nothing to undo) should NOT pollute
    history. Saves are only worth their bytes when something happened."""
    _clear_env(monkeypatch)
    monkeypatch.delenv("BEACON_ROLLBACK_NO_RECORD", raising=False)
    project_dir = tmp_path.parent / f"{tmp_path.name}-proj-noop"
    project_dir.mkdir(exist_ok=True)
    project_file = project_dir / "project.json"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    project_file.write_text(json.dumps({
        "name": "test-noop", "schema_version": "1", "summary": "",
        "milestones": [{
            "id": "ms-1", "title": "Active MS", "status": "in_progress",
            "progress": 0, "target_date": "", "entries": [],
        }],
    }))
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    commands.cmd_rollback()
    out = capsys.readouterr().out
    assert "nothing" in out.lower()
    assert "Recorded:" not in out
    saved = json.loads(project_file.read_text())
    assert saved["milestones"][0]["entries"] == []


def test_cli_no_record_flag_skips_history(monkeypatch, capsys, repo, tmp_path):
    """BEACON_ROLLBACK_NO_RECORD=1 disables the trail even when there's
    work to record. Useful for one-off cleanups + the test escape hatch."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BEACON_ROLLBACK_NO_RECORD", "1")
    project_dir = tmp_path.parent / f"{tmp_path.name}-proj-noflag"
    project_dir.mkdir(exist_ok=True)
    project_file = project_dir / "project.json"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    project_file.write_text(json.dumps({
        "name": "test-noflag", "schema_version": "1", "summary": "",
        "milestones": [{
            "id": "ms-1", "title": "Active MS", "status": "in_progress",
            "progress": 0, "target_date": "", "entries": [],
        }],
    }))
    (tmp_path / "wip.txt").write_text("dirty\n")
    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    commands.cmd_rollback()
    out = capsys.readouterr().out
    assert "Executed" in out
    assert "Recorded:" not in out
    saved = json.loads(project_file.read_text())
    assert saved["milestones"][0]["entries"] == []


def test_cli_explicit_commits_with_upstream(monkeypatch, capsys, repo, tmp_path):
    """With an upstream, --commits N triggers a soft reset."""
    _clear_env(monkeypatch)
    bare = tmp_path.parent / "remote2.git"
    if bare.exists():
        import shutil
        shutil.rmtree(bare)
    _run(repo, "git", "init", "-q", "--bare", str(bare))
    _run(repo, "git", "remote", "add", "origin", str(bare))
    _run(repo, "git", "push", "-q", "-u", "origin", "main")
    # Make an un-pushed commit.
    (tmp_path / "a.txt").write_text("x\n")
    _run(repo, "git", "add", "a.txt")
    _run(repo, "git", "commit", "-q", "-m", "add a")

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True,
    ).stdout.strip()

    monkeypatch.setenv("BEACON_ROLLBACK_CWD", repo)
    monkeypatch.setenv("BEACON_ROLLBACK_COMMITS", "1")
    monkeypatch.setenv("BEACON_ROLLBACK_REASON", "stop")
    commands.cmd_rollback()
    out = capsys.readouterr().out
    assert "reset 1 commit" in out

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True,
    ).stdout.strip()
    assert head_before != head_after
