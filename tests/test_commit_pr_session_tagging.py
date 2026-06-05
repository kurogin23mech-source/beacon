"""Tests for session_id metadata on commit / PR entries (ms-57 / e-1062).

Locks in the contract that powers session-log aggregation (e-1038, e-1039):

* ``core.log_commit`` writes ``meta.session_id`` when given a non-empty value.
* ``core.pr_add`` writes ``meta.session_id`` when given a non-empty value.
* Empty / omitted session_id leaves the field absent — forward-only,
  no "" sentinels in stored data so aggregation queries can use
  ``WHERE meta.session_id == X`` cleanly.
* ``commands._resolve_session_id`` swallows all errors and returns "" so
  a misbehaving session module can never break commit / PR recording.
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
import core  # noqa: E402
import session  # noqa: E402


@pytest.fixture
def project_dir(monkeypatch):
    """CWD = tmp project with .beacon/ + a single MS to receive entries."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "agent.json").write_text(
            json.dumps({"name": "test-agent"}), encoding="utf-8"
        )
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_SESSION_FRESH_SECONDS", raising=False)
        monkeypatch.delenv("BEACON_SESSION_CLOUD_DEBOUNCE_SECONDS", raising=False)
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        yield Path(tmp)


@pytest.fixture
def project_data():
    return {
        "name": "test",
        "milestones": [
            {
                "id": "ms-57",
                "title": "Session aggregation",
                "status": "in_progress",
                "entries": [],
                "progress": 0,
            }
        ],
    }


# ---------------------------------------------------------------------------
# core.log_commit
# ---------------------------------------------------------------------------

def test_log_commit_writes_session_id_when_given(project_data):
    core.log_commit(
        project_data,
        ms_id="ms-57",
        commit_hash="abc1234",
        message="feat: test",
        date="2026-06-06",
        session_id="my-session-1780000000000-aabbccdd",
    )
    entries = project_data["milestones"][0]["entries"]
    commit = next(e for e in entries if e["type"] == "commit")
    assert commit["meta"]["session_id"] == "my-session-1780000000000-aabbccdd"


def test_log_commit_omits_session_id_when_empty(project_data):
    """Empty string is the documented 'no session' sentinel and must NOT
    show up in stored meta — aggregation queries assume the field is
    either present and meaningful, or absent."""
    core.log_commit(
        project_data,
        ms_id="ms-57",
        commit_hash="abc1234",
        message="feat: test",
        date="2026-06-06",
        session_id="",
    )
    entries = project_data["milestones"][0]["entries"]
    commit = next(e for e in entries if e["type"] == "commit")
    assert "session_id" not in commit["meta"]


def test_log_commit_session_id_default_is_omitted(project_data):
    """Backwards-compat: existing callers that don't pass session_id at all
    must produce entries with no session_id field (forward-only design)."""
    core.log_commit(
        project_data,
        ms_id="ms-57",
        commit_hash="abc1234",
        message="feat: test",
        date="2026-06-06",
    )
    entries = project_data["milestones"][0]["entries"]
    commit = next(e for e in entries if e["type"] == "commit")
    assert "session_id" not in commit["meta"]


def test_log_commit_session_id_coexists_with_actor(project_data):
    """Both actor and session_id must be persisted independently."""
    core.log_commit(
        project_data,
        ms_id="ms-57",
        commit_hash="abc1234",
        message="feat: test",
        date="2026-06-06",
        actor={"machine": "mac1", "agent": "claude-mac"},
        session_id="sess-1",
    )
    commit = project_data["milestones"][0]["entries"][0]
    assert commit["meta"]["actor"] == {"machine": "mac1", "agent": "claude-mac"}
    assert commit["meta"]["session_id"] == "sess-1"


# ---------------------------------------------------------------------------
# core.pr_add
# ---------------------------------------------------------------------------

def test_pr_add_writes_session_id_when_given(project_data):
    eid = core.pr_add(
        project_data,
        ms_id="ms-57",
        url="https://github.com/x/y/pull/42",
        intent="test",
        date="2026-06-06",
        title="Test PR",
        session_id="my-session-1780000000000-aabbccdd",
    )
    pr_entry = next(e for e in project_data["milestones"][0]["entries"] if e["id"] == eid)
    assert pr_entry["meta"]["session_id"] == "my-session-1780000000000-aabbccdd"


def test_pr_add_omits_session_id_when_empty(project_data):
    eid = core.pr_add(
        project_data,
        ms_id="ms-57",
        url="https://github.com/x/y/pull/42",
        intent="test",
        date="2026-06-06",
        title="Test PR",
        session_id="",
    )
    pr_entry = next(e for e in project_data["milestones"][0]["entries"] if e["id"] == eid)
    assert "session_id" not in pr_entry["meta"]


def test_pr_add_session_id_default_is_omitted(project_data):
    eid = core.pr_add(
        project_data,
        ms_id="ms-57",
        url="https://github.com/x/y/pull/42",
        intent="test",
        date="2026-06-06",
        title="Test PR",
    )
    pr_entry = next(e for e in project_data["milestones"][0]["entries"] if e["id"] == eid)
    assert "session_id" not in pr_entry["meta"]


def test_pr_add_preserves_existing_meta_fields(project_data):
    """session_id must not displace url / author / pr_status / etc."""
    eid = core.pr_add(
        project_data,
        ms_id="ms-57",
        url="https://github.com/x/y/pull/42",
        author="alice",
        intent="ship X",
        date="2026-06-06",
        title="Test PR",
        session_id="sess-1",
    )
    pr_entry = next(e for e in project_data["milestones"][0]["entries"] if e["id"] == eid)
    meta = pr_entry["meta"]
    assert meta["session_id"] == "sess-1"
    assert meta["url"] == "https://github.com/x/y/pull/42"
    assert meta["author"] == "alice"
    assert meta["pr_number"] == 42
    assert meta["intent"] == "ship X"
    assert meta["review_status"] == "pending"


# ---------------------------------------------------------------------------
# commands._resolve_session_id
# ---------------------------------------------------------------------------

def test_resolve_session_id_returns_minted_id(project_dir):
    sid = commands._resolve_session_id()
    assert sid
    # Should match the one session.py would mint.
    assert sid == session.get_session_id()


def test_resolve_session_id_is_stable_within_window(project_dir):
    a = commands._resolve_session_id()
    b = commands._resolve_session_id()
    assert a == b


def test_resolve_session_id_swallows_errors(project_dir, monkeypatch):
    """A broken session module must NOT crash commit / PR recording —
    the contract is "best-effort tagging, never block the write"."""
    def _boom():
        raise RuntimeError("simulated session breakage")
    monkeypatch.setattr(session, "get_session_id", _boom)
    assert commands._resolve_session_id() == ""
