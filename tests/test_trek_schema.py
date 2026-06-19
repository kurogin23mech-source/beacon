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


# ms-88 / e-2107 — 5-state model: working/done/waiting-review に加え todo /
# leader_review / user_review が valid。 legacy `waiting-review` も accept
# (= silent migration to leader_review)。
@pytest.mark.parametrize("good", [
    "todo", "working", "leader_review", "user_review", "done",
    "waiting-review",  # legacy alias, migrated transparently
])
def test_validate_task_state_accepts_valid(good):
    # legacy token は migrate されて新 token を返す
    expected = trek.migrate_legacy_task_state(good)
    assert trek.validate_task_state(good) == expected


@pytest.mark.parametrize("bad", ["", "WORKING", "Done", "pending", None])
def test_validate_task_state_rejects_invalid(bad):
    with pytest.raises(ValueError):
        trek.validate_task_state(bad)  # type: ignore[arg-type]


def test_get_task_state_returns_default_for_unknown():
    """ms-88 / e-2107: default が `working` → `todo` に変更 (= claim 経由を強制)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    assert trek.get_task_state(t, "e-9999") == "todo"


def test_set_task_state_records_state_and_metadata():
    """ms-88 / e-2107: 5 状態 model 経由でメタデータが残る (claim → done)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-100", state="working",
                        updated_by_session_id="sv-exec")
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
    """ms-88 / e-2107: default = `todo` から `done` 直接遷移は禁止 (claim 必須)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # todo → done は禁止
    with pytest.raises(ValueError):
        trek.set_task_state(t, task_id="e-1", state="done")
    # todo → working なら可、 working → done OK
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="done")
    # done → leader_review は禁止 (= done から working 経由のみ)
    with pytest.raises(ValueError):
        trek.set_task_state(t, task_id="e-1", state="leader_review")


def test_set_task_state_allows_done_back_to_working_then_user_review():
    """ms-88 / e-2107: done → working → user_review の経路。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="done")
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="user_review")
    assert t["task_states"]["e-1"]["state"] == "user_review"


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
    """ms-88 / e-2107: default state is now `todo` (= 着手前)、 active 判定は
    todo / working / leader_review のいずれかが残っていれば真。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "active"
    # default が todo に変わったため todo=2、 working=0
    assert agg["todo"] == 2
    assert agg["working"] == 0
    assert agg["done"] == 0


def test_aggregate_task_state_all_done():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # todo → working → done (= claim 経路を通る)
    for tid in ("e-1", "e-2"):
        trek.set_task_state(t, task_id=tid, state="working")
        trek.set_task_state(t, task_id=tid, state="done")
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "all-done"
    assert agg["done"] == 2


def test_aggregate_task_state_all_user_review():
    """ms-88 / e-2107: all-user-review は Trek 完遂等価 (= leader が user に forward 済)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    for tid in ("e-1", "e-2"):
        trek.set_task_state(t, task_id=tid, state="working")
        trek.set_task_state(t, task_id=tid, state="user_review")
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "all-user-review"


def test_aggregate_task_state_leader_review_keeps_trek_active():
    """leader_review は terminal ではない (= Trek 完遂判定で active) ことを確認。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="leader_review")
    agg = trek.aggregate_task_state(t, task_ids=["e-1"])
    assert agg["overall"] == "active"
    assert agg["leader_review"] == 1


def test_aggregate_task_state_all_terminal_mixed():
    """ms-88 / e-2107: terminal mixed は done + user_review の組合せ。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="done")
    trek.set_task_state(t, task_id="e-2", state="working")
    trek.set_task_state(t, task_id="e-2", state="user_review")
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "all-terminal-mixed"
    assert agg["done"] == 1
    assert agg["user_review"] == 1
    # backward-compat alias: waiting-review = leader_review + user_review
    assert agg["waiting-review"] == 1


def test_aggregate_task_state_active_when_any_working():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    trek.set_task_state(t, task_id="e-1", state="done")
    # e-2 stays at default todo
    agg = trek.aggregate_task_state(t, task_ids=["e-1", "e-2"])
    assert agg["overall"] == "active"
    assert agg["todo"] == 1
    assert agg["done"] == 1


# ---------------------------------------------------------------------------
# ms-88 / e-2107 — 5-state state machine + legacy migration
# ---------------------------------------------------------------------------

