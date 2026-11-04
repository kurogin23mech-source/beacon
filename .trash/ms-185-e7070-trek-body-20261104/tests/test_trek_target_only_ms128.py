"""ms-128 方針3 (v2.1): Trek Targets are target-entities, not sub-tasks.

A single task carries no review lifecycle (leader_review / user_review are
target-level), so a task-kind slot would force the state machine to
special-case it. These tests pin two guarantees:

  1. A *legacy* task-scoped slot entry read-time migrates to an MS slot
     narrowed to that one task (intent preserved, no on-disk rewrite =
     遡行変更禁止と整合).
  2. An orphan task (parent MS missing from the pool) grandfathers to an
     atomic slot rather than crashing the tick.

The new-entry rejection (cmd_trek_slot_add refuses --task) is covered by
the CLI's own guidance path; here we pin the I/O-free materialize logic
since that is where the state machine reads the truth.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from lib import trek_slots as ts  # noqa: E402


def _project(ms_id: str, tasks: list[tuple[str, str]]) -> dict:
    return {
        "milestones": [{
            "id": ms_id,
            "entries": [
                {"type": "task", "entry_id": eid, "status": status}
                for eid, status in tasks
            ],
        }],
    }


def _get_project(mapping: dict[str, dict]):
    def get_project(pid: str) -> dict:
        return mapping.get(pid) or {}
    return get_project


def test_legacy_task_scope_entry_migrates_to_ms_slot():
    proj = _project("ms-5", [("e-1", "todo"), ("e-2", "done")])
    trek_doc = {"scope": [{"project": "p1", "task": "e-1"}], "task_states": {}}

    slots = ts.materialize_slots(
        trek_doc, get_project=_get_project({"p1": proj}),
    )

    assert len(slots) == 1
    s = slots[0]
    # Migrated up to the parent target-entity (the milestone) ...
    assert s.target_kind == "milestone"
    assert s.target_id == "ms-5"
    # ... narrowed to exactly the originally-scoped task (intent preserved).
    assert s.included_task_ids == ("e-1",)
    assert s.materialized_child_ids == ("e-1",)
    # e-1 is todo → aggregate resolved_state is todo, not pulled up by e-2.
    assert s.resolved_state == "todo"


def test_migrated_task_slot_aggregate_terminal_when_task_done():
    proj = _project("ms-5", [("e-1", "todo"), ("e-2", "done")])
    trek_doc = {"scope": [{"project": "p1", "task": "e-2"}], "task_states": {}}

    slots = ts.materialize_slots(
        trek_doc, get_project=_get_project({"p1": proj}),
    )

    assert slots[0].target_kind == "milestone"
    assert slots[0].included_task_ids == ("e-2",)
    # Only e-2 is in scope and it is done → the narrowed MS slot is terminal.
    assert slots[0].resolved_state in ts.TERMINAL_TASK_STATES


def test_orphan_task_scope_entry_grandfathers_to_atomic():
    # Parent MS not present in the pool → cannot migrate; must not crash.
    proj = _project("ms-5", [("e-1", "todo")])
    trek_doc = {"scope": [{"project": "p1", "task": "e-99"}], "task_states": {}}

    slots = ts.materialize_slots(
        trek_doc, get_project=_get_project({"p1": proj}),
    )

    assert len(slots) == 1
    assert slots[0].target_kind == "task"
    assert slots[0].target_id == "e-99"


def test_operation_slot_still_atomic_and_unmigrated():
    # Operation is itself a target-entity → stays as-is (only task migrates).
    proj = {"milestones": [], "operations": [{"id": "op-1", "status": "open"}]}
    trek_doc = {"scope": [{"project": "p1", "operation": "op-1"}],
                "task_states": {}}

    slots = ts.materialize_slots(
        trek_doc, get_project=_get_project({"p1": proj}),
    )

    assert slots[0].target_kind == "operation"
    assert slots[0].target_id == "op-1"
