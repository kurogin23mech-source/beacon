"""Tests for ms-93 / e-2502 shared bus protocol (lib/bus_protocol.py).

Pins the canonical contract that Claude Code's channel/bus.mjs and
Codex's lib/codex_receive_loop.py both follow. Three behaviours
specifically catch silent drifts the 2026-06-26 Codex dogfood
uncovered:

1. DM channel + missing recipient = drop (= e-1209, bus.mjs filter 2b)
2. Channel outside the allowlist = drop (= bus.mjs filter 3)
3. ``opened`` ack stage is a real protocol stage (not just ``delivered``)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import bus_protocol as bp


# ------------------------------------------------------------------ #
# Heartbeat
# ------------------------------------------------------------------ #


class TestHeartbeatBody:
    def test_shape_matches_bus_heartbeat_mjs(self):
        body = bp.heartbeat_body("2026-06-26T00:00:00Z", poll_interval_ms=2000)
        assert set(body) == {"last_active", "last_poll_at", "poll_interval_ms", "shutdown"}

    def test_shutdown_flag_propagates(self):
        body = bp.heartbeat_body("now", poll_interval_ms=1000, shutdown=True)
        assert body["shutdown"] is True

    def test_default_shutdown_is_false(self):
        body = bp.heartbeat_body("now", poll_interval_ms=1000)
        assert body["shutdown"] is False

    def test_transport_omitted_by_default(self):
        # ms-145 / e-5378: back-compat — no transport arg → field absent, so the
        # server merge doesn't clobber anything and old readers are unaffected.
        body = bp.heartbeat_body("now", poll_interval_ms=1000)
        assert "transport" not in body

    def test_transport_included_when_provided(self):
        # ms-145 / e-5378: Codex loop self-reports poll-only so the directory can
        # explain why the session is on frequent polling (by design, not broken WS).
        body = bp.heartbeat_body(
            "now", poll_interval_ms=2000,
            transport={"ws_state": "poll-only", "effective_poll_ms": 2000},
        )
        assert body["transport"] == {"ws_state": "poll-only", "effective_poll_ms": 2000}


class TestHeartbeatPath:
    def test_includes_project_and_session(self):
        assert bp.heartbeat_path("proj-1", "sid-abc") == "/api/projects/proj-1/sessions/sid-abc"


# ------------------------------------------------------------------ #
# Initial watermark
# ------------------------------------------------------------------ #


class TestInitialWatermark:
    def test_returns_iso8601_now(self):
        ts = bp.initial_watermark_now()
        # Format check — full parse is fine if the string fits.
        from datetime import datetime, timezone
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert parsed.tzinfo is not None
        delta = (datetime.now(timezone.utc) - parsed).total_seconds()
        assert 0 <= delta <= 5


# ------------------------------------------------------------------ #
# Poll URL builder
# ------------------------------------------------------------------ #


class TestPollUnreadPath:
    def test_includes_recipient_and_since(self):
        url = bp.poll_unread_path("proj-1", "sid-abc", "2026-06-26T00:00:00Z")
        assert "/api/projects/proj-1/bus/unread" in url
        assert "recipient_id=sid-abc" in url
        assert "since=2026-06-26T00%3A00%3A00Z" in url or "since=2026-06-26T00:00:00Z" in url

    def test_empty_since_renders_empty_param(self):
        url = bp.poll_unread_path("proj-1", "sid-abc", "")
        assert url.endswith("&since=")


# ------------------------------------------------------------------ #
# Filter chain — the core drift fix
# ------------------------------------------------------------------ #


def _config(sid="sid-abc", watermark="", channels=bp.DEFAULT_ALLOWED_CHANNELS):
    return bp.FilterConfig(
        our_session_id=sid,
        allowed_channels=tuple(channels),
        watermark=watermark,
    )


def _event(**kw):
    base = {
        "event_id": "evt-1",
        "created_at": "2026-06-26T01:00:00Z",
        "channel": "dm",
        "sender_session_id": "other-sid",
        "payload": {"recipient_session_id": "sid-abc", "text": "hi"},
    }
    base.update(kw)
    return base


class TestFilterEvent:
    def test_keeps_dm_addressed_to_us(self):
        assert bp.filter_event(_event(), _config()) == bp.FILTER_KEEP

    def test_drops_self_sent(self):
        e = _event(sender_session_id="sid-abc")
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_SELF

    def test_drops_recipient_mismatch(self):
        e = _event()
        e["payload"]["recipient_session_id"] = "someone-else"
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_RECIPIENT_MISMATCH

    def test_drops_dm_without_recipient_e1209(self):
        # Codex 2026-06-26 silent drift #1: missing this filter let
        # broadcast traffic (= operation-trigger etc.) into the DM inbox.
        e = _event()
        e["payload"] = {"text": "missing recipient"}
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_DM_UNADDRESSED

    def test_drops_non_dm_channel_outside_allowlist(self):
        # Codex 2026-06-26 silent drift #2: no allowlist filter let
        # test-* channels through.
        e = _event(channel="test-random", payload={})
        assert bp.filter_event(e, _config(channels=("dm",))) == bp.FILTER_DROP_CHANNEL

    def test_keeps_non_dm_channel_in_allowlist(self):
        e = _event(channel="operation-trigger", payload={})
        verdict = bp.filter_event(e, _config(channels=("dm", "operation-trigger")))
        assert verdict == bp.FILTER_KEEP

    def test_keeps_stop_signal_even_outside_allowlist(self):
        # ms-160 e-5800: the remote-STOP kill-switch must reach the receive loop
        # regardless of the opt-in channel allowlist — a session cannot opt OUT
        # of being stoppable. Without this the Codex filter dropped stop-signal
        # (channel-not-allowed) and Codex could not be stopped at all.
        e = _event(channel="stop-signal", payload={})
        assert bp.filter_event(e, _config(channels=("dm",))) == bp.FILTER_KEEP

    def test_stop_signal_addressed_to_another_session_still_drops(self):
        # The allowlist exemption must NOT bypass the recipient filter: a stop
        # aimed at someone else is not ours to act on.
        e = _event(channel="stop-signal",
                   payload={"recipient_session_id": "someone-else"})
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_RECIPIENT_MISMATCH

    def test_stop_signal_from_self_still_drops(self):
        e = _event(channel="stop-signal", sender_session_id="sid-abc", payload={})
        assert bp.filter_event(e, _config()) == bp.FILTER_DROP_SELF

    def test_drops_event_older_than_watermark(self):
        e = _event(created_at="2026-06-25T00:00:00Z")
        verdict = bp.filter_event(e, _config(watermark="2026-06-26T00:00:00Z"))
        assert verdict == bp.FILTER_DROP_WATERMARK

    def test_malformed_event_is_dropped_safely(self):
        assert bp.filter_event(None, _config()) == bp.FILTER_DROP_RECIPIENT_MISMATCH


# ------------------------------------------------------------------ #
# Ack
# ------------------------------------------------------------------ #


class TestAck:
    def test_path_includes_project_and_event(self):
        assert bp.ack_path("proj-1", "evt-1") == "/api/projects/proj-1/bus/evt-1/ack"

    def test_body_delivered(self):
        body = bp.ack_body(bp.ACK_STAGE_DELIVERED, "sid-abc")
        assert body == {"stage": "delivered", "recipient_session_id": "sid-abc"}

    def test_body_opened_is_a_real_stage(self):
        # Codex 2026-06-26 silent drift #3: only ``delivered`` was wired.
        body = bp.ack_body(bp.ACK_STAGE_OPENED, "sid-abc")
        assert body == {"stage": "opened", "recipient_session_id": "sid-abc"}

    def test_body_rejects_unknown_stage(self):
        with pytest.raises(ValueError):
            bp.ack_body("bogus", "sid-abc")