def test_5_state_machine_full_executor_path():
    """todo → working → done の executor 経路が通る。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    assert trek.get_task_state(t, "e-x") == "todo"
    trek.set_task_state(t, task_id="e-x", state="working")
    assert trek.get_task_state(t, "e-x") == "working"
    trek.set_task_state(t, task_id="e-x", state="done")
    assert trek.get_task_state(t, "e-x") == "done"


def test_5_state_machine_leader_review_path():
    """working → leader_review → done (= leader approve) の経路。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-x", state="working")
    trek.set_task_state(t, task_id="e-x", state="leader_review")
    trek.set_task_state(t, task_id="e-x", state="done")
    assert trek.get_task_state(t, "e-x") == "done"


def test_5_state_machine_leader_forward_to_user_path():
    """working → leader_review → user_review → done (= leader forward + user OK)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-x", state="working")
    trek.set_task_state(t, task_id="e-x", state="leader_review")
    trek.set_task_state(t, task_id="e-x", state="user_review")
    trek.set_task_state(t, task_id="e-x", state="done")
    assert trek.get_task_state(t, "e-x") == "done"


def test_5_state_machine_leader_rework_path():
    """leader_review → working (= leader re-work) の経路。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-x", state="working")
    trek.set_task_state(t, task_id="e-x", state="leader_review")
    trek.set_task_state(t, task_id="e-x", state="working")  # rework
    assert trek.get_task_state(t, "e-x") == "working"


def test_5_state_machine_user_rework_path():
    """user_review → working (= user 修正要請) の経路。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-x", state="working")
    trek.set_task_state(t, task_id="e-x", state="user_review")
    trek.set_task_state(t, task_id="e-x", state="working")
    assert trek.get_task_state(t, "e-x") == "working"


def test_5_state_machine_invalid_transitions_rejected():
    """todo → done は禁止 (= claim 経由必須)、 leader_review → leader_review 以外
    の不正経路は ValueError。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # todo → done は禁止 (claim 経由必須)
    with pytest.raises(ValueError):
        trek.set_task_state(t, task_id="e-x", state="done")
    # working → todo も禁止
    trek.set_task_state(t, task_id="e-y", state="working")
    with pytest.raises(ValueError):
        trek.set_task_state(t, task_id="e-y", state="todo")


def test_legacy_waiting_review_migrates_to_leader_review_on_set():
    """旧 `waiting-review` を set すると 透過的に `leader_review` に migrate される。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-x", state="working")
    trek.set_task_state(t, task_id="e-x", state="waiting-review")
    # 内部 state は新 token、 get_task_state も新 token を返す
    assert trek.get_task_state(t, "e-x") == "leader_review"
    assert t["task_states"]["e-x"]["state"] == "leader_review"


def test_legacy_waiting_review_migrates_on_read():
    """既存 data に `state="waiting-review"` で書かれていても get で `leader_review` を返す。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # 旧 schema を直接埋め込む (= migration 経路を通さない)
    t["task_states"] = {"e-old": {"state": "waiting-review"}}
    assert trek.get_task_state(t, "e-old") == "leader_review"


def test_default_ttl_changed_to_12_minutes():
    """ms-88 / e-2107: DEFAULT_WORKING_TTL_MINUTES 30 → 12 短縮。"""
    assert trek.DEFAULT_WORKING_TTL_MINUTES == 12


# ---------------------------------------------------------------------------
# ms-88 / e-2109 — per-session scheduler fanout filter helpers
# ---------------------------------------------------------------------------

def test_session_has_active_claim_true_for_working_claim():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working",
                        updated_by_session_id="sv-exec")
    assert trek.session_has_active_claim(t, session_id="sv-exec") is True


def test_session_has_active_claim_false_for_leader_review_claim():
    """leader_review は scheduler に「leader 待ち」 を伝える状態、 当該 session
    には tick 不要。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working",
                        updated_by_session_id="sv-exec")
    trek.set_task_state(t, task_id="e-1", state="leader_review",
                        updated_by_session_id="sv-exec")
    assert trek.session_has_active_claim(t, session_id="sv-exec") is False


def test_session_has_active_claim_false_for_done_claim():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working",
                        updated_by_session_id="sv-exec")
    trek.set_task_state(t, task_id="e-1", state="done",
                        updated_by_session_id="sv-exec")
    assert trek.session_has_active_claim(t, session_id="sv-exec") is False


def test_session_has_active_claim_true_when_one_of_multiple_is_working():
    """A session with 1 done + 1 working should still be active (= 1 claim 残り)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working",
                        updated_by_session_id="sv-exec")
    trek.set_task_state(t, task_id="e-1", state="done",
                        updated_by_session_id="sv-exec")
    trek.set_task_state(t, task_id="e-2", state="working",
                        updated_by_session_id="sv-exec")
    assert trek.session_has_active_claim(t, session_id="sv-exec") is True


