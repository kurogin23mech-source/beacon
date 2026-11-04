"""Unit tests for lib/trek_scheduler.py (ms-83 / e-1997 / e-1998).

Pure-function coverage:

  (a) is_trek_due cadence math:
        - planning trek → never due
        - archived trek → never due
        - active + halt set → not due (= Andon cord)
        - never fired → due
        - fired < cadence ago → not due
        - fired >= cadence ago → due
        - custom cadence_minutes override

  (b) integration: 10-min cadence trek fires once at t=10min, twice by
      t=20min (= simulated tick-by-tick).

  (c) build_progress_check_payload (e-1998):
        - empty scope → canonical empty-scope DM
        - scope set but all tasks done → all-done DM with latest done id
        - scope + todo tasks present → "next, please" body with at least
          one target entry id
        - scope only contains entries from unknown / non-existent MS →
          falls through to all-done message
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek as trek_mod  # noqa: E402
import trek_scheduler as scheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_trek(*,
                status: str = "active",
                cadence_minutes=None,
                last_at: str = "",
                scope: list[dict] | None = None,
                halt: dict | None = None) -> dict:
    t = trek_mod.new_trek(
        title="x",
        creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
        cadence_minutes=cadence_minutes,
        initial_scope=scope or [],
    )
    t["status"] = status
    if last_at:
        t.setdefault("meta", {})["last_progress_check_at"] = last_at
    if halt:
        t["halt"] = halt
    return t


def _utc(year=2026, month=6, day=18, hour=12, minute=0):
    return datetime.datetime(year, month, day, hour, minute,
                             tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# (a) is_trek_due cadence math
# ---------------------------------------------------------------------------

def test_planning_trek_never_due():
    t = _build_trek(status="planning")
    assert scheduler.is_trek_due(t, now=_utc()) is False


def test_archived_trek_never_due():
    t = _build_trek(status="archived")
    assert scheduler.is_trek_due(t, now=_utc()) is False


def test_halted_trek_never_due():
    t = _build_trek(
        status="active",
        halt={"issued_by_session_id": "sv-leader", "reason": "STOP",
              "issued_at": "2026-06-18T11:00:00.000000Z"},
    )
    assert scheduler.is_trek_due(t, now=_utc()) is False


def test_never_fired_active_trek_is_due():
    t = _build_trek(status="active")
    assert scheduler.is_trek_due(t, now=_utc()) is True


def test_fired_within_cadence_not_due():
    # cadence=10, fired 5 minutes ago → not due.
    last = "2026-06-18T11:55:00.000000Z"
    t = _build_trek(status="active", cadence_minutes=10, last_at=last)
    assert scheduler.is_trek_due(t, now=_utc(hour=12, minute=0)) is False


def test_fired_at_exact_cadence_boundary_is_due():
    # cadence=10, fired exactly 10 minutes ago → due (= ``>=`` boundary).
    last = "2026-06-18T11:50:00.000000Z"
    t = _build_trek(status="active", cadence_minutes=10, last_at=last)
    assert scheduler.is_trek_due(t, now=_utc(hour=12, minute=0)) is True


def test_fired_beyond_cadence_is_due():
    # cadence=10, fired 30 minutes ago → due.
    last = "2026-06-18T11:30:00.000000Z"
    t = _build_trek(status="active", cadence_minutes=10, last_at=last)
    assert scheduler.is_trek_due(t, now=_utc(hour=12, minute=0)) is True


def test_default_cadence_when_unset():
    """Trek without meta.cadence_minutes → default = 10 minutes."""
    t = _build_trek(status="active")  # no cadence_minutes
    # fired 6 minutes ago, default cadence 10 → not yet due.
    last = "2026-06-18T11:54:00.000000Z"
    t.setdefault("meta", {})["last_progress_check_at"] = last
    assert scheduler.is_trek_due(t, now=_utc(hour=12, minute=0)) is False


# ---------------------------------------------------------------------------
# (b) Integration — simulated tick loop
# ---------------------------------------------------------------------------

def test_select_due_treks_picks_only_due():
    a = _build_trek(status="active", cadence_minutes=10,
                    last_at="2026-06-18T11:30:00.000000Z")  # 30 min ago — due
    b = _build_trek(status="active", cadence_minutes=10,
                    last_at="2026-06-18T11:58:00.000000Z")  # 2 min ago — not
    c = _build_trek(status="active")  # never fired — due
    d = _build_trek(status="planning")  # planning — not due
    due = scheduler.select_due_treks(
        [a, b, c, d], now=_utc(hour=12, minute=0),
    )
    assert a in due
    assert c in due
    assert b not in due
    assert d not in due


def test_10min_cadence_fires_correctly_over_20min():
    """Simulated minute-by-minute scheduler ticks for a 10-min trek.

    Sequence:
      t=00: never fired → due → tick records last_at=t=00
      t=05: 5 min ago → not due
      t=10: exactly 10 min ago → due → record last_at=t=10
      t=15: 5 min ago → not due
      t=20: 10 min ago → due → record last_at=t=20
    Total fires by t=20: 3 (= initial + 10 + 20).
    """
    t = _build_trek(status="active", cadence_minutes=10)
    fire_count = 0
    for minute in range(0, 21):
        now = _utc(hour=12, minute=minute)
        if scheduler.is_trek_due(t, now=now):
            fire_count += 1
            t.setdefault("meta", {})["last_progress_check_at"] = (
                now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            )
    assert fire_count == 3


# ---------------------------------------------------------------------------
# (c) build_progress_check_payload
# ---------------------------------------------------------------------------

def test_payload_empty_scope():
    t = _build_trek(status="active", scope=[])
    payload = scheduler.build_progress_check_payload(
        t, project_data=None, now=_utc(),
    )
    assert payload["kind"] == "trek-progress-check"
    assert payload["trek_id"] == t["trek_id"]
    assert "scope が空" in payload["body"]
    assert payload["target_entries"] == []


def test_payload_all_done_with_latest_done_anchor():
    t = _build_trek(
        status="active",
        scope=[{"project": "beacon-1", "milestone": "ms-83"}],
    )
    project_data = {
        "entries": [
            {"id": "e-1994", "type": "task", "status": "done",
             "milestone": "ms-83",
             "description": "trek cadence schema"},
            {"id": "e-1995", "type": "task", "status": "done",
             "milestone": "ms-83",
             "description": "T1-system envelope"},
        ],
    }
    payload = scheduler.build_progress_check_payload(
        t, project_data=project_data, now=_utc(),
    )
    assert "todo Target が見当たりません" in payload["body"]
    # AC 3: payload contains at least one trek-scope-internal entry id.
    assert len(payload["target_entries"]) >= 1
    assert payload["target_entries"][0] in {"e-1994", "e-1995"}


def test_payload_with_todo_includes_first_target_entry():
    t = _build_trek(
        status="active",
        scope=[{"project": "beacon-1", "milestone": "ms-83"}],
    )
    project_data = {
        "entries": [
            {"id": "e-1997", "type": "task", "status": "todo",
             "milestone": "ms-83",
             "description": "Cloud Scheduler trek tick endpoint"},
            {"id": "e-1998", "type": "task", "status": "todo",
             "milestone": "ms-83",
             "description": "progress-check DM payload builder"},
            {"id": "e-1994", "type": "task", "status": "done",
             "milestone": "ms-83",
             "description": "cadence schema"},
        ],
    }
    payload = scheduler.build_progress_check_payload(
        t, project_data=project_data, now=_utc(),
        last_commit_summary="abc1234 feat(ms-83): cadence (e-1994)",
    )
    assert payload["trek_id"] == t["trek_id"]
    # AC 3: trek-scope-internal entry id appears in body.
    assert "e-1997" in payload["body"]
    assert payload["target_entries"][0] == "e-1997"
    # First-todo description excerpt present.
    assert "Cloud Scheduler" in payload["body"]
    # Last commit included.
    assert "abc1234" in payload["body"]


# ---------------------------------------------------------------------------
# (d) Idle detection (ms-83 / e-2001)
# ---------------------------------------------------------------------------

def test_idle_inactive_trek_not_idle():
    """planning / archived treks are never idle."""
    t = _build_trek(status="planning")
    assert scheduler.is_trek_idle(t, now=_utc()) is False


def test_idle_halted_trek_not_idle():
    """Halted trek is intentionally paused — not idle."""
    t = _build_trek(
        status="active",
        halt={"issued_by_session_id": "sv-leader", "reason": "STOP",
              "issued_at": "2026-06-18T00:00:00.000000Z"},
    )
    assert scheduler.is_trek_idle(t, now=_utc()) is False


def test_idle_never_pinged_not_idle():
    """A trek that hasn't received its first progress check yet is NOT
    idle — the next tick will fire one and start the clock."""
    t = _build_trek(status="active", cadence_minutes=10)
    assert scheduler.is_trek_idle(t, now=_utc(hour=12)) is False


def test_idle_within_window_not_idle():
    """cadence=10, last response 25 min ago → 25 < 30 → not idle yet."""
    t = _build_trek(
        status="active",
        cadence_minutes=10,
        last_at="2026-06-18T11:00:00.000000Z",  # 60 min before now=12:00
    )
    # But last_session_response_at is more recent.
    t["meta"]["last_session_response_at"] = "2026-06-18T11:35:00.000000Z"
    assert scheduler.is_trek_idle(t, now=_utc(hour=12, minute=0)) is False


def test_idle_just_past_threshold_is_idle():
    """cadence=10, last activity 30 min ago → >= 30 → idle."""
    t = _build_trek(status="active", cadence_minutes=10)
    t.setdefault("meta", {})["last_session_response_at"] = (
        "2026-06-18T11:30:00.000000Z"  # 30 min before 12:00
    )
    assert scheduler.is_trek_idle(t, now=_utc(hour=12, minute=0)) is True


def test_idle_progress_check_only_used_as_fallback():
    """If last_session_response_at is unset, last_progress_check_at is
    the activity anchor."""
    t = _build_trek(
        status="active", cadence_minutes=10,
        last_at="2026-06-18T11:00:00.000000Z",  # 60 min before now
    )
    # 60 min > 30 min idle threshold → idle.
    assert scheduler.is_trek_idle(t, now=_utc(hour=12, minute=0)) is True


# ---------------------------------------------------------------------------
# ms-128 / e-4284 — leader-digest heartbeat
# ---------------------------------------------------------------------------

def test_leader_heartbeat_inactive_trek_not_due():
    """planning / archived treks don't heartbeat (no ongoing work)."""
    t = _build_trek(status="planning",
                    last_at="2026-06-18T10:00:00.000000Z")
    assert scheduler.is_leader_digest_heartbeat_due(t, now=_utc()) is False


