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

    def test_done_stamps_done_by_via_base(self):
        # ms-109 e-3696: the generic done-stamp (status/done_at/meta.done_by/
        # done_reason) now routes through work_model.mark_done.
        entry = make_entry("e-1", status="todo")
        data = make_project(milestones=[make_ms(entries=[entry])])
        _, e = core.task_done(data, "e-1", date="2026-05-11", reason="fixed it")
        assert e["status"] == "done"
        assert e["done_at"] == "2026-05-11"
        assert e["meta"]["done_by"]  # stamped via the base
        assert e["meta"]["done_reason"] == "fixed it"

    def test_done_preserves_date_fallback_and_human_author(self):
        entry = make_entry("e-1", status="todo")
        data = make_project(milestones=[make_ms(entries=[entry])])
        author = {"user_id": "u1", "email": "a@b.co", "display_name": "A"}
        _, e = core.task_done(data, "e-1", date="2026-05-11", author=author)
        # dev-specific bits still layered on top of the base stamp
        assert e["date"] == "2026-05-11"  # date mirror when absent
        assert e["meta"]["done_by_user"]["user_id"] == "u1"

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

    def test_update_motivation_ac_behavior_priority(self):
        # ms-43 e-553: task update must update the MS-32 fields
        entry = make_entry("e-1")
        ms = make_ms(entries=[entry])
        data = make_project(milestones=[ms])
        _, updated = core.task_update(
            data, "e-1",
            motivation="because Y",
            acceptance_criteria="works when Z",
            behavior="behaves like W",
            priority="high",
        )
        assert updated["motivation"] == "because Y"
        assert updated["acceptance_criteria"] == "works when Z"
        assert updated["behavior"] == "behaves like W"
        assert updated.get("meta", {}).get("priority") == "high"

    def test_update_invalid_priority(self):
        entry = make_entry("e-1")
        data = make_project(milestones=[make_ms(entries=[entry])])
        with pytest.raises(ValueError, match="Invalid priority"):
            core.task_update(data, "e-1", priority="bogus")

    def test_update_motivation_empty_no_change(self):
        # Empty string is treated as "no change" (preserves existing value)
        entry = make_entry("e-1")
        entry["motivation"] = "original"
        data = make_project(milestones=[make_ms(entries=[entry])])
        _, updated = core.task_update(data, "e-1", motivation="")
        assert updated["motivation"] == "original"

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

    def test_resolves_id_is_authoritative_over_fuzzy_match(self):
        """--resolves e-2 must win even when the message fuzzy-matches e-1.

        Regression for the auto-bind footgun: previously `resolves` was only
        stored in meta and never consulted, so the commit nested under
        whichever task shared the most message tokens."""
        t1 = make_entry("e-1", desc="Fix authentication login flow")
        t2 = make_entry("e-2", desc="Refactor database layer")
        ms = make_ms(ms_id="ms-1", status="in_progress", entries=[t1, t2])
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix authentication",  # tokens overlap e-1, not e-2
            date="2026-05-11", resolves="e-2", resolves_explicit=True,
        )
        assert result["matched_task"] == "e-2"          # authoritative
        assert len(t2.get("entries", [])) == 1          # nested under e-2
        assert len(t1.get("entries", [])) == 0          # NOT the fuzzy pick

    def test_explicit_empty_resolves_opts_out_of_binding(self):
        """--resolves "" must land the commit at milestone top level, never a
        coincidental fuzzy match (the exact e-3062→e-2556 mis-bind case)."""
        task = make_entry("e-1", desc="Fix authentication bug")
        ms = make_ms(ms_id="ms-1", status="in_progress", entries=[task])
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix authentication", date="2026-05-11",
            resolves="", resolves_explicit=True,
        )
        assert "matched_task" not in result             # no binding
        assert len(task.get("entries", [])) == 0        # task untouched
        assert len(ms["entries"]) == 2                  # task + top-level commit

    def test_unset_resolves_still_fuzzy_matches(self):
        """The post-commit hook / auto-fire path passes no --resolves; legacy
        fuzzy nesting must still work so hook-driven logs self-organize."""
        task = make_entry("e-1", desc="Fix authentication bug")
        ms = make_ms(ms_id="ms-1", status="in_progress", entries=[task])
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix authentication", date="2026-05-11",
            resolves="", resolves_explicit=False,  # unset sentinel
        )
        assert result["matched_task"] == "e-1"
        assert len(task.get("entries", [])) == 1

    def test_resolves_id_absent_falls_to_top_level_not_fuzzy(self):
        """--resolves names a task that doesn't exist → top level, never a
        fuzzy consolation pick."""
        task = make_entry("e-1", desc="Fix authentication bug")
        ms = make_ms(ms_id="ms-1", status="in_progress", entries=[task])
        data = make_project(milestones=[ms])
        result = core.log_commit(
            data, ms_id="ms-1", commit_hash="abc1234",
            message="Fix authentication", date="2026-05-11",
            resolves="e-999", resolves_explicit=True,
        )
        assert "matched_task" not in result
        assert len(task.get("entries", [])) == 0
        # resolves value is still recorded in the commit's meta.
        commit = ms["entries"][-1]
        assert commit["meta"].get("resolves") == "e-999"


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


# ---------------------------------------------------------------------------
# backfill_target_labels — dev half of the migrate step (ms-109 e-3625)
# ---------------------------------------------------------------------------

class TestBackfillTargetLabels:
    def test_backfills_milestones_and_operations(self):
        data = {
            "milestones": [{"id": "ms-1", "title": "A"}, {"id": "ms-2", "title": "B", "label": "B"}],
            "operations": [{"id": "op-1", "title": "Watch"}],
        }
        n = core.backfill_target_labels(data)
        assert n == 2  # ms-1 + op-1; ms-2 already canonical
        assert data["milestones"][0]["label"] == "A"
        assert data["milestones"][1]["label"] == "B"
        assert data["operations"][0]["label"] == "Watch"

    def test_idempotent(self):
        data = {"milestones": [{"id": "ms-1", "title": "A"}], "operations": []}
        assert core.backfill_target_labels(data) == 1
        assert core.backfill_target_labels(data) == 0

    def test_missing_collections(self):
        assert core.backfill_target_labels({}) == 0
