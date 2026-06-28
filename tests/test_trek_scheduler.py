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
    assert "todo task が見当たりません" in payload["body"]
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
    assert "todo task が見当たりません" in payload["body"]
    # No matching todo + no matching done → no anchor.
    assert payload["target_entries"] == []


# ---------------------------------------------------------------------------
# Trek task aggregate terminal (ms-75 / e-2048)
# ---------------------------------------------------------------------------

def test_aggregate_terminal_false_when_no_states_stamped():
    trek = {"task_states": {}}
    assert scheduler.is_trek_task_aggregate_terminal(trek) is False


def test_aggregate_terminal_false_when_any_working():
    trek = {"task_states": {
        "e-1": {"state": "done"},
        "e-2": {"state": "working"},
    }}
    assert scheduler.is_trek_task_aggregate_terminal(trek) is False


def test_aggregate_terminal_true_when_all_done():
    trek = {"task_states": {
        "e-1": {"state": "done"},
        "e-2": {"state": "done"},
    }}
    assert scheduler.is_trek_task_aggregate_terminal(trek) is True


def test_aggregate_terminal_false_when_all_leader_review():
    """ms-88 / e-2107: leader_review は NON-terminal (= leader 判断要請、
    scheduler 走り続け)。 legacy `waiting-review` も leader_review に
    migrate するので同じく non-terminal。"""
    trek = {"task_states": {
        "e-1": {"state": "leader_review"},
    }}
    assert scheduler.is_trek_task_aggregate_terminal(trek) is False
    # legacy alias も同じ挙動 (migrate されて leader_review 扱い)
    trek_legacy = {"task_states": {
        "e-1": {"state": "waiting-review"},
    }}
    assert scheduler.is_trek_task_aggregate_terminal(trek_legacy) is False


def test_aggregate_terminal_true_when_all_user_review():
    """ms-88 / e-2107: user_review は terminal (= Trek 完遂等価、 leader が
    user に forward 済)。"""
    trek = {"task_states": {
        "e-1": {"state": "user_review"},
    }}
    assert scheduler.is_trek_task_aggregate_terminal(trek) is True


def test_aggregate_terminal_true_when_mixed_terminal():
    """ms-88 / e-2107: terminal mixed = done + user_review。"""
    trek = {"task_states": {
        "e-1": {"state": "done"},
        "e-2": {"state": "user_review"},
    }}
    assert scheduler.is_trek_task_aggregate_terminal(trek) is True


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
    assert "まだ pulse-ack" in payload["body"]