def test_leader_heartbeat_halted_trek_not_due():
    """Halted trek is intentionally paused — no heartbeat."""
    t = _build_trek(
        status="active",
        last_at="2026-06-18T10:00:00.000000Z",
        halt={"issued_by_session_id": "sv-leader", "reason": "STOP",
              "issued_at": "2026-06-18T00:00:00.000000Z"},
    )
    assert scheduler.is_leader_digest_heartbeat_due(t, now=_utc()) is False


def test_leader_heartbeat_never_digested_but_started_is_due():
    """Never fired a digest but the trek has been progress-checked
    (= started) → a first leader pulse is due (quiet-from-birth stall)."""
    t = _build_trek(status="active", cadence_minutes=10,
                    last_at="2026-06-18T11:00:00.000000Z")
    assert scheduler.is_leader_digest_heartbeat_due(
        t, now=_utc(hour=12, minute=0)) is True


def test_leader_heartbeat_never_ticked_not_due():
    """Brand-new trek not yet progress-checked → no heartbeat until its
    first tick starts the clock."""
    t = _build_trek(status="active", cadence_minutes=10)
    assert scheduler.is_leader_digest_heartbeat_due(
        t, now=_utc(hour=12, minute=0)) is False


def test_leader_heartbeat_recent_digest_not_due():
    """cadence=10 → heartbeat interval = 30 min. Last digest 20 min ago
    → not due yet (avoids per-tick noise)."""
    t = _build_trek(status="active", cadence_minutes=10,
                    last_at="2026-06-18T10:00:00.000000Z")
    t["meta"]["last_leader_digest_at"] = "2026-06-18T11:40:00.000000Z"  # 20 min
    assert scheduler.is_leader_digest_heartbeat_due(
        t, now=_utc(hour=12, minute=0)) is False


def test_leader_heartbeat_stale_digest_is_due():
    """Last digest 30 min ago (= cadence×3) → heartbeat due so a silent
    stall surfaces to the leader."""
    t = _build_trek(status="active", cadence_minutes=10,
                    last_at="2026-06-18T10:00:00.000000Z")
    t["meta"]["last_leader_digest_at"] = "2026-06-18T11:30:00.000000Z"  # 30 min
    assert scheduler.is_leader_digest_heartbeat_due(
        t, now=_utc(hour=12, minute=0)) is True


