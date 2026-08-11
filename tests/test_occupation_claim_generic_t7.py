"""ms-142 T7 (e-5162): the live occupation claim ("someone is sitting at this
target right now") is target-class-GENERIC, not milestone-only. Before T7 the
write path (``core.milestone_claim_occupation``) only stamped ``data['milestones']``,
so two agents touching a non-milestone target (an operation / a release) silently
double-worked. These tests pin that the generic primitive stamps + the reader
surfaces a live collision for every target-class."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402
import claim_view as cv  # noqa: E402


def _project():
    return {
        "profession": "dev",
        "milestones": [{"id": "ms-1", "status": "in_progress"}],
        "operations": [{"id": "op-1", "status": "open"}],
        "release_targets": [
            {"id": "rel-1", "kind": "release", "status": "in_progress",
             "phase": "draft"}],
    }


def test_claim_occupation_stamps_every_target_class():
    data = _project()
    for tid in ("ms-1", "op-1", "rel-1"):
        rec, previous = core.claim_occupation(
            data, tid, session_id="sv-A", machine="m", agent="a")
        assert previous is None
        assert rec["occupation"]["session_id"] == "sv-A"
        assert rec["occupation"]["machine"] == "m"


def test_claim_returns_previous_for_collision_warning():
    data = _project()
    core.claim_occupation(data, "op-1", session_id="sv-A")
    _rec, previous = core.claim_occupation(data, "op-1", session_id="sv-B")
    # The takeover path: the caller gets the prior claim so it can warn.
    assert previous is not None and previous["session_id"] == "sv-A"


def test_release_occupation_clears_any_target_class():
    data = _project()
    core.claim_occupation(data, "rel-1", session_id="sv-A")
    rec, released = core.release_occupation(data, "rel-1", reason="close")
    assert released["session_id"] == "sv-A"
    assert "occupation" not in rec
    # idempotent: releasing an unoccupied target returns None, no raise.
    _rec2, again = core.release_occupation(data, "rel-1")
    assert again is None


def test_claim_not_found_raises():
    data = _project()
    import pytest
    with pytest.raises(ValueError, match="not found"):
        core.claim_occupation(data, "nope-9", session_id="s")


def test_release_leaves_status_untouched():
    data = _project()
    core.claim_occupation(data, "op-1", session_id="sv-A")
    core.release_occupation(data, "op-1", reason="x")
    assert data["operations"][0]["status"] == "open"  # unchanged


def test_claim_view_flags_live_collision_for_non_milestone():
    # The reader was already generic (T1); T7 makes the WRITE generic, so a
    # non-milestone target now actually carries a claim the reader can flag.
    data = _project()
    core.claim_occupation(data, "op-1", session_id="sv-other")
    core.claim_occupation(data, "rel-1", session_id="sv-other")
    views = cv.build_claim_views(data, live_session_ids=["sv-other"])
    for tid in ("op-1", "rel-1"):
        assert tid in views, tid
        assert views[tid]["flags"]["live_by_others"] is True, tid


def test_milestone_wrappers_delegate_to_generic():
    # The milestone-named entry points still work (delegate to the generic).
    data = _project()
    _ms, prev = core.milestone_claim_occupation(data, "ms-1", session_id="sv-A")
    assert prev is None
    assert data["milestones"][0]["occupation"]["session_id"] == "sv-A"
    _ms2, released = core.milestone_release_occupation(data, "ms-1")
    assert released["session_id"] == "sv-A"
