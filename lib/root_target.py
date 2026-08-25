"""Read the existing project as the ROOT target (ms-153 e-5546 / SPEC 方針1).

ms-150 spine §2b decided that ``project`` is not an independent privileged
concept but the TOP of the target hierarchy — the *root target*. The axis stays
target-only: the 大目的 (grand objective) is the root target's goal, and each
child work-item is itself a target-instance (§0 applied at the top ＝ a fractal /
self-similar structure).

This module is the in-place *strangler* foothold (SPEC 方針1): it reads a live
project dict AS a root target through a **view / projection** — NO new records,
NO mutation of the project data. Every existing cloud project therefore becomes
readable as a root target with zero migration.

The root is NOT fully同型 with a leaf target. It is the **phase-less /
evidence-less special form** whose arm mapping is declared once in
``ROOT_TARGET_ARMS`` (SPEC 受入条件2):

    ┌─────────────┬────────────────────────────────────────────────────────┐
    │ arm         │ root's binding                                          │
    ├─────────────┼────────────────────────────────────────────────────────┤
    │ work-item   │ its child Targets — the collection walked by            │
    │             │ ``occupation.iter_target_records`` (NOT a literal arm    │
    │             │ on the project dict). Each child IS a target-instance.   │
    │ phase       │ NONE. The root has no phase model of its own.            │
    │ evidence    │ NONE. No evidence arm hangs off the root directly.       │
    │ decision    │ its children's completion approval (kept MINIMAL here —   │
    │             │ the decision-arm一級化 is ms-154; the root only declares │
    │             │ the binding, it does not adjudicate completion).         │
    │ deliverable │ the root's achievement (the union over children is       │
    │             │ ms-155's generalisation; here it is only declared).      │
    └─────────────┴────────────────────────────────────────────────────────┘

Import layering: this module sits ABOVE ``occupation`` (it consumes
``project_targets`` / ``iter_target_records`` / ``resolve_profession``).
``occupation`` does not import back, so there is no cycle.

Project-level information 2-split (ms-153 e-5547 / SPEC 方針2)
------------------------------------------------------------
The information a "project" carries splits into two kinds with opposite
provenance, and conflating them is what left 器級 information homeless under the
axis inversion:

- **合成投影 (synthesized projection)** — 現在地 / 進捗 / deliverable. NOT stored
  at project level: it is rolled up from each adopted target-class's own
  contribution every read (``synthesized_projection``). Adopting a target-class
  automatically adds its contribution, so no project-level field can go stale or
  be "lost" — the projection simply recomputes over whatever classes exist.
- **root 固有の物語 (root-owned narrative)** — 大目的 vision / 経緯 summary. This
  CANNOT be derived from any target; it is the irreducible minimal core the root
  OWNS (``root_narrative``). If every child target vanished, the narrative would
  remain.

``project_as_root_target`` composes the two into ONE root view, keeping them
structurally distinct (``projection`` vs ``narrative``) rather than a single
undifferentiated ``detail`` bag.
"""

from __future__ import annotations

import occupation
import work_model


# ---------------------------------------------------------------------------
# The root target's arm mapping — declared ONCE as data (SPEC 受入条件2). This is
# the special phase-less / evidence-less form; a leaf target's arms come from
# ``occupation.profession_manifest`` per target-class, but the root is singular,
# so its arms are declared here rather than in the per-class manifest.
#
# ``work_item_arm.via`` names the ENUMERATION seam rather than a literal arm on
# the project dict: the root's children are not a single nested array (a project
# holds milestones OR opportunities OR …), they are every Target record across
# collections, which ``occupation.iter_target_records`` already walks. Declaring
# the seam (not a collection name) is what keeps the root occupation-agnostic —
# a sales project's root has the same arm mapping, its children merely resolve to
# opportunities instead of milestones.
# ---------------------------------------------------------------------------

ROOT_TARGET_KIND = "root"

ROOT_TARGET_ARMS = {
    "kind": ROOT_TARGET_KIND,
    # phase-less: the root carries no state model / phase_ball of its own.
    "phase_ball": None,
    "state_model": None,
    # work-item arm = the root's child Targets, enumerated through the target
    # abstraction rather than a literal project-dict array.
    "work_item_arm": {
        "item_type": "target",
        "via": "occupation.iter_target_records",
    },
    # evidence-less: no evidence hangs off the root directly (evidence lives on
    # the child targets / their work items).
    "evidence_arms": [],
    # decision arm = children's completion approval. Declared, not adjudicated —
    # the decision-arm一級化 is ms-154 (SPEC やらない). The root only names the
    # binding so ms-154 has a home to plug into.
    "decision": {
        "kind": "completion_approval",
        "via": "child_completion",
    },
    # deliverable arm = the root's achievement. The union-over-children
    # generalisation is ms-155 (SPEC やらない); here it is only declared.
    "deliverable": {
        "kind": "achievement",
    },
}


def root_target_arms(data: dict | None = None) -> dict:
    """Return the root target's arm-mapping declaration (SPEC 受入条件2).

    Takes ``data`` for signature symmetry with the per-class manifest readers and
    so a future project-level override has a seam to hook, but the mapping is
    singular today, so the built-in ``ROOT_TARGET_ARMS`` is returned verbatim (a
    fresh copy — callers must not mutate the module constant)."""
    return {
        "kind": ROOT_TARGET_ARMS["kind"],
        "phase_ball": ROOT_TARGET_ARMS["phase_ball"],
        "state_model": ROOT_TARGET_ARMS["state_model"],
        "work_item_arm": dict(ROOT_TARGET_ARMS["work_item_arm"]),
        "evidence_arms": list(ROOT_TARGET_ARMS["evidence_arms"]),
        "decision": dict(ROOT_TARGET_ARMS["decision"]),
        "deliverable": dict(ROOT_TARGET_ARMS["deliverable"]),
    }


