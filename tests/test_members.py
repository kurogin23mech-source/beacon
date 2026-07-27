"""Unit tests for member management (e-624, e-625).

These tests exercise the pure `core` functions only — no I/O, no fcntl,
no apply_operation. Concurrency is already covered by lib/operations.py's
own test suite; here we just verify the data transformation is correct.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def empty_project():
    return {"name": "t", "milestones": [], "summary": ""}


# ---------------------------------------------------------------------------
# members_list
# ---------------------------------------------------------------------------

def test_members_list_empty_project_returns_empty_list():
    data = empty_project()
    assert core.members_list(data) == []


def test_members_list_returns_existing_members():
    data = empty_project()
    data["members"] = [{"id": "alice", "role": "owner"}]
    assert len(core.members_list(data)) == 1
    assert core.members_list(data)[0]["id"] == "alice"


def test_members_list_heals_corrupt_field():
    """If someone hand-edited project.json and put a non-list in `members`,
    we must return [] rather than crash."""
    data = empty_project()
    data["members"] = "garbage"  # malformed
    assert core.members_list(data) == []


# ---------------------------------------------------------------------------
# member_add
# ---------------------------------------------------------------------------

def test_member_add_creates_member_with_required_fields():
    data = empty_project()
    m = core.member_add(data, "alice", name="Alice", email="a@x", role="maintainer")
    assert m["id"] == "alice"
    assert m["name"] == "Alice"
    assert m["email"] == "a@x"
    assert m["role"] == "maintainer"
    assert "added_at" in m
    assert "added_by" in m
    # Project dict is mutated
    assert data["members"] == [m]


def test_member_add_defaults_role_to_contributor():
    data = empty_project()
    m = core.member_add(data, "bob")
    assert m["role"] == "contributor"
    assert m["name"] == "bob"  # name defaults to id


def test_member_add_rejects_invalid_role():
    data = empty_project()
    with pytest.raises(ValueError) as exc:
        core.member_add(data, "alice", role="superadmin")
    assert "Invalid role" in str(exc.value)


def test_member_add_rejects_duplicate_id():
    data = empty_project()
    core.member_add(data, "alice")
    with pytest.raises(ValueError) as exc:
        core.member_add(data, "alice")
    assert "already exists" in str(exc.value)


def test_member_add_rejects_empty_id():
    data = empty_project()
    with pytest.raises(ValueError):
        core.member_add(data, "")
    with pytest.raises(ValueError):
        core.member_add(data, "   ")


# ---------------------------------------------------------------------------
# member_remove
# ---------------------------------------------------------------------------

def test_member_remove_removes_existing_member():
    data = empty_project()
    core.member_add(data, "alice", role="contributor")
    core.member_add(data, "bob", role="maintainer")
    removed = core.member_remove(data, "alice", reason="left team")
    assert removed["id"] == "alice"
    assert len(data["members"]) == 1
    assert data["members"][0]["id"] == "bob"


def test_member_remove_raises_when_not_found():
    data = empty_project()
    with pytest.raises(ValueError) as exc:
        core.member_remove(data, "ghost", reason="x")
    assert "not found" in str(exc.value)


def test_member_remove_refuses_to_remove_owner():
    """Last-owner protection — must escalate someone else first."""
    data = empty_project()
    core.member_add(data, "alice", role="owner")
    core.member_add(data, "bob", role="maintainer")
    with pytest.raises(ValueError) as exc:
        core.member_remove(data, "alice", reason="x")
    assert "Cannot remove owner" in str(exc.value)
    # Project state unchanged
    assert len(data["members"]) == 2


# ---------------------------------------------------------------------------
# member_set_role
# ---------------------------------------------------------------------------

def test_member_set_role_changes_role():
    data = empty_project()
    core.member_add(data, "alice", role="contributor")
    updated = core.member_set_role(data, "alice", "maintainer")
    assert updated["role"] == "maintainer"
    assert data["members"][0]["role"] == "maintainer"


def test_member_set_role_promotes_to_owner():
    data = empty_project()
    core.member_add(data, "alice", role="maintainer")
    updated = core.member_set_role(data, "alice", "owner")
    assert updated["role"] == "owner"


def test_member_set_role_rejects_invalid_role():
    data = empty_project()
    core.member_add(data, "alice")
    with pytest.raises(ValueError):
        core.member_set_role(data, "alice", "godmode")


def test_member_set_role_raises_when_not_found():
    data = empty_project()
    with pytest.raises(ValueError):
        core.member_set_role(data, "ghost", "viewer")


# ---------------------------------------------------------------------------
# milestone owner / assignee (e-625)
# ---------------------------------------------------------------------------

def test_milestone_add_accepts_owner_and_assignee():
    data = empty_project()
    ms_id = core.milestone_add(data, "test", owner="alice", assignee="bob", allow_untriaged=True)
    ms = data["milestones"][0]
    assert ms["id"] == ms_id
    assert ms["owner"] == "alice"
    assert ms["assignee"] == "bob"


def test_milestone_add_omits_fields_when_unset():
    """Don't pollute the doc with empty owner/assignee fields."""
    data = empty_project()
    core.milestone_add(data, "test", allow_untriaged=True)
    ms = data["milestones"][0]
    assert "owner" not in ms
    assert "assignee" not in ms


def test_milestone_update_sets_owner_assignee():
    data = empty_project()
    core.milestone_add(data, "test", allow_untriaged=True)
    ms_id = data["milestones"][0]["id"]
    ms = core.milestone_update(data, ms_id, owner="alice", assignee="bob")
    assert ms["owner"] == "alice"
    assert ms["assignee"] == "bob"


def test_milestone_update_clears_owner_with_dash():
    """`--owner -` is the convention for clearing the field."""
    data = empty_project()
    core.milestone_add(data, "test", owner="alice", allow_untriaged=True)
    ms_id = data["milestones"][0]["id"]
    ms = core.milestone_update(data, ms_id, owner="-")
    assert ms["owner"] == ""


def test_milestone_update_leaves_owner_unchanged_when_omitted():
    data = empty_project()
    core.milestone_add(data, "test", owner="alice", allow_untriaged=True)
    ms_id = data["milestones"][0]["id"]
    ms = core.milestone_update(data, ms_id, title="new title")
    assert ms["owner"] == "alice"  # untouched
    assert ms["title"] == "new title"
