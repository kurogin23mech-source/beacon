"""Tests for actor metadata on commit entries (ms-51 / e-934).

Verifies that:

* ``core.log_commit`` attaches ``meta.actor = {machine, agent}`` when given.
* ``cmd_log`` / ``cmd_log_finalize`` resolve the actor via lib/agent and
  pass it down to ``core.log_commit`` so every commit entry has a
  per-machine + per-agent footprint without user setup.
* Duplicate-commit fast path doesn't lose the previously recorded actor.
* Defensive shape: extra fields in the actor dict are stripped; partial
  fields are preserved as-is.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import core  # noqa: E402


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
                        "title": "Test",
                        "status": "in_progress",
                        "entries": [],
                        "progress": 0,
                    }
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


def _load(project_dir: Path) -> dict:
    return json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8")
    )


def _last_commit_entry(project_dir: Path, ms_id: str = "ms-51") -> dict:
    for ms in _load(project_dir)["milestones"]:
        if ms["id"] == ms_id:
            commits = [e for e in ms.get("entries", []) if e.get("type") == "commit"]
            assert commits, f"No commit entries in {ms_id}"
            return commits[-1]
    raise AssertionError(f"{ms_id} not found")


# ---------------------------------------------------------------------------
# core.log_commit() — direct API
# ---------------------------------------------------------------------------

def test_log_commit_attaches_actor_when_given():
    data = {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "entries": [], "progress": 0}],
        "summary": "",
    }
    core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234", message="m",
        date="2026-06-03T00:00:00Z",
        actor={"machine": "host-a", "agent": "alice"},
    )
    entry = data["milestones"][0]["entries"][-1]
    assert entry["meta"]["actor"] == {"machine": "host-a", "agent": "alice"}


def test_log_commit_without_actor_omits_field():
    data = {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "entries": [], "progress": 0}],
        "summary": "",
    }
    core.log_commit(data, ms_id="ms-1", commit_hash="abc1234",
                    message="m", date="2026-06-03T00:00:00Z")
    entry = data["milestones"][0]["entries"][-1]
    assert "actor" not in entry["meta"]


def test_log_commit_strips_unknown_actor_fields():
    """Defensive: callers might tack on extras; the meta shape stays stable."""
    data = {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "entries": [], "progress": 0}],
        "summary": "",
    }
    core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234", message="m",
        date="2026-06-03T00:00:00Z",
        actor={"machine": "host-a", "agent": "alice",
               "extra": "should-be-dropped", "another": 1},
    )
    actor = data["milestones"][0]["entries"][-1]["meta"]["actor"]
    assert set(actor.keys()) == {"machine", "agent"}


def test_log_commit_partial_actor_preserved():
    """If only one of {machine, agent} is supplied, persist what's there."""
    data = {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "entries": [], "progress": 0}],
        "summary": "",
    }
    core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234", message="m",
        date="2026-06-03T00:00:00Z", actor={"machine": "host-a"},
    )
    actor = data["milestones"][0]["entries"][-1]["meta"]["actor"]
    assert actor == {"machine": "host-a"}


def test_log_commit_empty_actor_dict_omits_field():
    data = {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "T", "status": "in_progress",
                        "entries": [], "progress": 0}],
        "summary": "",
    }
    core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234", message="m",
        date="2026-06-03T00:00:00Z", actor={"machine": "", "agent": ""},
    )
    entry = data["milestones"][0]["entries"][-1]
    # Empty values are dropped in the cleaning step → no actor key at all.
    assert "actor" not in entry["meta"]


# ---------------------------------------------------------------------------
# cmd_log / cmd_log_finalize — env-driven dispatch
# ---------------------------------------------------------------------------

def test_cmd_log_finalize_auto_attaches_actor_from_agent_module(project, monkeypatch):
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_HASH", "deadbeef")
    monkeypatch.setenv("BEACON_MESSAGE", "test commit")
    monkeypatch.setenv("BEACON_DATE", "2026-06-03T00:00:00Z")
    monkeypatch.setenv("BEACON_SUMMARY", "summary")
    monkeypatch.setenv("BEACON_PROGRESS", "")
    monkeypatch.setenv("BEACON_BEHAVIOR", "")
    monkeypatch.setenv("BEACON_RESOLVES", "")

    commands.cmd_log_finalize()

    entry = _last_commit_entry(project)
    actor = entry["meta"].get("actor")
    assert actor is not None
    # agent.json fixture set name="test-claude"
    assert actor["agent"] == "test-claude"
    assert actor["machine"]  # hostname is non-empty


def test_cmd_log_auto_attaches_actor(project, monkeypatch):
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_HASH", "feedface")
    monkeypatch.setenv("BEACON_MESSAGE", "msg")
    monkeypatch.setenv("BEACON_DATE", "2026-06-03T00:00:00Z")
    monkeypatch.setenv("BEACON_SUMMARY", "")

    commands.cmd_log()

    entry = _last_commit_entry(project)
    assert entry["meta"]["actor"]["agent"] == "test-claude"


def test_cmd_log_finalize_with_subagent_reflects_dispatch_chain(project, monkeypatch):
    """Multi-agent dispatcher scenario: child commits carry parent.<n> in meta.

    This is the 'Mac/Windows 並走' bookkeeping foundation (AC-4): each
    machine and dispatcher writes a distinguishable actor.
    """
    monkeypatch.setenv("BEACON_AGENT_PARENT", "mac-claude")
    monkeypatch.setenv("BEACON_AGENT_CHILD_ID", "dispatch-7")
    monkeypatch.setenv("BEACON_MS_ID", "ms-51")
    monkeypatch.setenv("BEACON_HASH", "cafebabe")
    monkeypatch.setenv("BEACON_MESSAGE", "child commit")
    monkeypatch.setenv("BEACON_DATE", "2026-06-03T00:00:00Z")

    commands.cmd_log_finalize()

    entry = _last_commit_entry(project)
    assert entry["meta"]["actor"]["agent"] == "mac-claude.dispatch-7"
