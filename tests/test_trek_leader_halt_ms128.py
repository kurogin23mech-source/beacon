"""ms-128 方針8 (e-4368) chunk 1: leader halt detection.

The leader is the root of the watch-tree — no watchdog inside the Trek. The
existing idle-escalation anchors on executor pulses, so a leader asleep *while
executors are active* isn't caught (the trek isn't "idle"). ``evaluate_leader_review_stall``
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
    r = trek.evaluate_leader_review_stall(
        _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}}), now=_NOW)
    assert r is not None
    assert r["reason"] == trek.LEADER_REVIEW_STALL_REASON
    assert r["oldest_task_id"] == "e-1"
    assert r["waited_minutes"] == 300
    assert r["queue_size"] == 1
    assert r["leader_session_id"] == "sv-lead"


def test_recent_leader_review_is_not_halt():
    r = trek.evaluate_leader_review_stall(
        _trek({"e-1": {"state": "leader_review", "updated_at": _RECENT}}),
        now=_NOW)
    assert r is None


def test_empty_queue_is_not_halt():
    # No leader_review items → the leader can't be "not draining".
    r = trek.evaluate_leader_review_stall(
        _trek({"e-1": {"state": "working", "updated_at": _OLD}}), now=_NOW)
    assert r is None


def test_oldest_item_drives_detection_robust_to_churn():
    # A fresh item at the back must NOT reset the clock on the stale front item.
    r = trek.evaluate_leader_review_stall(_trek({
        "e-1": {"state": "leader_review", "updated_at": _OLD},
        "e-2": {"state": "leader_review", "updated_at": _RECENT},
    }), now=_NOW)
    assert r is not None
    assert r["oldest_task_id"] == "e-1"
    assert r["queue_size"] == 2


def test_all_recent_items_not_halt():
    r = trek.evaluate_leader_review_stall(_trek({
        "e-1": {"state": "leader_review", "updated_at": _RECENT},
        "e-2": {"state": "leader_review", "updated_at": _RECENT},
    }), now=_NOW)
    assert r is None


def test_boundary_at_threshold():
    # Exactly 240 min → halt (>=).
    at = "2026-07-29T08:00:00.000000Z"  # 240m before _NOW
    r = trek.evaluate_leader_review_stall(
        _trek({"e-1": {"state": "leader_review", "updated_at": at}}), now=_NOW)
    assert r is not None
    assert r["waited_minutes"] == 240


def test_custom_threshold():
    r = trek.evaluate_leader_review_stall(
        _trek({"e-1": {"state": "leader_review", "updated_at": _RECENT}}),
        now=_NOW, stall_timeout_minutes=20)
    assert r is not None  # 30m > 20m


def test_guards_planning_halt_no_leader():
    q = {"e-1": {"state": "leader_review", "updated_at": _OLD}}
    assert trek.evaluate_leader_review_stall(_trek(q, status="planning"), now=_NOW) is None
    assert trek.evaluate_leader_review_stall(_trek(q, status="archived"), now=_NOW) is None
    assert trek.evaluate_leader_review_stall(_trek(q, halt={"reason": "x"}), now=_NOW) is None
    assert trek.evaluate_leader_review_stall(_trek(q, leader=""), now=_NOW) is None


def test_detection_is_read_only():
    doc = _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    import copy
    before = copy.deepcopy(doc)
    trek.evaluate_leader_review_stall(doc, now=_NOW)
    assert doc == before  # no mutation


def test_legacy_done_migrated_not_counted_as_leader_review():
    # A legacy "waiting-review" migrates to leader_review; "done" migrates to
    # user_review (not leader_review) so it must not count.
    r = trek.evaluate_leader_review_stall(_trek({
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
    hi = _sched.pending_leader_review_stall_escalation(doc, now=_NOW)
    assert hi is not None
    assert hi["reason"] == trek.LEADER_REVIEW_STALL_REASON


def test_should_fire_none_when_not_stalled():
    doc = _active_trek({"e-1": {"state": "leader_review", "updated_at": _RECENT}})
    assert _sched.pending_leader_review_stall_escalation(doc, now=_NOW) is None


def test_cooldown_suppresses_recent_refire():
    doc = _active_trek(
        {"e-1": {"state": "leader_review", "updated_at": _OLD}},
        meta={"last_leader_review_stall_escalation_at": "2026-07-29T11:50:00.000000Z"})
    # 10 min ago < 30 min cooldown → suppressed
    assert _sched.pending_leader_review_stall_escalation(doc, now=_NOW) is None


def test_refire_allowed_after_cooldown():
    doc = _active_trek(
        {"e-1": {"state": "leader_review", "updated_at": _OLD}},
        meta={"last_leader_review_stall_escalation_at": "2026-07-29T11:00:00.000000Z"})
    # 60 min ago > 30 min cooldown → allowed
    assert _sched.pending_leader_review_stall_escalation(doc, now=_NOW) is not None


def test_payload_shape_and_transfer_guidance():
    doc = _active_trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    hi = _sched.pending_leader_review_stall_escalation(doc, now=_NOW)
    p = _sched.build_leader_review_stall_payload(doc, stall_info=hi, now=_NOW)
    assert p["kind"] == "trek-leader-review-stall-escalation"
    assert p["waited_minutes"] == 300
    assert p["oldest_task_id"] == "e-1"
    assert "take-over" in p["body"]  # correct recovery for a halted leader
    assert p["leader_session_id"] == "sv-lead"


# --- chunk 3: recovery drains the queue; graceful degradation ---------------

def test_take_over_preserves_leader_review_queue_for_new_leader():
    # "leader-transfer で溜まったキューが drain": the queue is derived from
    # task_states, so after transfer_leader the accumulated leader_review items
    # are fully visible to whoever is now leader (nothing to migrate, no loss).
    doc = _active_trek({
        "e-1": {"state": "leader_review", "updated_at": _OLD},
        "e-2": {"state": "leader_review", "updated_at": _RECENT},
    })
    before = _sched.build_task_state_aggregate(doc)["leader_review_queue"]
    trek.transfer_leader(doc, target_session_id="sv-new-leader")
    after = _sched.build_task_state_aggregate(doc)["leader_review_queue"]
    assert doc["leader_session_id"] == "sv-new-leader"
    assert [r["task_id"] for r in after] == [r["task_id"] for r in before]
    assert len(after) == 2  # the whole backlog carries to the new leader


def test_leader_halt_detection_does_not_touch_executor_state():
    # Graceful degradation: detecting a leader halt is read-only and must not
    # change any executor task state or halt the trek.
    doc = _active_trek({
        "e-review": {"state": "leader_review", "updated_at": _OLD},
        "e-work": {"state": "working", "updated_at": _RECENT},
    })
    import copy
    before = copy.deepcopy(doc)
    r = trek.evaluate_leader_review_stall(doc, now=_NOW)
    assert r is not None            # leader halt detected
    assert doc == before            # executor state untouched, trek not halted
    assert doc.get("halt") is None


# --- review hardening (2026-07-29) -----------------------------------------

def test_fresh_leader_gets_grace_on_inherited_backlog():
    # AX A1: a leader who just took over an old queue must NOT be flagged
    # immediately. leader_assumed_at recent → anchor clamped → no stall yet.
    doc = _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    doc["meta"] = {"leader_assumed_at": _RECENT}  # took over 30m ago
    assert trek.evaluate_leader_review_stall(doc, now=_NOW) is None


def test_long_serving_leader_still_flagged():
    # leader_assumed_at older than threshold → anchor falls back to oldest item.
    doc = _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    doc["meta"] = {"leader_assumed_at": "2026-07-29T06:00:00.000000Z"}  # 6h ago
    r = trek.evaluate_leader_review_stall(doc, now=_NOW)
    assert r is not None


def test_transfer_leader_stamps_leader_assumed_at():
    doc = {"leader_session_id": "sv-old", "meta": {}}
    trek.transfer_leader(doc, target_session_id="sv-new")
    assert doc["meta"].get("leader_assumed_at")  # stamped for the grace window


def test_take_over_recovery_resets_stall_clock():
    # End-to-end: stale queue → stall detected; after take-over (transfer_leader
    # stamps leader_assumed_at=now) the new leader is not immediately re-flagged.
    doc = _trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    assert trek.evaluate_leader_review_stall(doc, now=_NOW) is not None
    # take-over routes through transfer_leader; stamp assumed_at at _NOW.
    doc.setdefault("meta", {})["leader_assumed_at"] = _NOW.strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    doc["leader_session_id"] = "sv-fresh"
    assert trek.evaluate_leader_review_stall(doc, now=_NOW) is None


def test_uses_dedicated_entered_at_not_updated_at():
    # Maint #3: a leader_review item whose updated_at was re-stamped recently
    # (e.g. a note edit) but whose leader_review_entered_at is old must still be
    # detected — detection reads the dedicated entry-time field.
    doc = _trek({"e-1": {
        "state": "leader_review",
        "updated_at": _RECENT,                       # recently touched
        "leader_review_entered_at": _OLD,            # but entered long ago
    }})
    r = trek.evaluate_leader_review_stall(doc, now=_NOW)
    assert r is not None
    assert r["oldest_task_id"] == "e-1"


def test_set_task_state_stamps_entered_at_on_transition():
    doc = {}
    trek.set_task_state(doc, task_id="e-1", state="working")
    trek.set_task_state(doc, task_id="e-1", state="leader_review")
    assert doc["task_states"]["e-1"].get("leader_review_entered_at")


def test_entered_at_preserved_on_idempotent_reaffirm():
    doc = {}
    trek.set_task_state(doc, task_id="e-1", state="working")
    trek.set_task_state(doc, task_id="e-1", state="leader_review")
    first = doc["task_states"]["e-1"]["leader_review_entered_at"]
    trek.set_task_state(doc, task_id="e-1", state="leader_review")  # re-affirm
    assert doc["task_states"]["e-1"]["leader_review_entered_at"] == first


def test_queue_size_counts_broken_timestamp_items():
    # AX A6: an item with an unparseable timestamp is skipped for age but must
    # still count toward queue_size so the backlog isn't under-reported.
    doc = _trek({
        "e-1": {"state": "leader_review", "updated_at": _OLD},
        "e-2": {"state": "leader_review", "updated_at": "not-a-date"},
    })
    r = trek.evaluate_leader_review_stall(doc, now=_NOW)
    assert r is not None
    assert r["queue_size"] == 2         # both counted
    assert r["oldest_task_id"] == "e-1"  # only the parseable one drives age


def test_reason_is_not_a_halt_reason_constant():
    # Vocabulary: the reason is NOT in the HALT_REASON_* namespace (reserved for
    # the trek Andon-cord halt-sweep).
    assert trek.LEADER_REVIEW_STALL_REASON == "leader-review-stall"
    assert not trek.LEADER_REVIEW_STALL_REASON.startswith("halt")


def test_payload_body_uses_stall_vocab_not_halt():
    doc = _active_trek({"e-1": {"state": "leader_review", "updated_at": _OLD}})
    si = _sched.pending_leader_review_stall_escalation(doc, now=_NOW)
    p = _sched.build_leader_review_stall_payload(doc, stall_info=si, now=_NOW)
    assert p["kind"] == "trek-leader-review-stall-escalation"
    assert "停滞" in p["body"]
    assert "take-over" in p["body"]
    # transfer-leader fallback was dropped (dead-end for a stalled leader)
    assert "transfer-leader" not in p["body"]
