"""Unit tests for deliverable_capture — recording a completed target's produced
value onto the root deliverable-changelog (ms-161 e-5823 / SPEC 方針1, 受入条件1).

Pure-function tests over the capture bridge: hand it a project dict + a completed
target and assert (a) a milestone→機能 completion appends one active entry with the
class-declared category, (b) a class without a deliverable is a no-op (additive),
(c) the capture is idempotent (re-completion / observe→done never double-counts),
and (d) summary derivation falls back sanely. CLI wiring (cmd_milestone_done /
cmd_target._apply_transition) is asserted structurally by the import-and-call-site
test so the two seams cannot silently drop the call.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import deliverable_capture as cap  # noqa: E402
import deliverable_changelog as dc  # noqa: E402


def _dev():
    return {"name": "P", "profession": "dev", "milestones": []}


def _ms(id_="ms-42", **over):
    m = {"id": id_, "title": "DM idle-wake", "status": "done"}
    m.update(over)
    return m


# --- milestone completion → produced-value entry (受入条件1) ------------------

def test_milestone_completion_appends_active_entry():
    data = _dev()
    stored = cap.capture_target_completion(
        data, _ms(), reason="AC 全部満たした", at="2026-08-29T00:00:00Z",
        actor="claude")
    assert stored is not None
    assert stored["source"] == {"target_id": "ms-42", "kind": "milestone"}
    # category = the class-DECLARED deliverable token (not hardcoded)
    assert stored["category"] == "feature-map"
    assert stored["title"] == "DM idle-wake"
    assert stored["summary"] == "AC 全部満たした"
    # ms-161 e-5825: milestone repointed to the changelog projector (no doc ref
    # proxy), so a captured entry carries no drill-down ref.
    assert stored["ref"] == ""
    assert stored["status"] == dc.STATUS_ACTIVE
    # and it is readable as the current-state value
    assert [e["title"] for e in dc.active_deliverables(data)] == ["DM idle-wake"]


def test_summary_falls_back_when_no_reason():
    data = _dev()
    stored = cap.capture_target_completion(
        data, _ms(description="機能の説明"), reason="")
    assert stored["summary"] == "機能の説明"
    # and finally to the label when nothing else is present
    data2 = _dev()
    stored2 = cap.capture_target_completion(data2, _ms(id_="ms-7"), reason="")
    assert stored2["summary"] == stored2["title"]


# --- additive: a class without a deliverable is a no-op -----------------------

def test_no_deliverable_class_is_noop():
    data = _dev()
    # opportunity declares no deliverable today → nothing captured, log absent
    assert cap.capture_target_completion(data, {"id": "opp-3"}, reason="x") is None
    assert dc.CHANGELOG_KEY not in data


def test_missing_id_is_noop():
    data = _dev()
    assert cap.capture_target_completion(data, {}, reason="x") is None
    assert cap.capture_target_completion(data, None, reason="x") is None
    assert dc.CHANGELOG_KEY not in data


# --- idempotent: observe→done / re-run does not double-count ------------------

def test_capture_is_idempotent_for_same_target():
    data = _dev()
    first = cap.capture_target_completion(data, _ms(), reason="first")
    second = cap.capture_target_completion(data, _ms(), reason="second")
    assert first is not None
    assert second is None
    assert len(data[dc.CHANGELOG_KEY]) == 1
    # the first (current) entry is unchanged
    assert data[dc.CHANGELOG_KEY][0]["summary"] == "first"


def test_retired_predecessor_allows_recapture():
    """If the earlier entry is no longer active (retired), a later completion may
    record the produced value afresh — dedup keys on the ACTIVE set only."""
    data = _dev()
    first = cap.capture_target_completion(data, _ms(), reason="v1")
    dc.retire_deliverable(data, first["id"])
    again = cap.capture_target_completion(data, _ms(), reason="v2")
    assert again is not None
    assert len(data[dc.CHANGELOG_KEY]) == 2


# --- wiring: both completion seams call the capture (drift guard) -------------

def test_direct_completion_seam_calls_capture():
    import cmd_milestone
    src = _read_src(cmd_milestone.__file__)
    assert "deliverable_capture" in src
    assert "capture_target_completion(data, ms" in src


def test_review_gated_seam_calls_capture():
    import cmd_target
    src = _read_src(cmd_target.__file__)
    assert "deliverable_capture" in src
    assert "capture_target_completion(data, ms" in src


def test_server_api_seam_calls_capture():
    """ms-161 e-5823 (maintainability review PR#694): the web/API done endpoint is a
    THIRD completion seam that also reaches core.milestone_done — it must capture too,
    else a milestone completed via the server silently skips the deliverable log."""
    import os
    server_file = os.path.join(os.path.dirname(__file__), "..", "server",
                               "routers_projects.py")
    src = _read_src(server_file)
    assert "deliverable_capture" in src
    assert "capture_target_completion(" in src


def _read_src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()
