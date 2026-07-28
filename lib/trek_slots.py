"""Trek slot inventory primitive (ms-99 / e-2832).

``materialize_slots(trek_doc, *, get_project)`` walks the scope entries
of a trek and returns a list of ``Slot`` records — the single, tick-
scoped source of truth the Phase 2 scheduler consumes to decide whether
to fire executor ticks, publish leader digests, or mark the trek quiesced.

Design notes: memo doc ``V4DdebvK4k3EqU6nV5iH`` (ms-99 Phase 2 design
memo) and the SPEC in doc ``lgdevK2ZMDVNiR5LwgaF``. Director e-2830 /
e-2840 DM feedback anchors:

- Director e-2830 approval:
  - Q1 → ``resolved_state_source`` is an implementation-visible field
    surfaced via the observability diag payload, not a main-tree SPEC AC.
  - Q2 (a+b) → auto-mirror runs at materialize time, at most once per
    tick per trek, stamps ``updated_by_session_id="auto-mirror"``.
  - Q1 supplement → ``resolved_state_source`` enum is closed to
    ``{cache, pool, auto_mirror, unstamped}``.
- Director e-2840 xfail contract:
  - Future signature ``is_trek_task_aggregate_terminal(trek_doc, *,
    get_project=...)`` — this file matches by exposing the primitive as
    keyword-only so downstream refactors don't collide.

This file is data-model + resolver only. Wiring the scheduler onto it
is e-2833 (separate task). Observability emit (diag lines / leader DM
/ meta.quiesced_at) is e-2834.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

try:
    from trek import (  # type: ignore
        NARROWING_KEYS, TERMINAL_TASK_STATES,
        _scope_entry_identity_key, utcnow_iso,
        migrate_legacy_task_state,
    )
except ImportError:  # pragma: no cover — flat-layout import fallback
    from lib.trek import (  # type: ignore
        NARROWING_KEYS, TERMINAL_TASK_STATES,
        _scope_entry_identity_key, utcnow_iso,
        migrate_legacy_task_state,
    )


# ---------------------------------------------------------------------------
# Slot dataclass
# ---------------------------------------------------------------------------

# Closed enum for ``resolved_state_source`` (director Q1 supplement). Kept
# as a plain string tuple so the on-wire diag payload stays JSON-native.
RESOLVED_STATE_SOURCES: tuple = ("cache", "pool", "auto_mirror", "unstamped")

# Sentinel session_id stamped on task_states entries that materialize_slots
# auto-mirrors from the project pool (director Q2-b). "auto-mirror" is not
# a valid session_id shape (= no sv- prefix) so audit trails can filter it
# from real actor stamps.
AUTO_MIRROR_SESSION_ID: str = "auto-mirror"


@dataclass(frozen=True)
class Slot:
    """A materialized Trek slot — inventory row Phase 2 scheduler reads.

    ``frozen=True`` so the six scheduler functions in ``lib.trek_scheduler``
    can iterate over the same list concurrently without racing on state
    mutations. All mutations happen on ``trek_doc`` before or after
    materialize; the returned list is a pure re-derive.
    """
    # Identity
    slot_id: str
    project: str
    target_kind: str          # "milestone" | "operation" | "task"
    target_id: str
    # Runtime state (SPEC 方針 3)
    resolved_state: str       # one of VALID_TASK_STATES
    resolved_state_source: str  # one of RESOLVED_STATE_SOURCES
    # Ownership (SPEC 方針 4)
    owner_session_id: str     # "" when unclaimed
    claimed_at: str           # "" when unclaimed
    # MS slot child expansion (SPEC 方針 2 + AC 4)
    included_task_ids: tuple[str, ...] | None  # None = legacy null
    materialized_child_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public primitive
# ---------------------------------------------------------------------------

def materialize_slots(
    trek_doc: dict,
    *,
    get_project: Callable[[str], dict],
) -> list[Slot]:
    """Return the tick-scoped slot inventory for ``trek_doc``.

    The scope entries are the on-disk truth; project pools (fetched via
    ``get_project``) resolve the child-set of MS slots and any pool-side
    ``status="done"`` that Trek's ``task_states`` cache hasn't stamped
    yet (= legacy null MS slots get auto-mirrored, director Q2-b).

    ``get_project`` is called at most **once per unique project_id per
    tick** (director Q2-a); an in-loop cache keeps the guarantee explicit.
    Exceptions raised by ``get_project`` are absorbed as empty-project
    dicts so a temporarily-unreachable project degrades to ghost slots
    rather than halting the whole Trek's tick evaluation.

    Auto-mirror mutates ``trek_doc["task_states"]`` in place: any pool
    task that reads ``status="done"`` but has no ``task_states`` entry
    gets stamped with the ``AUTO_MIRROR_SESSION_ID`` sentinel and a fresh
    ``mirrored_at`` timestamp. The stamp is idempotent — a second
    materialize on the same tick sees the marker and skips.
    """
    scope = trek_doc.get("scope") or []
    if not scope:
        return []
    unique_pids = _unique_projects(scope)
    project_cache = _fetch_projects(get_project, unique_pids)
    task_states = trek_doc.setdefault("task_states", {})
    return [
        _materialize_one_slot(entry, project_cache, task_states)
        for entry in scope
    ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _unique_projects(scope: list[dict]) -> list[str]:
    """Dedup project_ids while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for s in scope:
        pid = s.get("project") or ""
        if pid and pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _fetch_projects(
    get_project: Callable[[str], dict], pids: Iterable[str],
) -> dict[str, dict]:
    """Call ``get_project`` at most once per pid, absorb exceptions."""
    out: dict[str, dict] = {}
    for pid in pids:
        try:
            out[pid] = get_project(pid) or {}
        except Exception:
            # Ghost project — full slot rows for this pid resolve to
            # unstamped/pool-empty and surface via leader digest.
            out[pid] = {}
    return out


