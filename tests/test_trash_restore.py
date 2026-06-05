"""Unit + integration tests for trash + restore (ms-14 e-826).

Two layers:
  - core.* layer: milestone_restore / entry_restore / list_trashed_*
    operate on plain dicts. No I/O.
  - commands.* layer: cmd_milestone_restore / cmd_milestone_trash /
    cmd_task_restore / cmd_task_trash drive an on-disk project and write
    a changelog entry via save_project.

Doc trash is intentionally NOT covered here — that path is filed as a
follow-up task on ms-14 because cmd_doc_delete is currently hard-delete
and the existing cmd_doc_restore name collides with the new restore
semantics.
"""

from __future__ import annotations

import datetime
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
# Helpers
# ---------------------------------------------------------------------------

def _iso_days_ago(days: int) -> str:
    """ISO timestamp `days` days before now (matches core._now_iso shape)."""
    dt = datetime.datetime.now() - datetime.timedelta(days=days)
    return dt.isoformat()


def _make_data(
    cancelled_ms_days_ago: int | None = 5,
    cancelled_task_days_ago: int | None = 5,
    extra_task_days_ago: int | None = None,
) -> dict:
    """Build a minimal project dict with a cancelled milestone and task."""
    ms = {
        "id": "ms-1",
        "title": "first",
        "status": "cancelled",
        "target_date": "",
        "entries": [
            {
                "id": "e-1",
                "type": "task",
                "status": "cancelled",
                "description": "do thing",
                "meta": {
                    "cancelled_at": _iso_days_ago(cancelled_task_days_ago)
                    if cancelled_task_days_ago is not None else "",
                    "cancelled_by": "alice",
                    "cancel_reason": "scope creep",
                },
            },
            {
                "id": "e-2",
                "type": "task",
                "status": "todo",
                "description": "alive task",
            },
        ],
        "meta": {
            "cancelled_at": _iso_days_ago(cancelled_ms_days_ago)
            if cancelled_ms_days_ago is not None else "",
            "cancelled_by": "alice",
            "cancel_reason": "deferred",
        },
    }
    if extra_task_days_ago is not None:
        ms["entries"].append({
            "id": "e-3",
            "type": "task",
            "status": "cancelled",
            "description": "old cancelled",
            "meta": {
                "cancelled_at": _iso_days_ago(extra_task_days_ago),
                "cancelled_by": "bob",
                "cancel_reason": "",
            },
        })
    return {"name": "demo", "milestones": [ms], "operations": []}


# ===========================================================================
# core layer
# ===========================================================================

def test_milestone_restore_flips_status_and_clears_meta():
    data = _make_data()
    ms = core.milestone_restore(data, "ms-1", reason="changed our mind")
    assert ms["status"] == "todo"
    meta = ms["meta"]
    # Cancel metadata is wiped — only restore audit fields remain.
    assert "cancelled_at" not in meta
    assert "cancelled_by" not in meta
    assert "cancel_reason" not in meta
    assert meta["restored_at"]
    assert meta["restore_reason"] == "changed our mind"


def test_milestone_restore_refuses_active_milestone():
    data = _make_data()
    data["milestones"][0]["status"] = "todo"
    with pytest.raises(ValueError) as ei:
        core.milestone_restore(data, "ms-1")
    assert "not cancelled" in str(ei.value)


def test_milestone_restore_missing_id_raises():
    data = _make_data()
    with pytest.raises(ValueError) as ei:
        core.milestone_restore(data, "ms-999")
    assert "not found" in str(ei.value).lower()


def test_entry_restore_flips_status_and_refuses_non_cancelled():
    data = _make_data()
    entry = core.entry_restore(data, "e-1", reason="re-prioritized")
    assert entry["status"] == "todo"
    assert entry["meta"]["restore_reason"] == "re-prioritized"
    assert "cancelled_at" not in entry["meta"]

    # e-2 is already todo — should refuse.
    with pytest.raises(ValueError) as ei:
        core.entry_restore(data, "e-2")
    assert "not cancelled" in str(ei.value)

    with pytest.raises(ValueError):
        core.entry_restore(data, "e-9999")


def test_list_trashed_milestones_respects_window_and_sorts_desc():
    # Mix recent + old cancelled milestones; the old one must be filtered
    # by the default 30-day window.
    data = _make_data(cancelled_ms_days_ago=2)
    data["milestones"].append({
        "id": "ms-2", "title": "old",
        "status": "cancelled", "target_date": "", "entries": [],
        "meta": {
            "cancelled_at": _iso_days_ago(90),
            "cancelled_by": "bob",
        },
    })
    data["milestones"].append({
        "id": "ms-3", "title": "recent",
        "status": "cancelled", "target_date": "", "entries": [],
        "meta": {
            "cancelled_at": _iso_days_ago(1),
            "cancelled_by": "carol",
        },
    })
    out = core.list_trashed_milestones(data, since_days=30)
    ids = [item["id"] for item in out]
    assert "ms-1" in ids
    assert "ms-3" in ids
    assert "ms-2" not in ids  # 90 days > 30-day window
    # Desc sort: ms-3 (1d) before ms-1 (2d).
    assert ids.index("ms-3") < ids.index("ms-1")