def test_session_has_active_claim_false_for_session_with_no_claims():
    """Fresh session with no claims should return False (= caller decides
    fallback)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    assert trek.session_has_active_claim(t, session_id="sv-fresh") is False


def test_session_has_any_claim_distinguishes_fresh_vs_finished():
    """fresh session (= no claims at all) vs finished session (= claims but
    all terminal) を区別する helper。 fan-out fallback の核。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # sv-fresh: 何も claim していない
    assert trek.session_has_any_claim(t, session_id="sv-fresh") is False
    # sv-finished: 全部 done
    trek.set_task_state(t, task_id="e-1", state="working",
                        updated_by_session_id="sv-finished")
    trek.set_task_state(t, task_id="e-1", state="done",
                        updated_by_session_id="sv-finished")
    assert trek.session_has_any_claim(t, session_id="sv-finished") is True
    # 同 trek でも別 session
    assert trek.session_has_active_claim(t, session_id="sv-finished") is False
    assert trek.session_has_active_claim(t, session_id="sv-fresh") is False


def test_session_has_active_claim_legacy_waiting_review_treated_as_terminal_ish():
    """legacy `waiting-review` (= 旧 schema) は migrate されて leader_review
    扱い、 active claim ではなくなる。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # 古い data を直接埋め込む (= migration を経由しない)
    t["task_states"] = {
        "e-old": {
            "state": "waiting-review",
            "updated_by_session_id": "sv-legacy",
        }
    }
    assert trek.session_has_active_claim(t, session_id="sv-legacy") is False
    assert trek.session_has_any_claim(t, session_id="sv-legacy") is True


def test_force_stall_session_working_tasks_bulk_transitions():
    """server-side 罰則: 当該 session の全 working を leader_review に一括遷移。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working",
                        updated_by_session_id="sv-target")
    trek.set_task_state(t, task_id="e-2", state="working",
                        updated_by_session_id="sv-target")
    trek.set_task_state(t, task_id="e-3", state="working",
                        updated_by_session_id="sv-other")
    transitioned = trek.force_stall_session_working_tasks(
        t, session_id="sv-target", reason="ttl-12min-expired",
    )
    assert set(transitioned) == {"e-1", "e-2"}
    assert trek.get_task_state(t, "e-1") == "leader_review"
    assert trek.get_task_state(t, "e-2") == "leader_review"
    # 別 session の task は unchanged
    assert trek.get_task_state(t, "e-3") == "working"


# ---------------------------------------------------------------------------
# ms-88 / e-2138 — Trek Kickoff Ritual helpers
# ---------------------------------------------------------------------------

def test_get_kickoff_pending_returns_true_for_unknown_session():
    """lazy init: kickoff_status に entry がなければ pending=True (= 未送信)。
    既存 trek docs (= deploy 前データ) も次の interaction で kickoff 強制。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    assert trek.get_kickoff_pending(t, session_id="sv-fresh") is True


def test_mark_kickoff_completed_flips_pending_and_stamps():
    """mark_kickoff_completed が pending=false + sent_at を立てる。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    trek.mark_kickoff_completed(
        t, session_id="sv-exec-1", user_id="u-1",
        kickoff_dm_event_id="ev-abc",
    )
    assert trek.get_kickoff_pending(t, session_id="sv-exec-1") is False
    entry = (t.get("kickoff_status") or {}).get("sv-exec-1")
    assert entry is not None
    assert entry["pending"] is False
    assert entry["user_id"] == "u-1"
    assert entry["kickoff_dm_event_id"] == "ev-abc"
    assert entry["sent_at"]