def _derive_target(entry: dict) -> tuple[str, str]:
    """Return ``(target_kind, target_id)``, preferring explicit v2 fields
    over the legacy narrowing keys."""
    kind = entry.get("target_kind") or ""
    tid = entry.get("target_id") or ""
    if kind and tid:
        return kind, tid
    for k in NARROWING_KEYS:
        if entry.get(k):
            return k, entry[k]
    return "", ""


def _materialize_one_slot(
    entry: dict, project_cache: dict, task_states: dict,
) -> Slot:
    """Build a single Slot from a scope entry + pool + cache."""
    project = entry.get("project") or ""
    kind, target_id = _derive_target(entry)
    slot_id = entry.get("slot_id") or ""
    owner_sid = entry.get("claim_session_id") or ""
    claimed_at = entry.get("claimed_at") or ""

    project_doc = project_cache.get(project) or {}

    if kind == "task":
        # ms-128 方針3 (v2.1): Trek Targets are target-entities (target-class
        # instances = milestone / operation / …), never sub-tasks. A single
        # task carries no review lifecycle (leader_review / user_review are
        # target-level), so a task-kind slot would force the state machine to
        # special-case it. Read-time migrate a legacy task-scoped entry to an
        # MS slot narrowed to that one task via included_task_ids — intent is
        # preserved ("Trek just this task") with no on-disk rewrite (=
        # 遡行変更禁止と整合). A ghost task (parent MS missing) grandfathers to
        # an atomic slot below so a stale entry never crashes the tick.
        parent_ms = _find_task_parent_ms(project_doc, target_id)
        if parent_ms:
            migrated = dict(entry)
            migrated.pop("task", None)
            migrated["target_kind"] = "milestone"
            migrated["target_id"] = parent_ms
            migrated["milestone"] = parent_ms
            migrated["included_task_ids"] = (target_id,)
            return _materialize_ms_slot(
                entry=migrated, project=project, project_doc=project_doc,
                target_id=parent_ms, slot_id=slot_id,
                owner_sid=owner_sid, claimed_at=claimed_at,
                task_states=task_states,
            )

    if kind == "milestone":
        return _materialize_ms_slot(
            entry=entry, project=project, project_doc=project_doc,
            target_id=target_id, slot_id=slot_id,
            owner_sid=owner_sid, claimed_at=claimed_at,
            task_states=task_states,
        )
    # Non-MS narrowing: operation (target-entity), or a ghost task that could
    # not be migrated above. There's no child list; the slot's resolved_state
    # comes from the task_states cache directly, or from the pool as a
    # fallback when un-stamped.
    return _materialize_atomic_slot(
        project=project, kind=kind, target_id=target_id,
        slot_id=slot_id, owner_sid=owner_sid, claimed_at=claimed_at,
        project_doc=project_doc, task_states=task_states,
    )


