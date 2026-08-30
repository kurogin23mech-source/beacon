"""beacon CLI — deliverable family (ms-155 e-5602).

``beacon deliverable list [--resolve] [--json]`` — show the project's DELIVERABLE
union: the produced-value projection of every adopted target-class that declares
one (dev → milestone→機能=application-map). Without ``--resolve`` it shows the
pure POINTERS (``occupation.project_deliverables``); with ``--resolve`` it fetches
each pointer's actual content through the I/O resolver
(``deliverable_resolve.resolve_project_deliverables``) so a human / AI can ask
"what value has this project produced?" and get the real map body / roll-up
summary, uniformly across classes — the consumer that makes the deliverable
projection more than a declaration (the philosophy-review gap PR #677 left).

AX contract (ms-155 e-5666, PR #679 AX review): a resolve FAILURE must be visible
at the process boundary, not buried in a stdout line — so ``--resolve`` exits
non-zero on any unresolved pointer (2 = partial, 1 = total), warnings go to
stderr, and ``--json`` carries a top-level ``{mode, all_resolved, unresolved}``
discriminator so a consumer detects partial failure without iterating every entry
and can tell pointer output from resolved output.
"""

import os
import sys
import json
import argparse

import occupation
import target_descriptor as _td
import deliverable_resolve as _dr
import deliverable_changelog as _dc
import deliverable_map as _dm
import deliverable_doc_sync as _dsync
from commands_shared import load_project, save_project


def _regenerate_map(data):
    """Write-through: refresh the generated application-map doc after a changelog
    mutation (ms-161 e-5851 / 受入条件4 = append/retire に自動追随). BEST-EFFORT — a
    doc-regeneration failure must NOT fail the command, because the changelog was
    already persisted by ``save_project``; losing the doc refresh is recoverable
    (next mutation, or ``beacon deliverable map``), losing the append is not. Warn
    to stderr and continue."""
    try:
        _dsync.sync_application_map(data)
    except Exception as e:  # pragma: no cover - defensive, exercised via warn path
        print(f"⚠ application-map の自動再生成に失敗しました "
              f"(成果ログは記帳済み、`beacon deliverable map` で確認可): {e}",
              file=sys.stderr)

# How much of a resolved doc body to echo in the human view before eliding — the
# --json path always carries the full content.
_DOC_PREVIEW_CHARS = 280


def cmd_deliverable_list():
    """List the project's deliverable projection.

    beacon deliverable list [--resolve] [--json]
    """
    resolve = os.environ.get("BEACON_RESOLVE", "") == "1"
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    data = load_project()

    if resolve:
        rows = _dr.resolve_project_deliverables(data)
        unresolved = [r for r in rows if not r.get("resolved", {}).get("found")]
    else:
        rows = occupation.project_deliverables(data)
        unresolved = []

    mode = "resolved" if resolve else "pointer"

    if json_mode:
        out = {"mode": mode, "items": rows}
        if resolve:
            # top-level failure signal so a consumer detects partial resolution
            # without inspecting every entry's resolved.found (AX high).
            out["all_resolved"] = not unresolved
            out["unresolved"] = [r.get("ref") or r.get("target_class", "?")
                                 for r in unresolved]
        print(json.dumps(out, ensure_ascii=False))
    elif not rows:
        print("(この project の採用クラスは deliverable を宣言していません)")
    else:
        _print_human(rows, resolve, mode)

    # Exit non-zero when a resolve was asked for but some pointer could not be
    # resolved — the failure must reach the process boundary, not just stdout.
    if resolve and unresolved:
        sys.exit(1 if len(unresolved) == len(rows) else 2)


