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

def test_normalize_scope_keeps_project_only_legacy():
    # ms-97 / e-2659 (AC8): strict mode now rejects project-wide entries
    # (= no narrowing key), but the non-strict path is retained for
    # reading grandfathered on-disk rows. Lock both halves of the contract.
    out = trek.normalize_scope_entry({"project": "p-1"}, strict=False)
    assert out == {"project": "p-1"}
    with pytest.raises(ValueError, match="narrowing key"):
        trek.normalize_scope_entry({"project": "p-1"})


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
    """No cadence + no manager URL → meta carries only the G6 (e-2660)
    completion-flow seed fields, all set to ``None``.

    ms-97 / Phase 7-A — ``new_trek`` seeds three tracking fields
    (``completion_notified_at`` / ``summary_sent_at`` /
    ``stall_threshold_minutes``) so AC20 / AC21 / AC11 readers find a
    stable invariant from t=0 instead of branching on
    KeyError-vs-None. Cadence + manager URL stay opt-in.
    """
    t = trek.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-x",
    )
    assert t["meta"] == {
        "completion_notified_at": None,
        "summary_sent_at": None,
        "stall_threshold_minutes": None,
    }


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
# ms-97 Phase 4 / AC10 — MS slot precedence
# ---------------------------------------------------------------------------

def test_compute_ms_slot_state_leader_review_wins():
    """precedence #1: any leader_review child → slot is leader_review.

    Even if other children are done / user_review / working / todo,
    leader_review wins because the leader's queue is the most
    actionable / blocking state.
    """
    children = [
        {"state": "done"},
        {"state": "leader_review"},
        {"state": "working"},
        {"state": "todo"},
    ]
    assert trek.compute_ms_slot_state(children) == "leader_review"


def test_compute_ms_slot_state_all_done():
    """precedence #2: all done → slot is done."""
    children = [{"state": "done"}, {"state": "done"}]
    assert trek.compute_ms_slot_state(children) == "done"


def test_compute_ms_slot_state_mixed_terminal_user_review():
    """precedence #3: terminal mix (done + user_review) → slot is user_review.

    All children are terminal but not all done → user_review wins
    because the slot needs user judgment to fully close out.
    """
    children = [{"state": "done"}, {"state": "user_review"}]
    assert trek.compute_ms_slot_state(children) == "user_review"


def test_compute_ms_slot_state_working_presence():
    """precedence #4: any working child (no leader_review / no all-terminal) → working."""
    children = [
        {"state": "todo"},
        {"state": "working"},
        {"state": "done"},
    ]
    assert trek.compute_ms_slot_state(children) == "working"


def test_compute_ms_slot_state_all_todo():
    """precedence #5: all todo (or empty slot) → todo."""
    children = [{"state": "todo"}, {"state": "todo"}]
    assert trek.compute_ms_slot_state(children) == "todo"
    # empty slot collapses to todo as well.
    assert trek.compute_ms_slot_state([]) == "todo"


def test_compute_ms_slot_state_unknown_state_collapses_to_todo():
    """defensive: garbage / unknown state tokens collapse to todo for counting.

    A future-version of trek doc with a state token this version
    doesn't recognise must not crash the helper; it falls through
    to the all-todo path.
    """
    children = [{"state": "future-state-xyz"}, {"state": "todo"}]
    assert trek.compute_ms_slot_state(children) == "todo"


# ---------------------------------------------------------------------------
# ms-97 Phase 4 / AC14 — resolve_session_home_project
# ---------------------------------------------------------------------------

def test_resolve_session_home_project_found_on_first_pid():
    """First scope project wins when session is registered there."""
    def lister(pid):
        if pid == "proj-A":
            return [{"session_id": "sv-exec"}, {"session_id": "sv-other"}]
        return []
    home = trek.resolve_session_home_project(
        "sv-exec", ["proj-A", "proj-B"], lister,
    )
    assert home == "proj-A"


def test_resolve_session_home_project_found_on_second_pid():
    """Walks the list in order — second pid hit returned correctly."""
    def lister(pid):
        if pid == "proj-B":
            return [{"session_id": "sv-exec"}]
        return []
    home = trek.resolve_session_home_project(
        "sv-exec", ["proj-A", "proj-B"], lister,
    )
    assert home == "proj-B"


