"""Map projector — summarise the root deliverable-changelog into a current-state
map, rendered per profession (ms-161 e-5824 / SPEC 方針4).

    map = summarize( root.deliverable_log where status==active
                     group by category )  render per profession

The pipeline splits cleanly into a PROFESSION-INDEPENDENT summary and a
PROFESSION-SPECIFIC render (SPEC 方針2 / 受入条件3+5):

- ``summarize_map`` groups the ACTIVE deliverables (``deliverable_changelog
  .active_deliverables`` — retired/superseded already fell out) by category into a
  stable, occupation-agnostic structure. This is the "現在地" data — no dev/sales
  vocabulary, so every profession summarises the same way.
- ``render_map`` turns that summary into text for a given profession. The dev
  render produces an application-map-flavoured index (方針4: dev render =
  application-map フォーマット); other professions get a generic render until their
  own is authored (SPEC やらない: 営業/backoffice render は道筋のみ). Adding a
  profession render is a new branch here, never a change to ``summarize_map``.

Import layering: a bridge above ``occupation`` (for profession resolution) and
``deliverable_changelog`` (for the active view). Nothing imports this except the
CLI/consumer layer, so no cycle.
"""
from __future__ import annotations

import re

import deliverable_changelog as _dc
import occupation as _occ


# Dev-specific pretty headings for known category tokens. Lives in the DEV render
# concern only (not in the profession-independent summary): a category token is
# what the target-class DECLARES (milestone→``feature-map``), and only the dev
# render decides to show it as 「機能」. An unknown token falls back to itself, so
# a new dev category renders (as its token) rather than vanishing.
_DEV_CATEGORY_HEADINGS = {
    "feature-map": "機能 — 何ができるか",
}

# ms-161 e-5851: a surface-grained deliverable entry carries its machine-checkable
# WEDGE(s) in ``tags`` (SPEC 方針2: ``tags`` = "surface area 等"). A wedge is a
# ``type:ident`` token in the 4-surface vocabulary check-map-drift reconciles
# against the real CLI/API/Skill/file surfaces — so the DERIVED application-map
# keeps the exact same machine safety net the hand-maintained doc had. This RE is
# the SAME 4-type set as ``scripts/check-map-drift.py`` (WEDGE_RE); the two must
# stay in lockstep — the derived doc's wedges are meaningless if the reconciler
# does not recognise the same types.
_WEDGE_TAG_RE = re.compile(r"^(cli|api|skill|file):")

# An ``area:<heading>`` tag names the 大節 (top-level map section) an entry belongs
# to — a DISPLAY grouping key for the dev render only, NOT a wedge (its ``area:``
# prefix is outside ``_WEDGE_TAG_RE`` so check-map-drift never mistakes it for a
# surface to reconcile). Absent → the entry renders under its 小節 (category) with
# no top-level header.
_AREA_TAG_PREFIX = "area:"


def _entry_wedges(entry: dict) -> list:
    """The surface wedges (``cli:``/``api:``/``skill:``/``file:`` tokens) carried in
    an entry's ``tags``, in listed order. These are emitted as backtick
    ``type:ident`` tokens by the dev render so the derived map is machine-reconciled
    by check-map-drift exactly like the hand-maintained doc was (e-5851 楔維持)."""
    return [t for t in (entry.get("tags") or [])
            if isinstance(t, str) and _WEDGE_TAG_RE.match(t)]


def _entry_area(entry: dict) -> str:
    """The 大節 heading an entry belongs to (from its ``area:<heading>`` tag), or
    ``""`` if none. Used only to emit top-level section headers in the dev render;
    it is not a wedge and never reaches check-map-drift as one."""
    for t in (entry.get("tags") or []):
        if isinstance(t, str) and t.startswith(_AREA_TAG_PREFIX):
            return t[len(_AREA_TAG_PREFIX):].strip()
    return ""


def _is_auto_completion(entry: dict) -> bool:
    """True for a coarse auto-capture completion entry (ms-161 e-5902). These carry
    ``deliverable_changelog.AUTO_COMPLETION_TAG`` and are OUTCOME-granularity ("this
    milestone shipped"), not surface-granularity — so the dev render keeps them OUT
    of the surface index (shown in a separate 完遂 section) and the map stays a
    surface-単位 capability 索引, not a list of 完了理由."""
    return _dc.AUTO_COMPLETION_TAG in (entry.get("tags") or [])


def summarize_map(data: dict) -> dict:
    """The PROFESSION-INDEPENDENT current-state summary (方針4 の芯).

    Groups the active deliverables by ``category``, preserving first-seen category
    order (append order) so the output is stable and diffable. Shape::

        {
          "profession": "dev",
          "categories": [
            {"category": "feature-map", "count": 2, "entries": [ <entry>, ... ]},
            ...
          ],
          "total": 3,
        }

    ``entries`` are the active changelog rows (copies, via ``active_deliverables``)
    so a consumer sees each produced value's ``title`` / ``summary`` / ``ref`` /
    ``source``. Only ``active`` entries are here — the map never lists a retired or
    superseded capability (方針3: 要約 = 現在地)."""
    active = _dc.active_deliverables(data)
    order: list = []
    buckets: dict = {}
    for entry in active:
        cat = entry.get("category") or ""
        if cat not in buckets:
            buckets[cat] = []
            order.append(cat)
        buckets[cat].append(entry)
    return {
        "profession": _occ.resolve_profession(data),
        "categories": [
            {"category": cat, "count": len(buckets[cat]), "entries": buckets[cat]}
            for cat in order
        ],
        "total": len(active),
    }


