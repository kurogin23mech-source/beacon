"""Tests for the ms-79 / e-1817 source axis on commit entries.

Verifies that:

* ``core.log_commit`` persists ``meta.source`` when given.
* The default (= human dialog, no env var set) leaves ``meta.source``
  absent so existing entries continue to round-trip identically.
* ``commands._resolve_commit_source`` returns ``"auto-op"`` when the
  Operation auto-execute env vars are set, ``""`` otherwise.
* ``cmd_log`` / ``cmd_log_finalize`` propagate the env-detected source
  down to the persisted commit entry.

The source tag powers the retro_query source breakdown facet so
``/beacon-retrospect`` and ``/beacon-retro`` can distinguish AI auto-op
commits from human dialog commits.
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


@pytest.fixture
def project(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({
                "name": "test",
                "milestones": [
                    {
                        "id": "ms-1",
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
        # Make sure source env vars are not leaking in from the host shell.
        monkeypatch.delenv("BEACON_OPERATION_AUTO_EXECUTE", raising=False)
        monkeypatch.delenv("BEACON_OPERATION_ENVELOPE_ID", raising=False)
        yield project_file


def test_log_commit_default_source_is_absent(project):
    data = commands.load_project()
    result = core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234",
        message="default human commit", date="2026-06-15",
    )
    assert result["status"] == "logged"
    ms = data["milestones"][0]
    entry = ms["entries"][0]
    # No source key → human / untagged. Existing UIs that don't know about
    # the field continue working unchanged.
    assert "source" not in entry.get("meta", {})


def test_log_commit_persists_auto_op_source(project):
    data = commands.load_project()
    result = core.log_commit(
        data, ms_id="ms-1", commit_hash="def5678",
        message="auto-op commit", date="2026-06-15",
        source="auto-op",
    )
    assert result["status"] == "logged"
    ms = data["milestones"][0]
    entry = ms["entries"][0]
    assert entry["meta"]["source"] == "auto-op"


def test_resolve_commit_source_default_empty(monkeypatch):
    monkeypatch.delenv("BEACON_OPERATION_AUTO_EXECUTE", raising=False)
    monkeypatch.delenv("BEACON_OPERATION_ENVELOPE_ID", raising=False)
    assert commands._resolve_commit_source() == ""


def test_resolve_commit_source_from_auto_execute_flag(monkeypatch):
    monkeypatch.setenv("BEACON_OPERATION_AUTO_EXECUTE", "1")
    monkeypatch.delenv("BEACON_OPERATION_ENVELOPE_ID", raising=False)
    assert commands._resolve_commit_source() == "auto-op"


def test_resolve_commit_source_from_envelope_id(monkeypatch):
    monkeypatch.delenv("BEACON_OPERATION_AUTO_EXECUTE", raising=False)
    monkeypatch.setenv("BEACON_OPERATION_ENVELOPE_ID", "env-abc-123")
    assert commands._resolve_commit_source() == "auto-op"


def test_cmd_log_propagates_auto_op_source(project, monkeypatch):
    monkeypatch.setenv("BEACON_OPERATION_AUTO_EXECUTE", "1")
    monkeypatch.setenv("BEACON_HASH", "xyz9999")
    monkeypatch.setenv("BEACON_MESSAGE", "auto-op via cmd_log")
    monkeypatch.setenv("BEACON_DATE", "2026-06-15")
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_JSON", "1")
    commands.cmd_log()
    data = commands.load_project()
    ms = data["milestones"][0]
    entry = ms["entries"][0]
    assert entry["meta"]["source"] == "auto-op"


def test_retro_query_source_breakdown_after_log(project):
    """End-to-end: human + auto-op commits → source facet shows both."""
    import retro_query  # noqa: PLC0415

    data = commands.load_project()
    core.log_commit(data, ms_id="ms-1", commit_hash="h1",
                    message="human", date="2026-06-15")
    core.log_commit(data, ms_id="ms-1", commit_hash="h2",
                    message="auto", date="2026-06-15", source="auto-op")

    result = retro_query.retro_query(data)
    facet = result["facets"]["source"]
    assert facet.get("human") == 1
    assert facet.get("auto-op") == 1
