"""Representability check: a SEQUENTIAL-TASK cycle (task1 → task2 → … → taskN → idle,
then back to task1) can be DECLARED and VALIDATED on the very same cyclic-phase
mechanism the monitoring Operation uses (ms-152 e-5485 / SPEC 方針4 スコープ担保).

This task is deliberately test-only: it does NOT add a concrete sequential-task
Operation. Its whole point is to prove the mechanism built in e-5480 (descriptor phase
adjacency, cycle-permitted) + e-5481 (graph-driven transition validator) is GENERAL —
a different persistent shape (a fixed pipeline that loops) rides it with no new code.
If this passes, the cyclic-phase machinery is not monitoring-specific.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import target_descriptor as td  # noqa: E402
import target_engine as te  # noqa: E402


# A daily 3-step pipeline that runs task1 → task2 → task3 and then returns to idle to
# await the next day — a SEQUENTIAL persistent target. It is declared with the same
# per-phase ``next`` adjacency the monitoring cycle uses; nothing here is bespoke.
SEQUENTIAL = {
    "kind": "daily_pipeline",
    "label": "順次パイプライン",
    "type": "persistent",
    "id_prefix": "seq-",
    "collection": "pipelines",
    "phases": [
        {"key": "idle", "label": "待機", "next": ["task1"]},
        {"key": "task1", "label": "ステップ1", "next": ["task2"]},
        {"key": "task2", "label": "ステップ2", "next": ["task3"]},
        {"key": "task3", "label": "ステップ3", "next": ["idle"]},   # loops home
    ],
}


def test_sequential_cycle_is_declarable_and_valid():
    # Same declaration mechanism as the monitor: an explicit adjacency graph.
    assert td.has_explicit_adjacency(SEQUENTIAL)
    assert td.validate_descriptor(SEQUENTIAL) == []          # a persistent cycle is valid
    assert td.phase_graph_has_cycle(SEQUENTIAL) is True      # idle→task1→…→task3→idle


def test_sequential_cycle_adjacency_reads_the_pipeline():
    assert td.phase_adjacency(SEQUENTIAL) == {
        "idle": ["task1"],
        "task1": ["task2"],
        "task2": ["task3"],
        "task3": ["idle"],
    }


def test_sequential_cycle_transitions_end_to_end():
    # The SAME graph-driven validator lets a target walk the whole pipeline and loop.
    data = {"name": "t"}
    rec = te.create_target(data, SEQUENTIAL, label="日次ETL")
    assert te.current_phase(rec) == "idle"
    order = []
    for _ in range(4):                       # idle → task1 → task2 → task3 → idle
        _, _, new = te.advance_target(data, SEQUENTIAL, rec["id"])
        order.append(new)
    assert order == ["task1", "task2", "task3", "idle"]
    # and it can start the next run — the loop is repeatable.
    _, _, new = te.advance_target(data, SEQUENTIAL, rec["id"])
    assert new == "task1"


def test_sequential_cycle_rejects_skipping_a_step():
    # Graph enforcement: you cannot jump task1 → task3 (task2 is not adjacent).
    data = {"name": "t"}
    rec = te.create_target(data, SEQUENTIAL, label="x")
    te.advance_target(data, SEQUENTIAL, rec["id"])           # idle → task1
    with pytest.raises(te.TargetEngineError) as ei:
        te.advance_target(data, SEQUENTIAL, rec["id"], to_phase="task3")
    assert "遷移できません" in str(ei.value)


def test_sequential_cycle_uses_the_same_legality_predicate():
    # The representability claim, made concrete: the pipeline is checked by the SAME
    # is_legal_phase_transition the monitoring cycle rides — no sequential-specific code.
    assert td.is_legal_phase_transition(SEQUENTIAL, "task3", "idle") is True   # loop home
    assert td.is_legal_phase_transition(SEQUENTIAL, "idle", "task1") is True
    assert td.is_legal_phase_transition(SEQUENTIAL, "task1", "task3") is False  # skip
