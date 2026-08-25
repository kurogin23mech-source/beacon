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
    # ``milestones`` is present (empty) because run_record_add allocates an entry id
    # via next_entry_id, which walks the milestones collection.
    return {"milestones": [],
            "operations": [dict({"id": "op-1", "title": "監視", "status": status,
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


def test_same_execution_phase_is_a_true_noop_no_stamp():
    # ms-152 e-5514: a same-phase move writes NO audit stamp (true no-op), so idempotent
    # calls do not pollute the trail with fake transition events.
    data = _op(execution_phase="running")
    core.operation_set_execution_phase(data, "op-1", "running")  # must not raise
    op = data["operations"][0]
    assert core.operation_execution_phase(op) == "running"
    assert "exec_running_at" not in op["meta"]      # no spurious re-stamp


def test_pause_on_already_paused_is_a_true_noop():
    # Re-pausing a paused op leaves no fresh stamp (idempotent, honest audit).
    data = _op(status="open", execution_phase="paused")
    core.operation_pause(data, "op-1", actor="human", reason="再")
    assert "exec_paused_at" not in data["operations"][0]["meta"]


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


# --- first live application: recording a run drives the cycle (e-5483) --------

def test_run_record_drives_full_cycle_from_idle_to_idle():
    # An open Operation at idle: recording a run traverses idle→due→running→idle,
    # landing back at idle ready for the next fire (the loop actually transitions).
    data = _op(status="open")           # default execution phase = idle
    core.run_record_add(data, "op-1", batch="disk", status="ok",
                        description="ディスク使用率 42%")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"
    # each hop of the loop was audit-stamped.
    meta = data["operations"][0]["meta"]
    assert meta["exec_due_at"] and meta["exec_running_at"] and meta["exec_idle_at"]


def test_run_record_completes_cycle_from_running():
    data = _op(status="open", execution_phase="running")
    core.run_record_add(data, "op-1", batch="disk", status="ok", description="x")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"


def test_run_record_on_paused_does_not_resume():
    # A run recorded against a paused monitor must NOT auto-resume it (e-5484 owns
    # paused). The cycle stays paused; only the run_record entry is appended.
    data = _op(status="open", execution_phase="paused")
    _, entry = core.run_record_add(data, "op-1", batch="disk", status="ok",
                                   description="x")
    assert core.operation_execution_phase(data["operations"][0]) == "paused"
    assert entry["type"] == "run_record"


def test_run_record_on_non_open_operation_leaves_cycle_untouched():
    # A run against a todo/in_progress definition does not move an execution cycle.
    data = _op(status="in_progress")
    core.run_record_add(data, "op-1", batch="disk", status="ok", description="x")
    assert "execution_phase" not in data["operations"][0]   # default-on-read idle


def test_run_cycle_complete_is_reusable_driver():
    # The driver walks legal edges home from any starting phase.
    data = _op(status="open", execution_phase="due")
    core.operation_run_cycle_complete(data, "op-1")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"


def test_repeated_runs_keep_looping():
    # Two runs in a row: the op returns to idle each time (the loop is repeatable).
    data = _op(status="open")
    core.run_record_add(data, "op-1", batch="b1", status="ok", description="1")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"
    core.run_record_add(data, "op-1", batch="b2", status="warning", description="2")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"
    runs = [e for e in data["operations"][0]["entries"]
            if e.get("type") == "run_record"]
    assert len(runs) == 2


# --- human-triggered pause / resume (e-5484) ---------------------------------

def test_pause_moves_to_paused_and_stamps():
    data = _op(status="open")
    core.operation_pause(data, "op-1", actor="human", reason="メンテ中")
    op = data["operations"][0]
    assert core.operation_execution_phase(op) == "paused"
    assert op["meta"]["exec_paused_by"] == "human"
    assert op["meta"]["exec_paused_reason"] == "メンテ中"


def test_pause_from_running_is_legal():
    data = _op(status="open", execution_phase="running")
    core.operation_pause(data, "op-1")
    assert core.operation_execution_phase(data["operations"][0]) == "paused"


def test_resume_returns_paused_op_to_idle():
    data = _op(status="open", execution_phase="paused")
    core.operation_resume(data, "op-1", actor="human", reason="再開")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"


def test_resume_on_idle_is_a_noop():
    data = _op(status="open", execution_phase="idle")
    core.operation_resume(data, "op-1")   # nothing to resume, no raise
    assert core.operation_execution_phase(data["operations"][0]) == "idle"


def test_resume_on_mid_cycle_raises():
    # resume only applies to a paused op — a due/running op is not "resumable".
    data = _op(status="open", execution_phase="running")
    with pytest.raises(ValueError) as ei:
        core.operation_resume(data, "op-1")
    assert "not paused" in str(ei.value)


def test_pause_resume_round_trip_then_fires_again():
    # pause suppresses; resume re-enables; the loop can then complete a run.
    data = _op(status="open")
    core.operation_pause(data, "op-1")
    core.operation_resume(data, "op-1")
    core.run_record_add(data, "op-1", batch="b", status="ok", description="x")
    assert core.operation_execution_phase(data["operations"][0]) == "idle"


# --- fire suppression: a paused Operation does not fire (AC6) -----------------

def _fire_fixture(tmp_path, monkeypatch, execution_phase=None):
    import json
    import cmd_trigger
    beacon_dir = tmp_path / ".beacon"
    beacon_dir.mkdir(parents=True)
    op = {"id": "op-1", "title": "Daily", "status": "open", "log_source": "t",
          "schedule": {"frequency": "daily",
                       "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
          "entries": [], "meta": {}}
    if execution_phase is not None:
        op["execution_phase"] = execution_phase
    project = {"name": "t", "milestones": [], "operations": [op]}
    (beacon_dir / "project.json").write_text(json.dumps(project, ensure_ascii=False))
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(beacon_dir / "project.json"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEACON_OPERATION_TRIGGER_BROADCAST", raising=False)
    # Isolate from cloud/bus: the claim wins locally, the bus push is a no-op.
    monkeypatch.setattr(cmd_trigger, "_claim_operation_fire_for_bus_push",
                        lambda op_id: True)
    monkeypatch.setattr(cmd_trigger, "_push_operation_trigger_to_bus",
                        lambda *a, **k: None)
    return cmd_trigger, beacon_dir / "triggers" / "operation_check_op-1.json"


def test_paused_operation_does_not_fire(tmp_path, monkeypatch):
    cmd_trigger, trigger_file = _fire_fixture(tmp_path, monkeypatch,
                                              execution_phase="paused")
    cmd_trigger._auto_fire_operation_triggers()
    assert not trigger_file.exists()    # suppressed


def test_non_paused_operation_fires(tmp_path, monkeypatch):
    # Control: the same op, not paused, DOES fire (proves suppression is the cause).
    cmd_trigger, trigger_file = _fire_fixture(tmp_path, monkeypatch,
                                              execution_phase="idle")
    cmd_trigger._auto_fire_operation_triggers()
    assert trigger_file.exists()
