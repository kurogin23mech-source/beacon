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


# ---------------------------------------------------------------------------
# goal_state (ms-75 / e-1865)
# ---------------------------------------------------------------------------

def test_new_trek_goal_state_defaults_to_empty():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    # Field is always present so consumers can branch on "" without
    # KeyError. Empty = "leader decides", matching pre-e-1865 behaviour.
    assert t["goal_state"] == ""


def test_new_trek_with_goal_state():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        goal_state="customer profile diff < 1% across both projects",
    )
    assert t["goal_state"] == \
        "customer profile diff < 1% across both projects"


def test_set_goal_state_updates_value_and_bumps_updated_at():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    prior_updated = t["updated_at"]
    # ensure now() advances to a different microsecond
    import time
    time.sleep(0.001)
    out = trek.set_goal_state(t, goal_state="ship release v1.0")
    assert out["goal_state"] == "ship release v1.0"
    assert out["updated_at"] >= prior_updated


def test_set_goal_state_idempotent_no_op():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        goal_state="same value",
    )
    prior_updated = t["updated_at"]
    out = trek.set_goal_state(t, goal_state="same value")
    # No mutation when value is unchanged — keeps fixtures stable.
    assert out["updated_at"] == prior_updated


def test_set_goal_state_clear_via_empty_string():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        goal_state="something",
    )
    trek.set_goal_state(t, goal_state="")
    assert t["goal_state"] == ""


# ---------------------------------------------------------------------------
# cadence_minutes / manager_agent_url (ms-83 / e-1994)
# ---------------------------------------------------------------------------

def test_new_trek_defaults_meta_empty():
    """No cadence + no manager URL → meta is present but empty.

    Always-present empty dict keeps consumers from branching on KeyError
    vs. empty-dict (= the same pattern goal_state uses with empty string).
    """
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    assert t["meta"] == {}


def test_new_trek_with_cadence_minutes():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        cadence_minutes=15,
    )
    assert t["meta"]["cadence_minutes"] == 15


def test_new_trek_with_manager_agent_url():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        manager_agent_url="https://agents.example.com/trek-1",
    )
    assert (
        t["meta"]["manager_agent_url"]
        == "https://agents.example.com/trek-1"
    )


def test_new_trek_strips_manager_agent_url_whitespace():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        manager_agent_url="  https://x  ",
    )
    assert t["meta"]["manager_agent_url"] == "https://x"


@pytest.mark.parametrize("bad", [0, -1, -10])
def test_new_trek_rejects_non_positive_cadence(bad):
    with pytest.raises(ValueError):
        trek.new_trek(
            title="x", creator_user_id="u-1", creator_email="a@b.com",
            creator_session_id="sv-x",
            cadence_minutes=bad,
        )


def test_new_trek_rejects_bool_cadence():
    """``True`` is technically ``isinstance(True, int)`` → guard against
    accidental boolean coercion."""
    with pytest.raises(ValueError):
        trek.new_trek(
            title="x", creator_user_id="u-1", creator_email="a@b.com",
            creator_session_id="sv-x",
            cadence_minutes=True,  # type: ignore[arg-type]
        )


def test_get_cadence_minutes_default_when_unset():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    assert trek.get_cadence_minutes(t) == trek.DEFAULT_CADENCE_MINUTES
    assert trek.DEFAULT_CADENCE_MINUTES == 10


def test_get_cadence_minutes_returns_set_value():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        cadence_minutes=30,
    )
    assert trek.get_cadence_minutes(t) == 30


def test_set_cadence_minutes_updates():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    prior = t["updated_at"]
    import time
    time.sleep(0.001)
    trek.set_cadence_minutes(t, cadence_minutes=20)
    assert t["meta"]["cadence_minutes"] == 20
    assert t["updated_at"] > prior


def test_set_cadence_minutes_idempotent():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        cadence_minutes=20,
    )
    prior = t["updated_at"]
    trek.set_cadence_minutes(t, cadence_minutes=20)
    # No mutation → updated_at unchanged (fixtures stay stable).
    assert t["updated_at"] == prior