def _print_human(rows, resolve, mode):
    """Render the deliverable rows for a terminal. The header label
    ([POINTER] / [RESOLVED]) tells a reader which mode produced the output, so
    the same ``ref=...`` token is never ambiguous between modes (AX medium)."""
    tag = "RESOLVED" if resolve else "POINTER"
    for row in rows:
        tclass = row.get("target_class", "?")
        label = row.get("label") or row.get("kind", "?")
        projector = row.get("projector", "?")
        ref = row.get("ref", "")
        head = f"  [{tag}] [{tclass}] {label} (projector={projector}"
        head += f", ref={ref})" if ref else ")"
        print(head)
        if not resolve:
            continue
        r = row.get("resolved", {})
        if not r.get("found"):
            print(f"      ⚠ 未解決: {r.get('error', 'resolve failed')}",
                  file=sys.stderr)
            continue
        strategy = r.get("strategy")
        if strategy == _td.PROJECTOR_DOC:
            title = r.get("title", "")
            content = r.get("content", "") or ""
            print(f"      ✓ doc「{title}」 ({len(content)} 文字)")
            preview = content.strip().replace("\n", " ")
            if preview:
                shown = preview[:_DOC_PREVIEW_CHARS]
                ell = "…" if len(preview) > _DOC_PREVIEW_CHARS else ""
                print(f"        {shown}{ell}")
        elif strategy == _td.PROJECTOR_ROLLUP:
            total = r.get("count_total", 0)
            delivered = r.get("count_delivered", 0)
            print(f"      ✓ rollup: {delivered}/{total} delivered")
            labels = r.get("labels", [])
            if labels:
                joined = " / ".join(labels[:10])
                more = " …" if r.get("labels_truncated") or len(labels) > 10 else ""
                print(f"        {joined}{more}")
        else:
            print(f"      ✓ {strategy}")


# ---------------------------------------------------------------------------
# Curation surface — write path over the root deliverable-changelog (ms-161
# e-5902 / e-5903).
#
# The changelog lib (``deliverable_changelog``) already holds the append /
# retire / supersede primitives, but PR#694 wired only the AUTO capture on
# milestone completion — a human/AI could not add a surface-grained entry, and
# ``retire``/``supersede`` had NO operation surface at all (Python-only). These
# verbs are that missing surface: ``add`` lets one target record MULTIPLE
# surface/capability entries (e-5902 の「1 target 複数 entry」), and
# ``retire``/``supersede`` give the "足す＆消す" の『消す』 a real 動線 (e-5903).
# All three load → mutate via the lib → ``save_project`` with an audit ``op`` tag,
# matching every other write verb's discipline.
#
# ``map`` is the read seam that renders the derived current-state map — the same
# ``deliverable_map.render_map`` the application-map re-home (e-5851) points the
# CORE doc at, exposed so a human/AI (or the doc-swap step) can print exactly what
# the derived doc will contain and pipe it through check-map-drift.
# ---------------------------------------------------------------------------

def _tags_with_area(tags: list, area: str) -> list:
    """Prepend an ``area:<heading>`` tag (the 大節 grouping key the dev render
    reads) when ``--area`` is given, so a caller does not have to hand-format the
    ``area:`` prefix. Explicit ``--tag area:...`` still works; this is sugar."""
    out = list(tags or [])
    area = (area or "").strip()
    if area:
        out.insert(0, f"{_dc.AREA_TAG_PREFIX}{area}")
    return out


def _entry_args(ap: argparse.ArgumentParser) -> None:
    """Attach the shared produced-value fields (used by both ``add`` and the
    successor of ``supersede``) so the two stay in lockstep."""
    ap.add_argument("--title", required=True, help="読み手目線の成果名 (1 行)")
    ap.add_argument("--summary", required=True,
                    help="何ができる / 何が嬉しいか (map の表示本文)")
    ap.add_argument("--category", required=True,
                    help="束ねる軸の型トークン (dev の map では小節見出し)")
    ap.add_argument("--source-target", dest="source_target", default="root",
                    help="生んだ target の id (backfill は root)")
    ap.add_argument("--source-kind", dest="source_kind", default="root",
                    help="生んだ target の kind (milestone / root 等)")
    ap.add_argument("--ref", default="", help="詳細への drill-down pointer (任意)")
    ap.add_argument("--area", default="",
                    help="大節見出し (dev render の ## 見出し, 任意)")
    ap.add_argument("--tag", action="append", default=[], dest="tags",
                    help="追加タグ。楔は cli:/api:/skill:/file: 形式で (繰り返し可)")


