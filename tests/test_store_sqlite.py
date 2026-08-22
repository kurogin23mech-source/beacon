"""Tests for the SQLite local store (ms-148 e-5411).

Pins the three properties the SPEC's 受入条件 1/2/3 demand of the local store:
no lost update under concurrency, no id collision, and crash safety — plus the
v3 round-trip so a document survives decompose→store→assemble unchanged.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from store_sqlite import SqliteStore
from store_api import ConflictError


def _new_store(tmp_path):
    db = tmp_path / "project.db"
    SqliteStore._save_baseline.pop(os.path.abspath(str(db)), None)
    s = SqliteStore(str(db))
    # Seed a minimal valid project.
    s.apply(lambda _c: ({"name": "p", "summary": "", "milestones": []}, None),
            validate=True)
    return s


# --- v3 round-trip ----------------------------------------------------------

def test_roundtrip_preserves_milestones_and_entry_order(tmp_path):
    s = _new_store(tmp_path)

    def build(d):
        d["milestones"] = [
            {"id": "ms-1", "title": "first", "status": "in_progress",
             "progress": 0, "entries": [
                 {"id": "e-2", "type": "task", "description": "b",
                  "status": "todo", "created_at": "2026-01-01T00:00:05Z"},
                 {"id": "e-1", "type": "task", "description": "a",
                  "status": "todo", "created_at": "2026-01-01T00:00:00Z"},
             ]},
        ]
        return d, None

    s.apply(build, validate=True)
    got = s.load_project()
    assert [m["id"] for m in got["milestones"]] == ["ms-1"]
    # entries reordered by created_at on assemble.
    assert [e["id"] for e in got["milestones"][0]["entries"]] == ["e-1", "e-2"]
    assert got["name"] == "p"


def test_child_entry_stays_inline(tmp_path):
    s = _new_store(tmp_path)
    s.apply(lambda d: ({**d, "milestones": [
        {"id": "ms-1", "title": "m", "status": "todo", "entries": [
            {"id": "e-1", "type": "task", "description": "t", "status": "done",
             "entries": [{"id": "e-9", "type": "commit", "description": "c"}]},
        ]},
    ]}, None), validate=True)
    got = s.load_project()
    task = got["milestones"][0]["entries"][0]
    assert task["entries"][0]["id"] == "e-9"


# --- validation + crash safety ---------------------------------------------

def test_apply_validation_failure_writes_nothing(tmp_path):
    s = _new_store(tmp_path)
    # An invalid milestone id must be rejected and leave the store untouched.
    with pytest.raises(ValueError):
        s.apply(lambda d: ({**d, "milestones": [{"id": "BAD"}]}, None),
                validate=True)
    got = s.load_project()
    assert got["milestones"] == []


def test_apply_op_exception_rolls_back(tmp_path):
    s = _new_store(tmp_path)
    s.apply(lambda d: ({**d, "summary": "before"}, None), validate=True)

    def boom(d):
        raise RuntimeError("op failed mid-transaction")

    with pytest.raises(RuntimeError):
        s.apply(boom)
    # The prior committed state survives; the failed op left nothing behind.
    assert s.load_project()["summary"] == "before"


# --- lost-update detection on the whole-doc save path -----------------------

def test_save_project_detects_concurrent_change(tmp_path):
    s = _new_store(tmp_path)
    data = s.load_project()
    # In real usage each process has its own class-level baseline. Within one
    # test process the concurrent apply() below would clobber our shared slot, so
    # snapshot our load-time baseline and restore it afterwards to model that our
    # process still holds v0 while the db has moved on.
    key = os.path.abspath(s.db_path)
    our_baseline = SqliteStore._save_baseline[key]

    other = SqliteStore(s.db_path)
    other.apply(lambda d: ({**d, "summary": "by-other"}, None), validate=True)
    SqliteStore._save_baseline[key] = our_baseline

    # Our stale whole-doc write must be refused, not silently applied.
    data["summary"] = "mine"
    with pytest.raises(ConflictError):
        s.save_project(data)
    assert s.load_project()["summary"] == "by-other"


# --- concurrency: no lost update, no id collision ---------------------------

def test_concurrent_apply_no_lost_updates(tmp_path):
    """N threads x M increments via apply() must all land: BEGIN IMMEDIATE
    serialises the read-modify-write so none is lost (受入条件1). Each thread
    opens its own connection, contending at the DB level like separate
    processes."""
    s = _new_store(tmp_path)
    s.apply(lambda d: ({**d, "counter": 0}, None), validate=True)

    N, M = 8, 20

    def worker():
        w = SqliteStore(s.db_path)
        for _ in range(M):
            w.apply(lambda d: ({**d, "counter": d.get("counter", 0) + 1}, None),
                    validate=True)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert s.load_project()["counter"] == N * M


def test_concurrent_id_allocation_no_collision(tmp_path):
    """Allocating an id inside the transaction (max+1) cannot collide, because
    each apply sees the committed result of the previous one (受入条件2)."""
    s = _new_store(tmp_path)
    s.apply(lambda d: ({**d, "milestones": [
        {"id": "ms-1", "title": "m", "status": "in_progress", "entries": []},
    ]}, None), validate=True)

    N, M = 6, 15

    def add_entry(d):
        ms = d["milestones"][0]
        existing = [int(e["id"].split("-")[1]) for e in ms["entries"]]
        nxt = (max(existing) + 1) if existing else 1
        ms["entries"].append({"id": f"e-{nxt}", "type": "task",
                              "description": "x", "status": "todo"})
        return d, None

    def worker():
        w = SqliteStore(s.db_path)
        for _ in range(M):
            w.apply(add_entry, validate=True)

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = [e["id"] for e in s.load_project()["milestones"][0]["entries"]]
    assert len(ids) == N * M
    assert len(set(ids)) == N * M, f"id collision: {len(ids)-len(set(ids))} dups"
