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
    """Dev render = application-map-flavoured index (方針4). One section per
    category (pretty-labelled), each active produced value as a 散文 bullet with
    its drill-down ref. This is the shape ms-161 re-homes application-map onto
    (e-5825): the doc becomes THIS render's output, so append/retire move the map
    with no hand-maintenance."""
    lines = ["# アプリケーション全貌マップ（deliverable-changelog 導出）", ""]
    if summary["total"] == 0:
        lines.append("_(まだ記録された成果がありません — milestone を完遂すると"
                     "ここに積み上がります)_")
        return "\n".join(lines)
    for group in summary["categories"]:
        heading = _DEV_CATEGORY_HEADINGS.get(group["category"], group["category"])
        lines.append(f"## {heading}")
        for e in group["entries"]:
            ref = e.get("ref") or ""
            wedge = f" `→ {ref}`" if ref else ""
            summary_text = e.get("summary") or e.get("title") or ""
            lines.append(f"- {summary_text}{wedge}")
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
