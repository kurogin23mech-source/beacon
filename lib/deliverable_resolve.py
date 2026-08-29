"""Deliverable-projection RESOLVER — the I/O layer that turns a deliverable
projection SPEC (a pointer) into the ACTUAL produced value (ms-155 e-5602).

``occupation.project_deliverables`` is deliberately PURE: it unions the declared
deliverable SPECS of every adopted target-class, carrying POINTERS
(``{target_class, kind, label, projector, ref}``) with no I/O. This module is its
I/O counterpart — the "配管" (plumbing) that the pure union delegates content
resolution to. Two strategies mirror ``target_descriptor.DELIVERABLE_PROJECTORS``:

  - ``"doc"``    — the produced value IS a named document; resolve ``ref`` to the
                   document's real content (milestone→機能 = application-map).
  - ``"rollup"`` — the produced value is a roll-up over the class's DELIVERED
                   Targets (count + labels); compute it from the live project.

Keeping resolution OUT of the pure union preserves ``project_deliverables`` /
``root_target.synthesized_projection`` as side-effect-free (a caller that only
needs the SHAPE — gate derivation, retro grouping — pays no I/O), while a caller
that needs the VALUE calls ``resolve_project_deliverables`` here.

WHY this module exists (ms-155 e-5602): the independent philosophy review of PR
#677 flagged that the pure docstring delegated ref→content resolution to a
"session-start assembler" that was never written, so the projection carried
pointers no consumer resolved — the MS's "生み出した価値を一級次元に" had shrunk
to "価値への POINTER を一級次元に". This module is that missing resolver, and it
also closes the sibling finding that ``rollup`` was an allowed projector with no
resolver (a class could declare a hollow roll-up deliverable): a rollup spec now
resolves to a real summary.
"""
from __future__ import annotations

import occupation as _occ
import target_descriptor as _td
import deliverable_map as _dmap
from store import get_store

# Statuses that mean a Target of ANY class has DELIVERED its value (so it counts
# toward the produced roll-up). ``cancelled`` is a delete, not a delivery; open
# states (todo / in_progress / waiting) have not produced yet. This is a
# pragmatic cross-class set — a class cannot yet DECLARE which of its states
# count as "delivered"; when one needs to diverge, this becomes a per-class
# descriptor field (follow-up task e-5667). Kept explicit (not "everything not
# open") so a new non-terminal state is not silently counted as delivered. The
# tokens are Target STATUS values (milestone done/observing/closed, sales 成約/
# won, release released) — NOT claim outcomes; ``"completed"`` here is a generic
# terminal Target status, distinct from ``claims`` outcome vocab.
_DELIVERED_STATES = frozenset({
    "done", "observing", "closed", "released", "won", "成約", "completed",
})

# Cap on labels carried in a rollup summary so a class with thousands of Targets
# does not balloon the resolved payload; the count is always exact.
_ROLLUP_LABEL_CAP = 100


def _resolve_doc(spec: dict) -> dict:
    """Resolve a ``"doc"`` deliverable: fetch the document named by ``ref`` and
    return its real content. A missing / empty ref, or a ref that resolves to no
    document, returns ``found=False`` with an explanatory ``error`` rather than
    crashing — a stale deliverable pointer surfaces LOUDLY at resolve time (the
    silent-miss the review warned about)."""
    ref = (spec.get("ref") or "").strip()
    if not ref:
        return {"strategy": _td.PROJECTOR_DOC, "found": False, "ref": ref,
                "error": "empty ref (nothing to resolve)"}
    doc = get_store().get_document(ref)
    if not doc:
        return {"strategy": _td.PROJECTOR_DOC, "found": False, "ref": ref,
                "error": f"document '{ref}' not found"}
    return {
        "strategy": _td.PROJECTOR_DOC,
        "found": True,
        "ref": ref,
        "title": doc.get("title") or ref,
        "content": doc.get("content") or "",
        "updated_at": doc.get("updated_at") or "",
    }