def test_set_cadence_minutes_clear_via_none():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        cadence_minutes=20,
    )
    trek.set_cadence_minutes(t, cadence_minutes=None)
    assert "cadence_minutes" not in t["meta"]
    # After clear, get_cadence_minutes falls back to default.
    assert trek.get_cadence_minutes(t) == trek.DEFAULT_CADENCE_MINUTES


def test_set_manager_agent_url_updates_and_idempotent():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    trek.set_manager_agent_url(
        t, manager_agent_url="https://example.com/agent"
    )
    assert (
        t["meta"]["manager_agent_url"] == "https://example.com/agent"
    )
    prior = t["updated_at"]
    trek.set_manager_agent_url(
        t, manager_agent_url="https://example.com/agent"
    )
    assert t["updated_at"] == prior


def test_set_manager_agent_url_clear_via_empty():
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
        manager_agent_url="https://example.com/agent",
    )
    trek.set_manager_agent_url(t, manager_agent_url="")
    assert "manager_agent_url" not in t["meta"]


# ---------------------------------------------------------------------------
# Trek task state machine (ms-75 / e-2048)
# ---------------------------------------------------------------------------

def test_new_trek_initializes_task_states_empty():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    assert t["task_states"] == {}


@pytest.mark.parametrize("good", ["working", "done", "waiting-review"])
def test_validate_task_state_accepts_valid(good):
    assert trek.validate_task_state(good) == good


@pytest.mark.parametrize("bad", ["", "WORKING", "Done", "pending", None])
def test_validate_task_state_rejects_invalid(bad):
    with pytest.raises(ValueError):
        trek.validate_task_state(bad)  # type: ignore[arg-type]


def test_get_task_state_returns_default_for_unknown():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    assert trek.get_task_state(t, "e-9999") == "working"


def test_set_task_state_records_state_and_metadata():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(
        t, task_id="e-100", state="done",
        updated_by_session_id="sv-exec", note="phase 2 land",
    )
    entry = t["task_states"]["e-100"]
    assert entry["state"] == "done"
    assert entry["updated_by_session_id"] == "sv-exec"
    assert entry["note"] == "phase 2 land"
    assert entry["updated_at"]


def test_set_task_state_validates_transition_from_default():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # Default state is "working", so "done" is allowed.
    trek.set_task_state(t, task_id="e-1", state="done")
    # From done, only "working" allowed.
    with pytest.raises(ValueError):
        trek.set_task_state(t, task_id="e-1", state="waiting-review")


def test_set_task_state_allows_done_back_to_working_then_waiting_review():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="done")
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="waiting-review")
    assert t["task_states"]["e-1"]["state"] == "waiting-review"


def test_set_task_state_no_op_transition_allowed():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    # Re-affirming working state should not raise.
    trek.set_task_state(t, task_id="e-1", state="working")
    assert t["task_states"]["e-1"]["state"] == "working"


def test_set_task_state_requires_task_id():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    with pytest.raises(ValueError):
        trek.set_task_state(t, task_id="", state="done")


def test_aggregate_task_state_empty_scope():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    agg = trek.aggregate_task_state(t, task_ids=[])
    assert agg["overall"] == "empty"
    assert agg["total"] == 0


def test_aggregate_task_state_active_when_default_state():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "active"
    assert agg["working"] == 2
    assert agg["done"] == 0


def test_aggregate_task_state_all_done():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="done")
    trek.set_task_state(t, task_id="e-2", state="done")
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "all-done"
    assert agg["done"] == 2


def test_aggregate_task_state_all_waiting_review():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="waiting-review")
    trek.set_task_state(t, task_id="e-2", state="waiting-review")
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "all-waiting-review"


def test_aggregate_task_state_all_terminal_mixed():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="done")
    trek.set_task_state(t, task_id="e-2", state="waiting-review")
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "all-terminal-mixed"
    assert agg["done"] == 1
    assert agg["waiting-review"] == 1


def test_aggregate_task_state_active_when_any_working():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="done")
    # e-2 stays at default working
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "active"
    assert agg["working"] == 1
    assert agg["done"] == 1
