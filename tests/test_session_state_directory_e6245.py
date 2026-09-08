"""ms-159 / e-6245 — server projects `state` + `state_since` onto directory rows.

`_stamp_session_liveness` is the single choke point every `bus directory` row
passes through. These tests pin that it now stamps the canonical work-unit
`state` (via bus_liveness.derive_state) and `state_since` on each row, covering:

  * a live session's fresh declaration → that state, marker state_since preserved;
  * a long-waiting awaiting_human (live, state_since hours old) stays
    awaiting_human with its old state_since (so the attention面 sorts it to the
    top) — the MS-critical guarantee;
  * a live session with NO declaration → unknown (方針1: don't assume running);
  * a not-live session with a stale declaration → unknown (判断4 固着 backstop);
  * a not-live session with no declaration → terminated.
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import bus_liveness  # noqa: E402


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class TestStampState:
    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        import app as app_module
        # Redis unavailable ⇒ ws_live=None ⇒ live decided by poll heartbeat.
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        # No backlog scan noise for live sessions.
        monkeypatch.setattr(app_module.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(app_module.db, "list_bus_events", lambda pid, **k: [])
        return app_module

    def _live_row(self, now, **extra):
        row = {"session_id": "sv-x",
               "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
               "last_active": _iso(now - datetime.timedelta(seconds=3)),
               "poll_interval_ms": 5000}
        row.update(extra)
        return row

    def _dead_row(self, now, **extra):
        row = {"session_id": "sv-dead",
               "last_poll_at": _iso(now - datetime.timedelta(hours=1)),
               "poll_interval_ms": 5000}
        row.update(extra)
        return row

    def test_live_fresh_awaiting_human(self, _app):
        now = _now()
        since = _iso(now - datetime.timedelta(seconds=30))
        row = self._live_row(now, declared_state="awaiting_human",
                             declared_at=_iso(now - datetime.timedelta(seconds=30)),
                             state_since=since)
        _app._stamp_session_liveness(row, "proj", now)
        assert row["live"] is True
        assert row["state"] == bus_liveness.STATE_AWAITING_HUMAN
        assert row["state_since"] == since

    def test_long_waiting_awaiting_human_keeps_old_state_since(self, _app):
        # The MS-critical case: live, declared awaiting_human hours ago, no hook
        # has fired since (parked on the human). Must stay awaiting_human with the
        # OLD state_since so `beacon attention` sorts it to the top.
        now = _now()
        hours_ago = _iso(now - datetime.timedelta(hours=3))
        row = self._live_row(now, declared_state="awaiting_human",
                             declared_at=hours_ago, state_since=hours_ago)
        _app._stamp_session_liveness(row, "proj", now)
        assert row["state"] == bus_liveness.STATE_AWAITING_HUMAN
        assert row["state_since"] == hours_ago

    def test_live_no_declaration_is_unknown(self, _app):
        now = _now()
        row = self._live_row(now)  # no declared_state
        _app._stamp_session_liveness(row, "proj", now)
        assert row["live"] is True
        assert row["state"] == bus_liveness.STATE_UNKNOWN
        # state_since falls back to a known timestamp (not empty).
        assert row["state_since"]

    def test_not_live_stale_declaration_is_unknown(self, _app):
        # 判断4 固着 backstop: transport gone + stale awaiting_human ⇒ unknown.
        now = _now()
        old = _iso(now - datetime.timedelta(hours=1))
        row = self._dead_row(now, declared_state="awaiting_human",
                             declared_at=old, state_since=old)
        _app._stamp_session_liveness(row, "proj", now)
        assert row["live"] is False
        assert row["state"] == bus_liveness.STATE_UNKNOWN

    def test_not_live_no_declaration_is_terminated(self, _app):
        now = _now()
        row = self._dead_row(now)
        _app._stamp_session_liveness(row, "proj", now)
        assert row["live"] is False
        assert row["state"] == bus_liveness.STATE_TERMINATED

    def test_declared_terminated_stays_terminated(self, _app):
        now = _now()
        row = self._dead_row(now, declared_state="terminated",
                             declared_at=_iso(now - datetime.timedelta(seconds=5)),
                             state_since=_iso(now - datetime.timedelta(seconds=5)))
        _app._stamp_session_liveness(row, "proj", now)
        assert row["state"] == bus_liveness.STATE_TERMINATED