def _resolve_rollup(spec: dict, data: dict) -> dict:
    """Resolve a ``"rollup"`` deliverable: compute a roll-up summary over the
    producing class's DELIVERED Targets (count + labels). The producing class is
    the spec's ``target_class`` (the class that DECLARED the deliverable), not its
    ``kind`` (the deliverable's own type token, e.g. ``"pipeline"``).

    Matching relies on ``occupation.project_targets`` tagging every row with
    ``kind`` = its target-class kind (occupation.py ~L330), so the spec's
    ``target_class`` and a row's ``kind`` are the SAME namespace — if that
    guarantee ever breaks, this filter miscounts rather than crashes, hence the
    equivalence is pinned here explicitly."""
    target_kind = (spec.get("target_class") or "").strip()
    rows = [r for r in _occ.project_targets(data)
            if (r.get("kind") or "") == target_kind]
    delivered = [r for r in rows
                 if (r.get("status") or "") in _DELIVERED_STATES]
    labels = [r.get("label") or r.get("id") or "" for r in delivered]
    return {
        "strategy": _td.PROJECTOR_ROLLUP,
        "found": True,
        "count_total": len(rows),
        "count_delivered": len(delivered),
        "labels": labels[:_ROLLUP_LABEL_CAP],
        "labels_truncated": len(labels) > _ROLLUP_LABEL_CAP,
    }


def _resolve_changelog(data: dict) -> dict:
    """Resolve a ``"changelog"`` deliverable (ms-161 e-5825): the produced value IS
    the root deliverable-changelog, summarised to its current-state map. milestone→
    機能 rides this — the resolved value is the DERIVED application-map (the active
    entries grouped by category, dev-rendered), so ``deliverable list --resolve``
    shows what the project can do NOW straight from the log, with NO hand-maintained
    doc pointer. Always ``found`` (an empty log resolves to an empty-but-valid map,
    not a miss — there is nothing that can fail to resolve)."""
    summary = _dmap.summarize_map(data)
    return {
        "strategy": _td.PROJECTOR_CHANGELOG,
        "found": True,
        "count_active": summary["total"],
        "categories": [
            {"category": g["category"], "count": g["count"]}
            for g in summary["categories"]
        ],
        "rendered": _dmap.render_map(data),
    }


def resolve_deliverable_content(data: dict, spec: dict) -> dict:
    """Resolve ONE deliverable-projection spec to its produced VALUE.

    Returns the spec augmented with a ``resolved`` block — the actual content
    (doc body / roll-up summary / changelog-derived map) — so a caller keeps the
    pointer (``projector`` / ``ref``) alongside what it resolved to. An unknown
    projector returns ``resolved.found=False`` (defensive; ``normalize_deliverable``
    already keeps unknown projectors out of the union, so this only guards a
    hand-built spec)."""
    projector = (spec.get("projector") or "").strip()
    if projector == _td.PROJECTOR_DOC:
        resolved = _resolve_doc(spec)
    elif projector == _td.PROJECTOR_ROLLUP:
        resolved = _resolve_rollup(spec, data)
    elif projector == _td.PROJECTOR_CHANGELOG:
        resolved = _resolve_changelog(data)
    else:
        resolved = {"strategy": projector, "found": False,
                    "error": f"no resolver for projector '{projector}'"}
    return {**spec, "resolved": resolved}


def resolve_project_deliverables(data: dict | None) -> list:
    """The I/O counterpart to ``occupation.project_deliverables``: the SAME union
    of adopted-class deliverables, each entry's pointer RESOLVED to its actual
    produced value (ms-155 e-5602).

    ``data is None`` returns ``[]`` — matching ``project_deliverables``'s None
    tolerance, so a caller that learned None is safe on the pure side does not
    hit a crash here."""
    if data is None:
        return []
    return [resolve_deliverable_content(data, s)
            for s in _occ.project_deliverables(data)]
