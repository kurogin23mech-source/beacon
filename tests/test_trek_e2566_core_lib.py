"""Unit tests for ms-97 / e-2566 core lib changes (lib/trek.py).

Covers:
- ``normalize_scope_entry(strict=True)`` rejects project-wide entries
- ``TrekTaskState`` enum + ``TERMINAL_TASK_STATES`` frozenset shape
- ``aggregate_ms_slot_state`` precedence (leader_review > full-terminal >
  working > todo)
- ``resolve_session_home_project`` lookup helper
- ``migrate_members_to_session_id_keyed`` + rollback (with backup field)
- ``is_task_slot`` + ``is_task_add_banned_under_task_slot`` task-slot ban

These tests pin the SPEC contracts (= AC6 / AC7 / AC8 / AC9 / AC10 /
AC12 / AC14 / AC34) so the later server / CLI / Skill phases of ms-97
can rely on the lib layer as a stable foundation.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# AC9 — TrekTaskState enum + TERMINAL_TASK_STATES frozenset
# ---------------------------------------------------------------------------

def test_trek_task_state_enum_exports_five_states():
    """AC9: the enum exposes exactly the canonical 5 states with str values."""
    assert {s.value for s in trek.TrekTaskState} == {
        "todo", "working", "leader_review", "user_review", "done",
    }


def test_trek_task_state_enum_is_str_subclass():
    """str subclass lets existing string call sites keep working unchanged."""
    assert trek.TrekTaskState.DONE == "done"
    assert isinstance(trek.TrekTaskState.WORKING, str)


def test_terminal_task_states_is_frozenset():
    """TERMINAL_TASK_STATES must be a frozenset of {done, user_review}."""
    assert isinstance(trek.TERMINAL_TASK_STATES, frozenset)
    assert trek.TERMINAL_TASK_STATES == frozenset({"done", "user_review"})


def test_trek_task_state_is_terminal_helper():
    assert trek.TrekTaskState.DONE.is_terminal() is True
    assert trek.TrekTaskState.USER_REVIEW.is_terminal() is True
    assert trek.TrekTaskState.LEADER_REVIEW.is_terminal() is False
    assert trek.TrekTaskState.WORKING.is_terminal() is False
    assert trek.TrekTaskState.TODO.is_terminal() is False


# ---------------------------------------------------------------------------
# AC7 — normalize_scope_entry strict mode rejects project-wide
# ---------------------------------------------------------------------------

def test_normalize_scope_entry_strict_rejects_project_wide():
    """AC7: project-wide (= no narrowing key) raises under strict mode."""
    with pytest.raises(ValueError):
        trek.normalize_scope_entry({"project": "p-1"}, strict=True)


def test_normalize_scope_entry_strict_accepts_milestone_narrowing():
    out = trek.normalize_scope_entry(
        {"project": "p-1", "milestone": "ms-3"}, strict=True,
    )
    assert out == {"project": "p-1", "milestone": "ms-3"}


def test_normalize_scope_entry_strict_accepts_task_narrowing():
    out = trek.normalize_scope_entry(
        {"project": "p-1", "task": "e-9"}, strict=True,
    )
    assert out == {"project": "p-1", "task": "e-9"}


def test_normalize_scope_entry_strict_accepts_operation_narrowing():
    out = trek.normalize_scope_entry(
        {"project": "p-1", "operation": "op-2"}, strict=True,
    )
    assert out == {"project": "p-1", "operation": "op-2"}


def test_normalize_scope_entry_default_keeps_legacy_behavior():
    """Default (= strict=False) preserves the pre-ms-97 contract."""
    out = trek.normalize_scope_entry({"project": "p-1"})
    assert out == {"project": "p-1"}


def test_parse_scope_arg_strict_rejects_project_only():
    with pytest.raises(ValueError):
        trek.parse_scope_arg("p-1", strict=True)


def test_parse_scope_arg_strict_accepts_narrowed():
    assert trek.parse_scope_arg("p-1:ms-3", strict=True) == {
        "project": "p-1", "milestone": "ms-3",
    }


def test_add_scope_entry_strict_rejects_project_wide():
    base = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    with pytest.raises(ValueError):
        trek.add_scope_entry(base, entry={"project": "p"}, strict=True)


def test_add_scope_entry_default_accepts_project_wide():
    """Backward compat: default add path still accepts legacy project-wide."""
    base = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.add_scope_entry(base, entry={"project": "p", "milestone": "ms-1"})
    trek.add_scope_entry(base, entry={"project": "q"})
    assert any(s == {"project": "q"} for s in base.get("scope") or [])


# ---------------------------------------------------------------------------
# AC8 — has_project_wide_scope detects grandfathered legacy entries
# ---------------------------------------------------------------------------

def test_has_project_wide_scope_true_when_any_entry_lacks_narrowing():
    doc = {"scope": [{"project": "p"}, {"project": "q", "milestone": "ms-1"}]}
    assert trek.has_project_wide_scope(doc) is True


def test_has_project_wide_scope_false_when_all_entries_narrowed():
    doc = {
        "scope": [
            {"project": "p", "milestone": "ms-1"},
            {"project": "q", "task": "e-2"},
            {"project": "r", "operation": "op-3"},
        ],
    }
    assert trek.has_project_wide_scope(doc) is False


def test_has_project_wide_scope_empty_doc():
    assert trek.has_project_wide_scope({}) is False
    assert trek.has_project_wide_scope({"scope": []}) is False


# ---------------------------------------------------------------------------
# AC10 — aggregate_ms_slot_state precedence rule
# ---------------------------------------------------------------------------

def _new_doc():
    return trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )


def test_aggregate_ms_slot_state_empty_tasks_returns_todo():
    """AC10: empty MS slot defaults to todo (= consistent with newly scoped)."""
    assert trek.aggregate_ms_slot_state(_new_doc(), task_ids=[]) == "todo"


def test_aggregate_ms_slot_state_all_todo():
    """AC10: all children todo → slot is todo."""
    doc = _new_doc()
    # default state for unstamped task_ids is todo
    assert trek.aggregate_ms_slot_state(doc, task_ids=["e-1", "e-2"]) == "todo"


def test_aggregate_ms_slot_state_any_working_returns_working():
    """AC10: 1+ working AND no leader_review → working."""
    doc = _new_doc()
    trek.set_task_state(doc, task_id="e-1", state="working")
    # e-2 stays at default todo; mix of working+todo → working
    assert trek.aggregate_ms_slot_state(
        doc, task_ids=["e-1", "e-2"],
    ) == "working"


def test_aggregate_ms_slot_state_leader_review_takes_precedence_over_working():
    """AC10: leader_review trumps working (= leader queue surfaced first)."""
    doc = _new_doc()
    trek.set_task_state(doc, task_id="e-1", state="working")
    trek.set_task_state(doc, task_id="e-2", state="working")
    trek.set_task_state(doc, task_id="e-2", state="leader_review")
    assert trek.aggregate_ms_slot_state(
        doc, task_ids=["e-1", "e-2"],
    ) == "leader_review"


def test_aggregate_ms_slot_state_leader_review_takes_precedence_over_done():
    """leader_review trumps even terminal mixes (= queue still open)."""
    doc = _new_doc()
    trek.set_task_state(doc, task_id="e-1", state="working")
    trek.set_task_state(doc, task_id="e-1", state="done")
    trek.set_task_state(doc, task_id="e-2", state="working")
    trek.set_task_state(doc, task_id="e-2", state="leader_review")
    assert trek.aggregate_ms_slot_state(
        doc, task_ids=["e-1", "e-2"],
    ) == "leader_review"


def test_aggregate_ms_slot_state_all_done():
    """AC10: all terminal AND all done → slot is done."""
    doc = _new_doc()
    for tid in ("e-1", "e-2"):
        trek.set_task_state(doc, task_id=tid, state="working")
        trek.set_task_state(doc, task_id=tid, state="done")
    assert trek.aggregate_ms_slot_state(
        doc, task_ids=["e-1", "e-2"],
    ) == "done"


def test_aggregate_ms_slot_state_all_terminal_with_user_review_returns_user_review():
    """AC10: all terminal, mix of done + user_review → user_review (= leader surfaced for user hand-off)."""
    doc = _new_doc()
    trek.set_task_state(doc, task_id="e-1", state="working")
    trek.set_task_state(doc, task_id="e-1", state="done")
    trek.set_task_state(doc, task_id="e-2", state="working")
    trek.set_task_state(doc, task_id="e-2", state="user_review")
    assert trek.aggregate_ms_slot_state(
        doc, task_ids=["e-1", "e-2"],
    ) == "user_review"


def test_aggregate_ms_slot_state_all_user_review():
    """All children waiting for user judgment → slot is user_review."""
    doc = _new_doc()
    for tid in ("e-1", "e-2"):
        trek.set_task_state(doc, task_id=tid, state="working")
        trek.set_task_state(doc, task_id=tid, state="user_review")
    assert trek.aggregate_ms_slot_state(
        doc, task_ids=["e-1", "e-2"],
    ) == "user_review"


# ---------------------------------------------------------------------------
# AC14 — resolve_session_home_project lookup helper
# ---------------------------------------------------------------------------

def test_resolve_session_home_project_returns_matching_project():
    sessions = [
        {"session_id": "sv-1", "project_id": "p-a"},
        {"session_id": "sv-2", "project_id": "p-b"},
    ]
    assert trek.resolve_session_home_project(sessions, "sv-2") == "p-b"


def test_resolve_session_home_project_none_for_missing_session():
    sessions = [{"session_id": "sv-1", "project_id": "p-a"}]
    assert trek.resolve_session_home_project(sessions, "sv-x") is None


def test_resolve_session_home_project_none_for_empty_session_id():
    sessions = [{"session_id": "sv-1", "project_id": "p-a"}]
    assert trek.resolve_session_home_project(sessions, "") is None


def test_resolve_session_home_project_none_when_record_lacks_project_id():
    sessions = [{"session_id": "sv-1"}]
    assert trek.resolve_session_home_project(sessions, "sv-1") is None


# ---------------------------------------------------------------------------
# AC6 + AC34 — members[] session_id keyed migration + rollback
# ---------------------------------------------------------------------------

def _legacy_doc_with_members():
    """A legacy (= user_id keyed) trek doc with session_history populated."""
    doc = trek.new_trek(
        title="t", creator_user_id="u-leader", creator_email="leader@b.com",
        creator_session_id="sv-leader",
    )
    # Add a second member via the legacy path (= user_id keyed entry, no
    # session_id), and seed session_history with their join.
    doc["members"].append(trek.build_member(
        user_id="u-2", email="m2@b.com", role="member",
        invited_at=trek.utcnow_iso(), joined_at=trek.utcnow_iso(),
        invited_by="u-leader",
    ))
    doc["session_history"].append({
        "session_id": "sv-member-2",
        "user_id": "u-2",
        "email": "m2@b.com",
        "joined_at": trek.utcnow_iso(),
        "role_at_join": "member",
    })
    # Strip session_id from the leader entry to mimic the pre ms-97 schema.
    doc["members"][0].pop("session_id", None)
    return doc


def test_members_are_session_id_keyed_detects_legacy_doc():
    doc = _legacy_doc_with_members()
    assert trek.members_are_session_id_keyed(doc) is False


def test_migrate_members_to_session_id_keyed_writes_backup():
    """AC34: original members[] is preserved at members_legacy_backup."""
    doc = _legacy_doc_with_members()
    legacy_snapshot = [dict(m) for m in doc["members"]]
    trek.migrate_members_to_session_id_keyed(doc)
    assert doc.get(trek.MEMBERS_LEGACY_BACKUP_KEY) == legacy_snapshot


def test_migrate_members_to_session_id_keyed_stamps_session_id():
    """AC6: each member gains the session_id resolved from session_history."""
    doc = _legacy_doc_with_members()
    trek.migrate_members_to_session_id_keyed(doc)
    members_by_user = {m["user_id"]: m for m in doc["members"]}
    assert members_by_user["u-leader"]["session_id"] == "sv-leader"
    assert members_by_user["u-2"]["session_id"] == "sv-member-2"


def test_migrate_members_is_idempotent():
    """Re-running migration on already-migrated doc is a no-op."""
    doc = _legacy_doc_with_members()
    trek.migrate_members_to_session_id_keyed(doc)
    first_ts = doc["updated_at"]
    first_backup = list(doc[trek.MEMBERS_LEGACY_BACKUP_KEY])
    # Mutate updated_at so we can detect a churn write.
    trek.migrate_members_to_session_id_keyed(doc)
    assert doc["updated_at"] == first_ts
    # Backup must not be overwritten.
    assert doc[trek.MEMBERS_LEGACY_BACKUP_KEY] == first_backup


def test_migrate_members_partial_when_history_missing():
    """If a member's user_id has no matching session_history, leave entry alone."""
    doc = _legacy_doc_with_members()
    # Add a member whose user has no entry in session_history.
    doc["members"].append(trek.build_member(
        user_id="u-orphan", email="orphan@b.com", role="member",
    ))
    trek.migrate_members_to_session_id_keyed(doc)
    orphan = next(m for m in doc["members"] if m["user_id"] == "u-orphan")
    assert "session_id" not in orphan or not orphan.get("session_id")
    # Other migrations still complete.
    leader = next(m for m in doc["members"] if m["user_id"] == "u-leader")
    assert leader["session_id"] == "sv-leader"


