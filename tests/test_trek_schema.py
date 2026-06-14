"""Unit tests for lib/trek.py — pure schema builders / validators (e-1652).

No DB I/O here: this file pins the schema shape and validation rules so
backend clients (firestore_client / dynamodb_client) can be tested
independently with a backend mock.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import trek  # noqa: E402


# ---------------------------------------------------------------------------
# mint_trek_id
# ---------------------------------------------------------------------------

def test_mint_trek_id_shape():
    tid = trek.mint_trek_id()
    assert tid.startswith("tk-")
    assert len(tid) == 3 + 8  # "tk-" + 8 hex
    assert all(c in "0123456789abcdef" for c in tid[3:])


def test_mint_trek_id_distinct():
    # 100 ids should all be distinct (8 hex = 32-bit space; 100 ≪ √2^32)
    ids = {trek.mint_trek_id() for _ in range(100)}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("good", ["temporary", "persistent"])
def test_validate_type_accepts_valid(good):
    assert trek.validate_type(good) == good


@pytest.mark.parametrize("bad", ["", "permanent", "TEMPORARY", None])
def test_validate_type_rejects_invalid(bad):
    with pytest.raises(ValueError):
        trek.validate_type(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("good", ["planning", "active", "archived"])
def test_validate_status_accepts_valid(good):
    assert trek.validate_status(good) == good


@pytest.mark.parametrize("bad", ["", "closed", "done", "paused", None])
def test_validate_status_rejects_invalid(bad):
    # "paused" is explicitly rejected: the 4-state machine collapsed into
    # 3 states (= STOP signal replaces pause, leader instruction replaces
    # resume).
    with pytest.raises(ValueError):
        trek.validate_status(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("good", ["leader", "member"])
def test_validate_role_accepts_valid(good):
    assert trek.validate_role(good) == good


@pytest.mark.parametrize("bad", ["viewer", "admin", "owner", ""])
def test_validate_role_rejects_invalid(bad):
    with pytest.raises(ValueError):
        trek.validate_role(bad)


# ---------------------------------------------------------------------------
# build_actor_ref / build_member
# ---------------------------------------------------------------------------

def test_build_actor_ref_basic():
    ref = trek.build_actor_ref(user_id="u-1", email="a@example.com")
    assert ref == {"user_id": "u-1", "email": "a@example.com"}


@pytest.mark.parametrize("uid,email", [("", "a@b"), ("u-1", ""), ("", "")])
def test_build_actor_ref_requires_both(uid, email):
    with pytest.raises(ValueError):
        trek.build_actor_ref(user_id=uid, email=email)


def test_build_member_defaults_invited_at_to_now():
    m = trek.build_member(user_id="u-1", email="a@b.com")
    assert m["role"] == "member"
    assert m["joined_at"] == ""
    assert m["invited_at"]  # non-empty ISO


def test_build_member_role_validated():
    with pytest.raises(ValueError):
        trek.build_member(user_id="u-1", email="a@b.com", role="viewer")


# ---------------------------------------------------------------------------
# normalize_scope_entry
# ---------------------------------------------------------------------------

def test_normalize_scope_keeps_project_only():
    out = trek.normalize_scope_entry({"project": "p-1"})
    assert out == {"project": "p-1"}


def test_normalize_scope_keeps_project_plus_milestone():
    out = trek.normalize_scope_entry({"project": "p-1", "milestone": "ms-3"})
    assert out == {"project": "p-1", "milestone": "ms-3"}


def test_normalize_scope_drops_unknown_keys():
    # Unknown keys must NOT survive to disk (= keeps schema tight).
    out = trek.normalize_scope_entry({
        "project": "p-1",
        "milestone": "ms-3",
        "spurious": "value",
        "rabbit": True,
    })
    assert out == {"project": "p-1", "milestone": "ms-3"}
    assert "spurious" not in out
    assert "rabbit" not in out


def test_normalize_scope_requires_project():
    with pytest.raises(ValueError):
        trek.normalize_scope_entry({"milestone": "ms-3"})


def test_normalize_scope_accepts_operation_or_task():
    assert trek.normalize_scope_entry(
        {"project": "p", "operation": "op-2"}
    )["operation"] == "op-2"
    assert trek.normalize_scope_entry(
        {"project": "p", "task": "e-1"}
    )["task"] == "e-1"


# ---------------------------------------------------------------------------
# new_trek — full doc builder
# ---------------------------------------------------------------------------

def test_new_trek_minimal():
    t = trek.new_trek(
        title="Daily Ops",
        creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-aaaa-12345",
    )
    assert t["trek_id"].startswith("tk-")
    assert t["title"] == "Daily Ops"
    assert t["description"] == ""
    assert t["type"] == "persistent"
    assert t["status"] == "planning"
    assert t["creator_actor"] == {"user_id": "u-1", "email": "a@b.com"}
    # SPEC 方針 9: leader is the session, not the user
    assert t["leader_session_id"] == "sv-aaaa-12345"
    # legacy field must not be present
    assert "leader_actor" not in t
    assert len(t["members"]) == 1
    leader = t["members"][0]
    assert leader["role"] == "leader"
    assert leader["user_id"] == "u-1"
    assert leader["joined_at"]  # leader auto-joined
    assert t["scope"] == []
    # SPEC 方針 2: halt is a separate field, not a status
    assert t["halt"] is None
    assert t["created_at"] == t["updated_at"]
    assert t["archived_at"] is None


def test_new_trek_with_scope():
    t = trek.new_trek(
        title="Cross-project release",
        creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-aaaa-12345",
        type_="temporary",
        initial_scope=[
            {"project": "beacon-1", "milestone": "ms-64"},
            {"project": "pe-1", "operation": "op-12"},
        ],
    )
    assert t["type"] == "temporary"
    assert len(t["scope"]) == 2
    assert t["scope"][0] == {"project": "beacon-1", "milestone": "ms-64"}
    assert t["scope"][1] == {"project": "pe-1", "operation": "op-12"}


def test_new_trek_requires_title():
    with pytest.raises(ValueError):
        trek.new_trek(title="   ", creator_user_id="u-1",
                      creator_email="a@b.com",
                      creator_session_id="sv-x")


def test_new_trek_requires_creator_session_id():
    # SPEC 方針 9: creator's session becomes the initial leader, can't be empty
    with pytest.raises(ValueError):
        trek.new_trek(
            title="x", creator_user_id="u-1", creator_email="a@b.com",
            creator_session_id="",
        )


def test_new_trek_rejects_bad_type():
    with pytest.raises(ValueError):
        trek.new_trek(
            title="x", creator_user_id="u-1", creator_email="a@b.com",
            creator_session_id="sv-x",
            type_="permanent",
        )


# ---------------------------------------------------------------------------
# build_halt
# ---------------------------------------------------------------------------

def test_build_halt_minimal():
    h = trek.build_halt(issued_by_session_id="sv-x")
    assert h["issued_by_session_id"] == "sv-x"
    assert h["reason"] == ""
    assert h["issued_at"]  # non-empty ISO


def test_build_halt_with_reason():
    h = trek.build_halt(issued_by_session_id="sv-x", reason="deploy in progress")
    assert h["reason"] == "deploy in progress"


def test_build_halt_requires_session():
    with pytest.raises(ValueError):
        trek.build_halt(issued_by_session_id="")


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("frm,to", [
    ("planning", "active"),
    ("planning", "archived"),
    ("active", "archived"),
])
def test_validate_transition_allowed(frm, to):
    trek.validate_transition(frm, to)  # should not raise


def test_validate_transition_same_state_is_noop():
    trek.validate_transition("active", "active")  # should not raise


@pytest.mark.parametrize("frm,to", [
    ("active", "planning"),     # cannot go back to planning
    ("archived", "planning"),   # archived is terminal
    ("archived", "active"),     # archived is terminal — recreate trek instead
    # "paused" was retired in this iteration — reject any pause-related move
    ("active", "paused"),
    ("planning", "paused"),
    ("paused", "active"),
])
def test_validate_transition_rejected(frm, to):
    with pytest.raises(ValueError):
        trek.validate_transition(frm, to)


def test_validate_transition_rejects_invalid_states():
    with pytest.raises(ValueError):
        trek.validate_transition("unknown", "active")
    with pytest.raises(ValueError):
        trek.validate_transition("active", "closed")