def test_resolve_session_home_project_no_match_returns_empty():
    """Ghost session (registered nowhere in scope) returns ``""``."""
    home = trek.resolve_session_home_project(
        "sv-ghost", ["proj-A", "proj-B"],
        lambda _pid: [{"session_id": "sv-other"}],
    )
    assert home == ""


def test_resolve_session_home_project_empty_inputs():
    """Empty session_id or empty scope returns ``""`` immediately."""
    assert trek.resolve_session_home_project(
        "", ["proj-A"], lambda _pid: [{"session_id": "sv-x"}],
    ) == ""
    assert trek.resolve_session_home_project(
        "sv-exec", [], lambda _pid: [{"session_id": "sv-exec"}],
    ) == ""


def test_resolve_session_home_project_handles_lister_failure():
    """Listing exceptions on one pid don't poison the walk."""
    def flaky(pid):
        if pid == "proj-A":
            raise RuntimeError("listing failed")
        return [{"session_id": "sv-exec"}]
    home = trek.resolve_session_home_project(
        "sv-exec", ["proj-A", "proj-B"], flaky,
    )
    assert home == "proj-B"


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


def test_default_ttl_changed_to_24h():
    """ms-88 / e-2107 → ms-95 / e-2646: DEFAULT_WORKING_TTL_MINUTES 履歴
    30 → 12 → 1440 (24h)。 2026-06-28 dogfood で 12 min は prep 待機中の
    executor を「stuck」 と誤判定する病理を生んだので、 24h baseline に
    再緩和。 「すぐ stall 判定したい」 個別 Trek は meta.stall_threshold_minutes
    で短い override 可能 (= 機能変更ではなく default だけ動かす)。"""
    assert trek.DEFAULT_WORKING_TTL_MINUTES == 1440


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
    # ms-95 / e-2646: TTL default は 24h (= 1440 min) に再緩和。 prep 待機中
    # の executor を「stuck」 と誤判定する dogfood 病理を構造的に絶つ。
    assert trek.get_working_ttl_minutes(t) == 1440


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
    # ms-95 / e-2646: default 12 → 1440 (24h)。
    assert trek.get_working_ttl_minutes(t) == 1440


# ---------------------------------------------------------------------------
# ms-88 / e-2139 — 5-choice executor picker (= /beacon-trek-pulse)
#
# `dm-peer` を 4 択 (terminal / continue / dm-leader / no-op) に加えて 5 択
# 化する。 詰まった時の default を「user に問う」 から「peer に相談する」 に
# 移すことで、 user 起床まで Trek が autonomous に走り続ける経路を確立する
# (= peer-first culture の構造実装、 ms-88 / e-2140 と組み合わせる)。
# ---------------------------------------------------------------------------

def test_valid_pulse_picked_choices_includes_5_executor_choices_plus_legacy_empty():
    """5 択 (terminal / continue / dm-leader / dm-peer / no-op) + legacy '' を含む。"""
    expected = {"terminal", "continue", "dm-leader", "dm-peer", "no-op", ""}
    assert set(trek.VALID_PULSE_PICKED_CHOICES) == expected


def test_validate_pulse_picked_choice_accepts_dm_peer():
    """e-2139 で追加した 'dm-peer' (= 横向き相談) が validation を通る。"""
    assert trek.validate_pulse_picked_choice("dm-peer") == "dm-peer"


def test_validate_pulse_picked_choice_accepts_each_of_the_5_choices():
    """5 択すべてと空文字が known token として通る。"""
    for choice in ("terminal", "continue", "dm-leader", "dm-peer", "no-op", ""):
        assert trek.validate_pulse_picked_choice(choice) == choice


def test_validate_pulse_picked_choice_rejects_unknown_token():
    """known 5 + '' 以外は ValueError、 期待 list を error 文に含む。"""
    with pytest.raises(ValueError) as excinfo:
        trek.validate_pulse_picked_choice("bogus")
    msg = str(excinfo.value)
    # Sanity-check the diagnostic mentions both the bad token and the
    # canonical list so executors can fix typos without grep'ing source.
    assert "bogus" in msg
    assert "dm-peer" in msg


