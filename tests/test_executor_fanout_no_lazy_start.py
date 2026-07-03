"""ms-97 / e-2815 revision — executor fanout の 4-rule gate 実装 pin.

2026-07-03 dogfood 経緯:

1. LPS session が Trek 内で claim せず project pool 側で実装 → 旧 lazy-
   start gate (has_unclaim_todo) で silent skip される事故 (「executor
   に行かないと意味ない」 user 指摘)。
2. 初 fix (無条件 fire) で LPS への tick は復活したが、 PE 側で 「claim
   全部 terminal (leader_review) なのに tick 来る → token 無駄」 副作用
   (「レビュー待ちですぐまた止まって、ただのトークン無駄遣いじゃない?」
   user 追加指摘)。
3. revision (本ファイルが pin する invariant): ``should_fire_executor_tick``
   を 4-rule に整理し 「無駄 tick 排除 + LPS-style silent worker poke」
   を両立する。

4 rules:
- (i) active claim (working/todo) あり → fire
- (v) orphan slot (claimed-by=空、 state=todo/working) あり → fire all
- (iv) all claims terminal + no orphan → skip (waste avoidance)
- (iii) no claim + no orphan → fire (silent worker poke)
"""
from __future__ import annotations

import copy
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")
os.environ.setdefault("BEACON_SCHEDULER_INTERNAL_KEY", "test-scheduler-key")

import firestore_client  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


_sessions_by_project: dict[str, list[dict]] = {}


@pytest.fixture(autouse=True)
def _rebind_list_sessions():
    db_module = app_module.db
    prior = getattr(db_module, "list_sessions", None)
    setattr(
        db_module, "list_sessions",
        lambda pid: copy.deepcopy(_sessions_by_project.get(pid, [])),
    )
    _sessions_by_project.clear()
    yield
    if prior is None:
        if hasattr(db_module, "list_sessions"):
            delattr(db_module, "list_sessions")
    else:
        setattr(db_module, "list_sessions", prior)


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _iso_past(minutes: int) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=minutes)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _empty_task_states_trek(members: list[dict], leader_sid: str) -> dict:
    """Trek with MS-level scope but no task-level bind — the exact shape
    that triggered the LPS silent-skip bug. task_states is empty so the
    old should_fire_executor_tick gate would return False for every
    executor (has_unclaim_todo → False)."""
    return {
        "trek_id": "tk-e2815",
        "members": members,
        "leader_session_id": leader_sid,
        "scope": [{"project": "proj-lps", "milestone": "ms-23"}],
        "meta": {"cadence_minutes": 10, "migration_phase": "A"},
        "task_states": {},
    }


