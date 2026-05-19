"""Unit tests for core.py - pure business logic, no I/O."""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_project(**kwargs):
    """Create a minimal valid project dict."""
    base = {"name": "test", "milestones": []}
    base.update(kwargs)
    return base


def make_ms(ms_id="ms-1", title="Test MS", status="todo", progress=0, entries=None):
    return {
        "id": ms_id, "title": title, "status": status,
        "progress": progress, "target_date": "",
        "entries": entries or [], "commits": [],
    }


def make_entry(eid="e-1", etype="task", desc="task1", status="todo"):
    return {
        "id": eid, "type": etype, "description": desc,
        "status": status, "date": "2026-05-11",
        "created_at": "2026-05-11", "done_at": None, "meta": {},
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_project(self):
        core.validate_project(make_project())

    def test_missing_name(self):
        with pytest.raises(ValueError, match="Missing required field"):
            core.validate_project({"milestones": []})

    def test_missing_milestones(self):
        with pytest.raises(ValueError, match="Missing required field"):
            core.validate_project({"name": "test"})

    def test_invalid_ms_id(self):
        data = make_project(milestones=[{"id": "bad", "status": "todo"}])
        with pytest.raises(ValueError, match="does not match"):
            core.validate_project(data)

    def test_tasks_field_rejected(self):
        data = make_project(milestones=[{"id": "ms-1", "tasks": []}])
        with pytest.raises(ValueError, match="Use 'entries'"):
            core.validate_project(data)

    def test_invalid_status(self):
        data = make_project(milestones=[{"id": "ms-1", "status": "bogus"}])
        with pytest.raises(ValueError, match="invalid status"):
            core.validate_project(data)

    def test_invalid_entry_id(self):
        ms = make_ms(entries=[{"id": "bad-id", "type": "task", "status": "todo"}])
        data = make_project(milestones=[ms])
        with pytest.raises(ValueError, match="does not match"):
            core.validate_project(data)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

class TestLookups:
    def test_find_target_by_id(self):
        ms = make_ms(ms_id="ms-1")
        data = make_project(milestones=[ms])
        assert core.find_target_milestone(data, "ms-1") is ms

    def test_find_target_not_found(self):
        data = make_project(milestones=[make_ms()])
        with pytest.raises(ValueError, match="not found"):
            core.find_target_milestone(data, "ms-99")

    def test_auto_select_single_active(self):
        ms = make_ms(status="in_progress")
        data = make_project(milestones=[ms])
        assert core.find_target_milestone(data) is ms

    def test_no_active_raises(self):
        data = make_project(milestones=[make_ms(status="todo")])
        with pytest.raises(ValueError, match="No active"):
            core.find_target_milestone(data)

    def test_multiple_active_raises(self):
        data = make_project(milestones=[
            make_ms(ms_id="ms-1", status="in_progress"),
            make_ms(ms_id="ms-2", status="in_progress"),
        ])
        with pytest.raises(ValueError, match="Multiple"):
            core.find_target_milestone(data)

    def test_next_entry_id(self):
        ms = make_ms(entries=[make_entry("e-3"), make_entry("e-1")])
        data = make_project(milestones=[ms])
        assert core.next_entry_id(data) == "e-4"

    def test_next_entry_id_empty(self):
        data = make_project()
        assert core.next_entry_id(data) == "e-1"

    def test_find_entry(self):
        entry = make_entry("e-5")
        ms = make_ms(entries=[make_entry("e-1"), entry])
        data = make_project(milestones=[ms])
        result = core.find_entry(data, "e-5")
        assert result is not None
        assert result[2] is entry

    def test_find_entry_nested(self):
        child = make_entry("e-2")
        parent = make_entry("e-1")
        parent["entries"] = [child]
        ms = make_ms(entries=[parent])
        data = make_project(milestones=[ms])
        result = core.find_entry(data, "e-2")
        assert result is not None
        assert result[2] is child

    def test_find_entry_not_found(self):
        data = make_project(milestones=[make_ms()])
        assert core.find_entry(data, "e-99") is None


# ---------------------------------------------------------------------------
# Milestone operations
# ---------------------------------------------------------------------------

class TestMilestones:
    def test_add(self):
        data = make_project()
        ms_id = core.milestone_add(data, "New MS", "2026-12-31")
        assert ms_id == "ms-1"
        assert len(data["milestones"]) == 1
        assert data["milestones"][0]["title"] == "New MS"
        assert data["milestones"][0]["target_date"] == "2026-12-31"
        assert "description" not in data["milestones"][0]

    def test_add_with_description(self):
        data = make_project()
        ms_id = core.milestone_add(data, "New MS", description="A detailed goal")
        assert ms_id == "ms-1"
        assert data["milestones"][0]["description"] == "A detailed goal"

    def test_add_without_description_omits_key(self):
        data = make_project()
        core.milestone_add(data, "No desc MS")
        assert "description" not in data["milestones"][0]

    def test_start_not_found(self):
        data = make_project(milestones=[make_ms()])
        with pytest.raises(ValueError):
            core.milestone_start(data, "ms-99")

    def test_done(self):
        data = make_project(milestones=[make_ms(ms_id="ms-1")])
        ms = core.milestone_done(data, "ms-1")
        assert ms["status"] == "done"

    def test_update(self):
        data = make_project(milestones=[make_ms(ms_id="ms-1")])
        ms = core.milestone_update(data, "ms-1", title="Updated", progress="50")
        assert ms["title"] == "Updated"
        assert ms["progress"] == 50

    def test_update_description(self):
        data = make_project(milestones=[make_ms(ms_id="ms-1")])
        ms = core.milestone_update(data, "ms-1", description="New desc")
        assert ms["description"] == "New desc"

    def test_update_empty_description_no_change(self):
        data = make_project(milestones=[make_ms(ms_id="ms-1")])
        data["milestones"][0]["description"] = "Original"
        ms = core.milestone_update(data, "ms-1", description="")
        assert ms["description"] == "Original"

    def test_update_invalid_status(self):
        data = make_project(milestones=[make_ms()])
        with pytest.raises(ValueError, match="Invalid status"):
            core.milestone_update(data, "ms-1", status="bogus")

    def test_delete(self):
        data = make_project(milestones=[make_ms(ms_id="ms-1")])
        ms = core.milestone_delete(data, "ms-1")
        assert ms["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Task / Entry operations
# ---------------------------------------------------------------------------

class TestTasks:
    def test_add(self):
        ms = make_ms(ms_id="ms-1", status="in_progress")
        data = make_project(milestones=[ms])
        eid = core.task_add(data, "ms-1", "Do something", date="2026-05-11")
        assert eid == "e-1"
        assert len(ms["entries"]) == 1
        assert ms["entries"][0]["description"] == "Do something"

    def test_add_with_detail(self):
        ms = make_ms(ms_id="ms-1", status="in_progress")
        data = make_project(milestones=[ms])
        eid = core.task_add(data, "ms-1", "Task", detail="Some detail")
        assert ms["entries"][0]["detail"] == "Some detail"

    def test_done(self):
        entry = make_entry("e-1", status="todo")
        ms = make_ms(entries=[entry])
        data = make_project(milestones=[ms])
        ret_ms, ret_entry = core.task_done(data, "e-1", date="2026-05-11")
        assert ret_entry["status"] == "done"
        assert ret_entry["done_at"] == "2026-05-11"

    def test_done_not_found(self):
        data = make_project(milestones=[make_ms()])
        with pytest.raises(ValueError, match="not found"):
            core.task_done(data, "e-99")

    def test_update(self):
        entry = make_entry("e-1")
        ms = make_ms(entries=[entry])
        data = make_project(milestones=[ms])
        _, updated = core.task_update(data, "e-1", description="Updated desc", status="done", date="2026-05-11")
        assert updated["description"] == "Updated desc"
        assert updated["status"] == "done"
        assert updated["done_at"] == "2026-05-11"

    def test_update_invalid_status(self):
        entry = make_entry("e-1")
        data = make_project(milestones=[make_ms(entries=[entry])])
        with pytest.raises(ValueError, match="Invalid status"):
            core.task_update(data, "e-1", status="bogus")

    def test_delete(self):
        entry = make_entry("e-1")
        data = make_project(milestones=[make_ms(entries=[entry])])
        deleted = core.task_delete(data, "e-1")
        assert deleted["status"] == "cancelled"

    def test_entry_move_to_task(self):
        parent = make_entry("e-1", desc="parent task")
        child = make_entry("e-2", etype="commit", desc="commit")
        ms = make_ms(entries=[parent, child])
        data = make_project(milestones=[ms])
        core.entry_move(data, "e-2", task_id="e-1")
        assert len(ms["entries"]) == 1  # child removed from top
        assert len(ms["entries"][0]["entries"]) == 1  # child under parent

    def test_entry_move_to_milestone(self):
        entry = make_entry("e-1")
        ms1 = make_ms(ms_id="ms-1", entries=[entry])
        ms2 = make_ms(ms_id="ms-2")
        data = make_project(milestones=[ms1, ms2])
        core.entry_move(data, "e-1", ms_id="ms-2")
        assert len(ms1["entries"]) == 0
        assert len(ms2["entries"]) == 1

    def test_entry_move_to_self_raises(self):
        entry = make_entry("e-1")
        data = make_project(milestones=[make_ms(entries=[entry])])
        with pytest.raises(ValueError, match="itself"):
            core.entry_move(data, "e-1", task_id="e-1")


# ---------------------------------------------------------------------------
# Progress & Summary
# ---------------------------------------------------------------------------

class TestProgress:
    def test_update_progress(self):
        ms = make_ms(progress=10)
        core.update_progress(ms, "50")
        assert ms["progress"] == 50

    def test_progress_clamp(self):
        ms = make_ms()
        core.update_progress(ms, "150")
        assert ms["progress"] == 100

    def test_progress_auto_activates(self):
        ms = make_ms(status="todo")
        core.update_progress(ms, "10")
        assert ms["status"] == "in_progress"

    def test_empty_progress_no_change(self):
        ms = make_ms(progress=30)
        core.update_progress(ms, "")
        assert ms["progress"] == 30


class TestSummary:
    def test_auto_update_summary(self):
        entry = make_entry("e-1", desc="did something")
        ms = make_ms(entries=[entry])
        data = make_project(milestones=[ms])
        core.auto_update_summary(data)
        assert "did something" in data["summary"]

    def test_empty_entries_no_summary(self):
        data = make_project()
        core.auto_update_summary(data)
        assert "summary" not in data or data.get("summary", "") == ""


# ---------------------------------------------------------------------------
# Commit logging
# ---------------------------------------------------------------------------

class TestLogCommit:
    def test_log_new_commit(self):
        ms = make_ms(ms_id="ms-1", status="in_progress")
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix bug", date="2026-05-11",
        )
        assert result["status"] == "logged"
        assert result["hash"] == "abc1234"
        assert len(ms["entries"]) == 1
        assert ms["entries"][0]["type"] == "commit"

    def test_duplicate_commit(self):
        entry = make_entry("e-1", etype="commit")
        entry["meta"] = {"hash": "abc1234", "message": "Fix bug"}
        ms = make_ms(ms_id="ms-1", status="in_progress", entries=[entry])
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix bug", date="2026-05-11",
        )
        assert result["status"] == "duplicate"
        assert len(ms["entries"]) == 1  # no new entry

    def test_log_with_progress(self):
        ms = make_ms(ms_id="ms-1", status="in_progress", progress=10)
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix bug", date="2026-05-11", progress="30",
        )
        assert ms["progress"] == 30

    def test_log_auto_matches_task(self):
        task = make_entry("e-1", desc="Fix authentication bug")
        ms = make_ms(ms_id="ms-1", status="in_progress", entries=[task])
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix authentication", date="2026-05-11",
        )
        assert "matched_task" in result
        assert result["matched_task"] == "e-1"
        # Commit should be nested under the task
        assert len(ms["entries"]) == 1  # task still at top
        assert len(task.get("entries", [])) == 1  # commit nested


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_entries_to_json(self):
        entry = make_entry("e-1")
        result = core.entries_to_json([entry])
        assert len(result) == 1
        assert result[0]["id"] == "e-1"

    def test_filter_cancelled(self):
        entries = [
            make_entry("e-1", status="todo"),
            make_entry("e-2", status="cancelled"),
        ]
        filtered = core.filter_cancelled(entries)
        assert len(filtered) == 1
        assert filtered[0]["id"] == "e-1"

    def test_filter_cancelled_show_all(self):
        entries = [
            make_entry("e-1", status="todo"),
            make_entry("e-2", status="cancelled"),
        ]
        filtered = core.filter_cancelled(entries, show_all=True)
        assert len(filtered) == 2
