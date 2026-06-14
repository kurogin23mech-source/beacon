"""Schema-level tests for member operations (ms-69 / e-1654).

Covers ``add_invitation`` / ``accept_invitation`` / ``remove_member`` +
their find helpers. CLI integration is exercised separately in
``test_trek_cli.py``.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


@pytest.fixture
def base_trek():
    """A fresh active trek with only the creator as leader/member."""
    return trek.new_trek(
        title="T",
        creator_user_id="u-1", creator_email="creator@x",
        creator_session_id="sv-1",
    )


# ---------------------------------------------------------------------------
# find_member / find_member_by_email
# ---------------------------------------------------------------------------

def test_find_member_by_user_id(base_trek):
    m = trek.find_member(base_trek, "u-1")
    assert m is not None
    assert m["email"] == "creator@x"


def test_find_member_missing(base_trek):
    assert trek.find_member(base_trek, "u-nope") is None


def test_find_member_by_email(base_trek):
    m = trek.find_member_by_email(base_trek, "creator@x")
    assert m is not None
    assert m["user_id"] == "u-1"


def test_find_member_by_email_missing(base_trek):
    assert trek.find_member_by_email(base_trek, "nope@x") is None


def test_find_member_by_email_empty_returns_none(base_trek):
    assert trek.find_member_by_email(base_trek, "") is None


# ---------------------------------------------------------------------------
# add_invitation
# ---------------------------------------------------------------------------

def test_add_invitation_appends_member(base_trek):
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    assert len(base_trek["members"]) == 2
    m = trek.find_member(base_trek, "u-2")
    assert m["email"] == "b@x"
    assert m["role"] == "member"
    assert m["joined_at"] == ""  # invited but not joined
    assert m["invited_by"] == "u-1"
    assert m["invited_at"]


def test_add_invitation_rejects_duplicate(base_trek):
    with pytest.raises(ValueError, match="already a member"):
        trek.add_invitation(
            base_trek, user_id="u-1", email="creator@x",
            invited_by_user_id="u-1",
        )


def test_add_invitation_updates_updated_at(base_trek):
    original = base_trek["updated_at"]
    # ensure clock moves forward
    import time
    time.sleep(0.001)
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    assert base_trek["updated_at"] > original


# ---------------------------------------------------------------------------
# accept_invitation
# ---------------------------------------------------------------------------

def test_accept_invitation_sets_joined_at(base_trek):
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    assert trek.find_member(base_trek, "u-2")["joined_at"] == ""
    trek.accept_invitation(base_trek, user_id="u-2")
    assert trek.find_member(base_trek, "u-2")["joined_at"]


def test_accept_invitation_idempotent(base_trek):
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    trek.accept_invitation(base_trek, user_id="u-2")
    first_joined = trek.find_member(base_trek, "u-2")["joined_at"]
    # second call is a no-op (= idempotent), joined_at preserved
    trek.accept_invitation(base_trek, user_id="u-2")
    assert trek.find_member(base_trek, "u-2")["joined_at"] == first_joined


def test_accept_invitation_requires_invitation(base_trek):
    with pytest.raises(ValueError, match="not invited"):
        trek.accept_invitation(base_trek, user_id="u-rando")


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------

def test_remove_member_basic(base_trek):
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    assert len(base_trek["members"]) == 2
    trek.remove_member(base_trek, user_id="u-2")
    assert len(base_trek["members"]) == 1
    assert trek.find_member(base_trek, "u-2") is None


def test_remove_member_missing(base_trek):
    with pytest.raises(ValueError, match="not a member"):
        trek.remove_member(base_trek, user_id="u-rando")


def test_remove_member_blocks_leader_removal(base_trek):
    """Leader cannot leave without transferring leadership first."""
    trek.add_invitation(base_trek, user_id="u-2", email="b@x",
                        invited_by_user_id="u-1")
    with pytest.raises(ValueError, match="cannot remove leader"):
        trek.remove_member(base_trek, user_id="u-1")


def test_remove_member_blocks_last_member(base_trek):
    """Cannot leave the trek as the only member — archive instead."""
    # base_trek has only the creator/leader
    # First demote leader by adding another member and... well, you can't
    # remove the leader; but the "last member" rule fires regardless.
    # Build a trek with no leader to isolate this case.
    t = {
        "trek_id": "tk-empty",
        "members": [
            {"user_id": "u-only", "email": "only@x", "role": "member",
             "invited_at": "x", "joined_at": "x", "invited_by": "x"},
        ],
        "updated_at": "",
    }
    with pytest.raises(ValueError, match="last member"):
        trek.remove_member(t, user_id="u-only")


# ---------------------------------------------------------------------------
# parse_scope_arg / add_scope_entry / remove_scope_entry (e-1655)
# ---------------------------------------------------------------------------

def test_parse_scope_arg_project_only():
    assert trek.parse_scope_arg("beacon-1") == {"project": "beacon-1"}


def test_parse_scope_arg_milestone():
    assert trek.parse_scope_arg("beacon-1:ms-64") == {
        "project": "beacon-1", "milestone": "ms-64",
    }


def test_parse_scope_arg_operation():
    assert trek.parse_scope_arg("beacon-1:op-2") == {
        "project": "beacon-1", "operation": "op-2",
    }


def test_parse_scope_arg_task():
    assert trek.parse_scope_arg("pe-1:e-1234") == {
        "project": "pe-1", "task": "e-1234",
    }


def test_parse_scope_arg_empty_ref_means_project_wide():
    """Trailing `:` is allowed → project-wide."""
    assert trek.parse_scope_arg("beacon-1:") == {"project": "beacon-1"}


def test_parse_scope_arg_unknown_ref_prefix():
    with pytest.raises(ValueError, match="unknown ref prefix"):
        trek.parse_scope_arg("beacon-1:foo-99")


def test_parse_scope_arg_empty_rejected():
    with pytest.raises(ValueError):
        trek.parse_scope_arg("")
    with pytest.raises(ValueError):
        trek.parse_scope_arg("   ")


def test_parse_scope_arg_missing_project():
    with pytest.raises(ValueError):
        trek.parse_scope_arg(":ms-1")


def test_add_scope_entry_basic(base_trek):
    assert base_trek["scope"] == []
    trek.add_scope_entry(base_trek, entry={"project": "p", "milestone": "ms-1"})
    assert base_trek["scope"] == [{"project": "p", "milestone": "ms-1"}]


def test_add_scope_entry_rejects_duplicate(base_trek):
    trek.add_scope_entry(base_trek, entry={"project": "p", "milestone": "ms-1"})
    with pytest.raises(ValueError, match="already exists"):
        trek.add_scope_entry(base_trek, entry={"project": "p", "milestone": "ms-1"})


def test_remove_scope_entry_basic(base_trek):
    trek.add_scope_entry(base_trek, entry={"project": "p", "milestone": "ms-1"})
    trek.add_scope_entry(base_trek, entry={"project": "q"})
    trek.remove_scope_entry(base_trek, entry={"project": "p", "milestone": "ms-1"})
    assert base_trek["scope"] == [{"project": "q"}]


def test_remove_scope_entry_missing(base_trek):
    with pytest.raises(ValueError, match="not found"):
        trek.remove_scope_entry(base_trek, entry={"project": "p"})