class TestSessionGrainExecutorFanoutInvariant:
    def test_executor_with_no_claim_and_no_todo_is_still_targeted(self):
        """The e-2815 core invariant: an executor session that has NEVER
        claimed anything in Trek AND for whom the Trek has zero todo
        entries must STILL be a fanout target as long as the session is
        (a) a member, (b) not the leader, (c) home-resolvable, (d) live.
        """
        now = _iso_now()
        _sessions_by_project["proj-lps"] = [
            {"session_id": "sv-lps", "user_id": "uid-lps",
             "last_active": now},
        ]
        trek = _empty_task_states_trek(
            members=[
                {"user_id": "uid-leader", "session_id": "sv-leader",
                 "role": "leader"},
                {"user_id": "uid-lps", "session_id": "sv-lps",
                 "role": "member"},
            ],
            leader_sid="sv-leader",
        )
        targets = app_module._build_executor_targets_session_grain(
            fanout_trek_doc=trek,
            trek_doc=trek,
            scope_project_ids=["proj-lps"],
            leader_sid="sv-leader",
            live_cutoff=_iso_past(10),
        )
        assert len(targets) == 1, (
            "e-2815: executor session with empty task_states must be "
            "targeted, not silently skipped by the removed lazy-start gate."
        )
        assert targets[0]["session_id"] == "sv-lps"
        assert targets[0]["home_project_id"] == "proj-lps"

    def test_leader_still_excluded_from_executor_targets(self):
        """Belt-and-suspenders: removing the lazy-start gate must NOT
        break the leader exclusion (CORE doc trek-leader-stance)."""
        now = _iso_now()
        _sessions_by_project["proj-lps"] = [
            {"session_id": "sv-leader", "user_id": "uid-leader",
             "last_active": now},
        ]
        trek = _empty_task_states_trek(
            members=[
                {"user_id": "uid-leader", "session_id": "sv-leader",
                 "role": "leader"},
            ],
            leader_sid="sv-leader",
        )
        targets = app_module._build_executor_targets_session_grain(
            fanout_trek_doc=trek,
            trek_doc=trek,
            scope_project_ids=["proj-lps"],
            leader_sid="sv-leader",
            live_cutoff=_iso_past(10),
        )
        assert targets == [], (
            "leader must be excluded from executor progress-check targets "
            "even when the lazy-start gate is removed."
        )

    def test_stale_last_active_still_skipped(self):
        """live_cutoff filter must survive — a stale (offline) executor
        session should still be skipped so we don't fire into a dead
        bridge."""
        _sessions_by_project["proj-lps"] = [
            {"session_id": "sv-lps", "user_id": "uid-lps",
             "last_active": _iso_past(60)},
        ]
        trek = _empty_task_states_trek(
            members=[
                {"user_id": "uid-lps", "session_id": "sv-lps",
                 "role": "member"},
            ],
            leader_sid="",
        )
        targets = app_module._build_executor_targets_session_grain(
            fanout_trek_doc=trek,
            trek_doc=trek,
            scope_project_ids=["proj-lps"],
            leader_sid="",
            live_cutoff=_iso_past(10),
        )
        assert targets == [], (
            "live_cutoff filter is intentionally kept (only lazy-start "
            "gate was removed) — offline sessions should not receive fires."
        )

    def test_ghost_member_still_skipped(self):
        """Home-resolvable filter must survive — a member session not in
        any scope project's registry is still skipped."""
        _sessions_by_project["proj-lps"] = []
        trek = _empty_task_states_trek(
            members=[
                {"user_id": "uid-ghost", "session_id": "sv-ghost",
                 "role": "member"},
            ],
            leader_sid="",
        )
        targets = app_module._build_executor_targets_session_grain(
            fanout_trek_doc=trek,
            trek_doc=trek,
            scope_project_ids=["proj-lps"],
            leader_sid="",
            live_cutoff=_iso_past(10),
        )
        assert targets == [], (
            "session not resolvable in any scope project must still be "
            "skipped (ghost member protection)."
        )

    def test_source_calls_revised_should_fire_executor_tick(self):
        """Structural pin (revision): both _build_executor_targets functions
        must CALL the revised should_fire_executor_tick. The revision brings
        back the gate call, but the gate now implements the 4-rule semantic
        (not the old lazy-start with has_unclaim_todo)."""
        import inspect
        pattern = "trek_scheduler_mod.should_fire_executor_tick("
        src = inspect.getsource(
            app_module._build_executor_targets_session_grain,
        )
        assert pattern in src, (
            "e-2815 revision: session_grain path must call the revised gate."
        )
        src_user = inspect.getsource(
            app_module._build_executor_targets_user_grain,
        )
        assert pattern in src_user, (
            "e-2815 revision: user_grain path must call the revised gate."
        )


