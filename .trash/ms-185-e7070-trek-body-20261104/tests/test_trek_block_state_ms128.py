"""ms-128 方針4 (e-4365) chunk 1: the ``block`` Trek target state.

Trek gains a 5th target state ``block`` = "a dependency (blocker) is unresolved,
so this target cannot be worked yet". This file pins only the *state-machine
layer* (constants, partition, transition table) — blocker edges, AND
auto-unblock, and cycle detection land in later chunks with their own tests.

Pinned invariants:
  1. ``block`` is a valid state and lives in its own partition (BLOCKED_STATES)
     so the import-time completeness assertion still covers the whole set.
  2. ``block`` is neither executor-active, leader-owned, nor terminal — it is a
     neutral wait that drives no tick (per SPEC 方針4).
  3. Transitions: {todo, working} → block (leader draws a blocker), block → todo
     (AND auto-unblock). Everything else out of / into block is rejected.
  4. ``set_task_state`` can round-trip a target todo → block → todo → working.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402


def test_block_is_a_valid_state():
    assert "block" in trek.VALID_TASK_STATES
    # canonical validation accepts it and returns it unchanged
    assert trek.validate_task_state("block") == "block"


def test_block_lives_in_its_own_partition():
    assert trek.BLOCKED_STATES == ("block",)
    # block is NOT any of the three driving partitions — it is a neutral wait.
    assert "block" not in trek.EXECUTOR_ACTIVE_STATES
    assert "block" not in trek.LEADER_OWNED_STATES
    assert "block" not in trek.TERMINAL_TASK_STATES


def test_partition_completeness_covers_block():
    # This mirrors the import-time assertion: the four partitions must exactly
    # tile VALID_TASK_STATES (no state left un-partitioned, none double-listed
    # across the driving/terminal split with block).
    covered = (
        set(trek.EXECUTOR_ACTIVE_STATES)
        | set(trek.LEADER_OWNED_STATES)
        | set(trek.TERMINAL_TASK_STATES)
        | set(trek.BLOCKED_STATES)
    )
    assert covered == set(trek.VALID_TASK_STATES)


@pytest.mark.parametrize("frm,to", [
    ("todo", "block"),      # leader draws a blocker on an un-started target
    ("working", "block"),   # leader draws a blocker on an in-flight target
    ("block", "todo"),      # AND auto-unblock returns to the normal flow
    ("block", "block"),     # idempotent re-affirmation
])
def test_allowed_block_transitions(frm, to):
    # Should not raise.
    trek.validate_task_state_transition(frm, to)


@pytest.mark.parametrize("frm,to", [
    ("block", "working"),        # cannot jump straight to working — must re-todo
    ("block", "leader_review"),
    ("block", "user_review"),
    ("leader_review", "block"),  # a handed-off target is not blockable
    ("user_review", "block"),    # a terminal target is not blockable
])
def test_rejected_block_transitions(frm, to):
    with pytest.raises(ValueError):
        trek.validate_task_state_transition(frm, to)


def test_set_task_state_round_trips_through_block():
    doc: dict = {}
    trek.set_task_state(doc, task_id="ms-1", state="block",
                        updated_by_session_id="leader")
    assert trek.get_task_state(doc, "ms-1") == "block"
    trek.set_task_state(doc, task_id="ms-1", state="todo",
                        updated_by_session_id="server")
    assert trek.get_task_state(doc, "ms-1") == "todo"
    trek.set_task_state(doc, task_id="ms-1", state="working",
                        updated_by_session_id="exec")
    assert trek.get_task_state(doc, "ms-1") == "working"


def test_block_from_working_via_set_task_state():
    doc: dict = {}
    trek.set_task_state(doc, task_id="ms-2", state="working",
                        updated_by_session_id="exec")
    trek.set_task_state(doc, task_id="ms-2", state="block",
                        updated_by_session_id="leader")
    assert trek.get_task_state(doc, "ms-2") == "block"


def test_default_state_unchanged():
    # Adding block must not disturb the implicit default for untracked targets.
    assert trek.DEFAULT_TASK_STATE == "todo"
    assert trek.get_task_state({}, "never-stamped") == "todo"
