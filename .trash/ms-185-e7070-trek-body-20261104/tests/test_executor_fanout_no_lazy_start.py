"""ms-97 / e-2815 — executor fanout no longer requires lazy-start signal.

2026-07-03 dogfood で観察: Trek scope が MS-level bind のみで、 executor が
project pool 側で実装している (Trek 内で claim 未実行) ケースが主流。
旧実装は ``should_fire_executor_tick`` lazy-start gate で当該 executor を
silent skip し、 scheduler → executor 経路が事実上停止した (LPS session
が phase 1 実装 done 後も digest に載らず、 user から 「executor に行かな
いと意味ないじゃん」 指摘)。

fix: ``_build_executor_targets_session_grain`` と
``_build_executor_targets_user_grain`` の両方から lazy-start gate call を
撤去、 members[] 内の全 non-leader session (+ 前段の live cutoff / home
resolvable 通過) を無条件に progress-check 対象化する。 Trek philosophy
上の invariant (= server tick = PM、 executor に周期的な progress-check
DM) を復元する。
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

    def test_source_no_longer_calls_should_fire_executor_tick(self):
        """Structural pin: neither _build_executor_targets function may
        CALL should_fire_executor_tick anymore. Comments/docstrings that
        mention the historical gate are allowed (they document why the
        gate was removed); the pin is on the function-call pattern.
        If some future refactor re-adds the gate call, this test catches
        it."""
        import inspect
        pattern = "trek_scheduler_mod.should_fire_executor_tick("
        src = inspect.getsource(
            app_module._build_executor_targets_session_grain,
        )
        assert pattern not in src, (
            "e-2815: session_grain path must not call the lazy-start gate."
        )
        src_user = inspect.getsource(
            app_module._build_executor_targets_user_grain,
        )
        assert pattern not in src_user, (
            "e-2815: user_grain path must not call the lazy-start gate."
        )


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