def test_mark_kickoff_completed_is_idempotent():
    """既 完了 session に対する再 mark は sent_at を上書きしない (= 1 回送ったらそのまま)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    trek.mark_kickoff_completed(t, session_id="sv-exec-1", user_id="u-1")
    first_stamp = (t.get("kickoff_status") or {})["sv-exec-1"]["sent_at"]
    import time
    time.sleep(0.01)
    trek.mark_kickoff_completed(t, session_id="sv-exec-1", user_id="u-1")
    second_stamp = (t.get("kickoff_status") or {})["sv-exec-1"]["sent_at"]
    assert first_stamp == second_stamp


def test_reset_kickoff_pending_forces_pending_true_for_take_over():
    """take-over で fresh session が leadership を継ぐ時、 kickoff 強制 reset。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader-old",
    )
    # 前 session で kickoff 完了済
    trek.mark_kickoff_completed(t, session_id="sv-leader-new", user_id="u-1")
    assert trek.get_kickoff_pending(t, session_id="sv-leader-new") is False
    # take-over → 新 session の kickoff_pending を再 true 化
    trek.reset_kickoff_pending(t, session_id="sv-leader-new", user_id="u-1")
    assert trek.get_kickoff_pending(t, session_id="sv-leader-new") is True


def test_mark_kickoff_completed_requires_session_id():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    with pytest.raises(ValueError):
        trek.mark_kickoff_completed(t, session_id="", user_id="u-1")


def test_summarize_kickoff_status_pending_vs_completed():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    # 1 件完了、 1 件 pending (= reset で強制)
    trek.mark_kickoff_completed(t, session_id="sv-done", user_id="u-1")
    trek.reset_kickoff_pending(t, session_id="sv-pending", user_id="u-2")
    s = trek.summarize_kickoff_status(t)
    assert s["pending_count"] == 1
    assert s["completed_count"] == 1
    assert s["pending_sessions"][0]["session_id"] == "sv-pending"
    assert s["completed_sessions"][0]["session_id"] == "sv-done"


# ---------------------------------------------------------------------------
# Working-state TTL safety net (ms-75 / e-2067)
# ---------------------------------------------------------------------------

def test_set_task_state_stamps_last_activity_at():
    """ms-75 / e-2067 AC 1 — state stamp is one of the documented activity
    sources, so it must seed ``last_activity_at`` alongside ``updated_at``."""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    entry = t["task_states"]["e-1"]
    assert entry["last_activity_at"]
    # last_activity_at matches the same moment as updated_at on initial
    # write (= no clock drift between the two stamps).
    assert entry["last_activity_at"] == entry["updated_at"]


def test_bump_task_activity_refreshes_last_activity_at_without_state_change():
    """ms-75 / e-2067 AC 1 — commits / DM receipts bump activity without
    transitioning the state. The state stays 'working' but the auto-stall
    clock resets."""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.set_task_state(t, task_id="e-1", state="working")
    initial_state = t["task_states"]["e-1"]["state"]
    initial_activity = t["task_states"]["e-1"]["last_activity_at"]
    import time
    time.sleep(0.01)  # Force a different ISO timestamp.
    trek.bump_task_activity(t, task_id="e-1", reason="commit:abc123")
    entry = t["task_states"]["e-1"]
    assert entry["state"] == initial_state  # No state change.
    assert entry["last_activity_at"] > initial_activity
    assert entry["last_activity_reason"] == "commit:abc123"


def test_bump_task_activity_initializes_entry_for_unknown_task():
    """ms-88 / e-2107: default state is now `todo`、 bump_task_activity が
    fresh task に対して刻む entry も `todo` を seed する (= claim 未経路)。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.bump_task_activity(t, task_id="e-new", reason="dm-receipt")
    entry = t["task_states"]["e-new"]
    assert entry["state"] == "todo"  # ms-88 / e-2107: default が todo に変更
    assert entry["last_activity_at"]


def test_bump_task_activity_requires_task_id():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    with pytest.raises(ValueError):
        trek.bump_task_activity(t, task_id="", reason="x")


def test_get_working_ttl_minutes_default_when_unset():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    # ms-88 / e-2107: TTL default 30 → 12 min (= scheduler cadence + 2 min バッファ)。
    assert trek.get_working_ttl_minutes(t) == 12


def test_get_working_ttl_minutes_honors_meta_override():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    t.setdefault("meta", {})["working_ttl_minutes"] = 5
    assert trek.get_working_ttl_minutes(t) == 5


def test_get_working_ttl_minutes_falls_back_on_non_numeric_override():
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    t.setdefault("meta", {})["working_ttl_minutes"] = "garbage"
    # Bad config must not crash; fall back to default so safety net stays on.
    # ms-88 / e-2107: default 30 → 12 min。
    assert trek.get_working_ttl_minutes(t) == 12