class TestRevisedGateSemantics:
    """4-rule semantic pins for the revised should_fire_executor_tick.

    Directly exercises the pure gate function to enforce the 4 rules
    without needing the full server stack. The e-2815 revision splits
    the old lazy-start gate into 4 explicit branches so both LPS-style
    silent workers (fire) and terminal-only claimants (skip) are handled
    correctly.
    """

    @staticmethod
    def _import_gate():
        import trek_scheduler
        return trek_scheduler.should_fire_executor_tick

    def test_rule_i_active_claim_fires(self):
        """Rule (i): executor with a working / todo claim → fire."""
        gate = self._import_gate()
        trek = {
            "task_states": {
                "e-1": {"state": "working",
                        "updated_by_session_id": "sv-x"},
            },
        }
        assert gate(trek, session_id="sv-x") is True

    def test_rule_iv_all_terminal_claims_skip(self):
        """Rule (iv): executor with only terminal claims + no orphan →
        skip (waste avoidance)。 これが PE side の user 指摘直訳。"""
        gate = self._import_gate()
        trek = {
            "task_states": {
                "e-1": {"state": "leader_review",
                        "updated_by_session_id": "sv-pe"},
                "e-2": {"state": "done",
                        "updated_by_session_id": "sv-pe"},
            },
        }
        assert gate(trek, session_id="sv-pe") is False, (
            "PE session with all-terminal claims (leader_review + done) "
            "and no orphan must NOT be fired (Rule iv, waste avoidance)."
        )

    def test_rule_iii_no_claim_no_orphan_fires(self):
        """Rule (iii): executor with no claims and no orphan → fire.
        これが LPS side の silent worker poke。"""
        gate = self._import_gate()
        trek = {
            "task_states": {
                # Other session has a terminal claim; this session has none.
                "e-1": {"state": "leader_review",
                        "updated_by_session_id": "sv-other"},
            },
        }
        assert gate(trek, session_id="sv-lps") is True, (
            "LPS session with no Trek claim must be fired (Rule iii, "
            "silent worker poke)."
        )

    def test_rule_v_orphan_slot_fires_all(self):
        """Rule (v): orphan slot (state=todo/working, no updated_by) →
        fire ALL executors, even those with all-terminal claims."""
        gate = self._import_gate()
        trek = {
            "task_states": {
                "e-orphan": {"state": "todo",
                             "updated_by_session_id": ""},
                "e-done": {"state": "done",
                           "updated_by_session_id": "sv-pe"},
            },
        }
        # Even PE (all terminal on their own claim) should fire because
        # the orphan is Trek-wide.
        assert gate(trek, session_id="sv-pe") is True, (
            "PE with terminal claim must still fire when Trek has an "
            "orphan slot (Rule v)."
        )
        # A fresh session (no claims) also fires — same reason.
        assert gate(trek, session_id="sv-fresh") is True

    def test_rule_v_orphan_only_counts_non_terminal(self):
        """Rule (v) precision: orphan filter counts only non-terminal
        unclaimed entries. A terminal entry with no updated_by is not
        an orphan (it's already resolved)."""
        gate = self._import_gate()
        trek = {
            "task_states": {
                "e-done-noone": {"state": "done",
                                 "updated_by_session_id": ""},
                "e-terminal-pe": {"state": "leader_review",
                                  "updated_by_session_id": "sv-pe"},
            },
        }
        assert gate(trek, session_id="sv-pe") is False, (
            "A done entry with no updated_by is not orphan (Rule v "
            "requires non-terminal state)."
        )

    def test_empty_task_states_fires_iii(self):
        """Empty Trek (no task_states at all): Rule (iii) applies to
        any executor (no claim, no orphan) → fire. Matches LPS the
        moment they joined."""
        gate = self._import_gate()
        trek = {"task_states": {}}
        assert gate(trek, session_id="sv-anyone") is True


class TestUserGrainExecutorFanoutInvariant:
    """The legacy pre-A path must also fan out to executors unconditionally
    after e-2815."""

    def test_executor_with_no_claim_still_targeted(self):
        live_sessions = {
            "sv-lps": {
                "home_project_id": "proj-lps",
                "user_id": "uid-lps",
            },
        }
        trek = _empty_task_states_trek(
            members=[
                {"user_id": "uid-leader", "session_id": "sv-leader",
                 "role": "leader"},
                {"user_id": "uid-lps", "session_id": "sv-lps",
                 "role": "member"},
            ],
            leader_sid="sv-leader",
        )
        targets = app_module._build_executor_targets_user_grain(
            fanout_trek_doc=trek,
            live_sessions=live_sessions,
            leader_sid="sv-leader",
        )
        assert len(targets) == 1
        assert targets[0]["session_id"] == "sv-lps"

    def test_leader_still_excluded(self):
        live_sessions = {
            "sv-leader": {
                "home_project_id": "proj-lps",
                "user_id": "uid-leader",
            },
        }
        trek = _empty_task_states_trek(
            members=[
                {"user_id": "uid-leader", "session_id": "sv-leader",
                 "role": "leader"},
            ],
            leader_sid="sv-leader",
        )
        targets = app_module._build_executor_targets_user_grain(
            fanout_trek_doc=trek,
            live_sessions=live_sessions,
            leader_sid="sv-leader",
        )
        assert targets == []
