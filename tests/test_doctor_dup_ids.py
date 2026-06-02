"""Tests for `beacon doctor` duplicate-ID detection (Issue #14).

Verifies that doctor surfaces a WARN [dup-id] entry when project.json
contains duplicate milestone / entry / operation IDs, and stays silent
when the project is clean.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def project_with(monkeypatch):
    """Yield a setter that writes project.json into a tmp dir for doctor to read.

    Important: doctor cd-walks the *current working directory* for .beacon/
    so we monkeypatch chdir into the tmpdir.
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    beacon_dir = root / ".beacon"
    beacon_dir.mkdir()
    project_file = beacon_dir / "project.json"
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
    monkeypatch.delenv("BEACON_CLOUD", raising=False)
    monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
    sys.modules.pop("firestore_client", None)

    original_cwd = os.getcwd()
    os.chdir(root)

    def _write(data):
        project_file.write_text(json.dumps(data), encoding="utf-8")

    try:
        yield _write
    finally:
        os.chdir(original_cwd)
        tmp.cleanup()


def _run_doctor(capsys) -> tuple[int, str]:
    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_doctor()
    return ei.value.code, capsys.readouterr().out


def test_doctor_warns_on_duplicate_milestone_id(project_with, capsys):
    project_with({
        "name": "t",
        "milestones": [
            {"id": "ms-13", "title": "A", "status": "in_progress",
             "target_date": "", "commits": [], "entries": []},
            {"id": "ms-13", "title": "B", "status": "in_progress",
             "target_date": "", "commits": [], "entries": []},
        ],
    })
    code, out = _run_doctor(capsys)
    assert code == 1
    assert "WARN [dup-id]" in out
    assert "ms-13" in out
    assert "appears 2 times" in out
    assert "milestone purge" in out


def test_doctor_warns_on_duplicate_entry_id(project_with, capsys):
    project_with({
        "name": "t",
        "milestones": [
            {"id": "ms-1", "title": "A", "status": "todo",
             "target_date": "", "commits": [], "entries": [
                {"id": "e-1", "type": "task", "description": "x", "status": "todo"},
             ]},
            {"id": "ms-2", "title": "B", "status": "todo",
             "target_date": "", "commits": [], "entries": [
                {"id": "e-1", "type": "task", "description": "y", "status": "todo"},
             ]},
        ],
    })
    code, out = _run_doctor(capsys)
    assert code == 1
    assert "WARN [dup-id]" in out
    assert "Duplicate entry ID 'e-1'" in out


def test_doctor_clean_project_no_dup_warning(project_with, capsys):
    project_with({
        "name": "t",
        "milestones": [
            {"id": "ms-1", "title": "A", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
        ],
    })
    # doctor will still warn about hooks/skills/auth — but never about dup-id.
    _, out = _run_doctor(capsys)
    assert "WARN [dup-id]" not in out