def cmd_deliverable_add():
    """beacon deliverable add --title T --summary S --category C
        [--source-target ID] [--source-kind K] [--ref R] [--area A]
        [--tag TAG ...] [--json]

    Append ONE surface-grained produced-value entry to the root
    deliverable-changelog (e-5902). A single target may be recorded multiple
    times (one call per surface / capability) — the lib append has no
    per-(target,category) dedup, so nothing structurally caps a target at one
    entry (the auto-capture's idempotent dedup lives in the capture bridge, not
    here)."""
    ap = argparse.ArgumentParser(prog="beacon deliverable add", add_help=True)
    _entry_args(ap)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[2:])

    data = load_project()
    try:
        entry = _dc.append_deliverable(data, {
            "source": {"target_id": args.source_target, "kind": args.source_kind},
            "category": args.category,
            "title": args.title,
            "summary": args.summary,
            "ref": args.ref,
            "tags": _tags_with_area(args.tags, args.area),
        })
    except _dc.DeliverableValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "deliverable_add", "entry_id": entry["id"],
                           "source_target": args.source_target,
                           "category": args.category})
    _regenerate_map(data)
    if args.json:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        # Echo the source attribution (PR#699 AX review): --source-target defaults
        # to "root", so surfacing it here lets a caller catch a forgotten
        # --source-target ms-XX before the mis-attribution is buried in the log.
        print(f"✓ deliverable {entry['id']} を記帳: [{args.category}] {args.title} "
              f"(source: {args.source_target})")


def cmd_deliverable_retire():
    """beacon deliverable retire <entry-id> [--reason R] [--json]

    Retire a produced-value entry — the capability it recorded no longer exists,
    so it drops out of the derived current-state map ("消す", e-5903). Idempotent:
    retiring an already-retired entry re-stamps. An unknown id is a loud error."""
    ap = argparse.ArgumentParser(prog="beacon deliverable retire", add_help=True)
    ap.add_argument("entry_id", help="retire する deliverable の id (dlv-N)")
    ap.add_argument("--reason", default="", help="なぜ廃止したか (任意)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[2:])

    data = load_project()
    try:
        entry = _dc.retire_deliverable(data, args.entry_id, reason=args.reason)
    except _dc.DeliverableValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "deliverable_retire",
                           "entry_id": args.entry_id, "reason": args.reason})
    _regenerate_map(data)
    if args.json:
        print(json.dumps(entry, ensure_ascii=False))
    else:
        print(f"✓ deliverable {args.entry_id} を retire (現在地マップから脱落)")


def cmd_deliverable_supersede():
    """beacon deliverable supersede <old-id> --title T --summary S --category C
        [--source-target ID] [--source-kind K] [--ref R] [--area A]
        [--tag TAG ...] [--json]

    Replace an earlier entry with an evolved one atomically (足す＆消す, e-5903):
    append the successor AND flip the old entry to ``superseded`` so only the
    successor stays in the current-state map. The successor's ``supersedes`` link
    is set by the operation."""
    ap = argparse.ArgumentParser(prog="beacon deliverable supersede", add_help=True)
    ap.add_argument("old_id", help="置き換えられる先行 deliverable の id (dlv-N)")
    _entry_args(ap)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[2:])

    data = load_project()
    try:
        successor = _dc.supersede_deliverable(data, args.old_id, {
            "source": {"target_id": args.source_target, "kind": args.source_kind},
            "category": args.category,
            "title": args.title,
            "summary": args.summary,
            "ref": args.ref,
            "tags": _tags_with_area(args.tags, args.area),
        })
    except _dc.DeliverableValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    save_project(data, op={"type": "deliverable_supersede",
                           "old_id": args.old_id, "new_id": successor["id"]})
    _regenerate_map(data)
    if args.json:
        print(json.dumps(successor, ensure_ascii=False))
    else:
        print(f"✓ deliverable {args.old_id} → {successor['id']} に supersede: "
              f"[{args.category}] {args.title}")


def cmd_deliverable_map():
    """beacon deliverable map [--profession P] [--json]

    Render the derived current-state map — the active deliverable-changelog
    entries grouped by category, dev-rendered as the application-map format
    (e-5851). This is the seam the application-map re-home reads: its output IS
    what the derived CORE doc contains, so a human/AI can preview the map and pipe
    it through ``scripts/check-map-drift.py`` before swapping the hand-maintained
    doc. ``--json`` carries the structured summary (categories + counts) alongside
    the rendered text."""
    ap = argparse.ArgumentParser(prog="beacon deliverable map", add_help=True)
    ap.add_argument("--profession", default=None,
                    help="render する職種 (既定はプロジェクトの職種)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(sys.argv[2:])

    data = load_project()
    rendered = _dm.render_map(data, profession=args.profession)
    if args.json:
        summary = _dm.summarize_map(data)
        print(json.dumps({"summary": summary, "rendered": rendered},
                         ensure_ascii=False))
    else:
        print(rendered)
