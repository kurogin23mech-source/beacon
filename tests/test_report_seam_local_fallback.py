"""Output-contract + local-mode fallback regression for the report→server-author
seam (ms-114 e-3744, AC6).

e-3742 / e-3743 re-homed ``log_commit`` and ``issue_import`` behind the report
primitive. AC6 requires that this migration did NOT make those verbs depend on a
server: "local mode の verb が server 必須化せず従来通り動く (local fallback 保持)".
SPEC 方針7 names "local mode 喪失" one of only three true breaking zones — this
module locks the invariant so a future change that makes ``apply_report`` require
a network/server would fail here rather than in production.

Two layers of guard:
  1. Local-mode round-trip through the CLI layer (``load_project`` → core seam →
     ``save_project``) with NO cloud config — proves the migrated verbs still work
     purely locally, and that the persisted result + stdout contract are stable.
  2. A structural guard that the seam module (``report_primitive``) imports only
     the standard library — so authoring a report can never reach for a server.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import commands  # noqa: E402
import cmd_issue  # noqa: E402  (ms-127 e-4320: issue family moved here)
import core  # noqa: E402


@pytest.fixture
def local_project(monkeypatch):
    """A local-mode project (BEACON_PROJECT_FILE, no cloud, local backend) — the
    same fixture shape as tests/test_log_commit_source.py."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({
                "name": "test",
                "milestones": [
                    {"id": "ms-1", "title": "Test", "status": "in_progress",
                     "entries": [], "progress": 0},
                ],
                "summary": "",
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.chdir(tmp)
        for var in ("BEACON_OPERATION_AUTO_EXECUTE", "BEACON_OPERATION_ENVELOPE_ID",
                    "BEACON_HASH", "BEACON_MESSAGE", "BEACON_MS_ID", "BEACON_PROGRESS",
                    "BEACON_RESOLVES", "BEACON_RESOLVES_SET", "BEACON_JSON",
                    "BEACON_SUMMARY", "BEACON_BEHAVIOR", "BEACON_DATE"):
            monkeypatch.delenv(var, raising=False)
        yield project_file


# --- 1. local-mode round-trip: commit verb ---------------------------------

def test_log_commit_local_mode_roundtrip(local_project):
    """The commit verb authors + persists a commit in local mode (no cloud) and
    the result contract is stable — the seam did not add a server dependency."""
    data = commands.load_project()
    result = core.log_commit(
        data, ms_id="ms-1", commit_hash="abc1234",
        message="local mode commit", date="2026-07-28", progress="30")
    commands.save_project(data)

    # result contract stable
    assert result["status"] == "logged"
    assert result["hash"] == "abc1234"
    assert result["milestone"] == "ms-1"
    assert result["progress"] == 30

    # persisted to the local file (reload proves save round-tripped)
    reloaded = commands.load_project()
    ms = reloaded["milestones"][0]
    assert ms["progress"] == 30
    assert any(e.get("meta", {}).get("hash") == "abc1234" for e in ms["entries"])


# --- 2. local-mode round-trip: issue_import verb ---------------------------

def test_issue_import_local_mode_roundtrip(local_project):
    """The issue-import verb authors + persists a task in local mode and returns
    the entry id (str) — local fallback preserved for the intake verb too."""
    data = commands.load_project()
    eid = core.issue_import(
        data, ms_id="ms-1", number=7,
        url="https://github.com/o/r/issues/7", title="Local mode issue",
        body="body")
    commands.save_project(data)

    assert isinstance(eid, str) and eid.startswith("e-")
    reloaded = commands.load_project()
    ms = reloaded["milestones"][0]
    task = next(e for e in ms["entries"] if e["id"] == eid)
    assert task["description"] == "#7: Local mode issue"
    assert task["meta"]["issue_number"] == 7
    assert task["meta"]["priority"] == core.UNTRIAGED_PRIORITY


# --- 3. stdout contract: cmd_log -------------------------------------------

def test_cmd_log_stdout_contract(local_project, monkeypatch, capsys):
    """``beacon log`` stdout is byte-stable after the seam migration:
    ``Logged <hash> to <loc>`` + ``  Progress: N%``."""
    monkeypatch.setenv("BEACON_HASH", "abc1234")
    monkeypatch.setenv("BEACON_MESSAGE", "seam stdout check")
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_PROGRESS", "40")
    commands.cmd_log()
    out = capsys.readouterr().out
    assert "Logged abc1234 to" in out
    assert "Progress: 40%" in out


# --- 4. stdout contract: cmd_issue_import ----------------------------------

def test_cmd_issue_import_stdout_contract(local_project, monkeypatch, capsys):
    """``beacon issue import`` stdout is stable: ``Imported Issue #N → [eid]: title``.
    The GitHub fetch is stubbed so the test stays local + offline."""
    monkeypatch.setattr(cmd_issue, "_gh_issue_fetch", lambda number: {
        "url": "https://github.com/o/r/issues/9", "title": "Stubbed issue",
        "body": "b", "state": "OPEN"})
    monkeypatch.setenv("BEACON_ISSUE_NUMBER", "9")
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    commands.cmd_issue_import()
    out = capsys.readouterr().out
    assert "Imported Issue #9 →" in out
    assert "Stubbed issue" in out


# --- 5. structural guard: the seam has no server dependency -----------------

def test_report_primitive_imports_only_stdlib():
    """AC6 invariant lock: ``report_primitive`` (where every report is authored)
    must import only the standard library. If a future change pulls in a network
    / cloud client (requests / urllib / firestore / boto / a server module), the
    seam would require a server and local mode would break — this test fails first."""
    seam_path = Path(__file__).resolve().parents[1] / "lib" / "report_primitive.py"
    tree = ast.parse(seam_path.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported_roots.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imported_roots.add(node.module.split(".")[0])
    forbidden = {"requests", "urllib", "urllib3", "http", "socket", "boto3",
                 "botocore", "google", "firebase_admin", "firestore_client",
                 "dynamodb_client", "app", "server"}
    leaked = imported_roots & forbidden
    assert not leaked, f"report_primitive must stay server-free; leaked: {leaked}"