def test_leader_heartbeat_never_digested_but_recent_not_due():
    """ms-128 #536 review: never-digested must ALSO require cadence×3 of
    silence (no interval-zero first pulse). Progress-checked 10 min ago
    (< 30 min threshold) → not due, so a freshly-started trek doesn't get
    a false 'stalled' heartbeat."""
    t = _build_trek(status="active", cadence_minutes=10,
                    last_at="2026-06-18T11:50:00.000000Z")  # 10 min before 12:00
    assert scheduler.is_leader_digest_heartbeat_due(
        t, now=_utc(hour=12, minute=0)) is False


# ---------------------------------------------------------------------------
# ms-128 / e-4284 — is_leader_halted_by_summary + compose_leader_fire_decision
# ---------------------------------------------------------------------------

def test_is_leader_halted_by_summary_both_stamped():
    t = _build_trek(status="active")
    t.setdefault("meta", {})["summary_sent_at"] = "2026-06-18T11:00:00.000000Z"
    t["meta"]["completion_notified_at"] = "2026-06-18T11:00:00.000000Z"
    assert scheduler.is_leader_halted_by_summary(t) is True


def test_is_leader_halted_by_summary_only_one_stamped():
    """片方だけ stamped では halt しない (= 従来通り fire し続ける)。"""
    t = _build_trek(status="active")
    t.setdefault("meta", {})["summary_sent_at"] = "2026-06-18T11:00:00.000000Z"
    assert scheduler.is_leader_halted_by_summary(t) is False


def test_compose_leader_fire_halted_wins():
    """halted_by_summary は signal / heartbeat があっても発火を止める。"""
    fire, reasons = scheduler.compose_leader_fire_decision(
        signal_fire=True, heartbeat_due=True, halted_by_summary=True)
    assert fire is False
    assert reasons == []


def test_compose_leader_fire_signal_only():
    fire, reasons = scheduler.compose_leader_fire_decision(
        signal_fire=True, heartbeat_due=False, halted_by_summary=False)
    assert fire is True
    assert reasons == ["signal"]


def test_compose_leader_fire_heartbeat_only():
    fire, reasons = scheduler.compose_leader_fire_decision(
        signal_fire=False, heartbeat_due=True, halted_by_summary=False)
    assert fire is True
    assert reasons == ["heartbeat"]


def test_compose_leader_fire_both_reasons_present():
    """signal と heartbeat が同時 due の時、理由は両方 list に載る (フラグ欠落で
    理由を失わない)。"""
    fire, reasons = scheduler.compose_leader_fire_decision(
        signal_fire=True, heartbeat_due=True, halted_by_summary=False)
    assert fire is True
    assert reasons == ["signal", "heartbeat"]


def test_compose_leader_fire_no_reason_no_fire():
    fire, reasons = scheduler.compose_leader_fire_decision(
        signal_fire=False, heartbeat_due=False, halted_by_summary=False)
    assert fire is False
    assert reasons == []


def test_should_fire_idle_escalation_first_time():
    t = _build_trek(status="active", cadence_minutes=10)
    t.setdefault("meta", {})["last_session_response_at"] = (
        "2026-06-18T11:00:00.000000Z"
    )
    assert scheduler.should_fire_idle_escalation(
        t, now=_utc(hour=12),
    ) is True


def test_should_fire_idle_escalation_cooldown_blocks_refire():
    t = _build_trek(status="active", cadence_minutes=10)
    t.setdefault("meta", {})["last_session_response_at"] = (
        "2026-06-18T11:00:00.000000Z"
    )
    t["meta"]["last_idle_escalation_at"] = (
        "2026-06-18T11:55:00.000000Z"  # 5 min ago — under 30-min cooldown
    )
    assert scheduler.should_fire_idle_escalation(
        t, now=_utc(hour=12, minute=0),
    ) is False


def test_should_fire_idle_escalation_cooldown_elapsed_refire_allowed():
    t = _build_trek(status="active", cadence_minutes=10)
    t.setdefault("meta", {})["last_session_response_at"] = (
        "2026-06-18T10:00:00.000000Z"  # 2 hours ago, idle
    )
    t["meta"]["last_idle_escalation_at"] = (
        "2026-06-18T11:00:00.000000Z"  # 1 hour ago — past 30-min cooldown
    )
    assert scheduler.should_fire_idle_escalation(
        t, now=_utc(hour=12, minute=0),
    ) is True


def test_build_idle_escalation_payload_carries_minutes_and_session():
    t = _build_trek(status="active", cadence_minutes=10)
    t.setdefault("meta", {})["last_session_response_at"] = (
        "2026-06-18T11:00:00.000000Z"  # 60 min ago
    )
    payload = scheduler.build_idle_escalation_payload(
        t, now=_utc(hour=12, minute=0),
    )
    assert payload["kind"] == "trek-idle-escalation"
    assert payload["trek_id"] == t["trek_id"]
    assert payload["leader_session_id"] == "sv-leader"
    assert payload["idle_minutes"] == 60
    assert payload["cadence_minutes"] == 10
    assert "idle" in payload["body"]
    assert "60" in payload["body"]


def test_payload_unknown_scope_falls_to_all_done():
    """A scope pointing at an MS with zero matching entries lands in the
    all-done branch (= no todos found). The fallback DM still carries
    target_entries=[] which is fine — the AI side recognises 'nothing
    to do, propose new task'."""
    t = _build_trek(
        status="active",
        scope=[{"project": "beacon-1", "milestone": "ms-99"}],
    )
    project_data = {
        "entries": [
            {"id": "e-1994", "type": "task", "status": "todo",
             "milestone": "ms-83",
             "description": "irrelevant"},
        ],
    }
    payload = scheduler.build_progress_check_payload(
        t, project_data=project_data, now=_utc(),
    )
    assert "todo Target が見当たりません" in payload["body"]
    # No matching todo + no matching done → no anchor.
    assert payload["target_entries"] == []


# ---------------------------------------------------------------------------
# Trek task aggregate terminal (ms-75 / e-2048)
# ---------------------------------------------------------------------------

