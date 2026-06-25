"""Integration tests for `beacon milestone purge` CLI handler (Issue #14).

Exercises cmd_milestone_purge against an on-disk local project to verify:
  - duplicate-record purge with --index works
  - purge writes a changelog entry
  - purge without --reason exits non-zero
  - missing-ID exits non-zero
  - purge of a single record cleans the project, after which strict load
    (load_project) succeeds again
  - purge from a 3-dup corruption leaves residual dirt and warns
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
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps({"name": "test", "milestones": []}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        # ms-95 e-2441: monkeypatch.delitem auto-restores. Raw pop leaks the
        # deletion across the test boundary, wiping adjacent test_api mocks
        # (8 fails on CI).
        monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
        yield Path(tmp)


def _write_project(project_dir: Path, data: dict) -> None:
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def _read_project(project_dir: Path) -> dict:
    return json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8")
    )


def _read_changelog(project_dir: Path) -> list[dict]:
    path = project_dir / ".beacon" / "changelog.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_purge_requires_reason(project_dir, monkeypatch, capsys):
    _write_project(project_dir, {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "A", "status": "todo",
                        "target_date": "", "commits": [], "entries": []}],
    })
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_REASON", "")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "--reason" in err


def test_purge_requires_ms_id(project_dir, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "")
    monkeypatch.setenv("BEACON_REASON", "x")
    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1


def test_purge_missing_ms_id_exits(project_dir, monkeypatch, capsys):
    _write_project(project_dir, {
        "name": "t",
        "milestones": [{"id": "ms-1", "title": "A", "status": "todo",
                        "target_date": "", "commits": [], "entries": []}],
    })
    monkeypatch.setenv("BEACON_MS_ID", "ms-99")
    monkeypatch.setenv("BEACON_REASON", "missing")
    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower()


def test_purge_duplicate_without_index_prints_options(project_dir, monkeypatch, capsys):
    _write_project(project_dir, {
        "name": "t",
        "milestones": [
            {"id": "ms-13", "title": "Aポ獲得", "status": "in_progress",
             "target_date": "", "commits": [], "entries": []},
            {"id": "ms-13", "title": "PE警告レビューUI", "status": "in_progress",
             "target_date": "", "commits": [], "entries": []},
        ],
    })
    monkeypatch.setenv("BEACON_MS_ID", "ms-13")
    monkeypatch.setenv("BEACON_REASON", "dup")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_purge()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "ms-13" in err
    assert "--index 1" in err
    assert "--index 2" in err
    # Both candidate titles must appear so the operator can pick.
    assert "Aポ獲得" in err
    assert "PE警告レビューUI" in err


def test_purge_duplicate_with_index_removes_correct_record(project_dir, monkeypatch, capsys):
    _write_project(project_dir, {
        "name": "t",
        "milestones": [
            {"id": "ms-13", "title": "Aポ獲得", "status": "in_progress",
             "target_date": "", "commits": [], "entries": []},
            {"id": "ms-13", "title": "PE警告レビューUI", "status": "in_progress",
             "target_date": "", "commits": [], "entries": []},
        ],
    })
    monkeypatch.setenv("BEACON_MS_ID", "ms-13")
    monkeypatch.setenv("BEACON_REASON", "dup recovery: keep アポ獲得, drop PE警告")
    monkeypatch.setenv("BEACON_INDEX", "2")

    import commands  # type: ignore
    commands.cmd_milestone_purge()  # must not raise

    data = _read_project(project_dir)
    remaining = [m for m in data["milestones"] if m["id"] == "ms-13"]
    assert len(remaining) == 1
    assert remaining[0]["title"] == "Aポ獲得"

    # Changelog must record the purge with reason and index.
    cl = _read_changelog(project_dir)
    purge_entries = [e for e in cl if e.get("op") == "milestone_purge"]
    assert len(purge_entries) == 1
    p = purge_entries[0]
    assert p["ms_id"] == "ms-13"
    assert p["index"] == 2
    assert "dup recovery" in p["reason"]


def test_purge_residual_duplicates_still_dirty(project_dir, monkeypatch, capsys):
    # 3-dup corruption: one purge removes 1, 2 remain → save via unsafe
    # path, warn user, continue exit 0.
    _write_project(project_dir, {
        "name": "t",
        "milestones": [
            {"id": "ms-7", "title": "A", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
            {"id": "ms-7", "title": "B", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
            {"id": "ms-7", "title": "C", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
        ],
    })
    monkeypatch.setenv("BEACON_MS_ID", "ms-7")
    monkeypatch.setenv("BEACON_REASON", "removing one of three dups")
    monkeypatch.setenv("BEACON_INDEX", "1")

    import commands  # type: ignore
    commands.cmd_milestone_purge()  # must not raise

    out = capsys.readouterr().out
    assert "Purged" in out
    assert "residual duplicates remain" in out

    data = _read_project(project_dir)
    assert sum(1 for m in data["milestones"] if m["id"] == "ms-7") == 2


def test_purge_single_record_cleans_project(project_dir, monkeypatch, capsys):
    # After purging the only record, normal load_project (strict validation)
    # must succeed.
    _write_project(project_dir, {
        "name": "t",
        "milestones": [
            {"id": "ms-1", "title": "Keep", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
            {"id": "ms-2", "title": "Drop", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
        ],
    })
    monkeypatch.setenv("BEACON_MS_ID", "ms-2")
    monkeypatch.setenv("BEACON_REASON", "scope out")
    monkeypatch.delenv("BEACON_INDEX", raising=False)

    import commands  # type: ignore
    commands.cmd_milestone_purge()

    # strict load now works.
    data = commands.load_project()
    assert [m["id"] for m in data["milestones"]] == ["ms-1"]
