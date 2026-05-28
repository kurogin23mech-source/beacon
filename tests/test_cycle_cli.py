"""Integration tests for `beacon cycle status` CLI handler.

Covers cmd_cycle_status against an on-disk local project so that the
JSON contract Skills rely on (`{cycle: {active, last_action_date}}`) is
locked in. The pure-function layer is exercised by tests/test_cycle.py;
this file verifies the command-layer plumbing (env vars, doc directory
scanning, JSON output).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add lib/ to import path the same way test_operations.py does.
_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def local_project_dir(monkeypatch):
    """Set up a tmp directory containing .beacon/project.json and documents/.

    Yields the project directory path; cleanup is automatic.
    """
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        docs_dir = beacon_dir / "documents"
        docs_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "test", "milestones": [], "summary": ""}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        # Ensure we don't accidentally hit cloud mode in the test.
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        # Prevent the operations-layer mock backend from leaking from other test files.
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _run_cycle_status(capsys, monkeypatch, json_mode=True) -> dict:
    """Invoke cmd_cycle_status the way bin/beacon does and parse JSON output."""
    monkeypatch.setenv("BEACON_JSON", "1" if json_mode else "")
    # Re-import commands to pick up env mutations cleanly. cmd_cycle_status
    # only depends on env at call time, so we can call directly.
    import commands  # type: ignore
    commands.cmd_cycle_status()
    out = capsys.readouterr().out.strip()
    return json.loads(out)


def test_empty_project_reports_all_inactive(local_project_dir, monkeypatch, capsys):
    """A pristine project: every cycle off, every last_action_date null."""
    snap = _run_cycle_status(capsys, monkeypatch)
    for c in ("push", "deploy", "retro", "operation", "release"):
        assert snap[c] == {"active": False, "last_action_date": None}


def test_push_record_activates_push_cycle(local_project_dir, monkeypatch, capsys):
    """Writing a push record into project.json must surface in cycle status."""
    project_file = local_project_dir / ".beacon" / "project.json"
    data = json.loads(project_file.read_text(encoding="utf-8"))
    data["pushes"] = [
        {"id": "p-1", "branch": "main", "pushed_at": "2026-05-15T10:00:00Z",
         "from_hash": "a" * 7, "to_hash": "b" * 7, "commit_count": 1}
    ]
    project_file.write_text(json.dumps(data), encoding="utf-8")

    snap = _run_cycle_status(capsys, monkeypatch)
    assert snap["push"]["active"] is True
    assert snap["push"]["last_action_date"] == "2026-05-15T10:00:00Z"
    # Other cycles remain inactive — push doesn't bleed into deploy.
    assert snap["deploy"]["active"] is False


def test_retro_doc_in_documents_dir_activates_retro(local_project_dir, monkeypatch, capsys):
    """A scope=retro markdown file under .beacon/documents/ must flip retro on.

    This validates the doc-directory scanning path inside cmd_cycle_status —
    distinct from the pure-function test that injects documents directly.
    """
    docs_dir = local_project_dir / ".beacon" / "documents"
    (docs_dir / "retro-2026-W18.md").write_text(
        "---\nscope: retro\n---\n# Weekly Retro\n\nNotes\n",
        encoding="utf-8",
    )

    snap = _run_cycle_status(capsys, monkeypatch)
    assert snap["retro"]["active"] is True


def test_open_operation_activates_operation_cycle(local_project_dir, monkeypatch, capsys):
    """An Operation with status=open must activate the operation cycle."""
    project_file = local_project_dir / ".beacon" / "project.json"
    data = json.loads(project_file.read_text(encoding="utf-8"))
    data["operations"] = [{
        "id": "op-1",
        "title": "Health monitor",
        "status": "open",
        "opened_at": "2026-05-10T00:00:00Z",
        "schedule": {"frequency": "daily", "days": ["mon", "tue", "wed", "thu", "fri"]},
        "entries": [],
    }]
    project_file.write_text(json.dumps(data), encoding="utf-8")

    snap = _run_cycle_status(capsys, monkeypatch)
    assert snap["operation"]["active"] is True
    assert snap["operation"]["last_action_date"] == "2026-05-10T00:00:00Z"


def test_version_rules_doc_activates_release(local_project_dir, monkeypatch, capsys):
    """A CORE doc with doc_id == 'version-rules' opts in to the release cycle."""
    docs_dir = local_project_dir / ".beacon" / "documents"
    (docs_dir / "version-rules.md").write_text(
        "---\nscope: core\n---\n# Version Rules\n\n- push axis enabled\n",
        encoding="utf-8",
    )

    snap = _run_cycle_status(capsys, monkeypatch)
    assert snap["release"]["active"] is True
