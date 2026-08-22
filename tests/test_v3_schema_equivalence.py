"""Pin lib/v3_schema to the server's v3 reference implementation (ms-148 e-5411).

The SPEC names the server MySQL backend as the *sole reference* for the v3
item-level semantics. lib/v3_schema reimplements decompose/assemble locally (to
avoid a MySQL dependency in the CLI), so these tests guarantee the local copy
produces byte-for-byte the same split and round-trip as
server/mysql_client._v3_decompose / _v3_assemble. If they ever diverge, a local
project written by one and read by the other would corrupt silently.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import v3_schema

try:
    import mysql_client  # server reference
    HAVE_SERVER = True
except Exception:  # pragma: no cover - server deps unavailable in some envs
    HAVE_SERVER = False

server_only = pytest.mark.skipif(
    not HAVE_SERVER, reason="server/mysql_client not importable in this env")


PROJECT = {
    "name": "Fixture",
    "summary": "s",
    "profession": "dev",
    "milestones": [
        {
            "id": "ms-2", "title": "second", "status": "todo", "progress": 0,
            "entries": [
                {"id": "e-9", "type": "task", "description": "b",
                 "status": "todo", "created_at": "2026-01-02T00:00:00Z"},
            ],
        },
        {
            "id": "ms-1", "title": "first", "status": "in_progress",
            "progress": 50,
            "entries": [
                {"id": "e-2", "type": "task", "description": "a2",
                 "status": "done", "created_at": "2026-01-01T00:00:05Z"},
                {"id": "e-1", "type": "task", "description": "a1",
                 "status": "done", "created_at": "2026-01-01T00:00:00Z",
                 "entries": [
                     {"id": "e-100", "type": "commit", "description": "c",
                      "meta": {"hash": "abc"}},
                 ]},
            ],
        },
    ],
}


def test_decompose_round_trips_locally():
    meta, ms_map, entry_map = v3_schema.decompose(PROJECT)
    assert meta["schema_version"] == v3_schema.SCHEMA_V3_ENTRY
    assert "milestones" not in meta
    assert set(ms_map) == {"ms-1", "ms-2"}
    assert set(entry_map) == {"ms-1#e-1", "ms-1#e-2", "ms-2#e-9"}
    # child commit stays inline in its parent entry row (single-row write unit).
    assert entry_map["ms-1#e-1"]["entries"][0]["id"] == "e-100"

    ms_rows = list(ms_map.items())
    entry_rows = list(entry_map.items())
    rebuilt = v3_schema.assemble(meta, ms_rows, entry_rows)
    # milestones come back in numeric order, entries in created_at order.
    assert [m["id"] for m in rebuilt["milestones"]] == ["ms-1", "ms-2"]
    ms1 = rebuilt["milestones"][0]
    assert [e["id"] for e in ms1["entries"]] == ["e-1", "e-2"]
    # non-milestone meta preserved.
    assert rebuilt["name"] == "Fixture"
    assert rebuilt["profession"] == "dev"


def test_decompose_matches_frozen_server_snapshot():
    """No-dep parity guard. The @server_only tests below prove equivalence to
    the live server reference, but they SKIP without pymysql (absent from the
    standard CLI/CI env), so on their own the 'pinned to the server reference'
    claim is only checked where the server deps happen to be installed. This
    snapshot — captured from decompose() and verified equal to the server's
    _v3_decompose once — makes the structural pin run in every environment, so a
    drift in v3_schema is caught in plain CI, not only at a future local↔cloud
    boundary."""
    meta, ms_map, entry_map = v3_schema.decompose(PROJECT)
    assert meta == {
        "name": "Fixture", "summary": "s", "profession": "dev",
        "schema_version": 3,
    }
    assert ms_map == {
        "ms-1": {"id": "ms-1", "title": "first", "status": "in_progress",
                 "progress": 50},
        "ms-2": {"id": "ms-2", "title": "second", "status": "todo",
                 "progress": 0},
    }
    assert sorted(entry_map) == ["ms-1#e-1", "ms-1#e-2", "ms-2#e-9"]
    assert entry_map["ms-1#e-1"]["entries"] == [
        {"id": "e-100", "type": "commit", "description": "c",
         "meta": {"hash": "abc"}},
    ]


@server_only
def test_decompose_matches_server_reference():
    my_meta, my_ms, my_entry = v3_schema.decompose(PROJECT)
    sv_meta, sv_ms, sv_entry = mysql_client._v3_decompose(PROJECT)
    assert my_meta == sv_meta
    assert my_ms == sv_ms
    assert my_entry == sv_entry


@server_only
def test_assemble_matches_server_reference():
    _, ms_map, entry_map = v3_schema.decompose(PROJECT)
    ms_rows = list(ms_map.items())
    entry_rows = list(entry_map.items())
    meta = {"name": "Fixture", "summary": "s", "profession": "dev",
            "schema_version": 3}
    mine = v3_schema.assemble(meta, ms_rows, entry_rows)
    theirs = mysql_client._v3_assemble(meta, ms_rows, entry_rows)
    assert mine == theirs