def test_rollback_members_migration_restores_legacy():
    """AC34: rollback restores the original user_id keyed members[]."""
    doc = _legacy_doc_with_members()
    legacy_snapshot = [dict(m) for m in doc["members"]]
    trek.migrate_members_to_session_id_keyed(doc)
    trek.rollback_members_migration(doc)
    assert doc["members"] == legacy_snapshot
    assert trek.MEMBERS_LEGACY_BACKUP_KEY not in doc


def test_rollback_is_noop_without_backup():
    """rollback on a doc that was never migrated is a no-op."""
    doc = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    snapshot = dict(doc)
    trek.rollback_members_migration(doc)
    # No backup → no change.
    assert doc["members"] == snapshot["members"]
    assert "updated_at" in doc


def test_find_member_by_session_id_returns_match():
    doc = _legacy_doc_with_members()
    trek.migrate_members_to_session_id_keyed(doc)
    leader = trek.find_member_by_session_id(doc, "sv-leader")
    assert leader is not None
    assert leader["user_id"] == "u-leader"


def test_find_member_by_session_id_none_for_unknown():
    doc = _legacy_doc_with_members()
    trek.migrate_members_to_session_id_keyed(doc)
    assert trek.find_member_by_session_id(doc, "sv-unknown") is None
    assert trek.find_member_by_session_id(doc, "") is None


