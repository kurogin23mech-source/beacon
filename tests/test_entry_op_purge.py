"""Tests for entry / operation purge — duplicate-ID recovery (e-863).

The entry-level and operation-level analogues of milestone_purge (Issue #14).
validate_project rejects loading a project with duplicate IDs of ANY kind, so
the hard-delete recovery path must be symmetric — milestone-only recovery
would re-create Issue #14 one level down (entries / operations).

Covers:
  1. core.find_entries / find_operations (lookup, nested, duplicates)
  2. core.entry_purge / operation_purge (single, dup+index, errors, audit meta)
  3. validate_project blocks dup entry/op, purge unblocks (recovery flow)
  4. CLI cmd_entry_purge / cmd_operation_purge (reason required, dup listing,
     index correctness, changelog, residual-dirty, strict-load after cleanup)
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

import core  # noqa: E402


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _entry(eid, etype="task", desc=None, status="todo", children=None):
    e = {"id": eid, "type": etype, "description": desc or f"entry {eid}", "status": status}
    if children:
        e["entries"] = children
    return e


def _ms(ms_id, status="todo", entries=None):
    return {
        "id": ms_id, "title": f"MS {ms_id}", "status": status,
        "target_date": "", "commits": [], "entries": entries or [],
    }


def _op(op_id, status="open", title=None, entries=None):
    return {"id": op_id, "status": status, "title": title or f"Op {op_id}",
            "entries": entries or []}


def _project(milestones=None, operations=None):
    return {"name": "test", "milestones": milestones or [],
            "operations": operations or []}


# ---------------------------------------------------------------------------
# 1. find_entries / find_operations
# ---------------------------------------------------------------------------

class TestFinders:
    def test_find_entries_top_level(self):
        data = _project([_ms("ms-1", entries=[_entry("e-1"), _entry("e-2")])])
        assert [e["id"] for e in core.find_entries(data, "e-2")] == ["e-2"]

    def test_find_entries_nested(self):
        data = _project([_ms("ms-1", entries=[
            _entry("e-1", children=[_entry("e-9")]),
        ])])
        found = core.find_entries(data, "e-9")
        assert len(found) == 1 and found[0]["id"] == "e-9"

    def test_find_entries_in_operations(self):
        data = _project(operations=[_op("op-1", entries=[_entry("e-5")])])
        assert len(core.find_entries(data, "e-5")) == 1

    def test_find_entries_duplicates_across_ms(self):
        data = _project([
            _ms("ms-1", entries=[_entry("e-1", desc="A")]),
            _ms("ms-2", entries=[_entry("e-1", desc="B")]),
        ])
        found = core.find_entries(data, "e-1")
        assert [e["description"] for e in found] == ["A", "B"]

    def test_find_entries_none(self):
        assert core.find_entries(_project([_ms("ms-1")]), "e-99") == []

    def test_find_operations(self):
        data = _project(operations=[_op("op-1"), _op("op-2")])
        assert [o["id"] for o in core.find_operations(data, "op-1")] == ["op-1"]

    def test_find_operations_duplicates(self):
        data = _project(operations=[_op("op-1", title="A"), _op("op-1", title="B")])
        assert len(core.find_operations(data, "op-1")) == 2


# ---------------------------------------------------------------------------
# 2. entry_purge
# ---------------------------------------------------------------------------

class TestEntryPurge:
    def test_purge_single(self):
        data = _project([_ms("ms-1", entries=[_entry("e-1"), _entry("e-2")])])
        purged = core.entry_purge(data, "e-2", reason="dup")
        assert purged["id"] == "e-2"
        assert [e["id"] for e in data["milestones"][0]["entries"]] == ["e-1"]

    def test_purge_nested(self):
        data = _project([_ms("ms-1", entries=[
            _entry("e-1", children=[_entry("e-9", desc="deep")]),
        ])])
        purged = core.entry_purge(data, "e-9", reason="dup")
        assert purged["description"] == "deep"
        assert data["milestones"][0]["entries"][0].get("entries", []) == []

    def test_purge_in_operation(self):
        data = _project(operations=[_op("op-1", entries=[_entry("e-5"), _entry("e-6")])])
        core.entry_purge(data, "e-5", reason="dup")
        assert [e["id"] for e in data["operations"][0]["entries"]] == ["e-6"]

    def test_purge_requires_reason(self):
        data = _project([_ms("ms-1", entries=[_entry("e-1")])])
        with pytest.raises(ValueError, match="requires a reason"):
            core.entry_purge(data, "e-1", reason="")

    def test_purge_not_found(self):
        data = _project([_ms("ms-1")])
        with pytest.raises(ValueError, match="not found"):
            core.entry_purge(data, "e-99", reason="r")

    def test_purge_duplicate_requires_index(self):
        data = _project([
            _ms("ms-1", entries=[_entry("e-1", desc="A")]),
            _ms("ms-2", entries=[_entry("e-1", desc="B")]),
        ])
        with pytest.raises(ValueError, match="--index"):
            core.entry_purge(data, "e-1", reason="dup")

    def test_purge_duplicate_with_index(self):
        data = _project([
            _ms("ms-1", entries=[_entry("e-1", desc="A")]),
            _ms("ms-2", entries=[_entry("e-1", desc="B")]),
        ])
        purged = core.entry_purge(data, "e-1", reason="dup", index=2)
        assert purged["description"] == "B"
        remaining = core.find_entries(data, "e-1")
        assert len(remaining) == 1 and remaining[0]["description"] == "A"

    def test_purge_index_out_of_range(self):
        data = _project([
            _ms("ms-1", entries=[_entry("e-1")]),
            _ms("ms-2", entries=[_entry("e-1")]),
        ])
        with pytest.raises(ValueError, match="out of range"):
            core.entry_purge(data, "e-1", reason="dup", index=5)

    def test_purge_writes_audit_meta(self):
        data = _project([_ms("ms-1", entries=[_entry("e-1")])])
        purged = core.entry_purge(data, "e-1", reason="bad data")
        meta = purged.get("meta", {})
        assert meta.get("purge_reason") == "bad data"
        assert "purged_at" in meta and "purged_by" in meta


# ---------------------------------------------------------------------------
# 3. operation_purge
# ---------------------------------------------------------------------------

class TestOperationPurge:
    def test_purge_single(self):
        data = _project(operations=[_op("op-1"), _op("op-2")])
        purged = core.operation_purge(data, "op-2", reason="dup")
        assert purged["id"] == "op-2"
        assert [o["id"] for o in data["operations"]] == ["op-1"]

    def test_purge_requires_reason(self):
        data = _project(operations=[_op("op-1")])
        with pytest.raises(ValueError, match="requires a reason"):
            core.operation_purge(data, "op-1", reason="")

    def test_purge_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            core.operation_purge(_project(), "op-9", reason="r")

    def test_purge_duplicate_requires_index(self):
        data = _project(operations=[_op("op-1", title="A"), _op("op-1", title="B")])
        with pytest.raises(ValueError, match="--index"):
            core.operation_purge(data, "op-1", reason="dup")

    def test_purge_duplicate_with_index(self):
        data = _project(operations=[_op("op-1", title="A"), _op("op-1", title="B")])
        purged = core.operation_purge(data, "op-1", reason="dup", index=1)
        assert purged["title"] == "A"
        assert len(core.find_operations(data, "op-1")) == 1


# ---------------------------------------------------------------------------
# 4. recovery flow: validate blocks, purge unblocks
# ---------------------------------------------------------------------------

class TestRecoveryFlow:
    def test_dup_entry_blocks_then_purge_unblocks(self):
        data = _project([
            _ms("ms-1", entries=[_entry("e-1", desc="A")]),
            _ms("ms-2", entries=[_entry("e-1", desc="B")]),
        ])
        with pytest.raises(ValueError, match="Duplicate IDs"):
            core.validate_project(data)
        core.entry_purge(data, "e-1", reason="dup recovery", index=2)
        core.validate_project(data)  # now clean

    def test_dup_operation_blocks_then_purge_unblocks(self):
        data = _project(operations=[_op("op-1", title="A"), _op("op-1", title="B")])
        with pytest.raises(ValueError, match="Duplicate IDs"):
            core.validate_project(data)
        core.operation_purge(data, "op-1", reason="dup recovery", index=1)
        core.validate_project(data)


# ---------------------------------------------------------------------------
# 5. CLI integration
# ---------------------------------------------------------------------------

@pytest.fixture
def project_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "project.json").write_text(
            json.dumps({"name": "test", "milestones": []}), encoding="utf-8"
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _write(project_dir, data):
    (project_dir / ".beacon" / "project.json").write_text(
        json.dumps(data), encoding="utf-8")


def _read(project_dir):
    return json.loads(
        (project_dir / ".beacon" / "project.json").read_text(encoding="utf-8"))


def _changelog(project_dir):
    p = project_dir / ".beacon" / "changelog.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_cli_entry_purge_requires_reason(project_dir, monkeypatch, capsys):
    _write(project_dir, _project([_ms("ms-1", entries=[_entry("e-1")])]))
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "")
    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_entry_purge()
    assert ei.value.code == 1
    assert "--reason" in capsys.readouterr().err


def test_cli_entry_purge_dup_lists_options(project_dir, monkeypatch, capsys):
    _write(project_dir, _project([
        _ms("ms-1", entries=[_entry("e-1", desc="アポ獲得")]),
        _ms("ms-2", entries=[_entry("e-1", desc="PE警告UI")]),
    ]))
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "dup")
    monkeypatch.delenv("BEACON_INDEX", raising=False)
    import commands  # type: ignore
    with pytest.raises(SystemExit) as ei:
        commands.cmd_entry_purge()
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "--index 1" in err and "--index 2" in err
    assert "アポ獲得" in err and "PE警告UI" in err


def test_cli_entry_purge_index_and_changelog(project_dir, monkeypatch, capsys):
    _write(project_dir, _project([
        _ms("ms-1", entries=[_entry("e-1", desc="keep")]),
        _ms("ms-2", entries=[_entry("e-1", desc="drop")]),
    ]))
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "dup recovery")
    monkeypatch.setenv("BEACON_INDEX", "2")
    import commands  # type: ignore
    commands.cmd_entry_purge()  # must not raise

    remaining = core.find_entries(_read(project_dir), "e-1")
    assert len(remaining) == 1 and remaining[0]["description"] == "keep"
    purges = [e for e in _changelog(project_dir) if e.get("op") == "entry_purge"]
    assert len(purges) == 1 and purges[0]["entry_id"] == "e-1" and purges[0]["index"] == 2


def test_cli_entry_purge_two_dups_one_purge_cleans(project_dir, monkeypatch):
    # 2 duplicates: a single purge removes one and leaves exactly one e-1,
    # so the project is no longer corrupt and strict load_project succeeds.
    _write(project_dir, _project([
        _ms("ms-1", entries=[_entry("e-1")]),
        _ms("ms-2", entries=[_entry("e-1")]),
    ]))
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "dup")
    monkeypatch.setenv("BEACON_INDEX", "1")
    import commands  # type: ignore
    commands.cmd_entry_purge()
    data = commands.load_project()  # strict load now works
    assert len(core.find_entries(data, "e-1")) == 1


def test_cli_entry_purge_three_dups_residual_dirty(project_dir, monkeypatch, capsys):
    # 3 duplicates: one purge leaves 2 → still dirty, saved via unsafe path
    # with a residual warning, and strict load_project still refuses.
    _write(project_dir, _project([
        _ms("ms-1", entries=[_entry("e-1")]),
        _ms("ms-2", entries=[_entry("e-1")]),
        _ms("ms-3", entries=[_entry("e-1")]),
    ]))
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "removing one of three")
    monkeypatch.setenv("BEACON_INDEX", "1")
    import commands  # type: ignore
    commands.cmd_entry_purge()
    out = capsys.readouterr().out
    assert "residual duplicates remain" in out
    assert len(core.find_entries(_read(project_dir), "e-1")) == 2
    with pytest.raises(ValueError):
        commands.load_project()


def test_cli_operation_purge_index(project_dir, monkeypatch, capsys):
    _write(project_dir, _project(
        milestones=[_ms("ms-1")],
        operations=[_op("op-1", title="A"), _op("op-1", title="B")],
    ))
    monkeypatch.setenv("BEACON_OP_ID", "op-1")
    monkeypatch.setenv("BEACON_REASON", "dup recovery")
    monkeypatch.setenv("BEACON_INDEX", "2")
    import commands  # type: ignore
    commands.cmd_operation_purge()
    ops = core.find_operations(_read(project_dir), "op-1")
    assert len(ops) == 1 and ops[0]["title"] == "A"
