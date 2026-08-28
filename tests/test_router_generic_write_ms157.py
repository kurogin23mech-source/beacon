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


def test_generic_work_item_add_is_cross_class():
    # add a work item to a dev milestone through the profession-agnostic route —
    # occupation.add_work_item resolves the milestone's work-item arm (entries).
    holder = {"data": _dev()}
    eps = _build(holder)
    result = eps["work_item"]("p", "ms-1",
                              rp.WorkItemCreate(description="task X"),
                              {"sub": "u1"})
    entries = holder["data"]["milestones"][0]["entries"]
    assert len(entries) == 1
    assert entries[0]["id"] == result["id"]
    assert entries[0]["description"] == "task X"


def test_generic_work_item_add_unknown_target_400():
    holder = {"data": _dev()}
    eps = _build(holder)
    with pytest.raises(HTTPException) as ei:
        eps["work_item"]("p", "ms-nope",
                         rp.WorkItemCreate(description="x"), {"sub": "u1"})
    assert ei.value.status_code == 400
