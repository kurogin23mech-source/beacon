"""Operation EXECUTION CYCLE — the second state arm, separate from the definition
lifecycle (ms-152 e-5482).

An ``open`` Operation RUNS in a loop (idle→due→running→idle) that a monotonic status
field cannot express. This arm lives on its own ``execution_phase`` field, advanced on
a cyclic adjacency graph via the SAME graph-driven validator the descriptor phases use,
and NEVER touches ``status`` — so "一時停止 the monitor" cannot fold the definition.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import core  # noqa: E402
import target_descriptor as td  # noqa: E402


def _op(status="open", **extra):
    return {"operations": [dict({"id": "op-1", "title": "監視", "status": status,
                                 "meta": {}}, **extra)]}


# --- default-on-read (tolerant) ---------------------------------------------

def test_execution_phase_defaults_to_idle_when_absent():
    # An Operation created before this feature has no field and reads as idle.
    op = {"id": "op-1", "status": "open"}
    assert core.operation_execution_phase(op) == "idle"
    assert core.operation_execution_phase({}) == "idle"
    assert core.operation_execution_phase(None) == "idle"   # non-dict tolerant


# --- the cycle advances on its declared graph -------------------------------

def test_monitoring_cycle_idle_due_running_idle():
    data = _op()
    core.operation_set_execution_phase(data, "op-1", "due")
    assert core.operation_execution_phase(data["operations"][0]) == "due"
    core.operation_set_execution_phase(data, "op-1", "running")
    assert core.operation_execution_phase(data["operations"][0]) == "running"
    # the back edge closes the loop — the whole point of a cyclic arm.
    core.operation_set_execution_phase(data, "op-1", "idle")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"
    # and it can go round again.
    core.operation_set_execution_phase(data, "op-1", "due")
    assert core.operation_execution_phase(data["operations"][0]) == "due"


def test_non_adjacent_execution_move_raises():
    data = _op()
    # idle → running skips the declared idle → due edge.
    with pytest.raises(ValueError) as ei:
        core.operation_set_execution_phase(data, "op-1", "running")
    msg = str(ei.value)
    assert "illegal operation execution transition" in msg
    assert "'idle'" in msg and "'running'" in msg


def test_unknown_execution_phase_raises():
    data = _op()
    with pytest.raises(ValueError) as ei:
        core.operation_set_execution_phase(data, "op-1", "bogus")
    assert "Invalid execution phase" in str(ei.value)


def test_same_execution_phase_is_a_noop():
    data = _op(execution_phase="running")
    core.operation_set_execution_phase(data, "op-1", "running")  # must not raise
    assert core.operation_execution_phase(data["operations"][0]) == "running"


# --- paused off-ramp (structure declared here; fire-suppression is e-5484) ---

def test_paused_reachable_from_active_states_and_back_to_idle():
    data = _op()
    core.operation_set_execution_phase(data, "op-1", "paused")     # idle → paused
    assert core.operation_execution_phase(data["operations"][0]) == "paused"
    core.operation_set_execution_phase(data, "op-1", "idle")       # paused → idle
    assert core.operation_execution_phase(data["operations"][0]) == "idle"
    # paused is NOT reachable straight to running.
    core.operation_set_execution_phase(data, "op-1", "paused")
    with pytest.raises(ValueError):
        core.operation_set_execution_phase(data, "op-1", "running")


# --- separation from the definition lifecycle (AC3/AC4) ----------------------

def test_execution_phase_never_touches_status():
    data = _op(status="open")
    core.operation_set_execution_phase(data, "op-1", "due")
    core.operation_set_execution_phase(data, "op-1", "running")
    # status is untouched by any execution-cycle move.
    assert data["operations"][0]["status"] == "open"


def test_definition_lifecycle_unchanged_by_this_feature():
    # The monotonic status guard still blocks reopen / backward exactly as before.
    data = _op(status="closed")
    with pytest.raises(ValueError):
        core.operation_set_status(data, "op-1", "open")
    # and a legal forward status move is independent of the execution phase.
    data2 = _op(status="todo")
    core.operation_set_status(data2, "op-1", "open")
    assert data2["operations"][0]["status"] == "open"


# --- audit stamp (AC7) -------------------------------------------------------

def test_execution_transition_stamps_who_and_when():
    data = _op()
    core.operation_set_execution_phase(data, "op-1", "due", actor="claude",
                                       reason="スケジュール到来")
    meta = data["operations"][0]["meta"]
    assert meta["exec_due_by"] == "claude"
    assert meta["exec_due_at"]                       # a timestamp was written
    assert meta["exec_due_reason"] == "スケジュール到来"
    # the exec_ prefix keeps the two arms' stamps from colliding.
    assert "due_at" not in meta                      # not the status-arm key


# --- the shared graph validator is what the cycle rides ----------------------

def test_execution_cycle_uses_shared_adjacency_validator():
    adj = {k: sorted(v) for k, v in core.OPERATION_EXECUTION_CYCLE.items()}
    assert td.adjacency_allows(adj, "running", "idle") is True     # back edge
    assert td.adjacency_allows(adj, "idle", "running") is False    # skip
    assert td.adjacency_allows(adj, "idle", "idle") is True        # no-op
    # the graph is genuinely cyclic (a persistent execution loop).
    assert "idle" in core.OPERATION_EXECUTION_CYCLE["running"]
