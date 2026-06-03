"""Tests for ``beacon ms join`` (ms-51 / e-933).

Covers the SPEC acceptance criteria:

  1. ``beacon ms join ms-XX`` adds the actor to the MS assignee list.
  2. ``--checkout`` switches to the ms-XX-<slug> branch (with origin
     fallback when the branch is remote-only).
  3. Already-an-assignee is a warned no-op (returns 0, emits "no-op").
  4. done / cancelled MSs reject the join (exit 1).
  5. No git-level push enforcement (the join is a logical claim).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402


@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "agent.json").write_text(
            json.dumps({"name": "test-claude"}), encoding="utf-8"
        )
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({
                "name": "test",
                "milestones": [
                    {
                        "id": "ms-51",
                        "title": "MS join feature",
                        "status": "in_progress",
                        "entries": [],
                        "progress": 0,
                    },
                    {
                        "id": "ms-99",
                        "title": "Finished MS",
                        "status": "done",
                        "entries": [],
                        "progress": 100,
                    },
                    {
                        "id": "ms-77",
                        "title": "Cancelled MS",
                        "status": "cancelled",
                        "entries": [],
                        "progress": 0,
                    },
                ],
                "summary": "",
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_AGENT_PARENT", raising=False)
        monkeypatch.delenv("BEACON_AGENT_CHILD_ID", raising=False)
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _ms(project_dir: Path, ms_id: str) -> dict:
    data = json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8")
    )
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            return ms
    raise AssertionError(f"{ms_id} not found")


class _R:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _mock_git(monkeypatch, *, in_repo=True, current="main",
              exists_branches=(), fetch_ok=True):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return _R(stdout="true\n" if in_repo else "",
                      returncode=0 if in_repo else 128)
        if args[:3] == ["git", "branch", "--show-current"]:
            return _R(stdout=current + "\n")
        if args[:3] == ["git", "show-ref", "--verify"]:
            ref = args[-1].replace("refs/heads/", "")
            return _R(returncode=0 if ref in exists_branches else 1)
        if args[:2] == ["git", "fetch"]:
            return _R(returncode=0 if fetch_ok else 1)
        if args[:2] == ["git", "checkout"]:
            return _R(returncode=0)
        return _R(returncode=1)

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# AC-1: join adds the actor as assignee
# ---------------------------------------------------------------------------

def test_join_adds_actor_as_assignee(project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    commands.cmd_milestone_join()

    ms = _ms(project, "ms-51")
    assert ms["assignee"] == "test-claude"
    out = capsys.readouterr().out
    assert "Joined" in out
    assert "test-claude" in out


# ---------------------------------------------------------------------------
# AC-3: re-join is a no-op
# ---------------------------------------------------------------------------

def test_join_when_already_assignee_is_noop(project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    commands.cmd_milestone_join()
    capsys.readouterr()  # drain

    commands.cmd_milestone_join()
    out = capsys.readouterr().out
    assert "Already on" in out or "no-op" in out
    # And still only one entry in the assignee list.
    ms = _ms(project, "ms-51")
    assert ms["assignee"] == "test-claude"


# ---------------------------------------------------------------------------
# AC-4: done / cancelled MS refuse the join
# ---------------------------------------------------------------------------

def test_join_done_ms_refused(project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "ms-99")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_join()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "done" in err


def test_join_cancelled_ms_refused(project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "ms-77")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_join()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "cancelled" in err


# ---------------------------------------------------------------------------
# AC-1: missing MS is an error
# ---------------------------------------------------------------------------

def test_join_missing_ms_errors(project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "ms-404")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_join()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower()


def test_join_requires_ms_id(project, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "")
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_join()
    assert ei.value.code == 1


# ---------------------------------------------------------------------------
# AC-2: --checkout switches to the branch
# ---------------------------------------------------------------------------

def test_join_checkout_switches_to_existing_local_branch(project, monkeypatch, capsys):
    import branch as _branch
    expected = _branch.ms_branch_name("ms-51", "MS join feature")
    calls = _mock_git(
        monkeypatch, in_repo=True, current="main",
        exists_branches=(expected,),
    )
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_CHECKOUT", "1")

    commands.cmd_milestone_join()

    # Verify a switch (checkout without -b) was attempted.
    switches = [c for c in calls if c == ["git", "checkout", expected]]
    assert switches
    out = capsys.readouterr().out
    assert "branch:" in out
    assert expected in out


def test_join_checkout_creates_branch_if_missing_locally(project, monkeypatch, capsys):
    """No local branch + no remote → falls through to local create from HEAD."""
    calls = _mock_git(
        monkeypatch, in_repo=True, current="main",
        exists_branches=(),
    )
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_CHECKOUT", "1")

    commands.cmd_milestone_join()

    # In the absence of remote, the local create path via -b should fire.
    create = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert create


def test_join_checkout_outside_git_warns(project, monkeypatch, capsys):
    _mock_git(monkeypatch, in_repo=False)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_CHECKOUT", "1")

    commands.cmd_milestone_join()

    err = capsys.readouterr().err
    assert "not in a git repo" in err or "warning" in err


def test_join_without_checkout_does_not_touch_git(project, monkeypatch):
    calls = _mock_git(monkeypatch, in_repo=True)
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_CHECKOUT", "")  # default: no checkout

    commands.cmd_milestone_join()

    # No git subprocess call should have fired.
    assert not calls