# ---------------------------------------------------------------------------
# AC12 — Task slot ban
# ---------------------------------------------------------------------------

def test_is_task_slot_true_for_task_only_entry():
    assert trek.is_task_slot({"project": "p", "task": "e-1"}) is True


def test_is_task_slot_false_for_milestone_entry():
    assert trek.is_task_slot({"project": "p", "milestone": "ms-1"}) is False


def test_is_task_slot_false_for_project_wide():
    assert trek.is_task_slot({"project": "p"}) is False


def test_is_task_slot_false_for_operation_entry():
    assert trek.is_task_slot({"project": "p", "operation": "op-1"}) is False


def test_is_task_add_banned_under_task_slot_when_all_matching_are_task_only():
    """AC12: scope = [{project=p, task=e-1}] bans new task adds under p."""
    doc = {"scope": [{"project": "p", "task": "e-1"}]}
    assert trek.is_task_add_banned_under_task_slot(
        doc, target_project="p", target_milestone="ms-1",
    ) is True


def test_is_task_add_allowed_when_matching_entry_is_project_wide():
    """Project-wide grandfather wins (= task slot ban does not fire)."""
    doc = {"scope": [{"project": "p"}, {"project": "p", "task": "e-1"}]}
    assert trek.is_task_add_banned_under_task_slot(
        doc, target_project="p", target_milestone="ms-1",
    ) is False


def test_is_task_add_allowed_when_matching_entry_widens_to_target_ms():
    """Milestone-matching entry widens (= task slot ban does not fire)."""
    doc = {"scope": [
        {"project": "p", "milestone": "ms-1"},
        {"project": "p", "task": "e-9"},
    ]}
    assert trek.is_task_add_banned_under_task_slot(
        doc, target_project="p", target_milestone="ms-1",
    ) is False


def test_is_task_add_banned_false_when_project_not_in_scope():
    doc = {"scope": [{"project": "q", "task": "e-1"}]}
    assert trek.is_task_add_banned_under_task_slot(
        doc, target_project="p", target_milestone="ms-1",
    ) is False


def test_is_task_add_banned_false_when_args_empty():
    doc = {"scope": [{"project": "p", "task": "e-1"}]}
    assert trek.is_task_add_banned_under_task_slot(
        doc, target_project="", target_milestone="ms-1",
    ) is False
    assert trek.is_task_add_banned_under_task_slot(
        doc, target_project="p", target_milestone="",
    ) is False
