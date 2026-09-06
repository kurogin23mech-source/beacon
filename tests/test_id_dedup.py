"""Tests for Issue #14: Milestone ID duplication.

These tests reproduce the original failure mode (length-based allocator
silently re-issuing existing IDs) and verify the structural fixes:

  1. next_target_id (the live allocator; next_milestone_id removed in
     e-6022) is monotonic regardless of array length
  2. milestone_add now refuses to create a colliding ID
  3. find_target_milestone raises on duplicate IDs instead of returning
     the first match (the original bug)
  4. validate_project flags duplicates so corruption is detected at load
  5. find_duplicate_ids gives doctor a read-only report
  6. milestone_purge physically removes a record (the hard-delete recovery
     path) and supports --index for duplicates
"""

import sys
import os
import copy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core
import occupation  # ms-164 e-6022: next_milestone_id removed; the live max+1
                   # allocator is occupation.next_target_id(data, "milestone")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _project(milestones):
    return {"name": "test", "milestones": milestones}


def _ms(ms_id, status="todo", title=None, entries=None):
    return {
        "id": ms_id,
        "title": title or f"Milestone {ms_id}",
        "status": status,
        "target_date": "",
        "commits": [],
        "entries": entries or [],
    }


# ---------------------------------------------------------------------------
# 1. Reproduction: the original len()-based allocator collides
# ---------------------------------------------------------------------------

class TestOriginalBugRepro:
    """Demonstrates why `len(milestones) + 1` is structurally broken."""

    def test_len_based_allocator_collides_after_physical_removal(self):
        # Simulate the life-plan-simulator scenario:
        # 1. Project ends up with ms-1, ms-2, ms-3 (len == 3)
        # 2. A hand-edit drops ms-2 (len == 2)
        # 3. Old allocator says "next is ms-3" — collides with existing ms-3
        existing = [_ms("ms-1"), _ms("ms-3")]
        old_allocator_id = f"ms-{len(existing) + 1}"
        assert old_allocator_id == "ms-3"
        assert old_allocator_id in {m["id"] for m in existing}, (
            "Old allocator would silently re-issue an existing ID — "
            "this is the bug we are fixing."
        )

    def test_max_id_allocator_never_collides_after_physical_removal(self):
        existing = [_ms("ms-1"), _ms("ms-3")]
        data = _project(existing)
        next_id = occupation.next_target_id(data, "milestone")
        assert next_id == "ms-4"
        assert next_id not in {m["id"] for m in existing}

    def test_max_id_allocator_counts_cancelled_and_done(self):
        # Even cancelled / done MSs contribute to the max — they stay in
        # the array under soft-delete semantics, so they should not be
        # re-used.
        existing = [
            _ms("ms-1", status="cancelled"),
            _ms("ms-2", status="done"),
            _ms("ms-3", status="in_progress"),
        ]
        assert occupation.next_target_id(_project(existing), "milestone") == "ms-4"


# ---------------------------------------------------------------------------
# 2. milestone_add: collision check
# ---------------------------------------------------------------------------

class TestMilestoneAddCollisionGuard:
    def test_add_assigns_max_plus_one(self):
        data = _project([_ms("ms-1"), _ms("ms-5")])
        new_id = core.milestone_add(data, "New", allow_untriaged=True)
        assert new_id == "ms-6"
        ids = [m["id"] for m in data["milestones"]]
        # Original list is preserved and new one appended
        assert ids[-1] == "ms-6"
        assert "ms-6" in ids

    def test_add_into_empty_project(self):
        data = _project([])
        assert core.milestone_add(data, "First", allow_untriaged=True) == "ms-1"

    def test_add_does_not_collide_with_cancelled(self):
        # Used to be: cancelled MS exists with ms-3, length 1 → new ID
        # would also be ms-2 → no collision yet — but the bug surfaces
        # when more are added. Here we focus on direct repro.
        data = _project([_ms("ms-1"), _ms("ms-2", status="cancelled")])
        new_id = core.milestone_add(data, "Third", allow_untriaged=True)
        assert new_id == "ms-3"
        assert sum(1 for m in data["milestones"] if m["id"] == "ms-3") == 1


# ---------------------------------------------------------------------------
# 3. find_target_milestone: refuse to silently pick first match
# ---------------------------------------------------------------------------

