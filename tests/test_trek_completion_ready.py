"""ms-97 / Phase 7-A — AC20 completion_ready signal + G6 meta seed (e-2660).

Pure-function tests for ``lib.trek_scheduler.is_completion_ready`` and the
G6 meta seeding contract on ``new_trek``:

  * ``new_trek`` seeds ``meta.completion_notified_at`` / ``summary_sent_at``
    / ``stall_threshold_minutes`` to ``None`` so AC20 / AC21 / AC11 read
    against a stable invariant from t=0.
  * ``is_completion_ready`` returns True iff all stamped task_states are
    terminal AND scope contains no Operation slot AND
    ``meta.completion_notified_at`` is still unstamped.
  * Idempotency: after the stamp lands, subsequent reads return False so
    the leader-digest fire never duplicates the completion marker.

Server-side fanout idempotency (= leader-digest payload carries
``completion_ready=True`` exactly once per trek lifecycle) is covered in
``tests/test_trek_scheduler_api.py`` extensions / server integration.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek as trek_mod  # noqa: E402
import trek_scheduler as scheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_trek(*,
                scope: list[dict] | None = None,
                task_states: dict | None = None,
                completion_notified_at: str | None = None,
                summary_sent_at: str | None = None) -> dict:
    """Hand-rolled trek doc so tests can pin the matrix without invoking
    full ``new_trek`` (which validates scope strictness)."""
    meta: dict = {
        "completion_notified_at": completion_notified_at,
        "summary_sent_at": summary_sent_at,
        "stall_threshold_minutes": None,
    }
    return {
        "trek_id": "tk-test",
        "title": "x",
        "status": "active",
        "scope": scope or [],
        "task_states": task_states or {},
        "meta": meta,
    }


def _pool_from_task_states(scope: list[dict], task_states: dict) -> dict:
    """Synthesize a project pool that mirrors ``task_states`` for MS-scoped
    slots so ``materialize_slots`` can expand the MS slot's children.

    ms-99 / e-2833 — the Phase 2 scheduler reads slot inventory from
    ``scope[] × project pool``. Tests that pin ``is_completion_ready``
    matrices with only ``task_states`` populated need a pool that lists
    those same task ids under the scoped milestone, so materialize can
    map cache states onto the MS children. Cache still wins over pool
    for state resolution (= status field left as "todo" is fine).
    """
    scoped_ms_ids = {s.get("milestone") for s in scope if s.get("milestone")}
    scoped_task_ids = {s.get("task") for s in scope if s.get("task")}
    ms_entries = [
        {"id": eid, "type": "task", "status": "todo"}
        for eid in task_states.keys()
        if eid not in scoped_task_ids
    ]
    milestones = [
        {"id": ms_id, "entries": ms_entries}
        for ms_id in scoped_ms_ids
    ]
    return {"milestones": milestones, "operations": []}


def _make_gp(scope: list[dict], task_states: dict):
    pool = _pool_from_task_states(scope, task_states)
    def _gp(pid: str) -> dict:
        return pool
    return _gp


# ---------------------------------------------------------------------------
# G6 — new_trek seeds the completion-flow tracking fields
# ---------------------------------------------------------------------------

def test_new_trek_seeds_completion_meta_fields():
    """G6 (e-2660) — completion_notified_at / summary_sent_at /
    stall_threshold_minutes are all seeded to None on a fresh trek."""
    t = trek_mod.new_trek(
        title="seed test",
        creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    meta = t["meta"]
    assert "completion_notified_at" in meta
    assert meta["completion_notified_at"] is None
    assert "summary_sent_at" in meta
    assert meta["summary_sent_at"] is None
    assert "stall_threshold_minutes" in meta
    assert meta["stall_threshold_minutes"] is None


def test_new_trek_cadence_minutes_coexists_with_completion_meta():
    """Cadence override and completion seed live side-by-side in meta."""
    t = trek_mod.new_trek(
        title="seed test",
        creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
        cadence_minutes=15,
    )
    meta = t["meta"]
    assert meta["cadence_minutes"] == 15
    assert meta["completion_notified_at"] is None
    assert meta["summary_sent_at"] is None


# ---------------------------------------------------------------------------
# AC20 — is_completion_ready matrix
# ---------------------------------------------------------------------------

def test_is_completion_ready_true_when_all_terminal_no_op_slot():
    """AC20 positive — all states terminal + no Op slot + unstamped."""
    scope = [{"project": "p-1", "milestone": "ms-1"}]
    task_states = {
        "e-1": {"state": "done"},
        "e-2": {"state": "user_review"},
    }
    trek = _build_trek(scope=scope, task_states=task_states)
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is True


def test_is_completion_ready_false_when_any_non_terminal():
    """Any non-terminal slot (= todo / working / leader_review) blocks fire."""
    scope = [{"project": "p-1", "milestone": "ms-1"}]
    task_states = {
        "e-1": {"state": "done"},
        "e-2": {"state": "working"},
    }
    trek = _build_trek(scope=scope, task_states=task_states)
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is False

    task_states_lr = {
        "e-1": {"state": "done"},
        "e-2": {"state": "leader_review"},
    }
    trek_lr = _build_trek(scope=scope, task_states=task_states_lr)
    assert scheduler.is_completion_ready(
        trek_lr, get_project=_make_gp(scope, task_states_lr),
    ) is False


def test_is_completion_ready_false_when_op_slot_present():
    """AC20 explicit suppression — Op slot 入り trek は terminal でも fire しない。"""
    scope = [
        {"project": "p-1", "milestone": "ms-1"},
        {"project": "p-1", "operation": "op-7"},
    ]
    task_states = {
        "e-1": {"state": "done"},
        "e-2": {"state": "user_review"},
    }
    trek = _build_trek(scope=scope, task_states=task_states)
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is False


def test_is_completion_ready_false_after_completion_notified_at_stamp():
    """Idempotent — once ``meta.completion_notified_at`` is stamped the
    helper returns False so the leader-digest never re-fires."""
    scope = [{"project": "p-1", "milestone": "ms-1"}]
    task_states = {"e-1": {"state": "done"}}
    trek = _build_trek(
        scope=scope,
        task_states=task_states,
        completion_notified_at="2026-06-28T12:00:00.000000Z",
    )
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is False


def test_is_completion_ready_false_when_no_task_states_stamped():
    """An empty ``task_states`` is not completion (= mirrors
    ``is_trek_task_aggregate_terminal``)."""
    scope = [{"project": "p-1", "milestone": "ms-1"}]
    task_states: dict = {}
    trek = _build_trek(scope=scope, task_states=task_states)
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is False


def test_is_completion_ready_pre_g6_trek_reads_meta_get_silently():
    """Pre-G6 trek doc (= no completion_notified_at field at all) reads
    as ``meta.get(...)`` = None, so the helper still returns True if the
    other conditions hold. No migration required."""
    # Strip the seeded fields to simulate a pre-G6 doc.
    scope = [{"project": "p-1", "milestone": "ms-1"}]
    task_states = {"e-1": {"state": "done"}}
    trek = _build_trek(scope=scope, task_states=task_states)
    trek["meta"] = {}  # No completion_notified_at at all.
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is True


def test_is_completion_ready_all_done_no_op_slot_with_user_review_mix():
    """Mixed terminal (= done + user_review) still fires when no Op slot."""
    scope = [
        {"project": "p-1", "milestone": "ms-1"},
        {"project": "p-2", "task": "e-2"},
    ]
    task_states = {
        "e-1": {"state": "done"},
        "e-2": {"state": "user_review"},
        "e-3": {"state": "done"},
    }
    trek = _build_trek(scope=scope, task_states=task_states)
    assert scheduler.is_completion_ready(
        trek, get_project=_make_gp(scope, task_states),
    ) is True
