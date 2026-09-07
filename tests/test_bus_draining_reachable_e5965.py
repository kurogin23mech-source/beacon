"""ms-165 / e-5965 — the *progress* liveness dimension: draining / reachable.

A recipient can be LIVE (bridge polling, ``last_poll_at`` fresh) yet never
CONSUME its inbox — DMs pile up unread and the sender is fooled into "sent✓
delivered✗". These tests pin the four SPEC 方針 guarantees:

  1. draining derivation is False when unread backlog is stale (pure helper +
     the server ``_stamp_session_liveness`` end).
  2. a send to a WEDGED (live-but-not-draining) recipient returns loud
     structured flags (``recipient_wedged`` / ``delivery_uncertain``).
  3. a send to a NORMAL (live + draining) recipient is BYTE-UNCHANGED — no new
     keys (the negative-regression guard).
  4. an idle fork stays LIVE and reachable (the ``live`` union is unchanged, so
     the picker never drops it).
"""

from __future__ import annotations

import copy
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


# ===========================================================================
# 1. Pure derivation (SPEC 方針 a) — the "stale unread ⇒ not draining" rule.
# ===========================================================================

class TestDeriveDraining:
    def test_stale_unread_is_not_draining(self):
        now = _now()
        old = _iso(now - datetime.timedelta(seconds=600))  # older than 300s window
        assert bus_liveness.derive_draining(old, now, 300) is False

    def test_recent_unread_is_still_draining(self):
        now = _now()
        fresh = _iso(now - datetime.timedelta(seconds=5))  # in-flight, not a wedge
        assert bus_liveness.derive_draining(fresh, now, 300) is True

    def test_no_backlog_is_draining(self):
        assert bus_liveness.derive_draining("", _now(), 300) is True

    def test_unparseable_is_unknown(self):
        assert bus_liveness.derive_draining("not-a-date", _now(), 300) is None

    def test_reachable_only_false_on_definitive_wedge(self):
        assert bus_liveness.is_reachable(True, True) is True
        assert bus_liveness.is_reachable(True, None) is True   # unknown ⇒ keep
        assert bus_liveness.is_reachable(True, False) is False  # wedge ⇒ drop
        assert bus_liveness.is_reachable(False, True) is False  # not live ⇒ drop

    def test_classify_send_delivery_three_tiers(self):
        assert bus_liveness.classify_send_delivery(True, True) == bus_liveness.SEND_NORMAL
        assert bus_liveness.classify_send_delivery(True, None) == bus_liveness.SEND_NORMAL
        assert bus_liveness.classify_send_delivery(True, False) == bus_liveness.SEND_WEDGED
        assert bus_liveness.classify_send_delivery(False, False) == bus_liveness.SEND_NOT_LIVE
        assert bus_liveness.classify_send_delivery(False, True) == bus_liveness.SEND_NOT_LIVE


# ===========================================================================
# 4. Directory stamping — idle fork stays live (picker no-regression) + the
#    server end of the draining derivation (1).
# ===========================================================================

class TestStampReachability:
    @pytest.fixture(autouse=True)
    def _app(self, monkeypatch):
        import app as app_module
        # Redis unavailable ⇒ ws_live=None ⇒ live decided by poll heartbeat.
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        return app_module

    def _live_session(self, now):
        return {"session_id": "sv-fork",
                "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
                "last_active": _iso(now - datetime.timedelta(seconds=3)),
                "poll_interval_ms": 5000}

    def test_idle_fork_with_no_backlog_stays_live_and_reachable(self, _app, monkeypatch):
        now = _now()
        # No events addressed to it (idle fork, nobody sent anything).
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        # live union UNCHANGED (SPEC 方針 c): a healthy poll keeps it live.
        assert s["live"] is True
        assert s["draining"] is True     # no backlog ⇒ keeping up
        assert s["reachable"] is True    # live AND not-False ⇒ reachable

    def test_live_but_stale_backlog_is_wedged_not_reachable(self, _app, monkeypatch):
        now = _now()
        old = _iso(now - datetime.timedelta(seconds=600))
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        # An event addressed to sv-fork, unopened, older than the window.
        monkeypatch.setattr(_app.db, "list_bus_events", lambda pid, **k: [
            {"event_id": "old1", "channel": "dm", "sender_session_id": "sv-other",
             "created_at": old, "payload": {"recipient_session_id": "sv-fork"}},
        ])
        s = self._live_session(now)
        _app._stamp_session_liveness(s, "proj", now)
        assert s["live"] is True          # still LIVE (transport intact)
        assert s["draining"] is False     # but not consuming (wedged)
        assert s["reachable"] is False    # ⇒ not reachable for the strict send

    def test_not_live_skips_scan_and_is_unreachable(self, _app, monkeypatch):
        now = _now()
        calls = {"n": 0}

        def _count_list(pid, **k):
            calls["n"] += 1
            return []
        monkeypatch.setattr(_app.db, "get_bus_cursor", lambda pid, rid: {})
        monkeypatch.setattr(_app.db, "list_bus_events", _count_list)
        # Stale poll ⇒ not live.
        s = {"session_id": "sv-dead",
             "last_poll_at": _iso(now - datetime.timedelta(hours=1)),
             "poll_interval_ms": 5000}
        _app._stamp_session_liveness(s, "proj", now)
        assert s["live"] is False
        assert s["draining"] is None       # skipped (cost bound)
        assert s["reachable"] is False
        assert calls["n"] == 0             # no store scan for a not-live session