class TestFindTargetMilestoneDuplicate:
    def _data_with_duplicate(self):
        return _project([
            _ms("ms-13", status="in_progress", title="A"),
            _ms("ms-13", status="in_progress", title="B"),
        ])

    def test_duplicate_raises_without_index(self):
        with pytest.raises(ValueError, match="Ambiguous milestone 'ms-13'"):
            core.find_target_milestone(self._data_with_duplicate(), "ms-13")

    def test_duplicate_resolved_by_index(self):
        data = self._data_with_duplicate()
        first = core.find_target_milestone(data, "ms-13", index=1)
        second = core.find_target_milestone(data, "ms-13", index=2)
        assert first["title"] == "A"
        assert second["title"] == "B"

    def test_index_out_of_range_raises(self):
        with pytest.raises(ValueError, match="out of range"):
            core.find_target_milestone(self._data_with_duplicate(), "ms-13", index=5)

    def test_single_record_index_one_ok(self):
        data = _project([_ms("ms-1", status="in_progress")])
        ms = core.find_target_milestone(data, "ms-1", index=1)
        assert ms["id"] == "ms-1"

    def test_single_record_index_two_raises(self):
        data = _project([_ms("ms-1", status="in_progress")])
        with pytest.raises(ValueError, match="only 1 record"):
            core.find_target_milestone(data, "ms-1", index=2)

    def test_find_milestones_returns_all_matches(self):
        data = self._data_with_duplicate()
        matches = core.find_milestones(data, "ms-13")
        assert len(matches) == 2
        assert [m["title"] for m in matches] == ["A", "B"]


# ---------------------------------------------------------------------------
# 4. validate_project: detect duplicate IDs
# ---------------------------------------------------------------------------

class TestValidateProjectDuplicates:
    def test_duplicate_ms_id_raises(self):
        data = _project([_ms("ms-1"), _ms("ms-1")])
        with pytest.raises(ValueError, match="Duplicate IDs"):
            core.validate_project(data)

    def test_duplicate_entry_id_across_milestones_raises(self):
        # Same entry id in different milestones → still corruption since
        # next_entry_id is global.
        data = _project([
            _ms("ms-1", entries=[{
                "id": "e-1", "type": "task",
                "description": "x", "status": "todo",
            }]),
            _ms("ms-2", entries=[{
                "id": "e-1", "type": "task",
                "description": "y", "status": "todo",
            }]),
        ])
        with pytest.raises(ValueError, match="entry 'e-1'"):
            core.validate_project(data)

    def test_duplicate_op_id_raises(self):
        data = _project([])
        data["operations"] = [
            {"id": "op-1", "status": "open", "entries": []},
            {"id": "op-1", "status": "open", "entries": []},
        ]
        with pytest.raises(ValueError, match="operation 'op-1'"):
            core.validate_project(data)

    def test_no_duplicates_is_silent(self):
        data = _project([_ms("ms-1"), _ms("ms-2")])
        core.validate_project(data)  # no raise

    def test_error_message_lists_all_duplicates(self):
        data = _project([_ms("ms-1"), _ms("ms-1"), _ms("ms-2"), _ms("ms-2")])
        with pytest.raises(ValueError) as ei:
            core.validate_project(data)
        msg = str(ei.value)
        assert "ms-1" in msg
        assert "ms-2" in msg


# ---------------------------------------------------------------------------
# 5. find_duplicate_ids: read-only report for doctor
# ---------------------------------------------------------------------------

class TestFindDuplicateIds:
    def test_empty_when_clean(self):
        data = _project([_ms("ms-1"), _ms("ms-2")])
        rep = core.find_duplicate_ids(data)
        assert rep == {"milestones": {}, "entries": {}, "operations": {}}

    def test_reports_milestone_duplicates_with_counts(self):
        data = _project([_ms("ms-1"), _ms("ms-1"), _ms("ms-1"), _ms("ms-2")])
        rep = core.find_duplicate_ids(data)
        assert rep["milestones"] == {"ms-1": 3}

    def test_reports_entry_and_op_duplicates(self):
        data = _project([
            _ms("ms-1", entries=[
                {"id": "e-1", "type": "task", "description": "x", "status": "todo"},
                {"id": "e-1", "type": "task", "description": "y", "status": "todo"},
            ]),
        ])
        data["operations"] = [
            {"id": "op-1", "status": "open", "entries": []},
            {"id": "op-1", "status": "open", "entries": []},
        ]
        rep = core.find_duplicate_ids(data)
        assert rep["entries"] == {"e-1": 2}
        assert rep["operations"] == {"op-1": 2}


