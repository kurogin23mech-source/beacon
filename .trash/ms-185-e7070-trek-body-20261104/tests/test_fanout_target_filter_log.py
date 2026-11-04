"""ms-97 / e-2660 — _build_executor_targets_session_grain diagnostic logs.

target builder の 6 skip filter (= empty sid / leader sid match /
role=leader / no home project / live_cutoff / should_fire_executor_tick)
のどれが rejecting したかが log に出ていなかった (= 観測穴、 LPS sid
が member にも registry にも居るのに target に入らない原因を診断不能)。
fix: 各 filter pass で ``diag[ms-97 e-2660 AC16]`` prefix を stderr へ。
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


def _trek(trek_id: str, members: list[dict], leader_sid: str = "") -> dict:
    return {
        "trek_id": trek_id,
        "members": members,
        "leader_session_id": leader_sid,
        "scope": [{"project": "proj-x", "milestone": "ms-x"}],
        "meta": {"cadence_minutes": 10, "migration_phase": "A"},
        "task_states": {"e-todo1": {"state": "todo"}},
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _call(trek: dict, *, leader_sid: str = "",
          live_cutoff: str = "") -> list[dict]:
    return app_module._build_executor_targets_session_grain(
        fanout_trek_doc=trek,
        trek_doc=trek,
        scope_project_ids=["proj-x"],
        leader_sid=leader_sid,
        live_cutoff=live_cutoff or _now(),
    )


def test_diag_entry_header_logged(capsys):
    """Each call must log the entry header with input shape."""
    _call(_trek("tk-diag-1", []),
          live_cutoff="2026-06-29T00:00:00.000000Z")
    err = capsys.readouterr().err
    assert "diag[ms-97 e-2660 AC16]" in err
    assert "tk-diag-1" in err
    assert "len(members)=0" in err
    assert "scope_project_ids=['proj-x']" in err


def test_skip_reason_empty_session_id_logged(capsys):
    _call(_trek("tk-empty", [
        {"user_id": "uid-a", "session_id": "", "role": "member"},
    ]))
    assert "skipped: empty session_id" in capsys.readouterr().err


def test_skip_reason_leader_sid_match_logged(capsys):
    _call(
        _trek("tk-leader-match",
              [{"user_id": "uid-l", "session_id": "sv-leader",
                "role": "member"}],
              leader_sid="sv-leader"),
        leader_sid="sv-leader",
    )
    assert "skipped: leader_sid match" in capsys.readouterr().err


def test_skip_reason_role_leader_logged(capsys):
    _call(_trek("tk-role-leader", [
        {"user_id": "uid-l", "session_id": "sv-l", "role": "leader"},
    ]))
    assert "skipped: role=leader" in capsys.readouterr().err


def test_skip_reason_no_home_project_logged(capsys):
    """Session not in any scope project's registry → ghost skip."""
    _sessions_by_project["proj-x"] = []
    _call(_trek("tk-ghost", [
        {"user_id": "uid-g", "session_id": "sv-ghost", "role": "member"},
    ]))
    assert "skipped: no home project resolved" in capsys.readouterr().err


def test_skip_reason_last_active_below_cutoff_logged(capsys):
    """Session present but stale last_active → live cutoff skip."""
    _sessions_by_project["proj-x"] = [
        {"session_id": "sv-stale", "user_id": "uid-s",
         "last_active": "2026-01-01T00:00:00.000000Z"},
    ]
    _call(
        _trek("tk-stale", [
            {"user_id": "uid-s", "session_id": "sv-stale",
             "role": "member"},
        ]),
        live_cutoff=_now(),
    )
    err = capsys.readouterr().err
    assert "last_active=" in err
    assert "live_cutoff=" in err
    assert "skipped:" in err


def test_kept_target_logged(capsys):
    """Successful pass-through must emit a 'kept' log line."""
    now_iso = _now()
    past_cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(minutes=10)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _sessions_by_project["proj-x"] = [
        {"session_id": "sv-live", "user_id": "uid-live",
         "last_active": now_iso},
    ]
    out = _call(
        _trek("tk-kept", [
            {"user_id": "uid-live", "session_id": "sv-live",
             "role": "member"},
        ]),
        live_cutoff=past_cutoff,
    )
    err = capsys.readouterr().err
    assert "sv-live" in err
    if out:
        assert "kept: home_pid=proj-x" in err
    else:
        assert "should_fire_executor_tick=False" in err