def test_record_pulse_ack_persists_dm_peer_choice():
    """record_pulse_ack が dm-peer を last_picked_choice / history に書き込む。"""
    t = trek.new_trek(
        title="t", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )
    trek.record_pulse_ack(t, session_id="sv-exec-x", picked_choice="dm-peer",
                          note="peer に設計判断を相談")
    entry = t["pulse_acks"]["sv-exec-x"]
    assert entry["total_acks"] == 1
    assert entry["last_picked_choice"] == "dm-peer"
    assert entry["history"][0]["picked_choice"] == "dm-peer"
    assert entry["history"][0]["note"] == "peer に設計判断を相談"


# ---------------------------------------------------------------------------
# ms-92 / e-2165 — pulse-ack structured payload fields
# ---------------------------------------------------------------------------


def _seed_trek_for_pulse():
    return trek.new_trek(
        title="e2165-test", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-1",
    )


def test_record_pulse_ack_persists_structured_state_summary():
    """state_summary は record_pulse_ack で history + session-summary に書かれる。"""
    t = _seed_trek_for_pulse()
    trek.record_pulse_ack(
        t, session_id="sv-exec", picked_choice="continue",
        state_summary="working on e-2165 schema fields",
        time_on_task_seconds=1200,
    )
    entry = t["pulse_acks"]["sv-exec"]
    # session-level summary mirror (= for digest aggregation)
    assert entry["last_state_summary"] == "working on e-2165 schema fields"
    assert entry["last_time_on_task_seconds"] == 1200
    # history record
    h = entry["history"][0]
    assert h["state_summary"] == "working on e-2165 schema fields"
    assert h["time_on_task_seconds"] == 1200


def test_record_pulse_ack_persists_blockers_with_cap():
    """blockers リストは ≤3 件 ≤200 字 / 件で truncate される。"""
    t = _seed_trek_for_pulse()
    long_blocker = "x" * 250  # over 200-char cap
    trek.record_pulse_ack(
        t, session_id="sv-exec", picked_choice="dm-leader",
        blockers=["OOM in test_foo.py", long_blocker, "blk-3", "blk-4 ignored"],
        needs_leader_judgment=True,
        time_on_task_seconds=5400,
    )
    entry = t["pulse_acks"]["sv-exec"]
    # CAP of 3 + per-item ≤200 truncation
    assert len(entry["last_blockers"]) == 3
    assert entry["last_blockers"][0] == "OOM in test_foo.py"
    assert len(entry["last_blockers"][1]) == 200
    assert entry["last_blockers"][1] == "x" * 200
    assert entry["last_blockers"][2] == "blk-3"
    assert entry["last_needs_leader_judgment"] is True
    # history record carries the same shape
    h = entry["history"][0]
    assert h["blockers"] == entry["last_blockers"]
    assert h["needs_leader_judgment"] is True


def test_record_pulse_ack_truncates_long_state_summary():
    """state_summary は ≤100 字で silent truncate される。"""
    t = _seed_trek_for_pulse()
    long_state = "y" * 250
    trek.record_pulse_ack(
        t, session_id="sv-exec", picked_choice="continue",
        state_summary=long_state,
    )
    entry = t["pulse_acks"]["sv-exec"]
    assert len(entry["last_state_summary"]) == 100
    assert entry["last_state_summary"] == "y" * 100


def test_record_pulse_ack_coerces_invalid_time_on_task():
    """time_on_task_seconds が int 変換不能 / 負値の場合 0 にフォールバック。"""
    t = _seed_trek_for_pulse()
    trek.record_pulse_ack(
        t, session_id="sv-1", picked_choice="no-op",
        time_on_task_seconds=-10,
    )
    assert t["pulse_acks"]["sv-1"]["last_time_on_task_seconds"] == 0
    trek.record_pulse_ack(
        t, session_id="sv-2", picked_choice="no-op",
        time_on_task_seconds="not-a-number",  # type: ignore[arg-type]
    )
    assert t["pulse_acks"]["sv-2"]["last_time_on_task_seconds"] == 0


def test_record_pulse_ack_backward_compat_omits_structured_fields():
    """構造化フィールド未指定でも record は通る (= 旧 Skill / 旧 bridge 経路)。"""
    t = _seed_trek_for_pulse()
    trek.record_pulse_ack(
        t, session_id="sv-old", picked_choice="continue", note="legacy",
    )
    entry = t["pulse_acks"]["sv-old"]
    assert entry["last_state_summary"] == ""
    assert entry["last_blockers"] == []
    assert entry["last_needs_leader_judgment"] is False
    assert entry["last_time_on_task_seconds"] == 0
    # history record still gets structured field placeholders (= keys
    # always present so downstream code doesn't need existence checks)
    h = entry["history"][0]
    assert h["state_summary"] == ""
    assert h["blockers"] == []
    assert h["needs_leader_judgment"] is False
    assert h["time_on_task_seconds"] == 0


