"""ms-97 / e-2613 (AC33) — Tick fire lazy start (per-executor / per-leader).

Pre-e-2613 the scheduler fired every cadence window for every live session
of every member, gated only by the global aggregate-terminal quiesce and
the per-session terminal-claim filter. AC33 narrows further:

  * Per-executor: fire only when the executor has >=1 active claim OR
    the trek has unclaim todo float to pick up.
  * Per-leader (= digest): fire only when leader_review queue non-empty,
    todo float exists, or completion is within COMPLETION_IMMINENT_SLOTS
    slots of all-terminal.
  * Stop conditions: executor stops when all claim slots terminal AND no
    unclaim todo. Leader stops when all slots terminal (= existing
    quiesce path).
  * Edge case: minimal broadcast tick still fires (= no complete
    silence) — covered in the server-side fanout tests.

This file pins the pure helper functions (= unit tests). Server-side
orchestration (= the actual lazy gate + minimal fallback) is exercised
by test_trek_scheduler_api.py's AC33 cases.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek_scheduler as trek_scheduler_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trek_with_states(states: dict) -> dict:
    """Build a minimal trek doc carrying the given task_states.

    ms-99 / e-2833 — after the Phase 2 refactor the tick decision helpers
    read slot inventory via ``materialize_slots(trek_doc, get_project=...)``
    instead of walking ``task_states`` directly. To keep this test suite's
    per-task_states matrix meaningful, we synthesize one task-kind scope
    entry per task_states key and lift the entry's
    ``updated_by_session_id`` into ``claim_session_id`` (= scope-level
    ownership, the SPEC 方針 4 successor of the legacy per-stamp field).
    """
    scope = []
    for eid, entry in states.items():
        s = {"project": "p", "task": eid}
        owner = (entry or {}).get("updated_by_session_id") or ""
        if owner:
            s["claim_session_id"] = owner
        scope.append(s)
    return {
        "trek_id": "tk-test1234",
        "status": "active",
        "scope": scope,
        "task_states": states,
    }


def _empty_gp(pid: str) -> dict:
    """Empty project pool — cache-authoritative for task-kind slots."""
    return {}


# ---------------------------------------------------------------------------
# has_unclaim_todo
# ---------------------------------------------------------------------------

def test_has_unclaim_todo_true_for_unstamped_todo():
    t = _trek_with_states({
        "e-1": {"state": "todo"},  # no updated_by_session_id
    })
    assert trek_scheduler_mod.has_unclaim_todo(t, get_project=_empty_gp) is True


def test_has_unclaim_todo_false_when_todo_is_claimed():
    t = _trek_with_states({
        "e-1": {"state": "todo", "updated_by_session_id": "sv-exec1"},
    })
    assert trek_scheduler_mod.has_unclaim_todo(t, get_project=_empty_gp) is False


def test_has_unclaim_todo_false_when_no_todo():
    t = _trek_with_states({
        "e-1": {"state": "working", "updated_by_session_id": "sv-exec1"},
    })
    assert trek_scheduler_mod.has_unclaim_todo(t, get_project=_empty_gp) is False


def test_has_unclaim_todo_false_when_no_states():
    t = _trek_with_states({})
    assert trek_scheduler_mod.has_unclaim_todo(t, get_project=_empty_gp) is False


# ---------------------------------------------------------------------------
# has_leader_review_queue
# ---------------------------------------------------------------------------

def test_has_leader_review_queue_true_when_any_leader_review():
    t = _trek_with_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "leader_review"},
    })
    assert trek_scheduler_mod.has_leader_review_queue(t, get_project=_empty_gp) is True


def test_has_leader_review_queue_false_when_none():
    t = _trek_with_states({
        "e-1": {"state": "working"},
        "e-2": {"state": "todo"},
    })
    assert trek_scheduler_mod.has_leader_review_queue(t, get_project=_empty_gp) is False


# ---------------------------------------------------------------------------
# has_todo_float
# ---------------------------------------------------------------------------

def test_has_todo_float_true_for_any_todo():
    t = _trek_with_states({
        "e-1": {"state": "todo"},
        "e-2": {"state": "working"},
    })
    assert trek_scheduler_mod.has_todo_float(t, get_project=_empty_gp) is True


def test_has_todo_float_false_when_all_progressed():
    t = _trek_with_states({
        "e-1": {"state": "working"},
        "e-2": {"state": "leader_review"},
    })
    assert trek_scheduler_mod.has_todo_float(t, get_project=_empty_gp) is False


# ---------------------------------------------------------------------------
# is_completion_imminent
# ---------------------------------------------------------------------------

def test_is_completion_imminent_true_when_one_left_of_three():
    # 2 done + 1 working (= 1 non-terminal slot, threshold default 2).
    t = _trek_with_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "done"},
        "e-3": {"state": "working"},
    })
    assert trek_scheduler_mod.is_completion_imminent(t, get_project=_empty_gp) is True


def test_is_completion_imminent_false_when_more_than_threshold_left():
    # 1 done + 3 working (= 3 non-terminal, > threshold 2).
    t = _trek_with_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "working"},
        "e-3": {"state": "working"},
        "e-4": {"state": "todo"},
    })
    assert trek_scheduler_mod.is_completion_imminent(t, get_project=_empty_gp) is False


def test_is_completion_imminent_false_when_already_terminal():
    """If aggregate is already terminal, the dedicated terminal predicate
    handles it; this helper returns False (= 0 non-terminal not imminent).
    """
    t = _trek_with_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "user_review"},
    })
    assert trek_scheduler_mod.is_completion_imminent(t, get_project=_empty_gp) is False


def test_is_completion_imminent_false_when_empty():
    assert trek_scheduler_mod.is_completion_imminent(
        _trek_with_states({}), get_project=_empty_gp,
    ) is False


# ---------------------------------------------------------------------------
# should_fire_executor_tick — per-executor decision matrix
# ---------------------------------------------------------------------------

class TestShouldFireExecutorTick:
    def test_fires_when_executor_has_working_claim(self):
        t = _trek_with_states({
            "e-1": {"state": "working", "updated_by_session_id": "sv-exec1"},
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-exec1", get_project=_empty_gp,
        ) is True

    def test_fires_when_executor_has_todo_claim(self):
        t = _trek_with_states({
            "e-1": {"state": "todo", "updated_by_session_id": "sv-exec1"},
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-exec1", get_project=_empty_gp,
        ) is True

    def test_fresh_executor_fires_when_unclaim_todo_exists(self):
        """Fresh executor (= no claims) + unclaim todo float → fire."""
        t = _trek_with_states({
            "e-1": {"state": "todo"},  # unclaim
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-fresh", get_project=_empty_gp,
        ) is True

    def test_fresh_executor_silent_when_no_unclaim_todo(self):
        """Fresh executor + no unclaim todo (= someone else picked it up)
        → AC33 stop condition, silent."""
        t = _trek_with_states({
            "e-1": {"state": "todo", "updated_by_session_id": "sv-other"},
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-fresh", get_project=_empty_gp,
        ) is False

    def test_executor_with_only_terminal_claims_silent(self):
        """Executor whose claims are all done/user_review/leader_review
        and no unclaim todo → silent (= "nothing to do here")."""
        t = _trek_with_states({
            "e-1": {"state": "done", "updated_by_session_id": "sv-exec1"},
            "e-2": {"state": "user_review", "updated_by_session_id": "sv-exec1"},
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-exec1", get_project=_empty_gp,
        ) is False

    def test_executor_with_leader_review_claim_silent(self):
        """ms-128 方針 (e-4287) — 二層 tick: executor が slot を leader_review に
        倒した (= 完成し PR 化して hand-off した) 時点で executor tick は止まる。
        leader tick が leader_review 以降を引き取る。leader_review は executor に
        とって「もうやることが無い」状態。"""
        t = _trek_with_states({
            "e-1": {"state": "leader_review", "updated_by_session_id": "sv-exec1"},
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-exec1", get_project=_empty_gp,
        ) is False

    def test_executor_with_working_claim_fires(self):
        """ms-128 方針 (e-4287) — 対照: まだ working (作業中) の claim を持つ
        executor は発火し続ける (leader_review に倒すまでが executor の仕事)。"""
        t = _trek_with_states({
            "e-1": {"state": "working", "updated_by_session_id": "sv-exec1"},
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-exec1", get_project=_empty_gp,
        ) is True

    def test_two_layer_tick_state_partition_is_total(self):
        """ms-128 方針 (e-4287) — 二層 tick の状態空間分割が VALID_TASK_STATES を
        漏れなく覆うことを構造で pin する (独立レビュー #533 の指摘)。どの層も拾わ
        ない state を作ると executor/leader のどちらの tick も駆動せず silent stall
        になるため、被覆漏れをテストで捕まえる。scheduler が import した
        EXECUTOR_ACTIVE_STATES が正典と一致することも確認する。

        ms-128 方針4 (e-4365): block を第 4 区画 BLOCKED_STATES として追加。block
        は executor tick も leader review も駆動しない中立の待機なので、totality は
        4 区画の union で成立する (被覆は保つが block はどの駆動層にも属さない)。"""
        import trek as trek_mod
        partition = (
            set(trek_mod.EXECUTOR_ACTIVE_STATES)
            | set(trek_mod.LEADER_OWNED_STATES)
            | set(trek_mod.TERMINAL_TASK_STATES)
            | set(trek_mod.BLOCKED_STATES)
        )
        assert partition == set(trek_mod.VALID_TASK_STATES)
        # 4 領域は互いに素 (= 1 state が 2 層に属さない)
        assert not (set(trek_mod.EXECUTOR_ACTIVE_STATES)
                    & set(trek_mod.LEADER_OWNED_STATES))
        assert not (set(trek_mod.EXECUTOR_ACTIVE_STATES)
                    & set(trek_mod.TERMINAL_TASK_STATES))
        assert not (set(trek_mod.BLOCKED_STATES)
                    & (set(trek_mod.EXECUTOR_ACTIVE_STATES)
                       | set(trek_mod.LEADER_OWNED_STATES)
                       | set(trek_mod.TERMINAL_TASK_STATES)))
        # block は駆動層のどれにも属さない (= tick を発火させない中立待機)
        assert "block" in trek_mod.BLOCKED_STATES
        # scheduler が import した値が正典と一致 (2 ファイル間の drift 防止)
        assert (set(trek_scheduler_mod.EXECUTOR_ACTIVE_STATES)
                == set(trek_mod.EXECUTOR_ACTIVE_STATES))

    def test_executor_terminal_claims_fires_if_unclaim_todo(self):
        """Even with all-terminal claims, fire if there's unclaim todo
        the executor could pick up next."""
        t = _trek_with_states({
            "e-1": {"state": "done", "updated_by_session_id": "sv-exec1"},
            "e-2": {"state": "todo"},  # unclaim
        })
        assert trek_scheduler_mod.should_fire_executor_tick(
            t, session_id="sv-exec1", get_project=_empty_gp,
        ) is True


# ---------------------------------------------------------------------------
# should_fire_leader_tick — per-leader decision matrix
# ---------------------------------------------------------------------------

class TestShouldFireLeaderTick:
    def test_fires_when_leader_review_queue_non_empty(self):
        t = _trek_with_states({
            "e-1": {"state": "working"},
            "e-2": {"state": "leader_review"},
        })
        assert trek_scheduler_mod.should_fire_leader_tick(t, get_project=_empty_gp) is True

    def test_fires_when_todo_float_exists(self):
        t = _trek_with_states({
            "e-1": {"state": "todo"},
            "e-2": {"state": "working"},
        })
        assert trek_scheduler_mod.should_fire_leader_tick(t, get_project=_empty_gp) is True

    def test_fires_when_completion_imminent(self):
        t = _trek_with_states({
            "e-1": {"state": "done"},
            "e-2": {"state": "done"},
            "e-3": {"state": "working"},  # only 1 non-terminal left
        })
        assert trek_scheduler_mod.should_fire_leader_tick(t, get_project=_empty_gp) is True

    def test_silent_when_only_working_far_from_completion(self):
        """Working-only trek with many slots non-terminal → no leader
        action needed yet. Auto-stall safety net would later flip stuck
        working → leader_review which re-fires this gate."""
        t = _trek_with_states({
            "e-1": {"state": "working"},
            "e-2": {"state": "working"},
            "e-3": {"state": "working"},
            "e-4": {"state": "working"},
        })
        assert trek_scheduler_mod.should_fire_leader_tick(t, get_project=_empty_gp) is False

    def test_silent_when_no_states(self):
        """Empty task_states → no state for the leader to digest yet.
        The orchestrator's minimal-tick fallback covers visibility."""
        t = _trek_with_states({})
        assert trek_scheduler_mod.should_fire_leader_tick(t, get_project=_empty_gp) is False
