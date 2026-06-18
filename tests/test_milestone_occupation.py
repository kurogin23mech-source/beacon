"""Tests for the per-session occupation model (ms-81 e-1918).

Pins the SPEC AC #4 / #12 / #15 / #16 contracts at the core layer:

- Claiming an unoccupied MS returns ``previous_claim is None`` and stores
  the new claim on ``ms["occupation"]``.
- Re-claiming an MS that's already held by a different session returns
  the previous claim (= caller's responsibility to warn — block is
  explicitly rejected per SPEC §3-3).
- Releasing a claim removes ``ms["occupation"]`` without touching
  ``status`` (= the "active のまま専有解除" principle).
- ``milestone_record_occupation_event`` appends to a project-level
  ``worktree_sessions`` log shaped for the e-1921 audit tab; the event
  type vocabulary is locked down so future readers don't drift.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import core


def _project():
    return {
        "name": "test",
        "milestones": [
            {
                "id": "ms-1", "title": "Sample", "status": "in_progress",
                "progress": 0, "target_date": "",
                "entries": [], "commits": [],
            }
        ],
    }


class TestOccupationClaim:
    def test_claim_unoccupied(self):
        data = _project()
        ms, prev = core.milestone_claim_occupation(
            data, "ms-1", session_id="sv-a", machine="mac", agent="claude"
        )
        assert prev is None
        assert ms["occupation"]["session_id"] == "sv-a"
        assert ms["occupation"]["machine"] == "mac"
        assert "claimed_at" in ms["occupation"]

    def test_claim_already_held_returns_previous(self):
        data = _project()
        core.milestone_claim_occupation(
            data, "ms-1", session_id="sv-a", machine="mac"
        )
        _ms, prev = core.milestone_claim_occupation(
            data, "ms-1", session_id="sv-b", machine="win"
        )
        # Caller (= CLI) is expected to warn here and let user decide,
        # rather than block. Returning the previous claim is the signal.
        assert prev is not None
        assert prev["session_id"] == "sv-a"

    def test_claim_not_found(self):
        data = _project()
        with pytest.raises(ValueError, match="not found"):
            core.milestone_claim_occupation(
                data, "ms-99", session_id="sv-a"
            )


class TestOccupationRelease:
    def test_release_clears_occupation(self):
        data = _project()
        core.milestone_claim_occupation(
            data, "ms-1", session_id="sv-a"
        )
        ms, released = core.milestone_release_occupation(data, "ms-1")
        assert released is not None
        assert released["session_id"] == "sv-a"
        assert "occupation" not in ms

    def test_release_does_not_touch_status(self):
        # The core SPEC principle: occupation release leaves the MS phase
        # alone. An active MS stays active so the next session can grab it.
        data = _project()
        core.milestone_claim_occupation(data, "ms-1", session_id="sv-a")
        ms, _ = core.milestone_release_occupation(data, "ms-1")
        assert ms["status"] == "in_progress"

    def test_release_unoccupied_is_noop(self):
        data = _project()
        ms, released = core.milestone_release_occupation(data, "ms-1")
        assert released is None
        assert "occupation" not in ms


class TestWorktreeSessionsLog:
    def test_record_claim_event(self):
        data = _project()
        ev = core.milestone_record_occupation_event(
            data, ms_id="ms-1", event_type="claim",
            session_id="sv-a", machine="mac", agent="claude",
        )
        assert ev["event_type"] == "claim"
        assert ev["session_id"] == "sv-a"
        assert ev["event_id"] == "wts-1"
        assert data["worktree_sessions"] == [ev]

    def test_event_ids_are_sequential(self):
        data = _project()
        e1 = core.milestone_record_occupation_event(
            data, ms_id="ms-1", event_type="claim", session_id="sv-a"
        )
        e2 = core.milestone_record_occupation_event(
            data, ms_id="ms-1", event_type="release", session_id="sv-a",
            reason="done",
        )
        assert e1["event_id"] == "wts-1"
        assert e2["event_id"] == "wts-2"
        assert e2["reason"] == "done"

    def test_unknown_event_type_rejected(self):
        data = _project()
        with pytest.raises(ValueError, match="Invalid occupation event_type"):
            core.milestone_record_occupation_event(
                data, ms_id="ms-1", event_type="bogus",
                session_id="sv-a",
            )

    def test_takeover_event(self):
        # Per SPEC §3 a re-claim against a stale session is recorded as a
        # takeover event for the audit log.
        data = _project()
        core.milestone_record_occupation_event(
            data, ms_id="ms-1", event_type="takeover",
            session_id="sv-b", reason="superseded sv-a",
        )
        assert data["worktree_sessions"][0]["event_type"] == "takeover"
