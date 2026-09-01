"""ms-165 / e-5965 — "live but not draining" send-side advisory + directory
attentiveness signal.

Confirmed 2026-09-01 (VPS): a recipient can be LIVE (bridge polling, last_poll_at
fresh) yet never CONSUME its inbox — DMs addressed to it accumulate with the
`opened` receipt never set (the a695553f wedge / pre-e-5964 deadlock). The sender
was fooled into "sent✓ delivered✗" because the live-check only proved polling.
These tests pin:
  1. _recipient_backlog_advisory (lib/commands_shared) — warns when a live
     recipient has stale unopened events addressed to it; silent otherwise;
     fail-open on any error / opt-out.
  2. _heartbeat_is_fresh + the `attentive` directory field (server/app) — an
     informational "someone is driving this session" signal that does NOT alter
     the `live` judgment (so an idle-but-healthy fork stays live: no regression).
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import commands_shared  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class _FakeClient:
    def __init__(self, events):
        self._events = events
        self.calls = []

    def list_unread_bus_events(self, project_id, recipient_id, *, since="", limit=100):
        self.calls.append((project_id, recipient_id, since, limit))
        return list(self._events)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("BEACON_BUS_NO_BACKLOG_CHECK", raising=False)


def _stub_client(monkeypatch, events):
    client = _FakeClient(events)
    monkeypatch.setattr(commands_shared, "_get_api_client", lambda: (client, {}))
    return client


# --- _recipient_backlog_advisory -------------------------------------------

def test_advisory_fires_on_stale_unopened_backlog(monkeypatch):
    now = _now()
    old = _iso(now - datetime.timedelta(seconds=600))  # 10 min old, unopened
    _stub_client(monkeypatch, [
        {"event_id": "e1", "created_at": old, "opened_at": "",
         "payload": {"recipient_session_id": "R1"}},
    ])
    # `now` injected for a deterministic staleness boundary (no wall-clock race).
    msg = commands_shared._recipient_backlog_advisory("R1", "proj", now=now)
    assert msg is not None
    assert "not draining" in msg
    assert "delivered✗" in msg
    assert "/mcp" in msg  # actionable recovery hint (AX: no pre-send event_id)
    assert "bus status" not in msg  # the pre-send event_id hint was removed


def test_advisory_silent_when_events_are_opened(monkeypatch):
    now = _now()
    old = _iso(now - datetime.timedelta(seconds=600))
    _stub_client(monkeypatch, [
        {"event_id": "e1", "created_at": old, "opened_at": _iso(now),
         "payload": {"recipient_session_id": "R1"}},
    ])
    assert commands_shared._recipient_backlog_advisory("R1", "proj", now=now) is None


def test_advisory_silent_when_backlog_is_recent(monkeypatch):
    # An event that just arrived is in-flight, not a wedge — no warning.
    now = _now()
    fresh = _iso(now - datetime.timedelta(seconds=5))
    _stub_client(monkeypatch, [
        {"event_id": "e1", "created_at": fresh, "opened_at": "",
         "payload": {"recipient_session_id": "R1"}},
    ])
    assert commands_shared._recipient_backlog_advisory("R1", "proj", now=now) is None


def test_advisory_silent_when_no_events(monkeypatch):
    _stub_client(monkeypatch, [])
    assert commands_shared._recipient_backlog_advisory("R1", "proj") is None


def test_advisory_opt_out_env(monkeypatch):
    monkeypatch.setenv("BEACON_BUS_NO_BACKLOG_CHECK", "1")
    called = _stub_client(monkeypatch, [
        {"event_id": "e1", "created_at": _iso(_now() - datetime.timedelta(hours=1)),
         "opened_at": "", "payload": {"recipient_session_id": "R1"}}])
    assert commands_shared._recipient_backlog_advisory("R1", "proj") is None
    assert called.calls == []  # short-circuits before any network


def test_advisory_fail_open_when_client_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no client / auth exit")
    monkeypatch.setattr(commands_shared, "_get_api_client", _boom)
    # Must not raise — send proceeds unchanged.
    assert commands_shared._recipient_backlog_advisory("R1", "proj") is None


def test_advisory_silent_when_no_project(monkeypatch):
    called = _stub_client(monkeypatch, [{"event_id": "e1"}])
    assert commands_shared._recipient_backlog_advisory("R1", "") is None
    assert called.calls == []


# --- directory `attentive` field -------------------------------------------

def test_heartbeat_freshness_and_attentive_field(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                        lambda pid, sid: None)
    now = _now()

    def stamp(last_poll_off, last_hb_off):
        s = {"session_id": "sv-x",
             "last_poll_at": _iso(now - datetime.timedelta(seconds=last_poll_off)),
             "last_active": _iso(now - datetime.timedelta(seconds=last_poll_off)),
             "poll_interval_ms": 5000}
        if last_hb_off is not None:
            s["last_heartbeat_at"] = _iso(now - datetime.timedelta(seconds=last_hb_off))
        app_module._stamp_session_liveness(s, "proj", now)
        return s

    # Fresh poll + fresh heartbeat → live AND heartbeat_fresh.
    s = stamp(3, 10)
    assert s["live"] is True and s["heartbeat_fresh"] is True

    # Fresh poll (live) but stale heartbeat (idle fork) → live, NOT
    # heartbeat_fresh. This is the no-regression guarantee: an idle fork stays
    # live (still receives) even when nobody is driving it.
    s = stamp(3, 4000)
    assert s["live"] is True and s["heartbeat_fresh"] is False

    # No heartbeat stamp → heartbeat_fresh is None (unknown), live still holds.
    s = stamp(3, None)
    assert s["live"] is True and s["heartbeat_fresh"] is None


def test_heartbeat_is_fresh_helper_handles_malformed(monkeypatch):
    import app as app_module
    now = _now()
    assert app_module._heartbeat_is_fresh("", now) is None
    assert app_module._heartbeat_is_fresh("not-a-date", now) is None
    assert app_module._heartbeat_is_fresh(_iso(now), now) is True
