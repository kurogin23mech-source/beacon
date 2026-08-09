"""Unit tests for the occupation-agnostic work-item iterator (ms-142 e-5009).

``occupation.iter_work_items`` walks planned work items across occupations by
consuming ``profession_manifest``'s ``work_item_arm`` — so a caller gets dev
tasks AND sales activities through ONE read path with no profession branch.
These tests pin:

  * dev yields a milestone's ``entries`` filtered to ``type == "task"`` — commits
    in the SAME arm are evidence and are NOT yielded (leader pin, e-5009).
  * sales yields an opportunity's whole ``activities`` arm (item_type None) — the
    contrast to dev's shared, type-filtered arm.
  * each yield carries the parent Target (so a caller resolves target context,
    e.g. the deadline recipient) and the arm name.
  * a mixed project walks both occupations without branching; armless / empty
    inputs yield nothing rather than crashing.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation as occ  # noqa: E402


def _dev():
    return {
        "name": "d", "profession": "dev",
        "milestones": [
            {"id": "ms-1", "title": "M", "status": "in_progress",
             "occupation": {"session_id": "sv-dev"},
             "entries": [
                 {"id": "e-1", "type": "task", "description": "T1",
                  "status": "todo"},
                 {"id": "e-2", "type": "commit", "description": "did T1",
                  "status": "done"},
                 {"id": "e-3", "type": "task", "description": "T2",
                  "status": "todo"},
                 {"id": "e-4", "type": "note", "description": "n"},
             ]},
        ],
    }


def _sales():
    return {
        "name": "s", "profession": "sales", "milestones": [],
        "opportunities": [
            {"id": "opp-1", "label": "O", "status": "open", "phase": "lead",
             "occupation": {"session_id": "sv-sales"},
             "activities": [
                 {"id": "act-1", "description": "call", "status": "todo"},
                 {"id": "act-2", "description": "demo", "status": "todo"},
             ],
             "communications": [
                 {"id": "comm-1", "description": "sent deck"},
             ]},
        ],
    }


# ---------------------------------------------------------------------------
# dev: a shared entries arm — only type==task work items, commits excluded.
# ---------------------------------------------------------------------------

def test_dev_yields_tasks_only_not_commits():
    items = list(occ.iter_work_items(_dev()))
    ids = [wi["id"] for wi, _t, _a in items]
    assert ids == ["e-1", "e-3"]           # the two tasks, in order
    assert "e-2" not in ids                 # commit (evidence) NOT yielded
    assert "e-4" not in ids                 # note NOT yielded
    for _wi, _t, arm in items:
        assert arm == "entries"


def test_dev_yields_parent_target():
    (_wi, target, _arm) = next(iter(occ.iter_work_items(_dev())))
    assert target["id"] == "ms-1"
    # the parent Target carries the claiming session — a deadline caller reads it
    # off the yielded target with no second lookup.
    assert target["occupation"]["session_id"] == "sv-dev"


# ---------------------------------------------------------------------------
# sales: the whole activities arm is work items (item_type None) — the contrast.
# ---------------------------------------------------------------------------

def test_sales_yields_whole_activities_arm():
    items = list(occ.iter_work_items(_sales()))
    ids = [wi["id"] for wi, _t, _a in items]
    assert ids == ["act-1", "act-2"]
    for _wi, target, arm in items:
        assert arm == "activities"
        assert target["id"] == "opp-1"      # not communications (evidence)


def test_sales_excludes_communications_arm():
    ids = [wi["id"] for wi, _t, _a in occ.iter_work_items(_sales())]
    assert "comm-1" not in ids              # communications is an evidence arm


# ---------------------------------------------------------------------------
# Profession-agnostic: one call walks a mixed project without branching.
# ---------------------------------------------------------------------------

def test_mixed_project_walks_both_occupations():
    data = _dev()
    data["opportunities"] = _sales()["opportunities"]
    ids = {wi["id"] for wi, _t, _a in occ.iter_work_items(data)}
    assert ids == {"e-1", "e-3", "act-1", "act-2"}


# ---------------------------------------------------------------------------
# Robustness: empty / missing arms yield nothing, never crash.
# ---------------------------------------------------------------------------

def test_empty_and_missing_arms_yield_nothing():
    empty = {"name": "d", "profession": "dev",
             "milestones": [{"id": "ms-1", "title": "M", "status": "todo"}]}
    assert list(occ.iter_work_items(empty)) == []
    assert list(occ.iter_work_items({"name": "x", "profession": "dev"})) == []


# ---------------------------------------------------------------------------
# iter_deadline_candidates — the shared deadline enumeration (ms-142 e-5010):
# Target level + work items, each carrying kind / target_id / target_status /
# recipient / context for the two consumers (server reminder + deadline due CLI).
# ---------------------------------------------------------------------------

def test_deadline_candidates_dev_target_then_workitems():
    cands = list(occ.iter_deadline_candidates(_dev()))
    kinds = [(c["kind"], c["label"]) for c in cands]
    # milestone (Target level) first, then its two tasks (commit/note excluded).
    assert kinds == [("milestone", "M"), ("task", "T1"), ("task", "T2")]
    ms_cand = cands[0]
    assert ms_cand["target_id"] == "ms-1"
    assert ms_cand["target_status"] == "in_progress"
    task_cand = cands[1]
    assert task_cand["target_status"] == "in_progress"   # parent Target's status
    assert task_cand["recipient"] == "sv-dev"
    assert task_cand["context"] == "ms-1 / e-1"


def test_deadline_candidates_sales_activity():
    cands = list(occ.iter_deadline_candidates(_sales()))
    act = [c for c in cands if c["kind"] == "activity"]
    assert [c["label"] for c in act] == ["call", "demo"]
    assert act[0]["target_id"] == "opp-1"
    assert act[0]["recipient"] == "sv-sales"


def test_deadline_candidates_carry_parent_terminal_status():
    # target_status reflects the PARENT Target so a display can drop work items
    # under a done/cancelled Target without the enumerator imposing the policy.
    data = _dev()
    data["milestones"][0]["status"] = "done"
    for c in occ.iter_deadline_candidates(data):
        assert c["target_status"] == "done"
