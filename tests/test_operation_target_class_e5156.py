"""ms-142 e-5156 (T1): Operation becomes a first-class Target-class on the
enumeration + claim spine — without silently changing the ms-143 work-item CRUD.

These tests pin the T1 裁定 contract:
  * an Operation IS enumerated by iter_target_records (列挙経路) and appears in the
    claim view (クレーム経路 → the 2-layer claim filter), which it did not before;
  * but its OperationTasks are NOT pulled into the shared work-item CRUD spine —
    ``find_target_entry`` / ``iter_work_items`` must NOT grab an operation_task, so
    ``beacon task done`` behaviour is unchanged (leader's required regression pin);
  * the ripple consumers the leader flagged stay non-breaking (deadline enumerates
    the Operation but with no date → filtered; accounts stay covered in claims).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation as occ  # noqa: E402
import claim_view  # noqa: E402
import deadline  # noqa: E402
import work_model  # noqa: E402


def _dev_with_operation():
    return {
        "name": "d", "profession": "dev",
        "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                        "entries": []}],
        "operations": [{
            "id": "op-1", "title": "Health monitoring", "label": "Health monitoring",
            "status": "open", "schedule": {"frequency": "weekdays"},
            "entries": [
                {"id": "e-1", "type": "operation_task", "status": "todo",
                 "description": "wire alert"},
                {"id": "e-2", "type": "run_record", "status": "ok",
                 "description": "ran", "batch": "b1"},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# 列挙経路: the Operation is now a walked Target record.
# ---------------------------------------------------------------------------

def test_iter_target_records_includes_operation():
    ids = [t["id"] for t in occ.iter_target_records(_dev_with_operation())]
    assert "op-1" in ids           # newly enumerated (was absent before e-5156)
    assert "ms-1" in ids           # milestone still enumerated


def test_operation_kind_resolves():
    assert work_model.target_kind("op-1") == "operation"


# ---------------------------------------------------------------------------
# Regression pin (leader): the work-item CRUD spine does NOT reach an
# operation_task, so ``beacon task done`` keeps routing operation tasks to their
# own ``operation task done`` path.
# ---------------------------------------------------------------------------

def test_find_target_entry_does_not_grab_operation_task():
    data = _dev_with_operation()
    # e-1 is an operation_task under op-1. Because operation declares no
    # work_item_arm (T1 裁定), the manifest-driven locator must NOT find it.
    assert occ.find_target_entry(data, "e-1") is None


def test_iter_work_items_excludes_operation_tasks():
    data = _dev_with_operation()
    yielded_ids = [item.get("id") for item, _target, _arm in occ.iter_work_items(data)]
    assert "e-1" not in yielded_ids


# ---------------------------------------------------------------------------
# クレーム経路: the Operation appears in the claim view (2-layer filter), and the
# claim enumeration is now manifest-sourced without dropping accounts.
# ---------------------------------------------------------------------------

def test_claim_view_covers_operation():
    data = _dev_with_operation()
    views = claim_view.build_claim_views(data, live_session_ids=set())
    assert "op-1" in views
    v = views["op-1"]
    assert v["target_kind"] == "operation"
    assert v["exists"] is True
    # no occupation claim / assignee on the operation → free to grab, not a
    # false "unknown id" negative.
    assert v["flags"]["unclaimed"] is True


def test_claim_view_still_covers_accounts():
    # Sourcing claim enumeration from the manifest must NOT drop the sales
    # secondary Target collections (accounts / acquisitions) — SPEC AC1.
    data = {"name": "s", "profession": "sales", "milestones": [],
            "accounts": [{"id": "acc-1", "label": "顧客A"}]}
    views = claim_view.build_claim_views(data, live_session_ids=set())
    assert "acc-1" in views
    assert views["acc-1"]["target_kind"] == "account"


# ---------------------------------------------------------------------------
# Ripple non-breaking (leader caution a): the Operation is enumerated for
# deadlines but carries no date, so consumers filter it out.
# ---------------------------------------------------------------------------

def test_operation_has_no_deadline_and_is_filtered():
    data = _dev_with_operation()
    op = data["operations"][0]
    assert deadline.deadline_of(op) == ""  # no deadline/target_date → UNSET
    cands = list(occ.iter_deadline_candidates(data))
    op_cands = [c for c in cands if c["target_id"] == "op-1"]
    # enumerated (kind=operation) but dateless → consumers drop it as not-due.
    assert op_cands and op_cands[0]["kind"] == "operation"
    assert deadline.deadline_of(op_cands[0]["item"]) == ""
