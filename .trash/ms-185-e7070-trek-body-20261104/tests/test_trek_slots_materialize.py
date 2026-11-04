"""Unit tests for ``lib/trek_slots.materialize_slots`` (ms-99 / e-2832).

Pins the Phase 2 slot inventory primitive against the SPEC edge cases
18-23 (design memo doc ``V4DdebvK4k3EqU6nV5iH``) and director
e-2830 Q1-Q4 feedback:

- Q1 supplement: ``resolved_state_source`` enum closed to
  ``{cache, pool, auto_mirror, unstamped}``.
- Q2 (a+b): auto-mirror at materialize time, idempotent,
  ``updated_by_session_id="auto-mirror"`` sentinel.
- Q4 signature: ``materialize_slots(trek_doc, *, get_project=...)``
  keyword-only so it composes with the future
  ``is_trek_task_aggregate_terminal(trek_doc, *, get_project=...)``
  contract director e-2840's xfail suite pins.
"""
from __future__ import annotations

import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from lib import trek_slots as ts  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _project(
    ms_id: str = "ms-3",
    tasks: list[tuple[str, str]] | None = None,
    operations: list[tuple[str, str]] | None = None,
) -> dict:
    """Minimal project doc shape sufficient for materialize_slots reads."""
    return {
        "milestones": [{
            "id": ms_id,
            "entries": [
                {"type": "task", "entry_id": eid, "status": status}
                for eid, status in (tasks or [])
            ],
        }],
        "operations": [
            {"id": oid, "status": status}
            for oid, status in (operations or [])
        ],
    }


def _make_get_project(mapping: dict[str, dict]):
    calls: list[str] = []

    def get_project(pid: str) -> dict:
        calls.append(pid)
        return mapping.get(pid) or {}

    get_project.calls = calls  # type: ignore[attr-defined]
    return get_project


# ---------------------------------------------------------------------------
# Signature & basic shape
# ---------------------------------------------------------------------------

def test_signature_is_keyword_only_get_project():
    # Director e-2840 xfail suite pins keyword-only get_project; make
    # positional invocation impossible so downstream call sites can't
    # silently regress into the wrong shape.
    with pytest.raises(TypeError):
        ts.materialize_slots({"scope": []}, lambda pid: {})  # type: ignore[misc]


def test_empty_scope_returns_empty_list():
    out = ts.materialize_slots({"scope": []}, get_project=lambda pid: {})
    assert out == []


def test_resolved_state_source_enum_is_closed_to_four_values():
    # Director Q1 supplement fixed the enum: enforce so drift in future
    # code doesn't quietly introduce a fifth source.
    assert set(ts.RESOLVED_STATE_SOURCES) == {
        "cache", "pool", "auto_mirror", "unstamped",
    }


def test_slot_dataclass_is_frozen():
    # Frozen so scheduler functions can concurrently read the same list
    # without racing on state mutation.
    slot = ts.Slot(
        slot_id="sl-x", project="p", target_kind="task", target_id="e-1",
        resolved_state="todo", resolved_state_source="unstamped",
        owner_session_id="", claimed_at="",
        included_task_ids=None, materialized_child_ids=(),
    )
    with pytest.raises(Exception):
        slot.resolved_state = "done"  # type: ignore[misc]


def test_auto_mirror_sentinel_is_stable():
    assert ts.AUTO_MIRROR_SESSION_ID == "auto-mirror"


# ---------------------------------------------------------------------------
# get_project cache & degradation
# ---------------------------------------------------------------------------

def test_get_project_called_once_per_unique_project_per_tick():
    # Director Q2-a: tick-scoped in-loop cache — dedup across scope
    # entries that share a project id.
    scope = [
        {"project": "p-1", "milestone": "ms-3"},
        {"project": "p-1", "task": "e-2"},
        {"project": "p-2", "operation": "op-9"},
    ]
    gp = _make_get_project({
        "p-1": _project(ms_id="ms-3", tasks=[("e-1", "todo")]),
        "p-2": _project(operations=[("op-9", "open")]),
    })
    ts.materialize_slots({"scope": scope}, get_project=gp)
    assert gp.calls.count("p-1") == 1
    assert gp.calls.count("p-2") == 1


def test_get_project_exception_degrades_to_ghost_slot():
    # Q2 discussion in memo: temporary project failure MUST NOT halt
    # tick evaluation — slots for that project become unstamped ghosts
    # so leader digest can flag them.
    def gp(pid: str) -> dict:
        raise RuntimeError("firestore transient")

    scope = [{"project": "p-1", "milestone": "ms-3"}]
    out = ts.materialize_slots({"scope": scope}, get_project=gp)
    assert len(out) == 1
    assert out[0].resolved_state_source == "unstamped"
    assert out[0].resolved_state == "todo"


# ---------------------------------------------------------------------------
# Atomic slot (task / op)
# ---------------------------------------------------------------------------

