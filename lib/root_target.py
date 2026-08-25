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
``iter_target_records`` and ``resolve_profession``). ``occupation`` does not
import back, so there is no cycle. The project-level information 2-split
(合成投影 vs root 固有の物語 field) is e-5547; here the view exposes the
root-owned narrative (objective / summary) minimally so callers have a home for
it, but the full split lands next.
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


def _root_status(open_children: int, total_children: int) -> str:
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
    # open_children is informational for callers; both "some open" and "all
    # children terminal but unapproved" read as active because approval is ms-154.
    return "active"


def project_as_root_target(data: dict) -> dict:
    """Read ``data`` (a live project dict) AS the root target — a view/projection
    with NO new records and NO mutation of ``data`` (SPEC 受入条件1).

    The returned shape is the same occupation-agnostic projection the shared
    frame already consumes for leaf Targets (``id`` / ``label`` / ``status`` /
    ``kind`` / ``work_items_total`` / ``work_items_done`` / ``detail``) PLUS an
    ``arms`` blob carrying the root's phase-less / evidence-less arm mapping. So a
    caller can render the root through the exact same path as any child target.

    Work-item counts treat each **child Target** as one work item of the root
    (the fractal §0). Cancelled children are excluded, matching the default
    status view (``core.project_targets`` / ``occupation.project_targets``)."""
    children = [
        rec for rec in occupation.iter_target_records(data)
        if not work_model.is_cancelled(rec)
    ]
    total = len(children)
    done = sum(1 for rec in children if work_model.is_done(rec))
    open_count = sum(1 for rec in children if work_model.is_open(rec))

    return {
        "id": ROOT_TARGET_KIND,
        "label": data.get("name", ""),
        "status": _root_status(open_count, total),
        "kind": ROOT_TARGET_KIND,
        "work_items_total": total,
        "work_items_done": done,
        "arms": root_target_arms(data),
        "detail": {
            # root-owned narrative (真に project 所有 = root 固有 field). The full
            # 2-split of project-level info (合成投影 vs 物語) is e-5547; exposed
            # here so the narrative already has a home on the root view.
            "objective": data.get("objective", ""),   # 大目的 = root goal
            "summary": data.get("summary", ""),        # 経緯 summary
            "profession": occupation.resolve_profession(data),
        },
    }
