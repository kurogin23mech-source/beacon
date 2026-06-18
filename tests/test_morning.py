"""Tests for the morning briefing aggregator (ms-55 e-1650)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import morning  # noqa: E402
import stop_signal  # noqa: E402
import claims  # noqa: E402


NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------

def _stop_event(*, ts, scope="global", target_kind="", target_id="",
                reason_kind="manual", reason=""):
    payload = stop_signal.build_stop_payload(
        scope=scope,
        issued_by_session_id="sv-detector",
        target_kind=target_kind or None,
        target_id=target_id or None,
        reason=reason,
        reason_kind=reason_kind,
        issued_at=ts,
    )
    return {
        "event_id": f"ev-{ts}",
        "channel": stop_signal.STOP_CHANNEL,
        "payload": payload,
        "created_at": ts,
    }


def _release_event(*, ts, outcome="completed", reason=""):
    payload = claims.build_release_payload(
        claim_id=f"cl-{ts}",
        outcome=outcome,
        from_session_id="sv-A",
        reason=reason,
        issued_at=ts,
    )
    return {
        "event_id": f"ev-{ts}",
        "channel": claims.CLAIM_CHANNEL,
        "payload": payload,
        "created_at": ts,
    }


# ---------------------------------------------------------------------------
# build_briefing
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty_briefing():
    b = morning.build_briefing([])
    assert b.entries == []
    # All bucket counts present and 0.
    for bucket in morning.BUCKETS:
        assert b.counts[bucket] == 0


def test_stop_signal_buckets_as_halted():
    events = [_stop_event(
        ts=_iso(NOW - timedelta(hours=1)),
        scope="scoped", target_kind="ms", target_id="ms-55",
        reason_kind="build_fail", reason="build broke",
    )]
    b = morning.build_briefing(events)
    halted = b.by_bucket(morning.BUCKET_HALTED)
    assert len(halted) == 1
    assert "ms:ms-55" in halted[0].title
    assert "build_fail" in halted[0].title
    assert halted[0].detail == "build broke"


def test_stuck_signal_buckets_as_needs_attention():
    """STUCK = stop signal with reason_kind=stuck → 介入要望, not 停止."""
    events = [_stop_event(
        ts=_iso(NOW - timedelta(minutes=45)),
        scope="scoped", target_kind="session", target_id="sv-stuck",
        reason_kind="stuck", reason="idle > 30 min",
    )]
    b = morning.build_briefing(events)
    assert len(b.by_bucket(morning.BUCKET_HALTED)) == 0
    needs = b.by_bucket(morning.BUCKET_NEEDS_ATTENTION)
    assert len(needs) == 1
    assert "session:sv-stuck" in needs[0].title


def test_claim_release_completed_buckets_as_completed():
    events = [_release_event(
        ts=_iso(NOW - timedelta(minutes=10)),
        outcome="completed", reason="all tests green",
    )]
    b = morning.build_briefing(events)
    assert len(b.by_bucket(morning.BUCKET_COMPLETED)) == 1


def test_claim_release_abandoned_buckets_as_skipped():
    events = [_release_event(
        ts=_iso(NOW - timedelta(minutes=5)),
        outcome="abandoned", reason="dep halted",
    )]
    b = morning.build_briefing(events)
    assert len(b.by_bucket(morning.BUCKET_SKIPPED)) == 1


def test_mixed_events_bucket_correctly():
    events = [
        _release_event(
            ts=_iso(NOW - timedelta(hours=2)),
            outcome="completed",
        ),
        _stop_event(
            ts=_iso(NOW - timedelta(hours=1)),
            scope="global", reason_kind="approved_actions_violation",
            reason="hostile prompt",
        ),
        _stop_event(
            ts=_iso(NOW - timedelta(minutes=45)),
            scope="scoped", target_kind="session", target_id="sv-A",
            reason_kind="stuck",
        ),
        _release_event(
            ts=_iso(NOW - timedelta(minutes=5)),
            outcome="abandoned",
        ),
    ]
    b = morning.build_briefing(events)
    assert b.counts[morning.BUCKET_COMPLETED] == 1
    assert b.counts[morning.BUCKET_HALTED] == 1
    assert b.counts[morning.BUCKET_NEEDS_ATTENTION] == 1
    assert b.counts[morning.BUCKET_SKIPPED] == 1


def test_window_filters_out_old():
    since = NOW - timedelta(hours=1)
    events = [
        _release_event(
            ts=_iso(NOW - timedelta(hours=5)),  # too old
            outcome="completed",
        ),
        _release_event(
            ts=_iso(NOW - timedelta(minutes=10)),  # in window
            outcome="completed",
        ),
    ]
    b = morning.build_briefing(events, since=since)
    assert len(b.entries) == 1


def test_window_filters_out_future():
    until = NOW
    events = [
        _release_event(
            ts=_iso(NOW + timedelta(hours=1)),  # future, excluded
            outcome="completed",
        ),
        _release_event(
            ts=_iso(NOW - timedelta(minutes=10)),  # in window
            outcome="completed",
        ),
    ]
    b = morning.build_briefing(events, until=until)
    assert len(b.entries) == 1


def test_ignored_channel_skipped():
    """Events on unrelated channels (= dm, ops, etc.) must not show
    up in the briefing."""
    events = [{
        "event_id": "e1",
        "channel": "dm",
        "payload": {"text": "hello"},
        "created_at": _iso(NOW - timedelta(minutes=10)),
    }]
    assert morning.build_briefing(events).entries == []


def test_extra_entries_added_in_window():
    extra = [morning.BriefingEntry(
        bucket=morning.BUCKET_COMPLETED,
        title="commit abc1234 — feat: stop CLI",
        at=_iso(NOW - timedelta(minutes=30)),
        source="git",
        ref="abc1234",
    )]
    b = morning.build_briefing([], extra_entries=extra)
    assert b.counts[morning.BUCKET_COMPLETED] == 1
    assert b.entries[0].title.startswith("commit abc1234")


def test_extra_entries_outside_window_skipped():
    extra = [morning.BriefingEntry(
        bucket=morning.BUCKET_COMPLETED,
        title="too old",
        at=_iso(NOW - timedelta(days=2)),
    )]
    since = NOW - timedelta(hours=1)
    b = morning.build_briefing([], since=since, extra_entries=extra)
    assert b.entries == []


def test_extra_entries_invalid_bucket_skipped():
    """Defensive: a synthetic entry with a bogus bucket must not pollute
    the digest."""
    extra = [morning.BriefingEntry(
        bucket="nonsense",
        title="bad",
        at=_iso(NOW - timedelta(minutes=5)),
    )]
    b = morning.build_briefing([], extra_entries=extra)
    assert b.entries == []


def test_entries_sorted_by_at():
    events = [
        _release_event(
            ts=_iso(NOW - timedelta(minutes=30)),
            outcome="completed",
        ),
        _release_event(
            ts=_iso(NOW - timedelta(hours=2)),
            outcome="completed",
        ),
        _release_event(
            ts=_iso(NOW - timedelta(minutes=10)),
            outcome="completed",
        ),
    ]
    b = morning.build_briefing(events)
    ats = [e.at for e in b.entries]
    assert ats == sorted(ats)


def test_resume_event_not_surfaced():
    """A resume event on stop-signal channel must not show up in the
    briefing — it's housekeeping, not a morning event."""
    resume_payload = stop_signal.build_resume_payload(
        scope="global",
        issued_by_session_id="sv-A",
        issued_at=_iso(NOW - timedelta(minutes=10)),
    )
    events = [{
        "event_id": "rv-1",
        "channel": stop_signal.STOP_CHANNEL,
        "payload": resume_payload,
        "created_at": _iso(NOW - timedelta(minutes=10)),
    }]
    assert morning.build_briefing(events).entries == []


