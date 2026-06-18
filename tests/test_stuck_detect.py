"""Tests for the STUCK detector (ms-55 e-1649)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import stuck_detect  # noqa: E402
import stop_signal  # noqa: E402


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# find_stuck_sessions
# ---------------------------------------------------------------------------

def test_idle_within_window_not_stuck():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-A",
        last_active=_iso(NOW - timedelta(minutes=10)),
    )
    out = stuck_detect.find_stuck_sessions(
        [t], now=NOW, timeout_minutes=30,
    )
    assert out == []


def test_idle_at_threshold_is_stuck():
    """Exactly at the threshold counts as stuck (>=) — being explicit
    avoids the off-by-one bug where N=30min would let a session sit
    forever at exactly 30 minutes."""
    t = stuck_detect.SessionTelemetry(
        session_id="sv-A",
        last_active=_iso(NOW - timedelta(minutes=30)),
    )
    out = stuck_detect.find_stuck_sessions(
        [t], now=NOW, timeout_minutes=30,
    )
    assert len(out) == 1


def test_idle_past_threshold_is_stuck():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-A",
        last_active=_iso(NOW - timedelta(minutes=45)),
    )
    out = stuck_detect.find_stuck_sessions(
        [t], now=NOW, timeout_minutes=30,
    )
    assert len(out) == 1
    assert out[0].session_id == "sv-A"


def test_empty_last_active_is_not_stuck():
    """Don't flag sessions we've never seen — STUCK should be specific."""
    t = stuck_detect.SessionTelemetry(session_id="sv-A", last_active="")
    assert stuck_detect.find_stuck_sessions([t], now=NOW) == []


def test_malformed_last_active_is_not_stuck():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-A", last_active="not-a-date",
    )
    assert stuck_detect.find_stuck_sessions([t], now=NOW) == []


def test_future_last_active_is_skipped():
    """Clock skew: a session whose last_active is in the future
    shouldn't be flagged."""
    t = stuck_detect.SessionTelemetry(
        session_id="sv-A",
        last_active=_iso(NOW + timedelta(minutes=10)),
    )
    assert stuck_detect.find_stuck_sessions([t], now=NOW) == []


def test_ignore_flag_skips():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-A",
        last_active=_iso(NOW - timedelta(hours=2)),
        ignore=True,
    )
    assert stuck_detect.find_stuck_sessions([t], now=NOW) == []


def test_empty_session_id_skipped():
    t = stuck_detect.SessionTelemetry(
        session_id="",
        last_active=_iso(NOW - timedelta(hours=2)),
    )
    assert stuck_detect.find_stuck_sessions([t], now=NOW) == []


def test_mixed_input_returns_only_stuck():
    rows = [
        stuck_detect.SessionTelemetry(
            session_id="sv-A",
            last_active=_iso(NOW - timedelta(minutes=5)),  # fresh
        ),
        stuck_detect.SessionTelemetry(
            session_id="sv-B",
            last_active=_iso(NOW - timedelta(minutes=45)),  # stuck
        ),
        stuck_detect.SessionTelemetry(
            session_id="sv-C",
            last_active="",  # unknown
        ),
        stuck_detect.SessionTelemetry(
            session_id="sv-D",
            last_active=_iso(NOW - timedelta(hours=5)),  # very stuck
        ),
    ]
    out = stuck_detect.find_stuck_sessions(rows, now=NOW, timeout_minutes=30)
    ids = [t.session_id for t in out]
    assert ids == ["sv-B", "sv-D"]


def test_timeout_must_be_positive():
    with pytest.raises(ValueError):
        stuck_detect.find_stuck_sessions(
            [], now=NOW, timeout_minutes=0,
        )
    with pytest.raises(ValueError):
        stuck_detect.find_stuck_sessions(
            [], now=NOW, timeout_minutes=-5,
        )


# ---------------------------------------------------------------------------
# build_stuck_signal
# ---------------------------------------------------------------------------

def test_build_stuck_targets_session():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-stuck",
        last_active=_iso(NOW - timedelta(minutes=45)),
        ms_id="ms-55",
        task_id="e-1646",
    )
    payload = stuck_detect.build_stuck_signal(
        t,
        issued_by_session_id="sv-detector",
        now=NOW,
        timeout_minutes=30,
    )
    assert payload["kind"] == "stop"
    assert payload["scope"] == "scoped"
    assert payload["target"] == {"kind": "session", "id": "sv-stuck"}
    assert payload["reason_kind"] == "stuck"
    assert payload["issued_by_session_id"] == "sv-detector"


def test_build_stuck_machine_reason():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-stuck",
        last_active=_iso(NOW - timedelta(minutes=45)),
        ms_id="ms-55",
    )
    payload = stuck_detect.build_stuck_signal(
        t, issued_by_session_id="sv-detector", now=NOW, timeout_minutes=30,
    )
    mr = payload["machine_reason"]
    assert mr["detector"] == "stuck"
    assert mr["timeout_minutes"] == 30
    assert mr["idle_seconds"] == 45 * 60
    assert mr["ms_id"] == "ms-55"
    assert "task_id" not in mr  # not set


def test_build_stuck_idle_seconds_negative_when_no_last_active():
    """A session with no last_active shouldn't really reach build_stuck_signal
    (= find_stuck_sessions skips it), but if the caller bypasses the
    detector and calls build_stuck_signal directly, the builder must
    not crash."""
    t = stuck_detect.SessionTelemetry(
        session_id="sv-X", last_active="",
    )
    payload = stuck_detect.build_stuck_signal(
        t, issued_by_session_id="sv-detector", now=NOW,
    )
    assert payload["machine_reason"]["idle_seconds"] == -1


def test_build_stuck_requires_issuer():
    t = stuck_detect.SessionTelemetry(
        session_id="sv-X",
        last_active=_iso(NOW - timedelta(minutes=45)),
    )
    with pytest.raises(ValueError):
        stuck_detect.build_stuck_signal(t, issued_by_session_id="", now=NOW)


def test_build_stuck_requires_target_session_id():
    t = stuck_detect.SessionTelemetry(
        session_id="",
        last_active=_iso(NOW - timedelta(minutes=45)),
    )
    with pytest.raises(ValueError):
        stuck_detect.build_stuck_signal(
            t, issued_by_session_id="sv-detector", now=NOW,
        )


def test_built_stuck_payload_parses_as_stop():
    """The STUCK signal must round-trip through stop_signal.parse_stop_event
    — STUCK is just a flavor of stop, not a new protocol."""
    t = stuck_detect.SessionTelemetry(
        session_id="sv-stuck",
        last_active=_iso(NOW - timedelta(minutes=45)),
        ms_id="ms-55",
    )
    payload = stuck_detect.build_stuck_signal(
        t, issued_by_session_id="sv-detector", now=NOW,
    )
    event = {
        "event_id": "e-1",
        "channel": "stop-signal",
        "payload": payload,
        "created_at": _iso(NOW),
    }
    rec = stop_signal.parse_stop_event(event)
    assert rec is not None
    assert rec["reason_kind"] == "stuck"
    assert rec["target"] == {"kind": "session", "id": "sv-stuck"}
    # And the receiver-side gate honors it for the stuck session.
    assert stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-stuck",
    )
    assert not stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-different",
    )
