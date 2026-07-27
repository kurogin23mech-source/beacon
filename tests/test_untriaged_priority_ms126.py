"""ms-126: mandatory priority + untriaged sentinel + untriaged-backlog trigger.

Covers the forcing-function contract at the core layer (milestone_add /
task_add), the untriaged sentinel semantics, the machine-path issue_import
stamp, and the untriaged-backlog debt trigger counting (including the
no-backfill guarantee that legacy no-priority entries are never counted).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core  # noqa: E402
import commands  # noqa: E402


def make_project(**kwargs):
    base = {"name": "test", "milestones": []}
    base.update(kwargs)
    return base


def make_active_ms(ms_id="ms-1"):
    return {
        "id": ms_id, "title": "MS", "status": "in_progress",
        "progress": 0, "target_date": "", "entries": [], "commits": [],
    }


# ---------------------------------------------------------------------------
# core: milestone_add forcing function
# ---------------------------------------------------------------------------

class TestMilestoneAddPriorityForcing:
    def test_empty_priority_human_path_rejected(self):
        data = make_project()
        with pytest.raises(ValueError, match="Priority is required"):
            core.milestone_add(data, "M")  # allow_untriaged defaults False
        # No milestone should have been appended on rejection.
        assert data["milestones"] == []

    def test_empty_priority_machine_path_stores_untriaged(self):
        data = make_project()
        core.milestone_add(data, "M", allow_untriaged=True)
        assert data["milestones"][0]["priority"] == core.UNTRIAGED_PRIORITY

    def test_valid_priority_stored_normalized(self):
        data = make_project()
        core.milestone_add(data, "M", priority="high")
        assert data["milestones"][0]["priority"] == "high"

    def test_deprecated_alias_normalizes(self):
        data = make_project()
        core.milestone_add(data, "M", priority="middle")
        assert data["milestones"][0]["priority"] == "medium"

    def test_invalid_priority_still_rejected(self):
        data = make_project()
        with pytest.raises(ValueError, match="Invalid priority"):
            core.milestone_add(data, "M", priority="bogus")

    def test_human_cannot_type_untriaged_literal(self):
        # A human typing "untriaged" is not choosing a severity — treated as
        # the same forcing-function failure.
        data = make_project()
        with pytest.raises(ValueError, match="Priority is required"):
            core.milestone_add(data, "M", priority="untriaged")

    def test_machine_may_pass_untriaged_literal(self):
        data = make_project()
        core.milestone_add(data, "M", priority="untriaged", allow_untriaged=True)
        assert data["milestones"][0]["priority"] == core.UNTRIAGED_PRIORITY


# ---------------------------------------------------------------------------
# core: task_add forcing function
# ---------------------------------------------------------------------------

class TestTaskAddPriorityForcing:
    def test_empty_priority_human_task_rejected(self):
        data = make_project(milestones=[make_active_ms()])
        with pytest.raises(ValueError, match="Priority is required"):
            core.task_add(data, "ms-1", "T")
        assert data["milestones"][0]["entries"] == []

    def test_empty_priority_machine_task_stores_untriaged(self):
        data = make_project(milestones=[make_active_ms()])
        core.task_add(data, "ms-1", "T", allow_untriaged=True)
        meta = data["milestones"][0]["entries"][0]["meta"]
        assert meta["priority"] == core.UNTRIAGED_PRIORITY

    def test_valid_priority_task(self):
        data = make_project(milestones=[make_active_ms()])
        core.task_add(data, "ms-1", "T", priority="low")
        assert data["milestones"][0]["entries"][0]["meta"]["priority"] == "low"

    def test_non_task_entry_does_not_require_priority(self):
        # commit / note entries carry no severity — the forcing function must
        # not apply, or the Web-UI commit/note create path breaks.
        data = make_project(milestones=[make_active_ms()])
        core.task_add(data, "ms-1", "c", entry_type="commit")
        meta = data["milestones"][0]["entries"][0]["meta"]
        assert "priority" not in meta

    def test_non_task_entry_invalid_priority_still_validated(self):
        data = make_project(milestones=[make_active_ms()])
        with pytest.raises(ValueError, match="Invalid priority"):
            core.task_add(data, "ms-1", "n", entry_type="note", priority="bogus")


# ---------------------------------------------------------------------------
# core: sentinel is never a severity
# ---------------------------------------------------------------------------

class TestUntriagedSentinelSemantics:
    def test_untriaged_not_in_valid_priorities(self):
        assert core.UNTRIAGED_PRIORITY not in core.VALID_PRIORITIES
        assert core.UNTRIAGED_PRIORITY not in core._ACCEPTED_PRIORITIES

    def test_normalize_leaves_untriaged_unchanged(self):
        assert core.normalize_priority(core.UNTRIAGED_PRIORITY) == "untriaged"

    def test_required_message_lists_five_severities(self):
        msg = core._PRIORITY_REQUIRED_MSG
        for sev in ("highest", "high", "medium", "low", "lowest"):
            assert sev in msg
        assert "untriaged" not in msg


# ---------------------------------------------------------------------------
# core: issue_import machine path stamps untriaged
# ---------------------------------------------------------------------------

class TestIssueImportUntriaged:
    def test_imported_issue_is_untriaged(self):
        data = make_project(milestones=[make_active_ms()])
        eid = core.issue_import(
            data, ms_id="ms-1", number=42, url="http://x",
            title="Fix thing", body="body",
        )
        entry = data["milestones"][0]["entries"][0]
        assert entry["id"] == eid
        assert entry["meta"]["priority"] == core.UNTRIAGED_PRIORITY


# ---------------------------------------------------------------------------
# trigger: untriaged-backlog counting + no-backfill
# ---------------------------------------------------------------------------

class TestUntriagedBacklogCount:
    def test_counts_only_active_sentinel_entries(self):
        data = make_project(milestones=[
            {"id": "ms-1", "status": "in_progress",
             "priority": core.UNTRIAGED_PRIORITY, "entries": [
                {"id": "e-1", "type": "task", "status": "todo",
                 "meta": {"priority": core.UNTRIAGED_PRIORITY}},
                # done task with sentinel — not actionable, not counted
                {"id": "e-2", "type": "task", "status": "done",
                 "meta": {"priority": core.UNTRIAGED_PRIORITY}},
                # triaged task — not counted
                {"id": "e-3", "type": "task", "status": "todo",
                 "meta": {"priority": "high"}},
                # commit — not counted (not a task)
                {"id": "e-4", "type": "commit", "status": "todo", "meta": {}},
                # legacy task, no priority field — NOT counted (no-backfill)
                {"id": "e-5", "type": "task", "status": "todo", "meta": {}},
             ]},
            # done MS with sentinel — not counted
            {"id": "ms-2", "status": "done",
             "priority": core.UNTRIAGED_PRIORITY, "entries": []},
            # legacy MS, no priority field — NOT counted (no-backfill)
            {"id": "ms-3", "status": "todo", "entries": []},
        ])
        ms_count, task_count = commands._count_untriaged_active(data)
        assert ms_count == 1
        assert task_count == 1

    def test_no_backfill_legacy_entries_never_counted(self):
        # A whole project of legacy no-priority active entries must produce a
        # zero count so the debt trigger never floods on pre-ms-126 data.
        data = make_project(milestones=[
            {"id": "ms-1", "status": "todo", "entries": [
                {"id": "e-1", "type": "task", "status": "todo", "meta": {}},
                {"id": "e-2", "type": "task", "status": "in_progress",
                 "meta": {}},
            ]},
            {"id": "ms-2", "status": "in_progress", "entries": []},
        ])
        assert commands._count_untriaged_active(data) == (0, 0)


class TestUntriagedBacklogTriggerFile:
    def test_fires_and_clears(self, tmp_path, monkeypatch):
        triggers_dir = tmp_path / "triggers"
        triggers_dir.mkdir()
        monkeypatch.setattr(commands, "_get_triggers_dir",
                            lambda: str(triggers_dir))

        # Fake store returning a project with one untriaged active task.
        class _Store:
            def __init__(self, data):
                self._data = data

            def load_project(self):
                return self._data

        data_with = make_project(milestones=[
            {"id": "ms-1", "status": "in_progress", "entries": [
                {"id": "e-1", "type": "task", "status": "todo",
                 "meta": {"priority": core.UNTRIAGED_PRIORITY}},
            ]},
        ])
        monkeypatch.setattr(commands, "get_store", lambda: _Store(data_with))
        commands._auto_fire_untriaged_backlog_trigger()
        trigger_file = triggers_dir / "untriaged-backlog.json"
        assert trigger_file.exists()
        import json
        payload = json.loads(trigger_file.read_text(encoding="utf-8"))
        assert payload["kind"] == "untriaged-backlog"
        assert payload["total"] == 1
        assert payload["task_count"] == 1
        assert "未 triage" in payload["message"] or "未triage" in payload["message"]

        # Now the debt clears (task triaged) → trigger file removed.
        data_clear = make_project(milestones=[
            {"id": "ms-1", "status": "in_progress", "entries": [
                {"id": "e-1", "type": "task", "status": "todo",
                 "meta": {"priority": "high"}},
            ]},
        ])
        monkeypatch.setattr(commands, "get_store", lambda: _Store(data_clear))
        commands._auto_fire_untriaged_backlog_trigger()
        assert not trigger_file.exists()