def _trek_from_states(states: dict) -> dict:
    """ms-99 / e-2833 — synthesize scope entries for each task_states key.

    The Phase 2 scheduler reads slot inventory via ``materialize_slots``
    so the tick decision helpers need a scope[] to have anything to
    iterate. Task-kind scope entries with an empty project id and no
    ``claim_session_id`` map cleanly onto the cache-authoritative branch
    of ``_materialize_atomic_slot``.
    """
    return {
        "scope": [{"project": "p", "task": eid} for eid in states.keys()],
        "task_states": states,
    }


def _empty_gp(pid: str) -> dict:
    return {}


def test_aggregate_terminal_false_when_no_states_stamped():
    trek = _trek_from_states({})
    assert scheduler.is_trek_task_aggregate_terminal(
        trek, get_project=_empty_gp,
    ) is False


def test_aggregate_terminal_false_when_any_working():
    trek = _trek_from_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "working"},
    })
    assert scheduler.is_trek_task_aggregate_terminal(
        trek, get_project=_empty_gp,
    ) is False


def test_aggregate_terminal_true_when_all_done():
    trek = _trek_from_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "done"},
    })
    assert scheduler.is_trek_task_aggregate_terminal(
        trek, get_project=_empty_gp,
    ) is True


def test_aggregate_terminal_false_when_all_leader_review():
    """ms-88 / e-2107: leader_review は NON-terminal (= leader 判断要請、
    scheduler 走り続け)。 legacy `waiting-review` も leader_review に
    migrate するので同じく non-terminal。"""
    trek = _trek_from_states({
        "e-1": {"state": "leader_review"},
    })
    assert scheduler.is_trek_task_aggregate_terminal(
        trek, get_project=_empty_gp,
    ) is False
    # legacy alias も同じ挙動 (migrate されて leader_review 扱い)
    trek_legacy = _trek_from_states({
        "e-1": {"state": "waiting-review"},
    })
    assert scheduler.is_trek_task_aggregate_terminal(
        trek_legacy, get_project=_empty_gp,
    ) is False


def test_aggregate_terminal_true_when_all_user_review():
    """ms-88 / e-2107: user_review は terminal (= Trek 完遂等価、 leader が
    user に forward 済)。"""
    trek = _trek_from_states({
        "e-1": {"state": "user_review"},
    })
    assert scheduler.is_trek_task_aggregate_terminal(
        trek, get_project=_empty_gp,
    ) is True


def test_aggregate_terminal_true_when_mixed_terminal():
    """ms-88 / e-2107: terminal mixed = done + user_review。"""
    trek = _trek_from_states({
        "e-1": {"state": "done"},
        "e-2": {"state": "user_review"},
    })
    assert scheduler.is_trek_task_aggregate_terminal(
        trek, get_project=_empty_gp,
    ) is True


# ---------------------------------------------------------------------------
# Auto-stall detection (ms-75 / e-2067)
# ---------------------------------------------------------------------------

def _now_minus(minutes: int) -> datetime.datetime:
    base = datetime.datetime(2026, 6, 19, 12, 0, 0,
                             tzinfo=datetime.timezone.utc)
    return base - datetime.timedelta(minutes=minutes)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_BASE_NOW = datetime.datetime(2026, 6, 19, 12, 0, 0,
                              tzinfo=datetime.timezone.utc)


def test_detect_auto_stalled_returns_empty_for_planning_trek():
    """Safety nets must not fire while the leader is still drafting."""
    trek = {
        "status": "planning",
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(60))},
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_returns_empty_for_halted_trek():
    """Halt = leader deliberately paused; auto-stall is duplicate signal."""
    trek = {
        "status": "active",
        "halt": {"reason": "manual stop"},
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(60))},
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_returns_empty_when_no_states_stamped():
    trek = {"status": "active", "task_states": {}}
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_skips_task_under_ttl():
    """Working task with recent activity (< TTL) must not stall.
    ms-88 / e-2107: TTL 12 min default → 5 min は明らかに under。"""
    trek = {
        "status": "active",
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(5))},
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_detects_task_past_default_ttl():
    """ms-88 / e-2107 → ms-95 / e-2646: TTL default は 24h (1440 min) に再緩和。
    旧 12 min ベースの test は per-trek override で 12 min を inject して
    動作を保つ (= mechanism は変えていない、 default だけ変わった)。"""
    trek = {
        "status": "active",
        "meta": {"stall_threshold_minutes": 12},  # ms-95 / e-2646 — inject
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(13))},
        },
    }
    out = scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW)
    assert len(out) == 1
    assert out[0]["task_id"] == "e-1"
    assert out[0]["silence_minutes"] == 13
    assert out[0]["ttl_minutes"] == 12


def test_detect_auto_stalled_skips_terminal_states():
    """Done / waiting-review tasks must never auto-stall."""
    trek = {
        "status": "active",
        "task_states": {
            "e-done": {"state": "done",
                       "last_activity_at": _iso(_now_minus(120))},
            "e-wait": {"state": "waiting-review",
                       "last_activity_at": _iso(_now_minus(120))},
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_honors_per_trek_ttl_override():
    """meta.working_ttl_minutes per AC 6 (= per-trek override も 12 min default を上書き)。"""
    trek = {
        "status": "active",
        "meta": {"working_ttl_minutes": 5},
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(6))},
        },
    }
    out = scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW)
    assert len(out) == 1
    assert out[0]["ttl_minutes"] == 5
    assert out[0]["silence_minutes"] == 6


def test_detect_auto_stalled_falls_back_to_updated_at_for_legacy_entries():
    """task_states entries written before e-2067 land have no
    last_activity_at field. The detector falls back to updated_at so
    legacy stamps are still evaluated. ms-95 / e-2646: default 24h なので
    test は short threshold を inject して mechanism を verify。"""
    trek = {
        "status": "active",
        "meta": {"stall_threshold_minutes": 12},  # ms-95 / e-2646 — inject
        "task_states": {
            "e-1": {"state": "working",
                    "updated_at": _iso(_now_minus(15))},
        },
    }
    out = scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW)
    assert len(out) == 1
    assert out[0]["task_id"] == "e-1"


def test_detect_auto_stalled_skips_entry_with_no_activity_anchor():
    """No last_activity_at AND no updated_at → never had activity.
    Skipping avoids stalling a brand-new trek the moment it appears."""
    trek = {
        "status": "active",
        "task_states": {
            "e-1": {"state": "working"},
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_returns_multiple_tasks():
    """All working + stalled tasks in a single trek are returned together.
    ms-95 / e-2646: default 24h なので test は short threshold を inject。"""
    trek = {
        "status": "active",
        "meta": {"stall_threshold_minutes": 12},  # ms-95 / e-2646 — inject
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(20))},
            "e-2": {"state": "working",
                    "last_activity_at": _iso(_now_minus(30))},
            "e-3": {"state": "working",
                    "last_activity_at": _iso(_now_minus(5))},  # under TTL
        },
    }
    out = scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW)
    task_ids = sorted(s["task_id"] for s in out)
    assert task_ids == ["e-1", "e-2"]


