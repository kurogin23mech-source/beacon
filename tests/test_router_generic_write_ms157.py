"""ms-157 e-5749 (AC2 write half) — the generic target-class WRITE surface.

A descriptor-defined target-class (a new occupation added by DATA, not code) is
created and grown through ONE pair of routes with zero per-class wiring:
  POST /api/projects/{id}/targets
  POST /api/projects/{id}/targets/{target_id}/work-items

The routes drive the occupation-generic primitives (target_engine.create_target /
occupation.add_work_item), so no route names "contracts" anywhere. Built-in
classes (milestone) are explicitly rejected by the create route (they keep their
own endpoint) — the generic path stays honest, not a silent no-op.

Follows the make_router harness (build with stubs, extract the endpoint, call it
directly). ``_apply_op_and_broadcast`` is a fake that runs op() against a fixture
so the create/add logic is exercised end-to-end without a DB.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import routers_projects as rp  # noqa: E402


_CONTRACT = {
    "kind": "contract", "label": "契約", "profession": "backoffice",
    "type": "single-shot", "id_prefix": "ctr-", "collection": "contracts",
    "decomposition": {"id_field": "id", "arms": ["clauses"]},
    "phases": [{"key": "drafting", "label": "起草"}],
}


def _stub(*_a, **_k):
    return None


def _build(holder):
    """Build the router with a fake apply that runs op() on holder['data']."""
    def _apply(project_id, op, *, op_name="", actor="", reason="",
               project_file=None):
        new_data, result = op(holder["data"])
        holder["data"] = new_data
        return result

    router = rp.make_router(
        _stub,
        _load=_stub, _load_meta_only=_stub,
        _require_project_role=_stub, _require_write=_stub, _require_owner=_stub,
        _apply_op_and_broadcast=_apply,
        _resolve_author=lambda u: {}, _save=_stub,
        _broadcast_project_after_write=_stub,
        _broadcast_document_change=_stub,
        require_envelope_for_action=lambda *_a, **_k: _stub,
        is_auth_enabled=lambda: True,
    )
    eps = {}
    for r in router.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", set())
        if path == "/api/projects/{project_id}/targets" and "POST" in methods:
            eps["create"] = r.endpoint
        if (path == "/api/projects/{project_id}/targets/{target_id}/work-items"
                and "POST" in methods):
            eps["work_item"] = r.endpoint
    return eps


def _backoffice():
    return {"project_id": "p", "name": "bo", "profession": "backoffice",
            "milestones": [], "target_classes": [_CONTRACT]}


def _dev():
    return {"project_id": "p", "name": "d", "profession": "dev",
            "milestones": [{"id": "ms-1", "title": "M", "status": "in_progress",
                            "entries": []}]}


def _sales():
    return {"project_id": "p", "name": "s", "profession": "sales",
            "opportunities": [{"id": "opp-1", "label": "O", "phase": "lead",
                               "activities": []}]}


def test_generic_create_descriptor_target_zero_wiring():
    holder = {"data": _backoffice()}
    eps = _build(holder)
    result = eps["create"]("p", rp.TargetCreate(kind="contract", label="A社 NDA"),
                           {"sub": "u1"})
    assert result["kind"] == "contract"
    # the contract instance is now in the project — created through a route that
    # names no target-class, purely from the descriptor.
    contracts = holder["data"]["contracts"]
    assert len(contracts) == 1 and contracts[0]["id"] == result["id"]


def test_generic_create_rejects_builtin_kind():
    holder = {"data": _dev()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["create"]("p", rp.TargetCreate(kind="milestone", label="M"),
                      {"sub": "u1"})
    assert ei.value.status_code == 400


def test_generic_work_item_dev_routes_through_frontend_with_priority():
    # ms-167 Stage2 (e-5788): a dev task via the generic endpoint now ROUTES THROUGH
    # core.task_add (the dev frontend), so it gets the same dev-shaped stamping as the
    # /entries path — type="task", priority (mandatory per ms-126), created_at. Before
    # Stage2 the generic path called the skeleton directly and produced a task with no
    # priority / author (the silent gap e-5788 closes).
    holder = {"data": _dev()}
    eps = _build(holder)
    result = eps["work_item"]("p", "ms-1",
                              rp.WorkItemCreate(description="task X",
                                                extra={"priority": "high"}),
                              {"sub": "u1"})
    entries = holder["data"]["milestones"][0]["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == result["id"]
    assert e["description"] == "task X"
    assert e["type"] == "task"
    assert e["meta"]["priority"] == "high"   # ms-126 enforced via the frontend
    assert e["created_at"]                    # Stage1 stamp present


def test_generic_work_item_dev_missing_priority_is_400():
    # the core of the e-5788 fix: the generic path no longer silently produces a
    # priority-less dev task. Routing through core.task_add enforces ms-126 (a task
    # needs a priority), so an omitted priority is a clean 400, not a malformed entry.
    holder = {"data": _dev()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["work_item"]("p", "ms-1", rp.WorkItemCreate(description="x"),
                         {"sub": "u1"})
    assert ei.value.status_code == 400
    assert holder["data"]["milestones"][0]["entries"] == []


def test_generic_work_item_sales_routes_through_activity_frontend():
    # ms-167 Stage2: an activity via the generic endpoint routes through
    # sales_entities.activity_add — who_has_the_ball is carried and created_in_phase
    # is defaulted from the opportunity's current phase (sales frontend stamping).
    holder = {"data": _sales()}
    eps = _build(holder)
    result = eps["work_item"]("p", "opp-1",
                              rp.WorkItemCreate(
                                  description="visit",
                                  extra={"who_has_the_ball": "counterpart"}),
                              {"sub": "u1"})
    acts = holder["data"]["opportunities"][0]["activities"]
    assert len(acts) == 1
    a = acts[0]
    assert a["id"] == result["id"]
    assert a["who_has_the_ball"] == "counterpart"
    assert a["created_in_phase"] == "lead"   # defaulted from the opp's phase


def test_generic_work_item_dev_nondefault_status_is_400():
    # ms-167 Stage2 review (AX high): a frontend-owned kind creates work items in the
    # default (todo) state and has no create-time status hook. A caller-provided
    # non-default status is REJECTED (400), not silently dropped.
    holder = {"data": _dev()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["work_item"]("p", "ms-1",
                         rp.WorkItemCreate(description="x", status="done",
                                           extra={"priority": "high"}),
                         {"sub": "u1"})
    assert ei.value.status_code == 400
    assert holder["data"]["milestones"][0]["entries"] == []


def test_generic_work_item_sales_invalid_ball_is_400():
    # sales-specific validation now applies via the frontend: a bad who_has_the_ball
    # is rejected (it was silently accepted when the generic path skipped the
    # frontend and called the skeleton directly).
    holder = {"data": _sales()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["work_item"]("p", "opp-1",
                         rp.WorkItemCreate(description="x",
                                           extra={"who_has_the_ball": "bogus"}),
                         {"sub": "u1"})
    assert ei.value.status_code == 400


def test_generic_work_item_add_unknown_target_400():
    holder = {"data": _dev()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["work_item"]("p", "ms-nope",
                         rp.WorkItemCreate(description="x"), {"sub": "u1"})
    assert ei.value.status_code == 400


def test_generic_work_item_reserved_extra_key_is_400_not_500():
    # a caller putting a reserved key (status/description/item_type) in extra must
    # get a clean 400, not a TypeError-escaped 500 from the **spread collision.
    holder = {"data": _dev()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["work_item"]("p", "ms-1",
                         rp.WorkItemCreate(description="x",
                                           extra={"status": "done"}),
                         {"sub": "u1"})
    assert ei.value.status_code == 400
    # the guard fired before any write — no work item leaked in.
    assert holder["data"]["milestones"][0]["entries"] == []