def test_summarize_pulse_acks_aggregates_active_stuck_idle_counts():
    """summarize_pulse_acks は active / stuck / idle / needs-leader 集計を返す。"""
    t = _seed_trek_for_pulse()
    # 1 active working session
    trek.record_pulse_ack(
        t, session_id="sv-working", picked_choice="continue",
        state_summary="working on e-2165", time_on_task_seconds=1800,
    )
    # 1 stuck session with blockers + needs-leader flag
    trek.record_pulse_ack(
        t, session_id="sv-stuck", picked_choice="dm-leader",
        state_summary="stuck on e-2200", blockers=["OOM"],
        needs_leader_judgment=True, time_on_task_seconds=5400,
    )
    # 1 idle session (= state_summary contains "idle")
    trek.record_pulse_ack(
        t, session_id="sv-idle", picked_choice="no-op",
        state_summary="idle, waiting for peer", time_on_task_seconds=0,
    )
    summary = trek.summarize_pulse_acks(t)
    assert summary["active_session_count"] == 3
    assert summary["stuck_session_count"] == 1   # has blockers
    # idle: state_summary contains "idle" OR time_on_task=0 → sv-idle + sv-stuck
    # don't trip (state_summary != idle, time_on_task=5400). sv-idle: both
    # conditions true. sv-working: neither. So idle=1.
    # but wait sv-stuck time_on_task=5400 and state_summary="stuck on e-2200"
    # neither matches idle. sv-working state_summary="working on e-2165" and
    # time_on_task=1800, neither matches. So idle=1 (just sv-idle).
    assert summary["idle_session_count"] == 1
    assert summary["needs_leader_judgment_count"] == 1


def test_summarize_pulse_acks_per_session_carries_structured_snapshot():
    """summarize per-session dict carries the structured snapshot fields."""
    t = _seed_trek_for_pulse()
    trek.record_pulse_ack(
        t, session_id="sv-A", picked_choice="continue",
        state_summary="working on e-X", blockers=["b1"],
        needs_leader_judgment=False, time_on_task_seconds=999,
    )
    summary = trek.summarize_pulse_acks(t)
    s = summary["sessions"]["sv-A"]
    assert s["state_summary"] == "working on e-X"
    assert s["blockers"] == ["b1"]
    assert s["needs_leader_judgment"] is False
    assert s["time_on_task_seconds"] == 999


def test_summarize_pulse_acks_ignores_legacy_empty_sessions():
    """pulse_acks に session があるが total_acks=0 (= placeholder) なら
    aggregate 計算 (active/stuck/idle) には数えない。
    """
    t = _seed_trek_for_pulse()
    # Forcibly create a placeholder entry with total_acks=0 (= simulating
    # legacy / pre-e-2165 doc shape that has the key but no real data).
    t["pulse_acks"] = {
        "sv-placeholder": {
            "session_id": "sv-placeholder",
            "total_acks": 0,
            "last_pulse_ack_at": "",
            "last_picked_choice": "",
            "history": [],
            "last_state_summary": "",
            "last_blockers": [],
            "last_needs_leader_judgment": False,
            "last_time_on_task_seconds": 0,
        }
    }
    summary = trek.summarize_pulse_acks(t)
    assert summary["active_session_count"] == 0
    assert summary["stuck_session_count"] == 0
    assert summary["idle_session_count"] == 0


# ---------------------------------------------------------------------------
# Migration scaffolding (= ms-97 / e-2658)
# ---------------------------------------------------------------------------
# AC6 (= members[] を user_id keyed から session_id keyed に書き換える)
# cutover を、 既存 trek の不可逆破壊から守る最小単位 (= backup field +
# migration_phase tracker) の schema 契約を pin する。 Phase 0-A 段階では
# 挙動変更ゼロ (= seed のみ / helper は AC34 migration で使う)。

def _new_trek_for_migration() -> dict:
    return trek.new_trek(
        title="Migration scaffolding fixture",
        creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-aaaa-12345",
    )


def test_new_trek_seeds_members_legacy_backup_empty():
    t = _new_trek_for_migration()
    assert "members_legacy_backup" in t
    assert t["members_legacy_backup"] == []


