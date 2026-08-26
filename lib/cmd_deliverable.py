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
"""

import os
import json

import occupation  # noqa: F401
import deliverable_resolve as _dr  # noqa: F401
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
    else:
        rows = occupation.project_deliverables(data)

    if json_mode:
        print(json.dumps(rows, ensure_ascii=False))
        return

    if not rows:
        print("(この project の採用クラスは deliverable を宣言していません)")
        return

    for row in rows:
        tclass = row.get("target_class", "?")
        label = row.get("label") or row.get("kind", "?")
        projector = row.get("projector", "?")
        ref = row.get("ref", "")
        head = f"  [{tclass}] {label} (projector={projector}"
        head += f", ref={ref})" if ref else ")"
        print(head)
        if not resolve:
            continue
        r = row.get("resolved", {})
        if not r.get("found"):
            print(f"      ⚠ 未解決: {r.get('error', 'resolve failed')}")
            continue
        strategy = r.get("strategy")
        if strategy == "doc":
            title = r.get("title", "")
            content = r.get("content", "") or ""
            print(f"      ✓ doc「{title}」 ({len(content)} 文字)")
            preview = content.strip().replace("\n", " ")
            if preview:
                shown = preview[:_DOC_PREVIEW_CHARS]
                ell = "…" if len(preview) > _DOC_PREVIEW_CHARS else ""
                print(f"        {shown}{ell}")
        elif strategy == "rollup":
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
