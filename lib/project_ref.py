"""Project reference resolution — ms-97 / e-2694 dogfood fix.

Beacon mints project ids as ``<slug>-<6hex>`` (= ``life-plan-simulator-68c5df``)
but CLI users routinely type the slug alone (= ``life-plan-simulator``).
The session registry / DM fanout / Trek scope expansion all key off the
full id, so a slug input silently misses every record ("ghost member" /
no-op DM) instead of raising.

:func:`resolve_project_ref` centralises the slug → full-id expansion so
every input boundary (CLI ``--project``, ``normalize_scope_entry``,
server scope endpoint) shares one rule. Server-side has the equivalent
``_resolve_canonical_project_id`` in ``server/app.py``; this helper is
the client-side parallel and is db-agnostic for unit testing.
"""

from __future__ import annotations

from typing import Callable, Iterable, Union


ProjectRows = Iterable[dict]
ProjectLister = Union[Callable[[], ProjectRows], ProjectRows, None]


def _materialise(db_or_lister: ProjectLister) -> list[dict]:
    """Coerce ``db_or_lister`` into a concrete ``list[dict]``.

    Accepts callable, iterable, or ``None`` (= passthrough). Errors in
    the lister degrade to "no rows" so callers never crash on a
    transient DB hiccup — the resolver's contract is best-effort.
    """
    if db_or_lister is None:
        return []
    if callable(db_or_lister):
        try:
            rows = db_or_lister()
        except Exception:  # noqa: BLE001 — graceful degradation
            return []
    else:
        rows = db_or_lister
    try:
        return [r for r in rows if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        return []


def resolve_project_ref(
    ref: str,
    *,
    db_or_lister: ProjectLister = None,
) -> str:
    """Return canonical project_id from a short name or full id.

    1. ``ref`` matches an existing project_id exactly → return as-is.
    2. ``ref`` is a slug with exactly one matching ``<ref>-<hex6>`` →
       return the full id.
    3. ``ref`` matches multiple ids → raise ``ValueError`` (ambiguous).
    4. ``ref`` matches nothing → return ``ref`` unchanged (caller
       decides 404 vs accept).

    Empty input short-circuits to empty string. No lister / empty lister
    falls through to passthrough so the helper is safe in offline tests.
    """
    if not ref:
        return ref
    rows = _materialise(db_or_lister)
    if not rows:
        return ref
    ids = [(r.get("project_id") or "").strip() for r in rows]
    ids = [pid for pid in ids if pid]
    if ref in ids:
        return ref
    prefix = f"{ref}-"
    matches = [pid for pid in ids if pid.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"project ref {ref!r} is ambiguous — matches "
            f"{len(matches)} projects: {sorted(matches)!r}. "
            "Pass the full project_id to disambiguate."
        )
    return ref


def try_resolve_project_ref(
    ref: str,
    *,
    db_or_lister: ProjectLister = None,
) -> str:
    """Swallow ``ValueError`` (ambiguous) variant for server fanout.

    Used where one ambiguous slug shouldn't abort the whole loop (= the
    downstream layer surfaces its own clearer error if the unresolved
    ref doesn't work).
    """
    try:
        return resolve_project_ref(ref, db_or_lister=db_or_lister)
    except ValueError:
        return ref


__all__ = ["resolve_project_ref", "try_resolve_project_ref"]