def test_operation_slot_uses_cache_when_stamped():
    # ms-128 方針3 (v2.1): operation is the surviving atomic target-entity —
    # task slots now migrate to their parent MS, so the atomic cache-first
    # resolution contract is pinned here on an operation.
    scope = [{"project": "p-1", "operation": "op-9"}]
    trek_doc = {"scope": scope, "task_states": {"op-9": {"state": "working"}}}
    gp = _make_get_project({
        "p-1": _project(operations=[("op-9", "open")]),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].target_kind == "operation"
    assert out[0].resolved_state == "working"
    assert out[0].resolved_state_source == "cache"


def test_operation_slot_uses_pool_when_no_cache_entry():
    # ms-128 方針3: atomic pool-fallback resolution pinned on an operation.
    scope = [{"project": "p-1", "operation": "op-9"}]
    trek_doc = {"scope": scope, "task_states": {}}
    gp = _make_get_project({
        "p-1": _project(operations=[("op-9", "done")]),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    # ms-128 方針5: pool-done は Trek の terminal 等価 = user_review に写す。
    assert out[0].resolved_state == "user_review"
    assert out[0].resolved_state_source == "pool"


def test_orphan_task_slot_missing_from_pool_is_unstamped():
    # ms-128 方針3: a task whose parent MS is absent from the pool cannot be
    # migrated, so it grandfathers to an atomic task slot (never crashes the
    # tick) and resolves unstamped when also absent from task_states.
    scope = [{"project": "p-1", "task": "e-nope"}]
    trek_doc = {"scope": scope}
    gp = _make_get_project({"p-1": _project(tasks=[("e-1", "todo")])})
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].target_kind == "task"
    assert out[0].resolved_state == "todo"
    assert out[0].resolved_state_source == "unstamped"


def test_operation_slot_pool_status():
    scope = [{"project": "p-1", "operation": "op-2"}]
    trek_doc = {"scope": scope}
    gp = _make_get_project({
        "p-1": _project(operations=[("op-2", "closed")]),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    # "closed" is not a Beacon task-state token, so this rolls back to
    # the "not done" pool bucket. Downstream Phase 3 may need op-specific
    # rules; the primitive stays conservative.
    assert out[0].target_kind == "operation"
    assert out[0].resolved_state_source == "pool"


# ---------------------------------------------------------------------------
# MS slot — legacy null (all-include semantics + auto-mirror)
# ---------------------------------------------------------------------------

def test_legacy_null_ms_slot_expands_all_pool_tasks():
    scope = [{"project": "p-1", "milestone": "ms-3"}]
    trek_doc = {"scope": scope}
    gp = _make_get_project({
        "p-1": _project(
            ms_id="ms-3",
            tasks=[("e-A", "todo"), ("e-B", "todo")],
        ),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].included_task_ids is None
    assert set(out[0].materialized_child_ids) == {"e-A", "e-B"}


def test_legacy_null_ms_slot_auto_mirrors_pool_done():
    # SPEC AC 4 + edge case 20: pool has a done task that Trek hasn't
    # stamped; materialize must stamp it with the sentinel session_id.
    scope = [{"project": "p-1", "milestone": "ms-3"}]
    trek_doc = {"scope": scope, "task_states": {}}
    gp = _make_get_project({
        "p-1": _project(
            ms_id="ms-3",
            tasks=[("e-A", "done"), ("e-B", "todo")],
        ),
    })
    ts.materialize_slots(trek_doc, get_project=gp)
    assert trek_doc["task_states"]["e-A"]["state"] == "done"
    assert trek_doc["task_states"]["e-A"]["auto_mirrored"] is True
    assert (
        trek_doc["task_states"]["e-A"]["updated_by_session_id"]
        == "auto-mirror"
    )
    assert "e-B" not in trek_doc["task_states"]


def test_auto_mirror_does_not_overwrite_existing_stamp():
    # Real actor decisions win over the mirror — idempotency safeguard.
    scope = [{"project": "p-1", "milestone": "ms-3"}]
    trek_doc = {
        "scope": scope,
        "task_states": {"e-A": {"state": "working"}},
    }
    gp = _make_get_project({
        "p-1": _project(ms_id="ms-3", tasks=[("e-A", "done")]),
    })
    ts.materialize_slots(trek_doc, get_project=gp)
    assert trek_doc["task_states"]["e-A"]["state"] == "working"
    assert "auto_mirrored" not in trek_doc["task_states"]["e-A"]


def test_auto_mirror_is_idempotent_on_second_call():
    scope = [{"project": "p-1", "milestone": "ms-3"}]
    trek_doc = {"scope": scope, "task_states": {}}
    gp = _make_get_project({
        "p-1": _project(ms_id="ms-3", tasks=[("e-A", "done")]),
    })
    ts.materialize_slots(trek_doc, get_project=gp)
    first_stamp = trek_doc["task_states"]["e-A"]["mirrored_at"]
    # Second call — sentinel already stamped; existing state must win.
    ts.materialize_slots(trek_doc, get_project=gp)
    assert trek_doc["task_states"]["e-A"]["mirrored_at"] == first_stamp


def test_legacy_null_ms_terminal_when_all_pool_done():
    scope = [{"project": "p-1", "milestone": "ms-3"}]
    trek_doc = {"scope": scope}
    gp = _make_get_project({
        "p-1": _project(
            ms_id="ms-3",
            tasks=[("e-A", "done"), ("e-B", "done")],
        ),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    # ms-128 方針5: 全 pool-done の legacy null MS は user_review terminal に写す。
    assert out[0].resolved_state == "user_review"
    # Auto-mirror stamped both, so at least one child registers as
    # auto_mirror source; priority pick returns pool > auto_mirror.
    assert out[0].resolved_state_source in {"pool", "auto_mirror"}


# ---------------------------------------------------------------------------
# MS slot — explicit children (opt-in)
# ---------------------------------------------------------------------------

def test_explicit_children_only_include_listed_tasks():
    scope = [{
        "project": "p-1", "milestone": "ms-3",
        "included_task_ids": ["e-A"],
    }]
    trek_doc = {"scope": scope}
    gp = _make_get_project({
        "p-1": _project(
            ms_id="ms-3",
            tasks=[("e-A", "todo"), ("e-B", "todo")],
        ),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].included_task_ids == ("e-A",)
    assert set(out[0].materialized_child_ids) == {"e-A"}


def test_explicit_children_ghost_when_missing_from_pool():
    # SPEC edge case 19: included_task_ids references a task that's no
    # longer in the pool — the slot rolls back to non-terminal so the
    # tick doesn't fire "done" on stale references.
    scope = [{
        "project": "p-1", "milestone": "ms-3",
        "included_task_ids": ["e-ghost"],
    }]
    trek_doc = {"scope": scope}
    gp = _make_get_project({
        "p-1": _project(ms_id="ms-3", tasks=[("e-A", "done")]),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    # materialized_child_ids drops the ghost since it isn't in pool_tasks;
    # aggregate rolls up empty child list → "todo" / "unstamped".
    assert out[0].materialized_child_ids == ()
    assert out[0].resolved_state == "todo"


def test_explicit_children_user_review_rolls_up_to_user_review():
    scope = [{
        "project": "p-1", "milestone": "ms-3",
        "included_task_ids": ["e-A"],
    }]
    trek_doc = {
        "scope": scope,
        "task_states": {"e-A": {"state": "user_review"}},
    }
    gp = _make_get_project({
        "p-1": _project(ms_id="ms-3", tasks=[("e-A", "done")]),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].resolved_state == "user_review"


def test_explicit_children_working_keeps_ms_non_terminal():
    scope = [{
        "project": "p-1", "milestone": "ms-3",
        "included_task_ids": ["e-A", "e-B"],
    }]
    trek_doc = {
        "scope": scope,
        "task_states": {
            "e-A": {"state": "done"},
            "e-B": {"state": "working"},
        },
    }
    gp = _make_get_project({
        "p-1": _project(
            ms_id="ms-3",
            tasks=[("e-A", "done"), ("e-B", "todo")],
        ),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].resolved_state == "todo"


# ---------------------------------------------------------------------------
# Claim propagation
# ---------------------------------------------------------------------------

def test_claim_fields_flow_through_to_slot():
    scope = [{
        "project": "p-1", "milestone": "ms-3",
        "slot_id": "sl-abcd0001",
        "claim_session_id": "sv-owner",
        "claimed_at": "2026-07-03T12:00:00Z",
    }]
    trek_doc = {"scope": scope}
    gp = _make_get_project({"p-1": _project(ms_id="ms-3")})
    out = ts.materialize_slots(trek_doc, get_project=gp)
    assert out[0].slot_id == "sl-abcd0001"
    assert out[0].owner_session_id == "sv-owner"
    assert out[0].claimed_at == "2026-07-03T12:00:00Z"


def test_missing_claim_is_empty_string_not_none():
    scope = [{"project": "p-1", "milestone": "ms-3"}]
    gp = _make_get_project({"p-1": _project(ms_id="ms-3")})
    out = ts.materialize_slots({"scope": scope}, get_project=gp)
    assert out[0].owner_session_id == ""
    assert out[0].claimed_at == ""


# ---------------------------------------------------------------------------
# Cross-shape mix (SPEC edge case 21: MS + task + op in one Trek)
# ---------------------------------------------------------------------------

def test_mixed_scope_returns_slot_per_entry():
    scope = [
        {"project": "p-1", "milestone": "ms-3"},
        {"project": "p-1", "task": "e-solo"},
        {"project": "p-2", "operation": "op-x"},
    ]
    trek_doc = {"scope": scope}
    gp = _make_get_project({
        "p-1": _project(
            ms_id="ms-3",
            tasks=[("e-A", "done"), ("e-solo", "todo")],
        ),
        "p-2": _project(operations=[("op-x", "open")]),
    })
    out = ts.materialize_slots(trek_doc, get_project=gp)
    kinds = [s.target_kind for s in out]
    # ms-128 方針3 (v2.1): the task-scoped entry (e-solo, parent ms-3) is
    # read-time migrated up to an MS slot narrowed to that task, so the
    # middle entry is now a "milestone" kind (not "task").
    assert kinds == ["milestone", "milestone", "operation"]
    assert out[1].target_id == "ms-3"
    assert out[1].included_task_ids == ("e-solo",)