def _materialize_ms_slot(
    *,
    entry: dict, project: str, project_doc: dict, target_id: str,
    slot_id: str, owner_sid: str, claimed_at: str, task_states: dict,
) -> Slot:
    """MS slot: expand children (explicit or legacy null), auto-mirror,
    aggregate resolved_state."""
    child_ids_raw = entry.get("included_task_ids")
    if child_ids_raw is None:
        included_task_ids = None
    else:
        included_task_ids = tuple(child_ids_raw)

    pool_tasks = _pool_ms_tasks(project_doc, target_id)
    if included_task_ids is None:
        # Legacy null semantics: all pool tasks are members (SPEC AC 4).
        materialized_ids = tuple(pool_tasks.keys())
        # Auto-mirror at materialize time (director Q2 confirmation): any
        # pool task with status=done that lacks a task_states entry gets
        # stamped now, so aggregate-terminal reads truthfully. Idempotent
        # via the AUTO_MIRROR_SESSION_ID marker.
        _auto_mirror_done_children(pool_tasks, task_states)
    else:
        # Explicit list: only these children matter; pool-missing ids
        # become ghost states via _resolve_child_state.
        materialized_ids = tuple(
            eid for eid in included_task_ids if eid in pool_tasks
        )

    resolved_state, source = _aggregate_ms_state(
        materialized_ids, pool_tasks, task_states,
    )
    return Slot(
        slot_id=slot_id, project=project, target_kind="milestone",
        target_id=target_id, resolved_state=resolved_state,
        resolved_state_source=source, owner_session_id=owner_sid,
        claimed_at=claimed_at, included_task_ids=included_task_ids,
        materialized_child_ids=materialized_ids,
    )


def _materialize_atomic_slot(
    *,
    project: str, kind: str, target_id: str, slot_id: str,
    owner_sid: str, claimed_at: str,
    project_doc: dict, task_states: dict,
) -> Slot:
    """Task or op slot — no expansion; resolve state cache-first then pool."""
    cache_entry = task_states.get(target_id)
    if isinstance(cache_entry, dict) and cache_entry.get("state"):
        # ms-128 方針5: read-time migrate legacy "done" → "user_review"。
        resolved_state = migrate_legacy_task_state(
            str(cache_entry.get("state") or "")
        )
        source = "cache"
    else:
        pool_status = _pool_status(project_doc, kind, target_id)
        if pool_status is None:
            resolved_state, source = "todo", "unstamped"
        elif pool_status == "done":
            # ms-128 方針5: pool-done = Trek terminal 等価 = user_review。
            resolved_state, source = "user_review", "pool"
        else:
            resolved_state, source = "todo", "pool"
    return Slot(
        slot_id=slot_id, project=project, target_kind=kind,
        target_id=target_id, resolved_state=resolved_state,
        resolved_state_source=source, owner_session_id=owner_sid,
        claimed_at=claimed_at, included_task_ids=None,
        materialized_child_ids=(),
    )


def _pool_ms_tasks(project_doc: dict, ms_id: str) -> dict[str, dict]:
    """Return ``{entry_id: task_dict}`` for tasks under milestone ``ms_id``.

    Empty dict when the milestone or project is missing (ghost path).
    Walks nested ``entries[]`` since Beacon MS entries can carry child
    entries (task / commit / other).
    """
    ms = _find_milestone(project_doc, ms_id)
    if ms is None:
        return {}
    out: dict[str, dict] = {}
    _collect_tasks(ms.get("entries") or [], out)
    return out


def _find_task_parent_ms(project_doc: dict, task_id: str) -> str | None:
    """Return the milestone id that owns ``task_id``, or None (ghost).

    ms-128 方針3 (v2.1): used to read-time migrate a legacy task-scoped Trek
    slot up to its parent target-entity (the milestone). Every Beacon task
    lives under exactly one milestone, so a task's "Trek this task" intent is
    losslessly expressible as an MS slot narrowed via included_task_ids.
    """
    if not task_id:
        return None
    for ms in project_doc.get("milestones") or []:
        ms_id = ms.get("id") or ms.get("entry_id") or ""
        if not ms_id:
            continue
        if task_id in _pool_ms_tasks(project_doc, ms_id):
            return ms_id
    return None


def _find_milestone(project_doc: dict, ms_id: str) -> dict | None:
    for ms in project_doc.get("milestones") or []:
        if (ms.get("id") or ms.get("entry_id")) == ms_id:
            return ms
    return None


def _collect_tasks(entries: list[dict], out: dict[str, dict]) -> None:
    for e in entries or []:
        if e.get("type") == "task":
            eid = e.get("entry_id") or e.get("id") or ""
            if eid:
                out[eid] = e
        # Tasks may have nested sub-entries in some legacy shapes; recurse
        # so we don't drop grandchild tasks silently.
        nested = e.get("entries") or []
        if nested:
            _collect_tasks(nested, out)