def test_list_trashed_entries_walks_nested_and_filters_by_type():
    data = _make_data()
    # Nested cancelled task under e-2 (which is alive).
    data["milestones"][0]["entries"][1]["entries"] = [
        {
            "id": "e-99", "type": "task", "status": "cancelled",
            "description": "nested cancelled",
            "meta": {
                "cancelled_at": _iso_days_ago(3),
                "cancelled_by": "dave",
            },
        },
        {
            "id": "e-100", "type": "commit", "status": "cancelled",
            "description": "commit-style cancelled",
            "meta": {
                "cancelled_at": _iso_days_ago(3),
                "cancelled_by": "dave",
            },
        },
    ]
    out = core.list_trashed_entries(data, since_days=30, entry_type="task")
    ids = [item["id"] for item in out]
    assert "e-1" in ids       # top-level cancelled task
    assert "e-99" in ids      # nested cancelled task
    assert "e-100" not in ids # commit type filtered out


# ===========================================================================
# commands layer (CLI handlers)
# ===========================================================================

@pytest.fixture
def project_on_disk(monkeypatch):
    """An on-disk project with a cancelled MS + task, ready for CLI calls."""
    with tempfile.TemporaryDirectory() as tmp:
        beacon_dir = Path(tmp) / ".beacon"
        beacon_dir.mkdir()
        project_file = beacon_dir / "project.json"
        project_file.write_text(
            json.dumps(_make_data(), ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setenv("BEACON_PROJECT_FILE", str(project_file))
        monkeypatch.delenv("BEACON_CLOUD", raising=False)
        monkeypatch.setenv("BEACON_OPERATIONS_BACKEND", "local")
        sys.modules.pop("firestore_client", None)
        yield Path(tmp)


def _reload_commands():
    if "commands" in sys.modules:
        del sys.modules["commands"]
    import commands  # noqa: WPS433
    return commands


def _read_changelog(root: Path) -> list[dict]:
    path = root / ".beacon" / "changelog.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_project(root: Path) -> dict:
    return json.loads(
        (root / ".beacon" / "project.json").read_text(encoding="utf-8")
    )


def test_cmd_milestone_restore_writes_changelog_op(project_on_disk, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "ms-1")
    monkeypatch.setenv("BEACON_REASON", "ops mistake")
    monkeypatch.setenv("BEACON_JSON", "0")

    commands = _reload_commands()
    commands.cmd_milestone_restore()

    project = _read_project(project_on_disk)
    assert project["milestones"][0]["status"] == "todo"
    assert "restored_at" in project["milestones"][0]["meta"]

    log = _read_changelog(project_on_disk)
    ops = [e["op"] for e in log]
    assert "milestone_restore" in ops
    matched = [e for e in log if e["op"] == "milestone_restore"][0]
    assert matched["ms_id"] == "ms-1"
    assert matched["reason"] == "ops mistake"


def test_cmd_milestone_trash_json_lists_cancelled(project_on_disk, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DAYS", "30")

    commands = _reload_commands()
    commands.cmd_milestone_trash()

    out = capsys.readouterr().out.strip()
    items = json.loads(out)
    assert isinstance(items, list)
    assert any(item["id"] == "ms-1" for item in items)


def test_cmd_milestone_trash_all_flag_shows_unbounded(project_on_disk, monkeypatch, capsys):
    # Add an ancient cancelled MS that would be hidden by the default 30d window.
    proj = _read_project(project_on_disk)
    proj["milestones"].append({
        "id": "ms-2", "title": "ancient",
        "status": "cancelled", "target_date": "", "entries": [],
        "meta": {"cancelled_at": _iso_days_ago(365), "cancelled_by": "x"},
    })
    (project_on_disk / ".beacon" / "project.json").write_text(
        json.dumps(proj, ensure_ascii=False), encoding="utf-8",
    )

    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DAYS", "all")
    commands = _reload_commands()
    commands.cmd_milestone_trash()
    items = json.loads(capsys.readouterr().out.strip())
    ids = [i["id"] for i in items]
    assert "ms-2" in ids and "ms-1" in ids


def test_cmd_task_restore_writes_changelog_op(project_on_disk, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-1")
    monkeypatch.setenv("BEACON_REASON", "")
    monkeypatch.setenv("BEACON_JSON", "1")

    commands = _reload_commands()
    commands.cmd_task_restore()

    out = json.loads(capsys.readouterr().out.strip())
    assert out["id"] == "e-1" and out["status"] == "todo"

    log = _read_changelog(project_on_disk)
    assert any(e["op"] == "task_restore" and e["entry_id"] == "e-1" for e in log)


def test_cmd_task_trash_json_lists_cancelled_tasks(project_on_disk, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setenv("BEACON_DAYS", "30")

    commands = _reload_commands()
    commands.cmd_task_trash()

    items = json.loads(capsys.readouterr().out.strip())
    assert any(it["id"] == "e-1" for it in items)
    # All returned items must be tasks; commits etc. are filtered out.
    assert all(_read_project(project_on_disk) for _ in items)  # sanity


def test_cmd_milestone_restore_missing_id_exits_nonzero(project_on_disk, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_MS_ID", "")
    commands = _reload_commands()
    with pytest.raises(SystemExit) as ei:
        commands.cmd_milestone_restore()
    assert ei.value.code == 1


def test_cmd_task_restore_refuses_active_entry(project_on_disk, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_ENTRY_ID", "e-2")  # e-2 is todo, not cancelled
    monkeypatch.setenv("BEACON_REASON", "")
    monkeypatch.setenv("BEACON_JSON", "0")
    commands = _reload_commands()
    with pytest.raises(SystemExit) as ei:
        commands.cmd_task_restore()
    assert ei.value.code == 1
    err_out = capsys.readouterr().out
    assert "not cancelled" in err_out
