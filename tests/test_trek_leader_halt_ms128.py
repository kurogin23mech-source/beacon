"""ms-128 方針8 (e-4368) chunk 1: leader halt detection.

The leader is the root of the watch-tree — no watchdog inside the Trek. The
existing idle-escalation anchors on executor pulses, so a leader asleep *while
executors are active* isn't caught (the trek isn't "idle"). ``evaluate_leader_halt``
detects this by the leader_review queue not draining: the oldest leader_review
item's age. Oldest-item-age (not full-queue fingerprint) is robust to executors
piling new items at the back while the leader ignores the front.

Read-only detection; escalation wiring + transfer land in later chunks.
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402

_NOW = datetime.datetime(2026, 7, 29, 12, 0, 0, tzinfo=datetime.timezone.utc)
_OLD = "2026-07-29T07:00:00.000000Z"     # 5h before _NOW
_RECENT = "2026-07-29T11:30:00.000000Z"  # 30m before _NOW


def _trek(states, *, status="active", leader="sv-lead", halt=None):
    d = {"status": status, "leader_session_id": leader, "task_states": states}
    if halt:
        d["halt"] = halt
    return d


def test_stale_leader_review_is_halt():
    r = trek.evaluate_leader_halt(
        _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}}), now=_NOW)
    assert r is not None
    assert r["reason"] == trek.HALT_REASON_LEADER_REVIEW_STALL
    assert r["oldest_task_id"] == "e-1"
    assert r["waited_minutes"] == 300
    assert r["queue_size"] == 1
    assert r["leader_session_id"] == "sv-lead"


def test_recent_leader_review_is_not_halt():
    r = trek.evaluate_leader_halt(
        _trek({"e-1": {"state": "leader_review", "updated_at": _RECENT}}),
        now=_NOW)
    assert r is None


def test_empty_queue_is_not_halt():
    # No leader_review items → the leader can't be "not draining".
    r = trek.evaluate_leader_halt(
        _trek({"e-1": {"state": "working", "updated_at": _OLD}}), now=_NOW)
    assert r is None


def test_oldest_item_drives_detection_robust_to_churn():
    # A fresh item at the back must NOT reset the clock on the stale front item.
    r = trek.evaluate_leader_halt(_trek({
        "e-1": {"state": "leader_review", "updated_at": _OLD},
        "e-2": {"state": "leader_review", "updated_at": _RECENT},
    }), now=_NOW)
    assert r is not None
    assert r["oldest_task_id"] == "e-1"
    assert r["queue_size"] == 2


def test_all_recent_items_not_halt():
    r = trek.evaluate_leader_halt(_trek({
        "e-1": {"state": "leader_review", "updated_at": _RECENT},
        "e-2": {"state": "leader_review", "updated_at": _RECENT},
    }), now=_NOW)
    assert r is None


def test_boundary_at_threshold():
    # Exactly 240 min → halt (>=).
    at = "2026-07-29T08:00:00.000000Z"  # 240m before _NOW
    r = trek.evaluate_leader_halt(
        _trek({"e-1": {"state": "leader_review", "updated_at": at}}), now=_NOW)
    assert r is not None
    assert r["waited_minutes"] == 240


def test_custom_threshold():
    r = trek.evaluate_leader_halt(
        _trek({"e-1": {"state": "leader_review", "updated_at": _RECENT}}),
        now=_NOW, stall_timeout_minutes=20)
    assert r is not None  # 30m > 20m


def test_guards_planning_halt_no_leader():
    q = {"e-1": {"state": "leader_review", "updated_at": _OLD}}
    assert trek.evaluate_leader_halt(_trek(q, status="planning"), now=_NOW) is None
    assert trek.evaluate_leader_halt(_trek(q, status="archived"), now=_NOW) is None
    assert trek.evaluate_leader_halt(_trek(q, halt={"reason": "x"}), now=_NOW) is None
    assert trek.evaluate_leader_halt(_trek(q, leader=""), now=_NOW) is None


def test_detection_is_read_only():
    doc = _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    import copy
    before = copy.deepcopy(doc)
    trek.evaluate_leader_halt(doc, now=_NOW)
    assert doc == before  # no mutation


def test_legacy_done_migrated_not_counted_as_leader_review():
    # A legacy "waiting-review" migrates to leader_review; "done" migrates to
    # user_review (not leader_review) so it must not count.
    r = trek.evaluate_leader_halt(_trek({
        "e-1": {"state": "done", "updated_at": _OLD},
        "e-2": {"state": "waiting-review", "updated_at": _OLD},
    }), now=_NOW)
    assert r is not None
    assert r["oldest_task_id"] == "e-2"   # only the migrated leader_review
    assert r["queue_size"] == 1


# --- chunk 2: escalation helpers -------------------------------------------

import trek_scheduler as _sched  # noqa: E402


def _active_trek(states, *, meta=None):
    d = {"trek_id": "tk-x", "status": "active", "leader_session_id": "sv-lead",
         "task_states": states}
    if meta:
        d["meta"] = meta
    return d


def test_should_fire_returns_halt_info_when_stalled():
    doc = _active_trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    hi = _sched.should_fire_leader_halt_escalation(doc, now=_NOW)
    assert hi is not None
    assert hi["reason"] == trek.HALT_REASON_LEADER_REVIEW_STALL


def test_should_fire_none_when_not_stalled():
    doc = _active_trek({"e-1": {"state": "leader_review", "updated_at": _RECENT}})
    assert _sched.should_fire_leader_halt_escalation(doc, now=_NOW) is None


def test_cooldown_suppresses_recent_refire():
    doc = _active_trek(
        {"e-1": {"state": "leader_review", "updated_at": _OLD}},
        meta={"last_leader_halt_escalation_at": "2026-07-29T11:50:00.000000Z"})
    # 10 min ago < 30 min cooldown → suppressed
    assert _sched.should_fire_leader_halt_escalation(doc, now=_NOW) is None


def test_refire_allowed_after_cooldown():
    doc = _active_trek(
        {"e-1": {"state": "leader_review", "updated_at": _OLD}},
        meta={"last_leader_halt_escalation_at": "2026-07-29T11:00:00.000000Z"})
    # 60 min ago > 30 min cooldown → allowed
    assert _sched.should_fire_leader_halt_escalation(doc, now=_NOW) is not None


def test_payload_shape_and_transfer_guidance():
    doc = _active_trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    hi = _sched.should_fire_leader_halt_escalation(doc, now=_NOW)
    p = _sched.build_leader_halt_payload(doc, halt_info=hi, now=_NOW)
    assert p["kind"] == "trek-leader-halt-escalation"
    assert p["waited_minutes"] == 300
    assert p["oldest_task_id"] == "e-1"
    assert "transfer-leader" in p["body"]
    assert p["leader_session_id"] == "sv-lead"
