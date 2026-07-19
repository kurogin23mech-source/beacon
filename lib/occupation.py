"""Beacon occupation adapter registry — the single ③ shared-frame dispatch
point (ms-108 e-3269).

The shared frame (③ = session-start / status / operation) has a skeleton that
is occupation-invariant ("what am I working on, what's the next move"), but the
*thing* it projects changes per occupation: development drives Milestones/Tasks,
sales drives Opportunities/Activities. Before this module those two projections
were selected by ``if profession == "sales"`` branches scattered across
``commands.py`` (~40 sites). This registry replaces that scatter with ONE
dispatch: each occupation contributes a ``project_targets(data)`` adapter, and
the frame asks the registry for the projection without knowing which occupation
it is looking at.

Import layering: ``core`` (development) and ``sales_entities`` (sales) are the
occupation adapters; both depend on ``work_model`` (the occupation-agnostic
canonical Target/WorkItem accessors). This module sits ABOVE both adapters and
is imported by ``commands.py``. Nothing the adapters import reaches back here,
so there is no import cycle.

Only ③ shared-frame surfaces use this. L1 (occupation-invariant: DM / Trek /
auth / doc / session) needs no adapter, and pure L2/L3 surfaces (development
task/milestone, sales opportunity/activity) call their own occupation's code
directly. See SPEC ``XOaDpSaFITVkZKKgPvPT`` 設計方針 4 / 6 and the reuse map
``E42bCsD7eQSrtGWX0JOF``.
"""

from __future__ import annotations

import core
import sales_entities


DEFAULT_PROFESSION = "dev"


def resolve_profession(data: dict) -> str:
    """Return the project's profession (e.g. ``"dev"`` / ``"sales"``),
    normalised to lower case. Missing / blank defaults to ``"dev"`` so legacy
    projects (written before the profession field existed) keep the
    development projection."""
    return (data.get("profession") or DEFAULT_PROFESSION).strip().lower() \
        or DEFAULT_PROFESSION


# The registry: profession -> the adapter's Target projection. Adding a new
# occupation means adding one entry here plus its ``project_targets`` adapter,
# with no change to the shared-frame callers.
PROJECTION_ADAPTERS = {
    "dev": core.project_targets,
    "sales": sales_entities.project_targets,
}


def project_targets(data: dict) -> list:
    """Return the project's Targets in the occupation-agnostic shape the shared
    frame consumes, dispatched by profession. Unknown professions fall back to
    the development projection (fail-open: show *something* rather than an empty
    frame). Each item shape is defined by the adapters — see
    ``core.project_targets`` / ``sales_entities.project_targets``."""
    prof = resolve_profession(data)
    adapter = PROJECTION_ADAPTERS.get(prof, core.project_targets)
    return adapter(data)


# The project.json keys under which each occupation stores its Target records.
# This registry is the ONE place that knows "which collections are Targets"
# across occupations; occupation-agnostic base code (work_model / work_base)
# must NOT carry these names. Shared-frame code that needs the RAW Target
# records (not the projected shape) — e.g. session_log aggregation — asks here
# instead of hardcoding the collection names itself (ms-108 e-3701 / fable
# review B-1: keep occupation knowledge in the registry layer).
TARGET_COLLECTIONS = ("milestones", "opportunities")


def iter_target_records(data: dict) -> list:
    """Return every raw Target record across occupations (development
    Milestones + sales Opportunities). A project only ever populates one of
    these collections, so callers get exactly that occupation's Targets without
    branching on profession. Used by shared-frame aggregators that walk Target
    entries (session log). Unlike ``project_targets`` this returns the records
    verbatim (with their nested ``entries``), not the projected shape."""
    records = []
    for coll in TARGET_COLLECTIONS:
        records.extend(data.get(coll, []) or [])
    return records


# Trek scope narrowing vocabulary (ms-109 e-3699 / fable review B-2).
#
# A Trek scope entry narrows to a single target inside a project. Which target
# KINDS are sliceable is occupation-specific: development slices by
# milestone / operation / task; sales by opportunity / account. Trek is L1
# (project-vision: L1 — the coordination substrate including Trek — is domain-
# invariant), and a Trek can span a development and a sales project at once. A
# scope entry carries only ``{project, <kind>: ref}`` — not the occupation —
# so the recognised vocabulary is the UNION across occupations. Registering a
# new occupation's kinds HERE (not editing trek.py) is what keeps Trek from
# hardcoding development vocabulary — the exact L1 domain-leak fable B-2 caught.
NARROWING_KINDS = {
    "dev": ("milestone", "operation", "task"),
    "sales": ("opportunity", "account"),
}


def all_narrowing_kinds() -> tuple:
    """Return the union of every occupation's Trek scope narrowing kinds,
    de-duplicated, in registration order (development first so the legacy
    identity/target_kind resolution order — milestone, operation, task — is
    unchanged for existing dev Treks; sales kinds append after)."""
    out: list = []
    for kinds in NARROWING_KINDS.values():
        for k in kinds:
            if k not in out:
                out.append(k)
    return tuple(out)


# The id prefix each narrowing kind's target ids carry. Lets a CLI
# ``project:ref`` scope argument infer the narrowing kind from the ref alone,
# so the parser does not hardcode the vocabulary (ms-109 e-3699). Keeping this
# beside NARROWING_KINDS means a new occupation registers its kinds AND their
# id prefixes in one place.
NARROWING_ID_PREFIXES = {
    "milestone": "ms-",
    "operation": "op-",
    "task": "e-",
    "opportunity": "opp-",
    "account": "acc-",
}


def narrowing_kind_for_ref(ref: str) -> str:
    """Return the narrowing kind whose id prefix ``ref`` matches, or ``""`` when
    none does. Longest matching prefix wins so ``opp-3`` resolves to
    ``opportunity`` rather than colliding with ``op-`` (operation) — though the
    current prefixes are disjoint, the longest-match rule keeps it robust if a
    future prefix nests inside another."""
    ref = (ref or "").strip()
    best_kind, best_len = "", -1
    for kind, prefix in NARROWING_ID_PREFIXES.items():
        if ref.startswith(prefix) and len(prefix) > best_len:
            best_kind, best_len = kind, len(prefix)
    return best_kind
