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

import occupation
import target_descriptor as _td
import deliverable_resolve as _dr
from commands_shared import load_project

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