def test_build_auto_stall_note_includes_silence_minutes():
    """Stable wording so the leader's review Skill + dogfood retro can
    grep the marker."""
    note = scheduler.build_auto_stall_note(42)
    assert "42 min" in note
    assert "auto-stalled" in note
    # Recovery path must be mentioned so the leader knows how to undo.
    assert "re-stamp working" in note


def test_get_working_ttl_minutes_in_scheduler_module_matches_default():
    """Scheduler-side getter is the operational read path; it must agree
    with the schema-side getter so server + CLI render the same number.
    ms-95 / e-2646: default 12 → 1440 min (24h)。"""
    assert scheduler.get_working_ttl_minutes({}) == 1440
    # Legacy field name still wins.
    assert scheduler.get_working_ttl_minutes(
        {"meta": {"working_ttl_minutes": 7}}
    ) == 7
    # ms-95 / e-2646: new field name (= stall_threshold_minutes) is the
    # preferred override path.
    assert scheduler.get_working_ttl_minutes(
        {"meta": {"stall_threshold_minutes": 99}}
    ) == 99
    # Conflict: new name wins (= ms-95 / e-2646).
    assert scheduler.get_working_ttl_minutes(
        {"meta": {"stall_threshold_minutes": 99,
                  "working_ttl_minutes": 7}}
    ) == 99


# ---------------------------------------------------------------------------
# ms-95 / e-2646 — stall threshold 24h default + pause primitive
# ---------------------------------------------------------------------------

def test_detect_auto_stalled_does_not_fire_under_24h_default():
    """ms-95 / e-2646: 12 min silence under the new 24h default should NOT
    trigger stall. This is the dogfood regression reproducer (= prep
    待機中の executor を「stuck」 と誤判定する病理を構造的に絶つ)。"""
    trek = {
        "status": "active",
        # No meta override → default 1440 min applies.
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(12))},
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_default_still_fires_after_24h():
    """ms-95 / e-2646: 24h + 1 min silence DOES still fire (= the safety
    net is still there for genuine silent halts, just slower to fire)."""
    trek = {
        "status": "active",
        "task_states": {
            "e-1": {"state": "working",
                    "last_activity_at": _iso(_now_minus(60 * 24 + 1))},
        },
    }
    out = scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW)
    assert len(out) == 1
    assert out[0]["task_id"] == "e-1"


def test_detect_auto_stalled_skips_when_working_pause_until_is_in_future():
    """ms-95 / e-2646: per-task pause primitive. executor が「意図的に保留中」
    と marker を立てている間は stall 判定をスキップ (= staging URL 受領
    待ち / 別 session のレビュー待ちの正規表現)。"""
    future = _iso(_BASE_NOW + datetime.timedelta(hours=2))
    trek = {
        "status": "active",
        "meta": {"stall_threshold_minutes": 12},  # 短い threshold inject
        "task_states": {
            "e-1": {
                "state": "working",
                "last_activity_at": _iso(_now_minus(20)),  # 通常なら stall
                "meta": {"working_pause_until": future},
            },
        },
    }
    # threshold は超えているが pause 中なので skip される。
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


def test_detect_auto_stalled_fires_after_working_pause_until_expires():
    """ms-95 / e-2646: pause 期限が過ぎたら通常 stall 判定に戻る。"""
    past = _iso(_BASE_NOW - datetime.timedelta(minutes=1))
    trek = {
        "status": "active",
        "meta": {"stall_threshold_minutes": 12},
        "task_states": {
            "e-1": {
                "state": "working",
                "last_activity_at": _iso(_now_minus(20)),
                "meta": {"working_pause_until": past},
            },
        },
    }
    out = scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW)
    assert len(out) == 1


def test_detect_auto_stalled_skips_when_working_paused_boolean_is_truthy():
    """ms-95 / e-2646: 期限指定なしで「とにかく止めて」 と表現する
    boolean marker も受理する経路。"""
    trek = {
        "status": "active",
        "meta": {"stall_threshold_minutes": 12},
        "task_states": {
            "e-1": {
                "state": "working",
                "last_activity_at": _iso(_now_minus(60)),
                "meta": {"working_paused": True},
            },
        },
    }
    assert scheduler.detect_auto_stalled_tasks(trek, now=_BASE_NOW) == []


# ---------------------------------------------------------------------------
# ms-92 / e-2164 — build_leader_digest_payload
# ---------------------------------------------------------------------------


def _trek_with_pulse_acks() -> dict:
    """A minimal trek doc with 3 sessions in 3 different states.

    Constructs ``pulse_acks`` via the real ``trek.record_pulse_ack`` so
    we exercise the actual e-2165 schema-write path.
    """
    t = trek_mod.new_trek(
        title="digest test", creator_user_id="u-1",
        creator_email="a@b.com", creator_session_id="sv-leader",
    )
    # Active working session.
    trek_mod.record_pulse_ack(
        t, session_id="sv-working", picked_choice="continue",
        state_summary="working on e-2165", time_on_task_seconds=1200,
    )
    # Stuck session with blockers + needs-leader flag.
    trek_mod.record_pulse_ack(
        t, session_id="sv-stuck", picked_choice="dm-leader",
        state_summary="stuck on e-2200", blockers=["OOM in CI"],
        needs_leader_judgment=True, time_on_task_seconds=5400,
    )
    # Idle session.
    trek_mod.record_pulse_ack(
        t, session_id="sv-idle", picked_choice="no-op",
        state_summary="idle, waiting for peer", time_on_task_seconds=0,
    )
    return t


def test_leader_digest_payload_includes_kind_and_trek_id():
    """Channel dispatcher routes on payload.kind, so the field is fragile."""
    t = _trek_with_pulse_acks()
    payload = scheduler.build_leader_digest_payload(t)
    assert payload["kind"] == "trek-leader-digest"
    assert payload["trek_id"] == t["trek_id"]
    assert payload["created_at"]


