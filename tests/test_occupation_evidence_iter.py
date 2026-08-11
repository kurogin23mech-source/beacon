"""Unit tests for the occupation-agnostic evidence iterator + the manifest-driven
evidence write path (ms-142 T4 e-5159).

``occupation.iter_evidence`` is the 証跡-grain sibling of ``iter_work_items``: it
walks evidence across occupations by consuming ``profession_manifest``'s
``evidence_arms`` — so a caller gets dev commits AND sales Communications (target
level AND the closure grain nested under work items) through ONE read path with no
profession branch. ``occupation.add_evidence`` now CONSUMES the same declaration to
route its write, so ``evidence_arms`` is no longer a dead slot. These tests pin:

  * dev yields a milestone's ``entries`` filtered to ``type == "commit"`` — tasks
    in the SAME arm are work items and are NOT yielded.
  * sales yields communications at Target grain AND nested under the work item
    they closed, each exposing ``linked_id`` (the closure).
  * a mixed project walks both occupations without branching.
  * ``add_evidence`` resolves its arm from the manifest (``evidence_arm_for``), and
    a descriptor occupation that names its evidence arm anything still lights up.
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
                 {"id": "e-2", "type": "commit", "description": "did T1"},
                 {"id": "e-3", "type": "note", "description": "n"},
                 {"id": "e-4", "type": "commit", "description": "did more"},
             ]},
        ],
    }


def _sales():
    # opp-2 has a target-grain communication AND one nested under act-1 (closure).
    return {
        "name": "s", "profession": "sales", "milestones": [],
        "opportunities": [
            {"id": "opp-2", "label": "O", "status": "open", "phase": "lead",
             "activities": [
                 {"id": "act-1", "description": "call", "status": "todo",
                  "communications": [
                      {"id": "comm-2", "summary": "called", "linked_id": "act-1"},
                  ]},
             ],
             "communications": [
                 {"id": "comm-1", "summary": "sent deck", "linked_id": ""},
             ]},
        ],
    }


# ---------------------------------------------------------------------------
# dev: the shared entries arm — only type==commit evidence, tasks excluded.
# ---------------------------------------------------------------------------

def test_dev_yields_commits_only_not_tasks():
    items = list(occ.iter_evidence(_dev()))
    ids = [ev["id"] for ev, _t, _a in items]
    assert ids == ["e-2", "e-4"]           # the two commits, in order
    assert "e-1" not in ids                 # task (work item) NOT yielded
    assert "e-3" not in ids                 # note NOT yielded
    for _ev, target, arm in items:
        assert arm == "entries"
        assert target["id"] == "ms-1"


# ---------------------------------------------------------------------------
# sales: target-grain + the closure grain nested under the work item.
# ---------------------------------------------------------------------------

def test_sales_yields_target_and_nested_closure_grains():
    items = list(occ.iter_evidence(_sales()))
    ids = {ev["id"] for ev, _t, _a in items}
    assert ids == {"comm-1", "comm-2"}      # target-grain AND nested-under-act-1
    for _ev, target, arm in items:
        assert arm == "communications"
        assert target["id"] == "opp-2"      # owning Target for BOTH grains


def test_sales_evidence_exposes_linked_id_closure():
    by_id = {ev["id"]: ev for ev, _t, _a in occ.iter_evidence(_sales())}
    assert by_id["comm-1"]["linked_id"] == ""        # target grain, closes nothing
    assert by_id["comm-2"]["linked_id"] == "act-1"   # closes the work item act-1


# ---------------------------------------------------------------------------
# Profession-agnostic: one call walks a mixed project without branching.
# ---------------------------------------------------------------------------

def test_mixed_project_walks_both_occupations():
    data = _dev()
    data["opportunities"] = _sales()["opportunities"]
    ids = {ev["id"] for ev, _t, _a in occ.iter_evidence(data)}
    assert ids == {"e-2", "e-4", "comm-1", "comm-2"}


# ---------------------------------------------------------------------------
# Robustness: no evidence arm / empty inputs yield nothing, never crash.
# ---------------------------------------------------------------------------

def test_empty_and_missing_arms_yield_nothing():
    empty = {"name": "d", "profession": "dev",
             "milestones": [{"id": "ms-1", "title": "M", "status": "todo"}]}
    assert list(occ.iter_evidence(empty)) == []
    assert list(occ.iter_evidence({"name": "x", "profession": "dev"})) == []


# ---------------------------------------------------------------------------
# evidence_arm_for — the CONSUME side add_evidence reads.
# ---------------------------------------------------------------------------

def test_evidence_arm_for_resolves_declared_arm():
    dev, sales = _dev(), _sales()
    assert occ.evidence_arm_for(dev, "milestone") == "entries"
    assert occ.evidence_arm_for(sales, "opportunity") == "communications"
    # operations declare no evidence arm; an unknown kind is not a Target-class.
    assert occ.evidence_arm_for(dev, "operation") == ""
    assert occ.evidence_arm_for(sales, "account") == ""


def test_add_evidence_routes_through_manifest_arm():
    # add_evidence CONSUMES evidence_arm_for: the opportunity's declared arm
    # ("communications") is where a target-grain record lands, by declaration.
    data = {
        "id": "p", "profession": "sales",
        "opportunities": [{"id": "opp-1", "phase": "商談中",
                           "activities": [], "communications": []}],
        "accounts": [],
    }
    rec = occ.add_evidence(data, "opp-1", summary="s", direction="outbound",
                           channel="email", created_at="2026-08-11T00:00:00Z")
    assert data["opportunities"][0]["communications"] == [rec]
    assert rec["linked_id"] == ""
    # and iter_evidence reads it back through the same declaration.
    assert [ev["id"] for ev, _t, _a in occ.iter_evidence(data)] == [rec["id"]]