def _pool_status(
    project_doc: dict, kind: str, target_id: str,
) -> str | None:
    """Return the pool's status string for a task/op target, or None."""
    if kind == "operation":
        for op in project_doc.get("operations") or []:
            if (op.get("id") or op.get("entry_id")) == target_id:
                return op.get("status") or None
        return None
    if kind == "task":
        for ms in project_doc.get("milestones") or []:
            found: dict[str, dict] = {}
            _collect_tasks(ms.get("entries") or [], found)
            if target_id in found:
                return (found[target_id].get("status") or None)
    return None


def _auto_mirror_done_children(
    pool_tasks: dict[str, dict], task_states: dict,
) -> None:
    """Stamp done in ``task_states`` for pool-done tasks lacking an entry.

    Director Q2 confirmed: materialize-time auto-mirror, idempotent via
    ``AUTO_MIRROR_SESSION_ID``. This helper never overwrites an existing
    non-auto-mirror stamp — real actor decisions win over the mirror.
    """
    now = utcnow_iso()
    for eid, task in pool_tasks.items():
        if (task.get("status") or "") != "done":
            continue
        existing = task_states.get(eid)
        if isinstance(existing, dict) and existing.get("state"):
            # Someone already stamped a state (any state) — respect it.
            continue
        task_states[eid] = {
            "state": "done",
            "auto_mirrored": True,
            "mirrored_at": now,
            "mirrored_from_project_pool": True,
            "updated_by_session_id": AUTO_MIRROR_SESSION_ID,
        }


def _resolve_child_state(
    eid: str, pool_tasks: dict[str, dict], task_states: dict,
) -> tuple[str, str]:
    """Return ``(state, source)`` for a single MS child task."""
    cache_entry = task_states.get(eid)
    if isinstance(cache_entry, dict) and cache_entry.get("state"):
        # ms-128 方針5: read-time migrate legacy "done" → "user_review"
        # (Trek は user_review で打ち止め、done は Trek 外)。
        state = migrate_legacy_task_state(str(cache_entry.get("state") or ""))
        # A task_states entry stamped by auto-mirror still counts as
        # auto_mirror-sourced downstream, so the diag can tell it apart
        # from human decisions.
        if cache_entry.get("updated_by_session_id") == AUTO_MIRROR_SESSION_ID:
            return state, "auto_mirror"
        return state, "cache"
    task = pool_tasks.get(eid)
    if task is None:
        # Ghost: mentioned by included_task_ids but not present in pool
        # (SPEC edge case 19).
        return "todo", "unstamped"
    pool_status = task.get("status") or ""
    if pool_status == "done":
        # ms-128 方針5: pool-done = Trek の terminal 等価 = user_review
        # (= 「pool で done = 手前まで運び終えた」を Trek 状態に写す)。
        return "user_review", "pool"
    return "todo", "pool"


def _aggregate_ms_state(
    child_ids: tuple[str, ...],
    pool_tasks: dict[str, dict],
    task_states: dict,
) -> tuple[str, str]:
    """Roll up an MS slot's child states into ``(state, source)``.

    Rules (matches SPEC 方針 3 tick judgment semantics):
    - No children → ``("todo", "unstamped")`` so unclaim-todo readers
      still see the slot as work-to-do rather than done-by-accident.
    - Any child not in TERMINAL_TASK_STATES → the MS is not terminal.
    - All children terminal → MS is terminal; ``resolved_state`` is
      "done" unless every child sits in "user_review", in which case
      the MS surfaces "user_review" so leader_review_queue readers can
      see the halt line.
    - ``resolved_state_source`` is the highest-priority source across
      children in the order ``pool > auto_mirror > cache > unstamped``
      so debugging can spot pool-driven decisions at a glance.
    """
    if not child_ids:
        return "todo", "unstamped"
    seen_sources: set[str] = set()
    terminal = True
    all_user_review = True
    for eid in child_ids:
        state, src = _resolve_child_state(eid, pool_tasks, task_states)
        seen_sources.add(src)
        if state not in TERMINAL_TASK_STATES:
            terminal = False
        if state != "user_review":
            all_user_review = False
    if not terminal:
        return "todo", _pick_source_priority(seen_sources)
    return (
        ("user_review" if all_user_review else "done"),
        _pick_source_priority(seen_sources),
    )


_SOURCE_PRIORITY: tuple = ("pool", "auto_mirror", "cache", "unstamped")


def _pick_source_priority(sources: set[str]) -> str:
    for s in _SOURCE_PRIORITY:
        if s in sources:
            return s
    return "unstamped"