def test_garbage_event_safe():
    events = [
        None,
        "not a dict",
        {},
        {"channel": "stop-signal", "payload": {}, "created_at": _iso(NOW)},
    ]
    # Should not crash; should not produce entries.
    assert morning.build_briefing(events).entries == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# render_briefing
# ---------------------------------------------------------------------------

def test_render_empty_briefing_shows_all_buckets():
    b = morning.build_briefing([])
    rendered = morning.render_briefing(b)
    assert "完了" in rendered
    assert "停止" in rendered
    assert "Skip" in rendered
    assert "介入要望" in rendered
    assert "(none)" in rendered


def test_render_includes_titles_and_detail():
    events = [_stop_event(
        ts=_iso(NOW - timedelta(minutes=30)),
        scope="scoped", target_kind="ms", target_id="ms-55",
        reason_kind="build_fail", reason="exit 137 on build",
    )]
    b = morning.build_briefing(events)
    rendered = morning.render_briefing(b)
    assert "ms:ms-55" in rendered
    assert "exit 137" in rendered


def test_render_summary_counts():
    events = [
        _release_event(
            ts=_iso(NOW - timedelta(hours=1)),
            outcome="completed",
        ),
        _stop_event(
            ts=_iso(NOW - timedelta(minutes=30)),
            scope="global", reason_kind="manual",
        ),
    ]
    b = morning.build_briefing(events)
    rendered = morning.render_briefing(b)
    assert "summary:" in rendered
    # 1 completed, 1 halted, 0 skipped, 0 needs_attention
    assert "✓ 1" in rendered
    assert "⚠ 1" in rendered
