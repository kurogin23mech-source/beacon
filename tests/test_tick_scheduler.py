"""Unit tests for lib/tick_scheduler.py — target-agnostic periodic-tick cadence
(ms-107 e-3434/e-3461). The descriptor is entity-free: {enabled, cadence_minutes,
last_fired_at}, so the same logic drives Operations, Account 定期連絡, and
short-lived communication tasks."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import tick_scheduler as ts  # noqa: E402

NOW = "2026-07-15T12:00:00+00:00"


def _d(**kw):
    kw.setdefault("enabled", True)
    return kw


def test_enabled_never_fired_is_due():
    assert ts.is_due(_d(), NOW) is True


def test_disabled_never_due():
    assert ts.is_due(_d(enabled=False), NOW) is False
    assert ts.is_due({}, NOW) is False  # missing enabled → falsy


def test_cadence_gate_elapsed_vs_not():
    assert ts.is_due(_d(last_fired_at="2026-07-15T11:30:00+00:00"), NOW) is False  # 30m < 60
    assert ts.is_due(_d(last_fired_at="2026-07-15T10:30:00+00:00"), NOW) is True   # 90m


def test_cadence_override():
    assert ts.is_due(_d(cadence_minutes=10, last_fired_at="2026-07-15T11:45:00+00:00"), NOW) is True
    assert ts.is_due(_d(cadence_minutes=10, last_fired_at="2026-07-15T11:55:00+00:00"), NOW) is False


def test_instant_based_across_timezones():
    # last fired 11:30 JST = 02:30 UTC; now 12:00 UTC → 9.5h → due.
    assert ts.is_due(_d(last_fired_at="2026-07-15T11:30:00+09:00"), NOW) is True


def test_bad_cadence_falls_back_to_default():
    assert ts.is_due(_d(cadence_minutes="oops", last_fired_at="2026-07-15T11:30:00+00:00"), NOW) is False


def test_bad_now_not_due():
    assert ts.is_due(_d(), "") is False
    assert ts.is_due(_d(), "not-a-date") is False


def test_select_due_filters():
    ds = [
        _d(),                                                    # due (never fired)
        _d(enabled=False),                                       # disabled
        _d(last_fired_at="2026-07-15T11:59:00+00:00"),           # too recent
    ]
    due = ts.select_due(ds, NOW)
    assert len(due) == 1 and due[0] is ds[0]


def test_truthy_helper():
    assert ts.truthy(True) and ts.truthy("true") and ts.truthy("1") and ts.truthy("YES")
    assert not ts.truthy(False) and not ts.truthy("no") and not ts.truthy(None) and not ts.truthy("")


def test_entity_agnostic_descriptor_shape():
    # Same primitive drives an Account 定期連絡 (persistent target, not Operation)
    account_contact = {"enabled": True, "cadence_minutes": 20160,  # ~2 weeks
                       "last_fired_at": "2026-06-01T00:00:00+00:00"}
    assert ts.is_due(account_contact, NOW) is True
    # ...and a short-lived reply-watch task
    watch = {"enabled": True, "cadence_minutes": 60, "last_fired_at": None}
    assert ts.is_due(watch, NOW) is True