def _root_status(total_children: int) -> str:
    """Derive the root's status from its children — the root is phase-less, so it
    has NO stored status of its own; this is a pure derivation.

    Two honest states only:

    - ``"todo"``   — no children yet (a fresh project, nothing to advance).
    - ``"active"`` — the project has child Targets (work is under way).

    A ``"done"`` root is deliberately NOT derived here: root completion is the
    decision arm (完了承認), owned by ms-154. Inventing it now would pre-empt that
    boundary (SPEC: root の decision＝完了承認 は本 MS では最小に留める)."""
    if total_children == 0:
        return work_model.TODO_STATUS
    # Both "some children open" and "all children terminal but unapproved" read
    # as active, because completion approval is ms-154's decision arm.
    return "active"


def root_narrative(data: dict) -> dict:
    """The root-OWNED narrative — the project-level story that CANNOT be derived
    from any target (SPEC 方針2: 導出できない物語).

    This is the irreducible minimal core the root owns: the 大目的 (grand
    objective / vision) and the 経緯 (running summary). Both are read straight off
    the project dict — no roll-up, because no child target carries them. If every
    child Target vanished, this narrative would remain, which is exactly why it
    must live ON the root and not be reconstructed from children.

    Read-only: returns a fresh dict, never mutates ``data``. The fuller vision
    prose lives in the ``project-vision`` CORE doc (a separate store); this module
    is pure (no I/O), so it surfaces the on-project ``objective`` one-liner and
    leaves fetching the doc to the session-start assembler (e-5549)."""
    return {
        "objective": data.get("objective", ""),   # 大目的 = root goal / vision seed
        "summary": data.get("summary", ""),        # 経緯 summary (session narrative)
    }


def synthesized_projection(data: dict) -> dict:
    """The SYNTHESIZED project-level state — 現在地 / 進捗 / deliverable rolled up
    from each adopted target-class's own contribution (SPEC 方針2: 合成できる投影).

    Nothing here is stored at project level. It recomputes over
    ``occupation.project_targets`` (the occupation-agnostic per-class projection:
    dev milestones + sales opportunities + any adopted descriptor class) every
    read, so adopting a new target-class automatically adds its contribution and
    NO project-level field can go stale or be "lost" (方針2 の芯 = class を採用した
    瞬間に投影寄与が付く). Cancelled Targets are already excluded by
    ``project_targets``.

    Shape::

        {
          "targets": [...],                    # 現在地: the child Target rows
          "counts": {"total", "done", "open"}, # 進捗: roll-up over children
          "deliverables": [...],               # deliverable union (方針: minimal)
        }

    ``deliverables`` is the seam for the root's deliverable arm (union over
    children). The deliverable dimension's generalisation is ms-155 (SPEC やらない),
    and no built-in class emits deliverables yet, so it is an empty list today —
    present so the shape does not change when ms-155 fills it in."""
    rows = occupation.project_targets(data)
    done = sum(1 for r in rows if work_model.is_done(r))
    open_count = sum(1 for r in rows if work_model.is_open(r))
    return {
        "targets": rows,
        "counts": {
            "total": len(rows),
            "done": done,
            "open": open_count,
        },
        # deliverable union over children — ms-155 fills this; empty seam now.
        "deliverables": [],
    }


def project_as_root_target(data: dict) -> dict:
    """Read ``data`` (a live project dict) AS the root target — a view/projection
    with NO new records and NO mutation of ``data`` (SPEC 受入条件1).

    The returned shape carries the occupation-agnostic shared-frame core (``id`` /
    ``label`` / ``status`` / ``kind`` / ``work_items_total`` / ``work_items_done``)
    plus the root's ``arms`` mapping AND the project-level 2-split (SPEC 方針2):

    - ``projection`` — the SYNTHESIZED half (合成投影, ``synthesized_projection``).
    - ``narrative``  — the root-OWNED half (root 固有の物語, ``root_narrative``).

    Keeping the two halves as distinct keys (rather than one ``detail`` bag) is
    the point of the split: a reader knows a ``projection`` field is recomputed
    from children (safe to ignore staleness) while a ``narrative`` field is
    authored and must be preserved.

    Work-item counts treat each **child Target** as one work item of the root
    (the fractal §0), sourced from the same ``project_targets`` roll-up so the
    top-level counts and ``projection.counts`` never diverge."""
    projection = synthesized_projection(data)
    counts = projection["counts"]
    return {
        "id": ROOT_TARGET_KIND,
        "label": data.get("name", ""),
        "status": _root_status(counts["total"]),
        "kind": ROOT_TARGET_KIND,
        "work_items_total": counts["total"],
        "work_items_done": counts["done"],
        "profession": occupation.resolve_profession(data),
        "arms": root_target_arms(data),
        # SPEC 方針2 の 2分割 — synthesized (derivable) vs owned (authored).
        "projection": projection,
        "narrative": root_narrative(data),
    }