def test_get_migration_phase_defaults_to_pre_A():
    t = _new_trek_for_migration()
    # meta.migration_phase が無い trek は "pre-A" を silent に返す。
    assert "migration_phase" not in (t.get("meta") or {})
    assert trek.get_migration_phase(t) == "pre-A"


def test_get_migration_phase_reads_valid_stamp():
    t = _new_trek_for_migration()
    t.setdefault("meta", {})["migration_phase"] = "A"
    assert trek.get_migration_phase(t) == "A"


def test_get_migration_phase_falls_back_on_invalid_token():
    t = _new_trek_for_migration()
    t.setdefault("meta", {})["migration_phase"] = "bogus"
    # 不正トークンは silent に "pre-A" に丸める (= 読み手の分岐を守る)。
    assert trek.get_migration_phase(t) == "pre-A"


def test_set_migration_phase_validates_token():
    t = _new_trek_for_migration()
    with pytest.raises(ValueError):
        trek.set_migration_phase(t, "Z")
    # 有効トークンは stamp される。
    trek.set_migration_phase(t, "A")
    assert t["meta"]["migration_phase"] == "A"


def test_set_migration_phase_bumps_updated_at():
    t = _new_trek_for_migration()
    # updated_at を「過去」値に置き換えてから stamp、 mutation を観測する。
    t["updated_at"] = "1970-01-01T00:00:00Z"
    trek.set_migration_phase(t, "A")
    assert t["updated_at"] != "1970-01-01T00:00:00Z"


def test_backup_legacy_members_copies_members_to_backup():
    t = _new_trek_for_migration()
    # 既存 leader (1 名) に follower 1 名を足して 2 名分の members[] を作る。
    t["members"].append({
        "user_id": "u-2",
        "email": "c@d.com",
        "role": "follower",
        "joined_at": "2026-06-28T00:00:00Z",
    })
    assert len(t["members"]) == 2
    trek.backup_legacy_members(t)
    assert len(t["members_legacy_backup"]) == 2
    assert t["members_legacy_backup"][0]["user_id"] == "u-1"
    assert t["members_legacy_backup"][1]["user_id"] == "u-2"


def test_backup_legacy_members_refuses_double_backup():
    t = _new_trek_for_migration()
    trek.backup_legacy_members(t)
    # 既に backup が non-empty (= leader 1 名がコピーされている) なら
    # 二重 backup を ValueError で拒否する。
    assert t["members_legacy_backup"] != []
    with pytest.raises(ValueError):
        trek.backup_legacy_members(t)


def test_backup_legacy_members_is_independent_copy():
    t = _new_trek_for_migration()
    trek.backup_legacy_members(t)
    # 返却された backup entry を変更しても、 元の members[] には影響しない
    # (= dict(m) の shallow copy で snapshot を独立化している)。
    t["members_legacy_backup"][0]["role"] = "tampered"
    assert t["members"][0]["role"] == "leader"


def test_restore_legacy_members_restores_from_backup():
    t = _new_trek_for_migration()
    trek.backup_legacy_members(t)
    original_leader = dict(t["members"][0])
    # cutover を模擬: members[] を AC6 形 (session_id keyed) に書き換える。
    t["members"] = [{
        "session_id": "sv-aaaa-12345",
        "role": "leader",
        "joined_at": "2026-06-28T00:00:00Z",
    }]
    trek.restore_legacy_members(t)
    assert len(t["members"]) == 1
    assert t["members"][0]["user_id"] == original_leader["user_id"]
    # 復元後、 backup field は空に戻る (= 次回 backup を許す状態)。
    assert t["members_legacy_backup"] == []


def test_restore_legacy_members_fails_on_empty_backup():
    t = _new_trek_for_migration()
    # backup が空 (= 初期 seed のまま) なら復元元が無い → ValueError。
    assert t["members_legacy_backup"] == []
    with pytest.raises(ValueError):
        trek.restore_legacy_members(t)


def test_restore_legacy_members_resets_phase_to_pre_A():
    t = _new_trek_for_migration()
    trek.backup_legacy_members(t)
    trek.set_migration_phase(t, "A")
    assert t["meta"]["migration_phase"] == "A"
    trek.restore_legacy_members(t)
    assert t["meta"]["migration_phase"] == "pre-A"
