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


# ---------------------------------------------------------------------------
# Physical decomposition spec for row-oriented backends (ms-109 e-3591 / SPEC
# F7mdrDA4djd3byyDbZAv). For a backend that stores each record as its own row
# (MySQL v3), this declares — per Target collection — the id field and which
# nested "arms" (child arrays) are FAT (unbounded growth → split into their own
# rows) vs left inline in the Target row. This is the ONE place that knows the
# physical decomposition shape, so the storage layer stays occupation-agnostic
# (it reads this registry instead of hardcoding milestones/entries).
#
# sk rule (SPEC 方針 D2): a fat arm's child rows use sk = "{target_id}#{child_id}"
# when the Target has ONE fat arm (arm name implicit), else
# "{target_id}#{arm}#{child_id}". Development milestones have one arm (entries)
# so they keep the legacy 2-segment sk unchanged; sales Targets have several so
# they are arm-qualified. That the segment count differs by occupation is not an
# inconsistency to iron out but the honest consequence of "a Target's shape
# varies by occupation" (SPEC 方針 D2).
#
# Bounded arms (opportunity.gates, account.contacts / phase_history) are NOT
# listed → they ride inline in the Target row. Children nested under a fat-arm
# item (a communication under an activity) also stay inline in that item's row,
# exactly as a development commit nested under a task stays inline in the task's
# entry row. The unified attach-point model (SPEC 方針: Target↓Evidence /
# WorkItem↓Evidence / WorkItem↓WorkItem) is expressed by ``linked_id`` + this
# inline nesting, identically for both occupations.
# ---------------------------------------------------------------------------

TARGET_DECOMPOSITION = {
    "milestones":    {"id_field": "id", "arms": ("entries",)},
    "opportunities": {"id_field": "id", "arms": ("activities", "communications")},
    "accounts":      {"id_field": "id", "arms": ("nurturings", "communications")},
}


def target_child_tables() -> tuple:
    """Return the distinct child-table names across all Target collections (= the
    union of fat arm names, de-duplicated in declaration order). ``communications``
    is shared by the sales opportunity and account collections, so it appears
    once. A row-oriented backend creates one child table per name here."""
    seen: list = []
    for spec in TARGET_DECOMPOSITION.values():
        for arm in spec["arms"]:
            if arm not in seen:
                seen.append(arm)
    return tuple(seen)
