"""Tests for fork.json target_ms_id priority in cmd_log (ms-79 / e-1816).

Verifies that:

* ``_read_fork_target_ms_id`` returns the target_ms_id when fork.json is
  present, "" otherwise (including malformed JSON / missing fields).
* ``cmd_log_prepare`` picks the fork target as the single milestone
  when no --ms is given and fork.json is present.
* ``cmd_log_prepare`` falls back to active candidates when the
  fork-target ms doesn't exist in project.json (= stale fork hint).
* Explicit --ms overrides fork.json (= fork.json is a hint, not a
  command).
* ``cmd_log`` writes commit under the fork-target MS when fork.json
  exists and --ms is absent.

These tests guard against the regression UC3-F2 worried about:
"fork 子で commit したら親 MS に誤記録".
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_log  # noqa: E402  (ms-127 e-4320: _read_fork_target_ms_id lives here; not re-exported on commands)


def _make_project(tmp: Path, fork_target: str = "") -> Path:
    beacon_dir = tmp / ".beacon"
    beacon_dir.mkdir()
    project_file = beacon_dir / "project.json"
    project_file.write_text(
        json.dumps({
            "name": "test",
            "milestones": [
                {"id": "ms-1", "title": "First", "status": "in_progress",
                 "entries": [], "progress": 0},
                {"id": "ms-2", "title": "Second", "status": "in_progress",
                 "entries": [], "progress": 0},
            ],
            "summary": "",
        }),
        encoding="utf-8",
    )
    if fork_target:
        (beacon_dir / "fork.json").write_text(
            json.dumps({"target_ms_id": fork_target, "child_branch": "x",
                        "parent_session_id": "sv-parent"}),
            encoding="utf-8",
        )
    return project_file


@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        project_file = _make_project(tmp_p)
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_OPERATION_AUTO_EXECUTE", raising=False)
        monkeypatch.delenv("BEACON_OPERATION_ENVELOPE_ID", raising=False)
        yield project_file


@pytest.fixture
def project_with_fork(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        project_file = _make_project(tmp_p, fork_target="ms-2")
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.chdir(tmp)
        monkeypatch.delenv("BEACON_OPERATION_AUTO_EXECUTE", raising=False)
        monkeypatch.delenv("BEACON_OPERATION_ENVELOPE_ID", raising=False)
        yield project_file


def test_read_fork_target_ms_id_returns_value_when_present(project_with_fork):
    assert cmd_log._read_fork_target_ms_id() == "ms-2"


def test_read_fork_target_ms_id_returns_empty_when_absent(project):
    assert cmd_log._read_fork_target_ms_id() == ""


def test_read_fork_target_ms_id_handles_malformed_json(project, monkeypatch):
    fork_path = Path(os.path.dirname(commands.get_project_file())) / "fork.json"
    fork_path.write_text("not valid json {{", encoding="utf-8")
    assert cmd_log._read_fork_target_ms_id() == ""


def test_read_fork_target_ms_id_handles_missing_target_field(project, monkeypatch):
    fork_path = Path(os.path.dirname(commands.get_project_file())) / "fork.json"
    fork_path.write_text(json.dumps({"child_branch": "x"}), encoding="utf-8")
    assert cmd_log._read_fork_target_ms_id() == ""


def test_cmd_log_prepare_uses_fork_target_when_no_ms_arg(project_with_fork,
                                                          monkeypatch):
    # No BEACON_MS_ID. With 2 active MSs but fork.json pinning ms-2,
    # the output should narrow down to ms-2 (not show candidates).
    monkeypatch.delenv("BEACON_MS_ID", raising=False)
    monkeypatch.setenv("BEACON_HASH", "abc1234")
    monkeypatch.setenv("BEACON_MESSAGE", "test")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_log_prepare()
    payload = json.loads(buf.getvalue())
    # Should be single milestone, not candidates.
    assert "milestone" in payload
    assert "candidates" not in payload
    assert payload["milestone"]["id"] == "ms-2"
    # And the source hint is surfaced.
    assert payload.get("fork_target_ms_id") == "ms-2"


def test_cmd_log_prepare_explicit_ms_overrides_fork(project_with_fork, monkeypatch):
    # Explicit --ms ms-1 wins over fork.json's ms-2 hint.
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_HASH", "abc1234")
    monkeypatch.setenv("BEACON_MESSAGE", "test")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_log_prepare()
    payload = json.loads(buf.getvalue())
    assert payload["milestone"]["id"] == "ms-1"
    # Hint not surfaced when explicit choice was made
    assert "fork_target_ms_id" not in payload


def test_cmd_log_prepare_falls_back_when_fork_target_unknown(project, monkeypatch):
    # fork.json points to ms-99 which is not in project.json → fall back.
    fork_path = Path(os.path.dirname(commands.get_project_file())) / "fork.json"
    fork_path.write_text(json.dumps({"target_ms_id": "ms-99"}), encoding="utf-8")
    monkeypatch.delenv("BEACON_MS_ID", raising=False)
    monkeypatch.setenv("BEACON_HASH", "abc1234")
    monkeypatch.setenv("BEACON_MESSAGE", "test")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_log_prepare()
    payload = json.loads(buf.getvalue())
    # 2 active MSs in project → candidates path
    assert "candidates" in payload
    assert {c["id"] for c in payload["candidates"]} == {"ms-1", "ms-2"}


def test_cmd_log_writes_under_fork_target_ms(project_with_fork, monkeypatch):
    """End-to-end: commit recorded via cmd_log lands on the fork target MS."""
    monkeypatch.delenv("BEACON_MS_ID", raising=False)
    monkeypatch.setenv("BEACON_HASH", "abcdef1")
    monkeypatch.setenv("BEACON_MESSAGE", "fork commit")
    monkeypatch.setenv("BEACON_DATE", "2026-06-15")
    monkeypatch.setenv("BEACON_JSON", "1")
    buf = io.StringIO()
    with redirect_stdout(buf):
        commands.cmd_log()
    data = commands.load_project()
    ms2 = next(m for m in data["milestones"] if m["id"] == "ms-2")
    ms1 = next(m for m in data["milestones"] if m["id"] == "ms-1")
    # Recorded under the fork target, not the other active MS.
    assert any(e.get("type") == "commit" for e in ms2["entries"])
    assert not any(e.get("type") == "commit" for e in ms1["entries"])
