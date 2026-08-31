"""Unit tests for `beacon project dump` (ms-160 e-5816).

The crux of the SQLite↔Tauri read-path fix: in local mode the SQLite store is
the source of truth and ``.beacon/project.json`` is only a best-effort mirror
refreshed after each commit. When that mirror write is swallowed (permissions,
full disk, a race) the file goes stale while SQLite holds the current state.

``project dump`` must read the source of truth — so the Tauri desktop, which
now shells out to this verb instead of reading the mirror file, can never be
driven by stale JSON. These tests pin exactly that: dump emits the SQLite
content even when the mirror on disk is deliberately corrupted/stale.
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
def local_project(monkeypatch):
    """A local SQLite-backed project with a fresh .beacon/ tree."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        project = {
            "name": "demo",
            "summary": "initial",
            "milestones": [
                {"id": "ms-1", "title": "first", "status": "in_progress",
                 "progress": 0, "entries": []},
            ],
            "operations": [],
        }
        (beacon_dir / "project.json").write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.delenv("BEACON_LOCAL_BACKEND", raising=False)  # default sqlite
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        monkeypatch.delitem(sys.modules, "firestore_client", raising=False)
        # Drop the load-time baseline so a reused db path in the same process
        # (across tests) doesn't trip the whole-doc save conflict guard.
        from store_sqlite import SqliteStore, sqlite_db_path_for
        SqliteStore._save_baseline.pop(
            os.path.abspath(sqlite_db_path_for(str(beacon_dir / "project.json"))),
            None,
        )
        yield beacon_dir


def _run_dump(monkeypatch, capsys) -> dict:
    """Invoke the dispatch handler fresh and return the parsed JSON it prints."""
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands
    commands.cmd_project_dump()
    out = capsys.readouterr().out
    return json.loads(out)


def test_dump_emits_full_project_as_json(local_project, monkeypatch, capsys):
    dumped = _run_dump(monkeypatch, capsys)
    assert dumped["name"] == "demo"
    assert [m["id"] for m in dumped["milestones"]] == ["ms-1"]


def test_dump_reads_sqlite_source_of_truth_not_stale_mirror(
    local_project, monkeypatch, capsys
):
    beacon_dir = local_project
    project_file = beacon_dir / "project.json"

    # First store use migrates json -> sqlite. Apply a mutation so the DB
    # (source of truth) now differs from whatever ends up on disk.
    sys.path.insert(0, _LIB)
    from commands_shared import get_store

    def _add_ms(cur):
        cur = dict(cur)
        cur["milestones"] = list(cur.get("milestones", [])) + [
            {"id": "ms-2", "title": "second", "status": "todo",
             "progress": 0, "entries": []},
        ]
        return cur, None

    get_store().apply(_add_ms, validate=True)

    # Now deliberately STALE the mirror: overwrite project.json with the old,
    # pre-mutation content (simulates a swallowed mirror-write failure).
    stale = {
        "name": "STALE-should-not-be-read",
        "summary": "stale",
        "milestones": [
            {"id": "ms-1", "title": "first", "status": "in_progress",
             "progress": 0, "entries": []},
        ],
        "operations": [],
    }
    project_file.write_text(
        json.dumps(stale, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    dumped = _run_dump(monkeypatch, capsys)

    # dump must reflect SQLite (has ms-2, correct name), NOT the stale mirror.
    assert dumped["name"] == "demo"
    assert [m["id"] for m in dumped["milestones"]] == ["ms-1", "ms-2"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