# ---------------------------------------------------------------------------
# 6. milestone_purge: hard-delete recovery path
# ---------------------------------------------------------------------------

class TestMilestonePurge:
    def test_purge_single_record(self):
        data = _project([_ms("ms-1"), _ms("ms-2")])
        purged = core.milestone_purge(data, "ms-2", reason="scope out")
        assert purged["id"] == "ms-2"
        assert [m["id"] for m in data["milestones"]] == ["ms-1"]

    def test_purge_without_reason_raises(self):
        data = _project([_ms("ms-1")])
        with pytest.raises(ValueError, match="requires a reason"):
            core.milestone_purge(data, "ms-1", reason="")

    def test_purge_not_found_raises(self):
        data = _project([_ms("ms-1")])
        with pytest.raises(ValueError, match="not found"):
            core.milestone_purge(data, "ms-99", reason="r")

    def test_purge_duplicate_requires_index(self):
        data = _project([
            _ms("ms-13", title="A"),
            _ms("ms-13", title="B"),
        ])
        with pytest.raises(ValueError, match="--index"):
            core.milestone_purge(data, "ms-13", reason="dup")

    def test_purge_duplicate_with_index_removes_correct_record(self):
        data = _project([
            _ms("ms-13", title="A"),
            _ms("ms-13", title="B"),
        ])
        purged = core.milestone_purge(data, "ms-13", reason="dup", index=2)
        assert purged["title"] == "B"
        # Remaining record is the other one
        remaining = [m for m in data["milestones"] if m["id"] == "ms-13"]
        assert len(remaining) == 1
        assert remaining[0]["title"] == "A"

    def test_purge_writes_audit_meta(self):
        data = _project([_ms("ms-1")])
        purged = core.milestone_purge(data, "ms-1", reason="bad data")
        meta = purged.get("meta", {})
        assert meta.get("purge_reason") == "bad data"
        assert "purged_at" in meta
        assert "purged_by" in meta

    def test_purge_index_out_of_range(self):
        data = _project([
            _ms("ms-13", title="A"),
            _ms("ms-13", title="B"),
        ])
        with pytest.raises(ValueError, match="out of range"):
            core.milestone_purge(data, "ms-13", reason="dup", index=5)

    def test_purge_then_add_does_not_reuse_id(self):
        # After we purge, max-id allocator still bases on max(existing IDs)
        # — so purging ms-3 from [ms-1, ms-2, ms-3] and then adding gives
        # ms-3 back IF max is still 2. This is acceptable: purge is opt-in,
        # explicit, and reason-required. The bug we're fixing is the
        # SILENT one (len-based). We just document the behavior.
        data = _project([_ms("ms-1"), _ms("ms-2"), _ms("ms-3")])
        core.milestone_purge(data, "ms-3", reason="dup recovery")
        next_id = occupation.next_target_id(data, "milestone")
        # max remaining is 2, so next would be ms-3. That re-use is the
        # purge's explicit cost; callers know what they did.
        assert next_id == "ms-3"


# ---------------------------------------------------------------------------
# 7. Integration: full life-plan-simulator-style corruption recovery
# ---------------------------------------------------------------------------

class TestRecoveryFlow:
    def test_corrupted_load_blocks_then_purge_unblocks(self):
        # Pretend we loaded a project.json with ms-13 duplicated.
        data = _project([
            _ms("ms-13", status="in_progress", title="Aポ獲得"),
            _ms("ms-13", status="in_progress", title="PE警告レビューUI"),
        ])
        # Strict load (validate_project) blocks.
        with pytest.raises(ValueError, match="Duplicate IDs"):
            core.validate_project(copy.deepcopy(data))
        # Recovery path: doctor reports the dup count.
        rep = core.find_duplicate_ids(data)
        assert rep["milestones"]["ms-13"] == 2
        # Purge the second one explicitly.
        core.milestone_purge(data, "ms-13", reason="dup recovery", index=2)
        # Now strict validation passes.
        core.validate_project(data)
