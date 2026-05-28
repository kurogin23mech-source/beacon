"""Beacon cycle activation utilities.

Determine whether a Beacon-managed project is "riding" a given cycle (push /
deploy / retro / operation / release) based on hard evidence from the project
state. Centralizing this decision means every Skill (push/deploy/log/session-
start/retro) makes the same judgement; we never reimplement "is this project
on the push cycle?" in two different places with subtly different rules.

Reference docs:
  - CORE doc: "Beacon のサイクル活性判定原則" (doc_id `Cdg2zJtrOajm1q8adMa1`)
  - SPEC doc: "サイクル活性判定の実装方針" (doc_id `LqXEEbgsH712Z78KBELP`, ms-40)

Design principles enforced here:
  1. Hard signals only — never infer activity from indirect hints like
     "README mentions a deploy URL". The project must have at least one
     concrete record of the activity.
  2. "Never disembark" — once a cycle is active, it stays active for the
     remainder of the project's life. We do not model deactivation.
  3. Single source of truth — every Skill that asks "is this cycle active?"
     calls is_cycle_active(). Do not duplicate the per-cycle field check.

The functions in this module are intentionally pure: they take a project dict
(already loaded by the caller) and return booleans / strings. No I/O. This
makes them trivial to test and safe to call from inside transactional ops.
"""

from __future__ import annotations

from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Cycle definitions
# ---------------------------------------------------------------------------

# The set of cycles Beacon currently understands. Keep this list in lockstep
# with the SPEC doc — adding a new cycle here without updating the SPEC means
# downstream Skills may quietly not pick it up.
CYCLES: tuple[str, ...] = ("push", "deploy", "retro", "operation", "release")


# ---------------------------------------------------------------------------
# Per-cycle hard-signal predicates
# ---------------------------------------------------------------------------
#
# Each predicate accepts the project dict and an optional `documents` list
# (used by the `retro` and `release` predicates, both of which look at scope-
# tagged documents rather than fields on the project dict itself).
#
# The documents list is plumbed in separately because in cloud mode the docs
# live in a Firestore subcollection that the caller must fetch — we don't want
# this module to know about storage backends.

def _push_active(project: dict, _documents: Optional[list] = None) -> bool:
    """True if at least one push has been recorded.

    Field key: ``pushes`` (list of push record dicts as written by
    ``beacon push record``). The actual key was ``pushes`` rather than the
    conceptual ``push_records`` named in the SPEC; we follow the implementation
    here because that is the source of truth.
    """
    return len(project.get("pushes", []) or []) > 0


def _deploy_active(project: dict, _documents: Optional[list] = None) -> bool:
    """True if at least one deploy has been recorded.

    Field key: ``deployments`` (as written by ``beacon deploy record``).
    Older code paths sometimes wrote ``deploys`` directly; we accept both to
    be defensive against in-flight migrations, but only ``deployments`` is the
    canonical key.
    """
    if len(project.get("deployments", []) or []) > 0:
        return True
    # Defensive fallback for any legacy data that may still write the
    # short-form key. New code must not rely on this branch.
    return len(project.get("deploys", []) or []) > 0


def _retro_active(project: dict, documents: Optional[list] = None) -> bool:
    """True if at least one retro-scope document exists.

    The retro cycle is signalled by the presence of any document with
    ``scope == "retro"``. Callers that have the documents list pre-loaded
    pass it in; otherwise we conservatively fall back to scanning a
    ``documents`` field on the project dict if present (some test fixtures
    inline it).
    """
    docs = documents
    if docs is None:
        docs = project.get("documents") or []
    for d in docs:
        if d.get("scope") == "retro":
            return True
    return False


def _operation_active(project: dict, _documents: Optional[list] = None) -> bool:
    """True if at least one Operation has status == "open".

    Operations in todo / in_progress states are still being assembled and do
    not count as "the project is running this Operation cycle"; only `open`
    means the operation is actually being executed on a schedule.
    """
    for op in project.get("operations", []) or []:
        if op.get("status") == "open":
            return True
    return False


def _release_active(project: dict, documents: Optional[list] = None) -> bool:
    """True if the project has opted into version-rules.

    Two signals (either is sufficient):
      1. Project-level ``version_rules`` dict / non-empty value (newer schema).
      2. A CORE document with doc_id / slug == "version-rules" (the original
         opt-in mechanism used by lib/version_rules.py).

    We check both because lib/version_rules.py currently sources rules via the
    CORE doc, but the SPEC describes a project-level field for future use; we
    want activation to fire either way without requiring callers to migrate.
    """
    if project.get("version_rules"):
        return True
    # Project-level releases history is also a hard signal that someone has
    # rolled the release cycle.
    if len(project.get("releases", []) or []) > 0:
        return True
    docs = documents if documents is not None else (project.get("documents") or [])
    for d in docs:
        if d.get("scope") != "core":
            continue
        if d.get("doc_id") == "version-rules" or d.get("slug") == "version-rules":
            return True
    return False


# Dispatch table used by the public API. New cycles are added here and to
# the CYCLES tuple above. Keep the two in sync.
_PREDICATES = {
    "push": _push_active,
    "deploy": _deploy_active,
    "retro": _retro_active,
    "operation": _operation_active,
    "release": _release_active,
}


# ---------------------------------------------------------------------------
# Last-action-date extraction
# ---------------------------------------------------------------------------

