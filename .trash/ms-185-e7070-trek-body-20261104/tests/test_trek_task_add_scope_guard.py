"""Scope-guard tests for cross-project ``beacon trek task add`` (ms-92 e-2141).

The CLI / server share one yes/no question: given the Trek's recorded
``scope``, may the caller add a task under ``target_project`` /
``target_milestone``? The pure helper ``trek.check_trek_task_add_allowed``
returns ``(allowed, reason)`` so a single decision feeds both the
server's 403 and the CLI's friendly error message.

The 4 unit-test paths mirror SPEC AC of e-2141:

  1. Project-wide scope entry → allowed (= broad delegation case).
  2. Milestone-matched scope entry → allowed (= the typical Trek case
     where leader scoped explicit MS list).
  3. Project not in scope at all → rejected as 403 / project_not_in_scope.
  4. Project appears **only** under task-narrowed scope entries
     (= ``{project, task: e-XXX}``) → rejected as
     ``scope_only_has_task_narrowing`` so cross-Trek task trees can't
     sprout sideways without MS-grain user intent.

Plus a 5th edge case (milestone mismatch under a project that *does*
appear in scope with milestone entries) so the reason code splits
cleanly between "project unknown" vs "milestone unknown" — the CLI
shows different remediation hints for these two paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import trek  # noqa: E402


def _trek_doc_with_scope(scope_entries: list[dict]) -> dict:
    """Minimal trek doc carrying just the fields the scope-guard inspects.

    ms-97 / e-2659 (AC8): some of these fixtures intentionally exercise
    legacy project-wide scope rows (= ``{"project": pid}`` with no
    narrowing) which the new strict mode would reject. Normalise with
    ``strict=False`` so the on-disk grandfather path stays exercisable
    from the scope-guard tests without coupling them to AC7's write-side
    restrictions.
    """
    return {
        "trek_id": "tk-test-e2141",
        "status": "active",
        "scope": [
            trek.normalize_scope_entry(s, strict=False) for s in scope_entries
        ],
    }


# ---------------------------------------------------------------------------
# Allowed paths
# ---------------------------------------------------------------------------


def test_allowed_when_scope_has_project_wide_entry():
    """Project-wide scope (= no narrowing) authorises any MS under it."""
    doc = _trek_doc_with_scope([{"project": "proj-A"}])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is True
    assert reason == trek.TASK_ADD_ALLOWED


def test_allowed_when_scope_has_matching_milestone_entry():
    """Explicit milestone scope authorises task add under that MS."""
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "milestone": "ms-10"},
        {"project": "proj-A", "milestone": "ms-20"},
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is True
    assert reason == trek.TASK_ADD_ALLOWED


def test_allowed_when_other_project_in_scope_is_irrelevant():
    """Another project's milestone in scope doesn't authorise this project."""
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "milestone": "ms-10"},  # noise
        {"project": "proj-B", "milestone": "ms-99"},  # target
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-B", target_milestone="ms-99",
    )
    assert allowed is True
    assert reason == trek.TASK_ADD_ALLOWED


# ---------------------------------------------------------------------------
# Rejected paths
# ---------------------------------------------------------------------------


def test_rejected_when_project_not_in_scope_at_all():
    """403-equivalent: project doesn't appear in any scope entry."""
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "milestone": "ms-10"},
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-X", target_milestone="ms-99",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE


def test_rejected_when_project_in_scope_only_under_task_narrowing():
    """AC #4 of e-2141: task-only narrowing must reject cross-Trek task add.

    The Trek scope frame says "MS-grain decisions stay with the user"
    — letting Trek X add tasks under Trek Y's scope-narrowed task entry
    would let tasks sprout sideways without MS-level intent.
    """
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "task": "e-1234"},
        {"project": "proj-A", "task": "e-5678"},
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_SCOPE_ONLY_HAS_TASK_NARROWING


def test_rejected_when_project_in_scope_only_under_operation_narrowing():
    """Same protection for operation-narrowed entries (= treated like task)."""
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "operation": "op-7"},
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_SCOPE_ONLY_HAS_TASK_NARROWING


def test_rejected_when_project_in_scope_but_milestone_mismatch():
    """The reject reason splits between 'project unknown' and 'milestone unknown'."""
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "milestone": "ms-10"},
        {"project": "proj-A", "milestone": "ms-20"},
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-99",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_MILESTONE_NOT_IN_SCOPE


# ---------------------------------------------------------------------------
# Edge cases — empty inputs, mixed scope shapes
# ---------------------------------------------------------------------------


def test_rejected_when_target_project_empty():
    doc = _trek_doc_with_scope([{"project": "proj-A"}])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="", target_milestone="ms-10",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE


def test_rejected_when_target_milestone_empty():
    doc = _trek_doc_with_scope([{"project": "proj-A"}])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_MILESTONE_NOT_IN_SCOPE


def test_mixed_scope_project_wide_overrides_task_narrowing():
    """If both project-wide and task-narrowed entries exist for the same
    project, the wide one wins — task narrowing is additive, not subtractive.
    """
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "task": "e-1234"},
        {"project": "proj-A"},  # project-wide — broadens authorisation
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is True
    assert reason == trek.TASK_ADD_ALLOWED


def test_mixed_scope_milestone_match_wins_over_task_narrowing():
    """Same reasoning: a matching MS entry alongside task narrowings → allowed."""
    doc = _trek_doc_with_scope([
        {"project": "proj-A", "task": "e-1234"},
        {"project": "proj-A", "milestone": "ms-10"},
    ])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is True
    assert reason == trek.TASK_ADD_ALLOWED


def test_empty_trek_scope_rejects():
    doc = _trek_doc_with_scope([])
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE


def test_trek_doc_with_no_scope_key_rejects():
    """Missing ``scope`` field shouldn't KeyError — treated as empty."""
    doc = {"trek_id": "tk-x", "status": "active"}
    allowed, reason = trek.check_trek_task_add_allowed(
        doc, target_project="proj-A", target_milestone="ms-10",
    )
    assert allowed is False
    assert reason == trek.TASK_ADD_REJECT_PROJECT_NOT_IN_SCOPE
