"""Tests for JSON→SQLite migration + verification (ms-148 e-5413).

The migration must move a local project.json into the SQLite store AND prove the
move was faithful (受入条件6). These tests cover a clean round trip, the
order-insensitivity that a naive positional compare would get wrong, and that a
real content loss is actually reported (the verifier must not rubber-stamp).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import store_migrate
from store_sqlite import SqliteStore


PROJECT = {
    "name": "Fixture", "summary": "s", "profession": "dev",
    "milestones": [
        {"id": "ms-2", "title": "second", "status": "todo", "progress": 0,
         "entries": [
             {"id": "e-9", "type": "task", "description": "b", "status": "todo",
              "created_at": "2026-01-02T00:00:00Z"},
         ]},
        {"id": "ms-1", "title": "first", "status": "in_progress", "progress": 50,
         "entries": [
             {"id": "e-2", "type": "task", "description": "a2", "status": "done",
              "created_at": "2026-01-01T00:00:05Z"},
             {"id": "e-1", "type": "task", "description": "a1", "status": "done",
              "created_at": "2026-01-01T00:00:00Z", "entries": [
                  {"id": "e-100", "type": "commit", "description": "c",
                   "meta": {"hash": "abc"}},
              ]},
         ]},
    ],
}


def _write_json(tmp_path):
    p = tmp_path / "project.json"
    p.write_text(json.dumps(PROJECT), encoding="utf-8")
    return p


def test_migration_round_trips_and_verifies(tmp_path):
    src = _write_json(tmp_path)
    db = tmp_path / "project.db"
    report = store_migrate.migrate_json_to_sqlite(str(src), str(db))
    assert report["verified"] is True
    assert report["verification"]["issues"] == []
    assert report["verification"]["milestone_count"] == 2
    # 3 top-level entries + 1 inline child = 4 counted recursively.
    assert report["verification"]["entry_count"] == 4

    # The SQLite store now holds the same content (order may differ).
    restored = SqliteStore(str(db)).load_project()
    assert {m["id"] for m in restored["milestones"]} == {"ms-1", "ms-2"}
    ms1 = next(m for m in restored["milestones"] if m["id"] == "ms-1")
    assert {e["id"] for e in ms1["entries"]} == {"e-1", "e-2"}
    child = next(e for e in ms1["entries"] if e["id"] == "e-1")["entries"][0]
    assert child["id"] == "e-100"


def test_verify_is_order_insensitive():
    """The store re-sorts milestones and entries; the verifier must not flag
    that reordering as a content change."""
    reordered = {
        "name": "Fixture", "summary": "s", "profession": "dev",
        # schema_version is stamped at top-level meta by the store; the verifier
        # must ignore it (not report it as a meta change).
        "schema_version": 3,
        "milestones": [
            # reversed milestone order, reversed entry order.
            {"id": "ms-1", "title": "first", "status": "in_progress",
             "progress": 50, "entries": [
                 {"id": "e-1", "type": "task", "description": "a1",
                  "status": "done", "created_at": "2026-01-01T00:00:00Z",
                  "entries": [{"id": "e-100", "type": "commit",
                               "description": "c", "meta": {"hash": "abc"}}]},
                 {"id": "e-2", "type": "task", "description": "a2",
                  "status": "done", "created_at": "2026-01-01T00:00:05Z"},
             ]},
            {"id": "ms-2", "title": "second", "status": "todo", "progress": 0,
             "entries": [
                 {"id": "e-9", "type": "task", "description": "b",
                  "status": "todo", "created_at": "2026-01-02T00:00:00Z"}]},
        ],
    }
    result = store_migrate.verify_migration(PROJECT, reordered)
    assert result["match"] is True, result["issues"]


def test_verify_detects_lost_entry():
    """A genuinely lost entry MUST be reported — the verifier is a real check,
    not a rubber stamp."""
    lossy = json.loads(json.dumps(PROJECT))
    # drop e-2 from ms-1
    ms1 = next(m for m in lossy["milestones"] if m["id"] == "ms-1")
    ms1["entries"] = [e for e in ms1["entries"] if e["id"] != "e-2"]
    result = store_migrate.verify_migration(PROJECT, lossy)
    assert result["match"] is False
    assert any("e-2" in issue for issue in result["issues"])


def test_verify_detects_changed_field():
    changed = json.loads(json.dumps(PROJECT))
    changed["milestones"][0]["title"] = "MUTATED"
    result = store_migrate.verify_migration(PROJECT, changed)
    assert result["match"] is False
    assert any("ms-2" in issue for issue in result["issues"])


def test_verify_detects_changed_meta():
    changed = json.loads(json.dumps(PROJECT))
    changed["summary"] = "different"
    result = store_migrate.verify_migration(PROJECT, changed)
    assert result["match"] is False
    assert any("summary" in issue for issue in result["issues"])