def _last_iso_date(entries: Iterable[dict], date_keys: tuple[str, ...]) -> Optional[str]:
    """Return the maximum ISO date string across entries, or None.

    Each entry is checked against the supplied keys in order — we use the
    first key that exists on the entry. This lets us handle slight schema
    drift (some records use ``created_at``, others ``pushed_at`` /
    ``deployed_at``) without forcing the caller to know which key is current.
    """
    best: Optional[str] = None
    for e in entries:
        for k in date_keys:
            v = e.get(k)
            if isinstance(v, str) and v:
                if best is None or v > best:
                    best = v
                break
    return best


def _push_last(project: dict, _documents: Optional[list] = None) -> Optional[str]:
    return _last_iso_date(
        project.get("pushes", []) or [],
        ("pushed_at", "created_at", "date"),
    )


def _deploy_last(project: dict, _documents: Optional[list] = None) -> Optional[str]:
    return _last_iso_date(
        project.get("deployments", []) or project.get("deploys", []) or [],
        ("deployed_at", "created_at", "date"),
    )


def _retro_last(project: dict, documents: Optional[list] = None) -> Optional[str]:
    docs = documents if documents is not None else (project.get("documents") or [])
    return _last_iso_date(
        [d for d in docs if d.get("scope") == "retro"],
        ("updated_at", "created_at"),
    )


def _operation_last(project: dict, _documents: Optional[list] = None) -> Optional[str]:
    """Most recent operation-related timestamp.

    Returns the latest run_record date across all open operations; if no run
    records exist, falls back to the operation's opened_at. This is the
    "when did this cycle last move" timestamp that rhythm-suggestion code
    uses for "前回 deploy from N 日経過" style messaging.
    """
    best: Optional[str] = None
    for op in project.get("operations", []) or []:
        if op.get("status") != "open":
            continue
        # Walk run_record entries first
        for e in op.get("entries", []) or []:
            if e.get("type") == "run_record":
                v = e.get("date") or e.get("created_at")
                if isinstance(v, str) and v:
                    if best is None or v > best:
                        best = v
        # Fall back to opened_at
        v = op.get("opened_at")
        if isinstance(v, str) and v:
            if best is None or v > best:
                best = v
    return best


def _release_last(project: dict, documents: Optional[list] = None) -> Optional[str]:
    # Prefer explicit releases array; fall back to pushes (release cycle and
    # push cycle move together when version-rules is opted in).
    rel_last = _last_iso_date(
        project.get("releases", []) or [],
        ("released_at", "created_at", "date"),
    )
    if rel_last is not None:
        return rel_last
    return _push_last(project)


_LAST_DATE = {
    "push": _push_last,
    "deploy": _deploy_last,
    "retro": _retro_last,
    "operation": _operation_last,
    "release": _release_last,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_cycle_active(
    project: dict,
    cycle: str,
    documents: Optional[list] = None,
) -> bool:
    """Return True if `project` is currently riding `cycle`.

    Activation is a one-way door: True can flow back to False only if the
    underlying records are deleted (which Beacon does not normally do). Skills
    rely on this monotonicity when deciding whether to remember "we already
    showed the activation prompt for this cycle".

    Unknown cycles raise ValueError rather than silently returning False, so
    typos in caller code are caught early.
    """
    if cycle not in _PREDICATES:
        raise ValueError(
            f"Unknown cycle: {cycle!r}. Expected one of {CYCLES}."
        )
    return _PREDICATES[cycle](project, documents)


def list_active_cycles(
    project: dict,
    documents: Optional[list] = None,
) -> list[str]:
    """Return the list of cycle names where the project is active.

    Order matches CYCLES (push → deploy → retro → operation → release) for
    stable output that callers can render directly without re-sorting.
    """
    return [c for c in CYCLES if _PREDICATES[c](project, documents)]


def list_inactive_cycles(
    project: dict,
    documents: Optional[list] = None,
) -> list[str]:
    """Return the list of cycle names where the project is NOT active.

    Inverse of list_active_cycles(); same order. Used by session-start to
    emit the "活用可能な Beacon 機能" hint (see SPEC §"案内出力のフォーマット").
    """
    return [c for c in CYCLES if not _PREDICATES[c](project, documents)]


def cycle_last_action_date(
    project: dict,
    cycle: str,
    documents: Optional[list] = None,
) -> Optional[str]:
    """Return the most recent action date for `cycle`, or None.

    Used by rhythm-suggestion code to render "前回 push from N 日経過" prompts.
    The return value is an ISO 8601 string (date or datetime — whichever the
    underlying record stored). Callers should treat it as opaque and compare
    by string ordering, which works for both `YYYY-MM-DD` and full timestamps.
    """
    if cycle not in _LAST_DATE:
        raise ValueError(
            f"Unknown cycle: {cycle!r}. Expected one of {CYCLES}."
        )
    return _LAST_DATE[cycle](project, documents)


def cycle_status_snapshot(
    project: dict,
    documents: Optional[list] = None,
) -> dict[str, dict]:
    """Return a `{cycle: {active, last_action_date}}` snapshot for all cycles.

    Convenience wrapper used by the ``beacon cycle status --json`` CLI and by
    Skills that want to display a per-cycle dashboard line in one read. Keeps
    callers from looping over CYCLES manually and possibly missing one.
    """
    out: dict[str, dict] = {}
    for c in CYCLES:
        out[c] = {
            "active": _PREDICATES[c](project, documents),
            "last_action_date": _LAST_DATE[c](project, documents),
        }
    return out