def test_leader_digest_payload_summary_carries_aggregates():
    t = _trek_with_pulse_acks()
    payload = scheduler.build_leader_digest_payload(t)
    s = payload["summary"]
    assert s["active"] == 3
    assert s["stuck"] == 1   # sv-stuck has blockers
    assert s["idle"] == 1    # sv-idle: state contains "idle" AND time_on_task=0
    assert s["needs_leader_judgment"] == 1
    assert s["total_acks_across_sessions"] == 3


def test_leader_digest_payload_sessions_sorted_by_time_on_task_desc():
    """Longest-stuck-first surface so the leader's eye lands on the most
    likely attention candidate."""
    t = _trek_with_pulse_acks()
    payload = scheduler.build_leader_digest_payload(t)
    sids = [s["session_id"] for s in payload["sessions"]]
    assert sids == ["sv-stuck", "sv-working", "sv-idle"]


def test_leader_digest_payload_sessions_carry_structured_snapshot():
    t = _trek_with_pulse_acks()
    payload = scheduler.build_leader_digest_payload(t)
    stuck = next(s for s in payload["sessions"] if s["session_id"] == "sv-stuck")
    assert stuck["state_summary"] == "stuck on e-2200"
    assert stuck["blockers"] == ["OOM in CI"]
    assert stuck["needs_leader_judgment"] is True
    assert stuck["time_on_task_seconds"] == 5400
    assert stuck["last_picked_choice"] == "dm-leader"
    assert stuck["total_acks"] == 1


def test_leader_digest_payload_body_carries_human_readable_summary():
    t = _trek_with_pulse_acks()
    payload = scheduler.build_leader_digest_payload(t)
    body = payload["body"]
    assert "Trek leader digest" in body
    assert "active=3" in body
    assert "stuck=1" in body
    assert "idle=1" in body
    # Per-session lines with type tags
    assert "[stuck]" in body
    assert "[idle]" in body


# ---------------------------------------------------------------------------
# ms-128 / e-4307 — executor-waiting-on-leader + commit recency in digest
# ---------------------------------------------------------------------------

def test_digest_surfaces_waiting_on_leader():
    """sv-stuck picked dm-leader → waiting_on_leader per session + summary
    count, so the leader sees the judgment-wait even without a leader_review."""
    t = _trek_with_pulse_acks()
    payload = scheduler.build_leader_digest_payload(t)
    stuck = next(s for s in payload["sessions"] if s["session_id"] == "sv-stuck")
    assert stuck["waiting_on_leader"] is True
    working = next(
        s for s in payload["sessions"] if s["session_id"] == "sv-working")
    assert working["waiting_on_leader"] is False
    assert payload["summary"]["waiting_on_leader_count"] == 1


