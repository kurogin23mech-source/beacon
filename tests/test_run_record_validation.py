"""Regression tests for Issue #29: validate_project rejected run_record entries.

PR #16 (Issue #14) started validating operation entries with _validate_entry,
but the status check was type-blind — it compared every entry's status against
the general VALID_STATUSES. run_record entries use ok/warning/error and
incident entries use open/resolved, so validate_project rejected data the
CLI itself had just written (write-read inconsistency). This locked every
command (`beacon status`, `beacon log`, ...) for any project using Operations.

The fix dispatches the status check by entry type.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core  # noqa: E402


def _project(operations=None, milestones=None):
    return {
        "name": "t",
        "milestones": milestones or [
            {"id": "ms-1", "title": "M", "status": "todo",
             "target_date": "", "commits": [], "entries": []},
        ],
        "operations": operations or [],
    }


def _op(entries):
    return {"id": "op-1", "status": "open", "title": "O", "entries": entries}


# ---------------------------------------------------------------------------
# The bug: run_record / incident statuses must validate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["ok", "warning", "error"])
def test_run_record_status_accepted(status):
    data = _project(operations=[_op([
        {"id": "e-1", "type": "run_record", "status": status, "description": "r"},
    ])])
    core.validate_project(data)  # must not raise


@pytest.mark.parametrize("status", ["open", "resolved"])
def test_incident_status_accepted(status):
    data = _project(operations=[_op([
        {"id": "e-1", "type": "incident", "status": status, "description": "i"},
    ])])
    core.validate_project(data)


def test_operation_task_status_accepted():
    data = _project(operations=[_op([
        {"id": "e-1", "type": "operation_task", "status": "todo", "description": "t"},
    ])])
    core.validate_project(data)


def test_run_record_nested_under_operation_task():
    # run_record entries can be nested; the type dispatch must recurse.
    data = _project(operations=[_op([
        {"id": "e-1", "type": "operation_task", "status": "done", "description": "t",
         "entries": [
             {"id": "e-2", "type": "run_record", "status": "warning", "description": "r"},
         ]},
    ])])
    core.validate_project(data)


# ---------------------------------------------------------------------------
# Still reject genuinely invalid statuses (per type)
# ---------------------------------------------------------------------------

def test_task_with_run_status_still_rejected():
    # A normal task must NOT be allowed to use run_record statuses.
    data = _project(milestones=[
        {"id": "ms-1", "title": "M", "status": "todo", "target_date": "",
         "commits": [], "entries": [
             {"id": "e-1", "type": "task", "status": "ok", "description": "x"},
         ]},
    ])
    with pytest.raises(ValueError, match="invalid status 'ok' for type 'task'"):
        core.validate_project(data)


def test_run_record_with_bogus_status_rejected():
    data = _project(operations=[_op([
        {"id": "e-1", "type": "run_record", "status": "nope", "description": "r"},
    ])])
    with pytest.raises(ValueError, match="invalid status 'nope' for type 'run_record'"):
        core.validate_project(data)


def test_error_message_lists_correct_enum():
    data = _project(operations=[_op([
        {"id": "e-1", "type": "run_record", "status": "bad", "description": "r"},
    ])])
    with pytest.raises(ValueError) as ei:
        core.validate_project(data)
    msg = str(ei.value)
    # the run-record enum, not the general one
    assert "ok" in msg and "warning" in msg and "error" in msg
    assert "in_progress" not in msg


# ---------------------------------------------------------------------------
# End-to-end: write a run_record then validate (the reported flow)
# ---------------------------------------------------------------------------

def test_run_record_add_then_validate_roundtrip():
    data = _project(operations=[_op([])])
    core.run_record_add(data, "op-1", batch="incremental", status="ok",
                        description="nightly")
    core.validate_project(data)  # the write-read roundtrip Issue #29 broke
