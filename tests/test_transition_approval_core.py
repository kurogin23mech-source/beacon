"""Data-layer tests for the transition-approval primitive wired into core.py.

Verifies entry-id allocation + target lookup + persistence + find_entry
integration for milestone (dev) and operation (ops), and that unsupported
targets (sales opportunity — handled by the existing judge flow) raise.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core


def _fresh_data():
    return {
        "milestones": [
            {"id": "ms-5", "title": "T", "status": "in_progress", "entries": []},
        ],
        "operations": [
            {"id": "op-3", "title": "O", "status": "open", "entries": []},
        ],
    }


def test_add_persists_pending_entry_on_milestone():
    data = _fresh_data()
    eid = core.target_transition_approval_add(
        data, "ms-5", old_state="in_progress", new_state="done",
        intent="AC 3/3 満たし本番2週 incident ゼロ", evidence=["e-1", "e-2"],
        actor="kida",
    )
    ms = data["milestones"][0]
    assert len(ms["entries"]) == 1
    e = ms["entries"][0]
    assert e["id"] == eid
    assert e["type"] == "target-transition-approval"
    assert e["status"] == "pending"
    assert e["meta"]["target_kind"] == "milestone"
    assert e["meta"]["new_state"] == "done"
    assert e["meta"]["evidence"] == ["e-1", "e-2"]


def test_approve_returns_new_state_for_caller_to_apply():
    data = _fresh_data()
    eid = core.target_transition_approval_add(
        data, "ms-5", old_state="in_progress", new_state="done", intent="…",
    )
    entry, new_state = core.target_transition_approval_approve(
        data, eid, rationale="OK", actor="kida",
    )
    assert new_state == "done"                 # caller executes milestone_update(status="done")
    assert entry["status"] == "approved"
    assert entry["meta"]["approval_status"] == "approved"
    # the milestone itself is NOT transitioned by approve (caller's job)
    assert data["milestones"][0]["status"] == "in_progress"


def test_reject_does_not_return_new_state():
    data = _fresh_data()
    eid = core.target_transition_approval_add(
        data, "ms-5", old_state="in_progress", new_state="done", intent="…",
    )
    entry = core.target_transition_approval_reject(
        data, eid, rationale="AC5 未達", actor="kida",
    )
    assert entry["status"] == "cancelled"
    assert entry["meta"]["approval_status"] == "rejected"


def test_operation_target_supported():
    data = _fresh_data()
    eid = core.target_transition_approval_add(
        data, "op-3", old_state="open", new_state="closed", intent="役目終了",
    )
    op = data["operations"][0]
    assert op["entries"][0]["id"] == eid
    assert op["entries"][0]["meta"]["target_kind"] == "operation"


def test_unsupported_target_raises():
    data = _fresh_data()
    # sales opportunity is intentionally NOT wired here (existing judge is its gate)
    try:
        core.target_transition_approval_add(
            data, "opp-7", old_state="lead", new_state="qualified", intent="x",
        )
        assert False, "expected ValueError for unsupported target"
    except ValueError:
        pass


def test_approve_wrong_entry_type_raises():
    data = _fresh_data()
    # a non-approval entry
    data["milestones"][0]["entries"].append({"id": "e-99", "type": "task", "meta": {}})
    try:
        core.target_transition_approval_approve(data, "e-99")
        assert False, "expected ValueError"
    except ValueError:
        pass