# ===========================================================================
# 2 + 3. Send path (SPEC 方針 b) — graded response through POST /bus.
# ===========================================================================

PROJECT_ID = "test-draining-e5965"

_PROJECT_DOC = {
    "name": "test", "milestones": [], "owner": "uid-alice",
    "owner_email": "alice@example.com",
    "members": [
        {"user_id": "uid-alice", "email": "alice@example.com", "role": "owner"},
    ],
}


class _FakeVerify:
    rejection_reason = None
    effective_tier = "T1"
    steps: dict = {}

    def to_audit_dict(self):
        return {}


class TestSendPathGraded:
    @pytest.fixture
    def wired(self, monkeypatch):
        import app as app_module
        import store_router as db_module
        import firestore_client

        now = _now()
        # Two same-user sessions (sender + recipient) so the cross-user consent
        # gate is carved out — this isolates the ms-165 reachability logic.
        sessions = [
            {"session_id": "sv-alice", "user_id": "uid-alice",
             "actor": {"email": "alice@example.com"}},
            {"session_id": "sv-rehab", "user_id": "uid-alice",
             "actor": {"email": "alice@example.com"},
             "last_poll_at": _iso(now - datetime.timedelta(seconds=3)),
             "poll_interval_ms": 5000},
        ]
        # Per-test backlog the draining scan sees (default: none ⇒ draining).
        backlog = {"events": []}

        def _list_bus_events(pid, **k):
            return copy.deepcopy(backlog["events"])

        seq = [0]

        def _append(pid, data):
            seq[0] += 1
            return f"ev-{seq[0]:06d}"

        for name, ref in (
            ("append_bus_event", _append),
            ("list_bus_events", _list_bus_events),
            ("list_sessions", lambda pid: copy.deepcopy(sessions)),
            ("get_project", lambda pid: copy.deepcopy(_PROJECT_DOC)),
            ("save_project", lambda pid, data: None),
            ("list_projects", lambda: []),
            ("append_bus_audit", lambda pid, rec: "audit-1"),
            ("append_decision_event", lambda *a, **k: "dec-1"),
            ("list_treks", lambda *a, **k: []),
            ("get_bus_cursor", lambda pid, rid: {}),
            ("get_or_create_user", lambda *a, **k: None),
        ):
            monkeypatch.setattr(firestore_client, name, ref, raising=False)
            monkeypatch.setattr(db_module, name, ref, raising=False)

        monkeypatch.setattr(app_module.envelope_mod, "verify",
                            lambda *a, **k: _FakeVerify())
        monkeypatch.setattr(app_module.envelope_mod, "validate_t5_payload",
                            lambda p: None)
        monkeypatch.setattr(app_module.envelope_mod, "decide_delivery",
                            lambda **k: "propose-to-ai")
        monkeypatch.setattr(app_module.redis_client, "ws_session_live",
                            lambda pid, sid: None)
        monkeypatch.setattr(app_module, "_start_watcher", lambda pid: None)
        monkeypatch.setattr(app_module, "_stop_watcher", lambda pid: None)
        monkeypatch.setattr(app_module, "_auth_enabled", False)

        async def _noop_fanout(pid, event):
            return None
        monkeypatch.setattr(app_module, "_fanout_bus_event", _noop_fanout)
        return app_module, backlog

    def _post(self, app_module):
        from fastapi.testclient import TestClient
        body = {
            "channel": "dm", "sender_session_id": "sv-alice",
            "payload": {"text": "hi", "recipient_session_id": "sv-rehab"},
            "envelope": {"tier": "T1", "actions_authorized": []},
        }
        client = TestClient(app_module.app)
        return client.post(f"/api/projects/{PROJECT_ID}/bus", json=body)

    def test_send_to_wedged_recipient_flags_delivery_uncertain(self, wired):
        app_module, backlog = wired
        # Recipient has a stale unopened event addressed to it ⇒ wedged.
        backlog["events"] = [
            {"event_id": "old1", "channel": "dm", "sender_session_id": "sv-x",
             "created_at": _iso(_now() - datetime.timedelta(seconds=600)),
             "payload": {"recipient_session_id": "sv-rehab"}},
        ]
        resp = self._post(app_module)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out.get("recipient_wedged") is True
        assert out.get("delivery_uncertain") is True

    def test_send_to_normal_recipient_is_byte_unchanged(self, wired):
        app_module, backlog = wired
        backlog["events"] = []  # no backlog ⇒ draining ⇒ normal
        resp = self._post(app_module)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        # Negative regression: the healthy common path grows NO new keys.
        assert "recipient_wedged" not in out
        assert "delivery_uncertain" not in out
