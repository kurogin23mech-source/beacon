"""Unit tests for lib/operation_scheduler.py — pure cadence decision for
server-fired Operations (ms-107 e-3434 chunk 3, "trek tick 相乗り")."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import operation_scheduler as osched  # noqa: E402


def _op(**meta):
    return {"id": "op-1", "status": meta.pop("status", "open"), "meta": meta}


NOW = "2026-07-15T12:00:00+00:00"


def test_never_fired_open_server_op_is_due():
    op = _op(server_tick=True)  # no last_fired_at
    assert osched.is_operation_due(op, NOW) is True


def test_non_server_tick_op_never_due():
    op = _op(server_tick=False, last_fired_at=None)
    assert osched.is_operation_due(op, NOW) is False
    op2 = _op()  # meta has no server_tick key
    assert osched.is_operation_due(op2, NOW) is False


def test_non_open_op_never_due():
    for st in ("todo", "in_progress", "closed"):
        op = _op(server_tick=True, status=st)
        assert osched.is_operation_due(op, NOW) is False


def test_cadence_gate_elapsed_vs_not():
    # default cadence 60 min. Last fired 30 min ago → not due; 90 min ago → due.
    op_recent = _op(server_tick=True, last_fired_at="2026-07-15T11:30:00+00:00")
    assert osched.is_operation_due(op_recent, NOW) is False
    op_old = _op(server_tick=True, last_fired_at="2026-07-15T10:30:00+00:00")
    assert osched.is_operation_due(op_old, NOW) is True


def test_cadence_override():
    # 10-minute cadence: last fired 15 min ago → due.
    op = _op(server_tick=True, cadence_minutes=10,
             last_fired_at="2026-07-15T11:45:00+00:00")
    assert osched.is_operation_due(op, NOW) is True
    # last fired 5 min ago → not due.
    op2 = _op(server_tick=True, cadence_minutes=10,
              last_fired_at="2026-07-15T11:55:00+00:00")
    assert osched.is_operation_due(op2, NOW) is False


def test_cadence_across_timezones_is_instant_based():
    # last fired 11:30 JST = 02:30 UTC; now 12:00 UTC → 9.5h elapsed → due.
    op = _op(server_tick=True, last_fired_at="2026-07-15T11:30:00+09:00")
    assert osched.is_operation_due(op, NOW) is True


def test_string_truthy_server_tick_accepted():
    op = _op(server_tick="true", last_fired_at="2026-07-15T10:00:00+00:00")
    assert osched.is_operation_due(op, NOW) is True


def test_bad_cadence_falls_back_to_default():
    op = _op(server_tick=True, cadence_minutes="oops",
             last_fired_at="2026-07-15T11:30:00+00:00")  # 30 min < 60 default
    assert osched.is_operation_due(op, NOW) is False


def test_select_due_operations_filters():
    ops = [
        _op(server_tick=True),                                   # due (never fired)
        _op(server_tick=False),                                  # not server-fired
        _op(server_tick=True, status="closed"),                 # closed
        _op(server_tick=True, last_fired_at="2026-07-15T11:59:00+00:00"),  # too recent
    ]
    due = osched.select_due_operations(ops, NOW)
    assert len(due) == 1 and due[0] is ops[0]


def test_bad_now_returns_not_due():
    op = _op(server_tick=True)
    assert osched.is_operation_due(op, "") is False
    assert osched.is_operation_due(op, "not-a-date") is False