def test_working_targets_recency_flags_silent_commit():
    """A working target whose progress last advanced 45 min ago (> 30) is
    surfaced as silent with its minutes-since-progress (commit recency).
    Anchor is progress_last_advanced_at ONLY (no updated_at fallback)."""
    t = trek_mod.new_trek(
        title="recency", creator_user_id="u", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    t["task_states"] = {
        "e-stale": {"state": "working",
                    "progress_last_advanced_at": "2026-06-18T11:15:00.000000Z",
                    "last_commit_count": 2},
        "e-fresh": {"state": "working",
                    "progress_last_advanced_at": "2026-06-18T11:55:00.000000Z",
                    "last_commit_count": 5},
        "e-done": {"state": "user_review",
                   "progress_last_advanced_at": "2026-06-18T10:00:00.000000Z"},
    }
    rec = scheduler.build_working_targets_recency(
        t, now=_utc(hour=12, minute=0),
        migrate_state=trek_mod.migrate_legacy_task_state,
    )
    # Only working targets (user_review excluded)
    ids = [r["target_id"] for r in rec]
    assert ids == ["e-stale", "e-fresh"]  # silentest first
    stale = rec[0]
    assert stale["progress_anchor_known"] is True
    assert stale["minutes_since_progress"] == 45
    assert stale["is_silent"] is True
    fresh = rec[1]
    assert fresh["minutes_since_progress"] == 5
    assert fresh["is_silent"] is False


def test_working_targets_recency_no_updated_at_fallback_masking():
    """AX #537: a working target with NO progress anchor but a recent
    updated_at must NOT look 'freshly progressed' — it is surfaced as
    unknown (observation gap), sorted first, not silently healthy."""
    t = trek_mod.new_trek(
        title="mask", creator_user_id="u", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    t["task_states"] = {
        # No progress_last_advanced_at, but a very recent updated_at that
        # the OLD fallback would have read as "1 min ago progressed".
        "e-unknown": {"state": "working",
                      "updated_at": "2026-06-18T11:59:00.000000Z"},
        "e-known": {"state": "working",
                    "progress_last_advanced_at": "2026-06-18T11:50:00.000000Z"},
    }
    rec = scheduler.build_working_targets_recency(
        t, now=_utc(hour=12, minute=0),
        migrate_state=trek_mod.migrate_legacy_task_state,
    )
    unk = next(r for r in rec if r["target_id"] == "e-unknown")
    assert unk["progress_anchor_known"] is False
    assert unk["minutes_since_progress"] is None  # not masked by updated_at
    assert unk["is_silent"] is False              # unknown != silent, but…
    # …unknown is surfaced first (most invisible = needs attention)
    assert rec[0]["target_id"] == "e-unknown"


def test_digest_summary_counts_silent_and_unknown_working_targets():
    t = _trek_with_pulse_acks()
    t.setdefault("task_states", {})["e-stale"] = {
        "state": "working",
        "progress_last_advanced_at": "2026-06-18T11:15:00.000000Z",  # 45 min
    }
    t["task_states"]["e-unknown"] = {"state": "working"}  # no progress anchor
    payload = scheduler.build_leader_digest_payload(t, now=_utc(hour=12))
    assert payload["summary"]["silent_working_targets"] == 1
    assert payload["summary"]["unknown_progress_targets"] == 1
    rec_ids = [r["target_id"] for r in payload["working_targets_recency"]]
    assert "e-stale" in rec_ids and "e-unknown" in rec_ids
    # body surfaces the silent-wait line with payload-key-matching tokens
    assert "silent wait" in payload["body"]
    assert "silent_working_targets=1" in payload["body"]


def test_digest_fallback_and_main_payload_have_same_top_level_keys():
    """maint #537: the trek-module-unavailable fallback payload and the main
    payload must expose the same top-level keys (+ summary keys), so a
    consumer never hits a KeyError on the rare fallback path. Pins the
    3-way shape (docstring / fallback / main) against silent drift."""
    import trek_scheduler as ts_mod

    # main path (trek module available)
    main = scheduler.build_leader_digest_payload(_trek_with_pulse_acks())
    # fallback path: force _import_trek to return None
    orig = ts_mod._import_trek
    try:
        ts_mod._import_trek = lambda: None
        fb = scheduler.build_leader_digest_payload(
            {"trek_id": "tk-x", "task_states": {}})
    finally:
        ts_mod._import_trek = orig
    assert set(main.keys()) == set(fb.keys())
    assert set(main["summary"].keys()) == set(fb["summary"].keys())


def test_leader_digest_payload_excludes_placeholder_sessions():
    """pulse_acks entry with total_acks=0 (= legacy placeholder) is filtered out."""
    t = trek_mod.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    t["pulse_acks"] = {
        "sv-ghost": {
            "session_id": "sv-ghost",
            "total_acks": 0,
            "last_pulse_ack_at": "",
            "last_picked_choice": "",
            "history": [],
            "last_state_summary": "",
            "last_blockers": [],
            "last_needs_leader_judgment": False,
            "last_time_on_task_seconds": 0,
        },
    }
    payload = scheduler.build_leader_digest_payload(t)
    assert payload["sessions"] == []
    assert payload["summary"]["active"] == 0


def test_leader_digest_payload_empty_trek_fallback_body():
    """Trek with no pulse-acks still produces a valid payload."""
    t = trek_mod.new_trek(
        title="empty", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    payload = scheduler.build_leader_digest_payload(t)
    assert payload["summary"]["active"] == 0
    assert payload["sessions"] == []


# ---------------------------------------------------------------------------
# ms-97 Phase 4 / AC32 — halt 完全化 (= autonomous activity 4 paths suspend)
# ---------------------------------------------------------------------------

def test_ac32_halt_blocks_select_due_treks():
    """End-to-end: select_due_treks filters out a trek with halt set.

    Even if a trek would otherwise be due (= never fired before, status
    active), engaging the Andon cord (= halt dict on trek_doc) removes
    it from the due list. This locks in the scheduler tick fire skip
    that AC32 promises.
    """
    halted = trek_mod.set_halt(
        _build_trek(status="active"),
        issued_by_session_id="sv-leader",
        reason="manual stop",
    )
    not_halted = _build_trek(status="active")
    due = scheduler.select_due_treks([halted, not_halted], now=_utc())
    due_ids = [t.get("trek_id") for t in due]
    assert halted["trek_id"] not in due_ids, (
        "halted trek must not appear in due list"
    )
    assert not_halted["trek_id"] in due_ids, (
        "non-halted trek with no prior fire should still be due"
    )


def test_ac32_halt_blocks_auto_stall_detection():
    """AC32 path #3 — detect_auto_stalled_tasks returns [] for halted treks.

    Even a task long past its TTL must not auto-transition to
    leader_review while the trek is halted. The 2-pass tick endpoint
    relies on this for the auto-stall skip; this test pins the
    underlying detector behaviour.
    """
    # Build a halted trek with a stale working task.
    t = trek_mod.new_trek(
        title="x", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    t["status"] = "active"
    t["halt"] = {
        "issued_by_session_id": "sv-leader",
        "reason": "STOP",
        "issued_at": "2026-06-18T00:00:00.000000Z",
    }
    t["task_states"] = {
        "e-stuck": {
            "state": "working",
            "updated_by_session_id": "sv-exec",
            "updated_at": "2026-06-17T00:00:00.000000Z",
            "last_activity_at": "2026-06-17T00:00:00.000000Z",
            "note": "",
        }
    }
    stalled = scheduler.detect_auto_stalled_tasks(t, now=_utc())
    assert stalled == [], (
        f"halted trek must not produce auto-stall transitions, got {stalled}"
    )


# ---------------------------------------------------------------------------
# ms-97 / e-2707 — build_task_state_aggregate + leader-digest payload
# task_state_aggregate field (AC10 precedence surfaced for leader digest)
# ---------------------------------------------------------------------------


def _trek_with_task_states(states: dict) -> dict:
    """Build a trek with raw task_states entries pre-populated.

    Bypasses the set_task_state state-machine validator so we can pin
    arbitrary state combinations (= the aggregate is purely read-side
    over whatever the executor / leader has stamped).
    """
    t = trek_mod.new_trek(
        title="agg test", creator_user_id="u-1", creator_email="a@b.com",
        creator_session_id="sv-leader",
    )
    t["status"] = "active"
    t["task_states"] = dict(states)
    return t


def test_task_state_aggregate_empty_returns_zero_counts_and_todo():
    """Trek with no task_states (= fresh) aggregates to all-zero counts
    + overall_state='todo' (= compute_ms_slot_state's empty-slot fallback).
    """
    t = _trek_with_task_states({})
    agg = scheduler.build_task_state_aggregate(t)
    assert agg["counts"] == {
        "leader_review": 0, "done": 0, "user_review": 0,
        "working": 0, "todo": 0, "block": 0,
    }
    assert agg["leader_review_queue"] == []
    assert agg["blocked_queue"] == []
    assert agg["overall_state"] == "todo"


def test_task_state_aggregate_counts_each_state_bucket():
    """Each state stamped lands in its own bucket; the count matrix is the
    structural signal leaders read."""
    t = _trek_with_task_states({
        "e-a": {"state": "done", "updated_at": "2026-06-25T00:00:00Z"},
        "e-b": {"state": "done", "updated_at": "2026-06-25T01:00:00Z"},
        "e-c": {"state": "working", "updated_at": "2026-06-25T02:00:00Z"},
        "e-d": {"state": "todo", "updated_at": "2026-06-25T03:00:00Z"},
        "e-e": {"state": "user_review", "updated_at": "2026-06-25T04:00:00Z"},
    })
    agg = scheduler.build_task_state_aggregate(t)
    # ms-128 方針5: done は Trek 状態機械から除去され read-time に user_review へ
    # migrate される。done×2 + user_review×1 は user_review=3 に畳まれ done=0。
    assert agg["counts"] == {
        "leader_review": 0, "done": 0, "user_review": 3,
        "working": 1, "todo": 1, "block": 0,
    }
    # overall_state: 1 working → "working" wins per AC10 precedence
    assert agg["overall_state"] == "working"


def test_task_state_aggregate_leader_review_wins_overall_state():
    """AC10 precedence top: any leader_review present → overall_state='leader_review'.
    This is the case where the leader is structurally blocking executor
    progress and the digest must surface it."""
    t = _trek_with_task_states({
        "e-a": {"state": "done"},
        "e-b": {"state": "leader_review",
                "updated_by_session_id": "sv-exec",
                "updated_at": "2026-06-25T00:00:00Z",
                "note": "needs leader judgment"},
        "e-c": {"state": "working"},
    })
    agg = scheduler.build_task_state_aggregate(t)
    assert agg["overall_state"] == "leader_review"
    assert agg["counts"]["leader_review"] == 1


def test_task_state_aggregate_leader_review_queue_carries_entry_metadata():
    """Each leader_review entry surfaces task_id + updated_by + updated_at
    + note so the leader can act without a second lookup."""
    t = _trek_with_task_states({
        "e-target": {
            "state": "leader_review",
            "updated_by_session_id": "sv-exec-1",
            "updated_at": "2026-06-25T12:34:56Z",
            "note": "needs approve / re-work / forward",
        },
    })
    agg = scheduler.build_task_state_aggregate(t)
    assert len(agg["leader_review_queue"]) == 1
    row = agg["leader_review_queue"][0]
    assert row["task_id"] == "e-target"
    assert row["updated_by_session_id"] == "sv-exec-1"
    assert row["updated_at"] == "2026-06-25T12:34:56Z"
    assert "approve" in row["note"]


def test_task_state_aggregate_leader_review_queue_sorted_oldest_first():
    """Oldest leader_review surfaces first (= been waiting longest)."""
    t = _trek_with_task_states({
        "e-newest": {"state": "leader_review",
                     "updated_at": "2026-06-25T03:00:00Z"},
        "e-oldest": {"state": "leader_review",
                     "updated_at": "2026-06-25T01:00:00Z"},
        "e-middle": {"state": "leader_review",
                     "updated_at": "2026-06-25T02:00:00Z"},
    })
    agg = scheduler.build_task_state_aggregate(t)
    ids = [r["task_id"] for r in agg["leader_review_queue"]]
    assert ids == ["e-oldest", "e-middle", "e-newest"]


def test_task_state_aggregate_unknown_state_collapses_to_todo():
    """Malformed / unknown state token → counted as todo, not crashed."""
    t = _trek_with_task_states({
        "e-weird": {"state": "garbage-state"},
        "e-clean": {"state": "todo"},
    })
    agg = scheduler.build_task_state_aggregate(t)
    assert agg["counts"]["todo"] == 2
    assert agg["overall_state"] == "todo"


def test_leader_digest_payload_summary_carries_leader_review_queue_count():
    """summary.leader_review_queue_count surfaces in parallel to
    needs_leader_judgment so the leader can read either signal.
    Regression pin: the 2026-06-29 LPS dogfood post-mortem proved
    needs_leader_judgment=0 while task_states had leader_review queue,
    which let the leader read past the digest for 10 minutes."""
    t = _trek_with_task_states({
        "e-blocked": {"state": "leader_review",
                      "updated_at": "2026-06-25T00:00:00Z"},
    })
    # Add a pulse-ack so the digest doesn't short-circuit to the
    # empty-sessions path; the queue surface is independent of pulse-acks.
    trek_mod.record_pulse_ack(
        t, session_id="sv-exec", picked_choice="continue",
        state_summary="working", time_on_task_seconds=10,
    )
    payload = scheduler.build_leader_digest_payload(t)
    assert payload["summary"]["leader_review_queue_count"] == 1
    # needs_leader_judgment is pulse-ack derived and may be 0 even when
    # leader_review_queue_count is non-zero — the two are independent.


def test_leader_digest_payload_includes_task_state_aggregate_field():
    """The structured task_state_aggregate field is the Skill-facing
    payload so the leader-side AI can chain /beacon-trek-review without
    re-deriving state from the body string."""
    t = _trek_with_task_states({
        "e-r": {"state": "leader_review",
                "updated_by_session_id": "sv-exec",
                "updated_at": "2026-06-25T00:00:00Z",
                "note": "ready for review"},
        "e-w": {"state": "working"},
    })
    payload = scheduler.build_leader_digest_payload(t)
    assert "task_state_aggregate" in payload
    agg = payload["task_state_aggregate"]
    assert agg["counts"]["leader_review"] == 1
    assert agg["counts"]["working"] == 1
    assert agg["overall_state"] == "leader_review"
    assert agg["leader_review_queue"][0]["task_id"] == "e-r"


def test_leader_digest_payload_body_surfaces_leader_review_queue_when_nonempty():
    """Body text exposes the leader_review queue so legacy Skills /
    un-upgraded bridges still surface the actionable count."""
    t = _trek_with_task_states({
        "e-r1": {"state": "leader_review",
                 "updated_at": "2026-06-25T00:00:00Z"},
        "e-r2": {"state": "leader_review",
                 "updated_at": "2026-06-25T01:00:00Z"},
    })
    payload = scheduler.build_leader_digest_payload(t)
    body = payload["body"]
    assert "leader_review_queue=2" in body
    assert "leader_review queue: 2 件" in body
    assert "/beacon-trek-review" in body


def test_leader_digest_payload_body_omits_queue_line_when_empty():
    """Clean trek (= no leader_review entries) keeps the body terse —
    the queue line only fires when there is action to take."""
    t = _trek_with_task_states({
        "e-a": {"state": "working"},
    })
    payload = scheduler.build_leader_digest_payload(t)
    body = payload["body"]
    assert "leader_review_queue=0" in body
    assert "invoke /beacon-trek-review NOW" not in body


def test_leader_digest_payload_fallback_path_still_carries_aggregate(monkeypatch):
    """When the lazy trek import fails (= ``_import_trek`` returns None),
    the fallback empty-shape payload must still expose
    task_state_aggregate so callers do not crash on field access."""
    # Force the trek module unavailable for the duration of the call.
    monkeypatch.setattr(scheduler, "_import_trek", lambda: None)
    t = {
        "trek_id": "tk-fallback",
        "task_states": {
            "e-a": {"state": "leader_review",
                    "updated_at": "2026-06-25T00:00:00Z"},
        },
    }
    payload = scheduler.build_leader_digest_payload(t)
    assert "task_state_aggregate" in payload
    assert payload["summary"]["leader_review_queue_count"] == 1
    assert payload["task_state_aggregate"]["overall_state"] == "leader_review"
