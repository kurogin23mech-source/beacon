"""ms-128 e-4309 — server-tick wiring of the halt sweep.

Pins that ``app._sweep_trek_target_halts`` (the tick-side glue) builds
commit_count from the scope+pool and drives a stalled working Target to
leader_review with a halt_reason tag. The pure detection is covered by
test_trek_halt_detection_ms128; this pins the server glue + commit counting.
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import app  # noqa: E402
import trek  # noqa: E402


def _dt(h=0):
    return datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc) + \
        datetime.timedelta(hours=h)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_count_milestone_commits():
    pd = {"milestones": [{"id": "ms-1", "entries": [
        {"type": "commit"}, {"type": "task"}, {"type": "commit"}]}]}
    assert app._count_milestone_commits(pd, "ms-1") == 2
    assert app._count_milestone_commits(pd, "ms-9") == 0


def test_sweep_wiring_forces_stalled_target(monkeypatch):
    monkeypatch.setattr(app.db, "get_project", lambda pid: {})
    pulse = {"last_picked_choice": "no-op", "last_state_summary": "idle",
             "last_pulse_ack_at": _iso(_dt(5))}
    fp = trek.compute_response_fingerprint(pulse)
    trek_doc = {
        "trek_id": "tk-w",
        "scope": [{"project": "p-1", "milestone": "ms-1"}],
        "task_states": {"ms-1": {
            "state": "working", "owner_session_id": "sv-x",
            "last_response_fingerprint": fp, "last_commit_count": 0,
            "progress_last_advanced_at": _iso(_dt(0)),
        }},
        "pulse_acks": {"sv-x": pulse},
    }
    forced = app._sweep_trek_target_halts(trek_doc, now=_dt(5))
    assert forced == [{"target_id": "ms-1", "halt_reason": "progress-stall"}]
    assert trek_doc["task_states"]["ms-1"]["state"] == "leader_review"
    assert trek_doc["task_states"]["ms-1"]["halt_reason"] == "progress-stall"


def test_sweep_wiring_uses_commit_increment(monkeypatch):
    # commit count grew (0 → 3) → advanced, not forced even after 5h.
    monkeypatch.setattr(
        app.db, "get_project",
        lambda pid: {"milestones": [{"id": "ms-1", "entries": [
            {"type": "commit"}, {"type": "commit"}, {"type": "commit"}]}]})
    pulse = {"last_picked_choice": "no-op", "last_pulse_ack_at": _iso(_dt(5))}
    fp = trek.compute_response_fingerprint(pulse)
    trek_doc = {
        "trek_id": "tk-w2",
        "scope": [{"project": "p-1", "milestone": "ms-1"}],
        "task_states": {"ms-1": {
            "state": "working", "owner_session_id": "sv-x",
            "last_response_fingerprint": fp, "last_commit_count": 0,
            "progress_last_advanced_at": _iso(_dt(0)),
        }},
        "pulse_acks": {"sv-x": pulse},
    }
    forced = app._sweep_trek_target_halts(trek_doc, now=_dt(5))
    assert forced == []
    assert trek_doc["task_states"]["ms-1"]["state"] == "working"