def render_map(data: dict, *, profession: str | None = None) -> str:
    """Render the current-state map as text for ``profession`` (方針4 render 段).

    ``profession`` defaults to the project's own (``occupation.resolve_profession``);
    pass it explicitly to preview another profession's render. The dev render is
    the application-map-flavoured index; any other profession gets the generic
    render (a labelled placeholder that lists the same summary without a
    profession-specific shape) until its render is authored."""
    summary = summarize_map(data)
    prof = (profession or summary["profession"] or "dev").strip().lower()
    if prof == "dev":
        return _render_dev(summary)
    return _render_generic(summary, prof)


def _render_dev(summary: dict) -> str:
    """Dev render = application-map-flavoured index (方針4). This is the shape ms-161
    re-homes application-map onto (e-5825/e-5851): the doc becomes THIS render's
    output, so append/retire move the map with no hand-maintenance.

    Structure (e-5851 節構造維持): entries group by ``category`` (the 小節,
    ``### heading``), and a category's entries may declare an ``area:`` tag (the
    大節, ``## heading``) — a top-level header is emitted whenever the area changes,
    reproducing the hand-maintained map's 大節/小節 nesting. Category first-seen
    order (from ``summarize_map``) preserves the seeded document order, so a backfill
    that appends in map order renders in map order.

    Each produced value is a 散文 bullet: ``- <summary> [→ ref] <wedges>``. The
    machine-checkable WEDGES (``tags`` in the 4-surface vocabulary) are emitted as
    backtick ``type:ident`` tokens so check-map-drift reconciles the DERIVED doc
    exactly like the hand-maintained one (e-5851 楔維持). An entry with no wedges
    (e.g. a coarse milestone-completion entry) renders as before — backward
    compatible with the pre-e-5851 shape."""
    lines = ["# アプリケーション全貌マップ（deliverable-changelog 導出）", ""]
    if summary["total"] == 0:
        lines.append("_(まだ記録された成果がありません — milestone を完遂すると"
                     "ここに積み上がります)_")
        return "\n".join(lines)
    # Partition each category's entries: SURFACE (curated, part of the index) vs
    # AUTO-COMPLETION (coarse milestone-完遂, held out — ms-161 e-5902). The surface
    # index renders first; completion entries collect into a trailing section so the
    # index stays a surface-単位 capability 索引, not a list of 完了理由.
    completions: list = []
    current_area = None
    for group in summary["categories"]:
        surface = [e for e in group["entries"] if not _is_auto_completion(e)]
        completions.extend(e for e in group["entries"] if _is_auto_completion(e))
        if not surface:
            continue  # a completion-only category contributes nothing to the index
        # A category's 大節 is consistent across its entries (a backfill sets the
        # same area on every bullet of a section); read it off the first entry.
        area = _entry_area(surface[0])
        if area and area != current_area:
            lines.append(f"## {area}")
            lines.append("")
            current_area = area
        heading = _DEV_CATEGORY_HEADINGS.get(group["category"], group["category"])
        lines.append(f"### {heading}" if current_area else f"## {heading}")
        for e in surface:
            summary_text = e.get("summary") or e.get("title") or ""
            ref = e.get("ref") or ""
            ref_str = f" `→ {ref}`" if ref else ""
            wedges = _entry_wedges(e)
            wedge_str = ("  " + " ".join(f"`{w}`" for w in wedges)) if wedges else ""
            lines.append(f"- {summary_text}{ref_str}{wedge_str}")
        lines.append("")
    if completions:
        # The completion log — milestones that shipped but whose surfaces are not
        # yet curated into the index. A prompt to run `beacon deliverable add`, not
        # index noise.
        lines.append("## 🔧 未 index 化の完遂（`beacon deliverable add` で surface 化）")
        for e in completions:
            src = (e.get("source") or {}).get("target_id") or ""
            tag = f" ({src})" if src else ""
            lines.append(f"- {e.get('summary') or e.get('title') or ''}{tag}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_generic(summary: dict, profession: str) -> str:
    """Generic render for a profession without a bespoke one yet — lists the same
    active summary under its category tokens, clearly labelled as the not-yet-
    specialised view (SPEC やらない: 営業/backoffice の作り込みは後続)."""
    lines = [f"# 現在地マップ（{profession} / 汎用 render）", ""]
    if summary["total"] == 0:
        lines.append("_(まだ記録された成果がありません)_")
        return "\n".join(lines)
    for group in summary["categories"]:
        lines.append(f"## {group['category']} ({group['count']})")
        for e in group["entries"]:
            lines.append(f"- {e.get('title') or ''}: {e.get('summary') or ''}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
