"""Tests for the ``waiting`` MS status (ms-81 e-1915).

Pins the four canonical transition cases from the SPEC AC plus the rejection
shape: only active (= in_progress) and observing milestones may move to
waiting; todo / done / cancelled cannot. Waiting then exits back through
``milestone_start`` (active) or ``milestone_update(status='observing')``.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core


def _project(status):
    return {
        "name": "test",
        "milestones": [
            {
                "id": "ms-1", "title": "Test MS", "status": status,
                "progress": 0, "target_date": "",
                "entries": [], "commits": [],
            }
        ],
    }


class TestMilestoneWaitSuccess:
    def test_active_to_waiting(self):
        data = _project("in_progress")
        ms = core.milestone_wait(data, "ms-1", reason="pause for review")
        assert ms["status"] == "waiting"
        assert ms["meta"]["waiting_reason"] == "pause for review"
        assert "waiting_at" in ms["meta"]

    def test_observing_to_waiting(self):
        data = _project("observing")
        ms = core.milestone_wait(data, "ms-1", reason="step away")
        assert ms["status"] == "waiting"

    def test_waiting_back_to_active_via_start(self):
        data = _project("waiting")
        ms = core.milestone_start(data, "ms-1")
        assert ms["status"] == "in_progress"

    def test_waiting_to_observing_via_update(self):
        data = _project("waiting")
        ms = core.milestone_update(data, "ms-1", status="observing",
                                   reason="downgrade to observe")
        assert ms["status"] == "observing"


class TestMilestoneWaitReject:
    @pytest.mark.parametrize("status", ["todo", "done", "cancelled"])
    def test_invalid_source_status(self, status):
        data = _project(status)
        with pytest.raises(ValueError, match="Cannot wait milestone"):
            core.milestone_wait(data, "ms-1", reason="r")

    def test_not_found(self):
        data = _project("in_progress")
        with pytest.raises(ValueError, match="not found"):
            core.milestone_wait(data, "ms-99", reason="r")

    def test_no_reason_does_not_set_meta_reason(self):
        # Reason gating is enforced at the CLI layer (--reason gate); the core
        # function accepts empty reason and simply omits the meta field. This
        # pins that contract so future refactors don't accidentally make the
        # gate a core-level requirement.
        data = _project("in_progress")
        ms = core.milestone_wait(data, "ms-1", reason="")
        assert ms["status"] == "waiting"
        assert "waiting_reason" not in ms.get("meta", {})
