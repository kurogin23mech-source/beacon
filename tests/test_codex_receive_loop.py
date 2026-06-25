"""Tests for ms-93 / e-2497 Codex receive-loop primitives.

Pins the contract between the daemon (= scripts/codex-receive-loop.py)
and the inbox hook (= scripts/codex-inbox-hook.py):

- heartbeat survives transport errors without crashing the loop
- poll_inbox_once persists events addressed to us, ignores others
- a poll batch is deduped by the in-memory ``since`` watermark even
  when the server ignores the ``since`` query param
- inbox files live until the hook archives them (= no double-injection)
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import codex_receive_loop as crl


# ------------------------------------------------------------------ #
# Fake API client (one place, used by all the tests below)
# ------------------------------------------------------------------ #


class _FakeApi:
    """Minimal stand-in with controllable get/put/post outcomes."""

    def __init__(self):
        self.put_calls = []
        self.post_calls = []
        self.get_returns = []
        self.put_should_raise = False
        self.get_should_raise = False
        self.post_should_raise = False

    def get(self, path):
        if self.get_should_raise:
            raise OSError("simulated transport")
        if not self.get_returns:
            return []
        return self.get_returns.pop(0)

    def put(self, path, body):
        if self.put_should_raise:
            raise OSError("simulated transport")
        self.put_calls.append((path, body))
        return {}

    def post(self, path, body=None):
        if self.post_should_raise:
            raise OSError("simulated transport")
        self.post_calls.append((path, body))
        return {}


# ------------------------------------------------------------------ #
# Heartbeat
# ------------------------------------------------------------------ #


class TestHeartbeat:
    def test_returns_true_and_calls_put(self):
        api = _FakeApi()
        ok = crl.heartbeat_to_server(
            api,
            project_id="proj-1",
            session_id="codex-1-abc",
            actor={"machine": "m", "agent": "codex"},
        )
        assert ok is True
        assert len(api.put_calls) == 1
        path, body = api.put_calls[0]
        assert path == "/api/projects/proj-1/sessions/codex-1-abc"
        assert body["actor"]["agent"] == "codex"
        assert "last_active" in body

    def test_returns_false_on_transport_error(self):
        api = _FakeApi()
        api.put_should_raise = True
        ok = crl.heartbeat_to_server(
            api, project_id="proj-1", session_id="codex-1-abc",
            actor={"machine": "m", "agent": "codex"},
        )
        assert ok is False

    def test_body_mirrors_bus_mjs_heartbeat_shape(self):
        # Must match channel/bus-heartbeat.mjs::buildHeartbeatBody so the
        # server's poll-health computation (e-1318) treats Codex like
        # the Claude Code bridge.
        api = _FakeApi()
        crl.heartbeat_to_server(
            api, project_id="p", session_id="s",
            actor={"agent": "codex"},
            poll_interval_ms=2000,
        )
        _path, body = api.put_calls[0]
        for key in ("last_active", "last_poll_at", "poll_interval_ms", "shutdown"):
            assert key in body
        assert body["poll_interval_ms"] == 2000
        assert body["shutdown"] is False

    def test_shutdown_flag_is_propagated(self):
        api = _FakeApi()
        crl.heartbeat_to_server(
            api, project_id="p", session_id="s",
            actor={"agent": "codex"},
            shutdown=True,
        )
        _path, body = api.put_calls[0]
        assert body["shutdown"] is True


# ------------------------------------------------------------------ #
# Persist + ack
# ------------------------------------------------------------------ #


class TestPersistInboxEvent:
    def test_writes_event_to_inbox_dir(self, tmp_path):
        path = crl.persist_inbox_event(
            {"event_id": "evt-1", "payload": {"text": "hi"}},
            cwd=str(tmp_path),
        )
        assert path is not None
        assert path.is_file()
        body = json.loads(path.read_text())
        assert body["event_id"] == "evt-1"

    def test_skip_event_without_event_id(self, tmp_path):
        path = crl.persist_inbox_event({"payload": {"text": "hi"}}, cwd=str(tmp_path))
        assert path is None


class TestAckEvent:
    def test_posts_with_stage_and_sid(self):
        api = _FakeApi()
        ok = crl.ack_event(
            api, project_id="proj-1", event_id="evt-1",
            stage="delivered", recipient_session_id="codex-1-abc",
        )
        assert ok is True
        path, body = api.post_calls[0]
        assert path == "/api/projects/proj-1/bus/evt-1/ack"
        assert body == {
            "stage": "delivered",
            "recipient_session_id": "codex-1-abc",
        }

    def test_returns_false_for_bad_stage(self):
        api = _FakeApi()
        assert crl.ack_event(
            api, project_id="proj-1", event_id="evt-1",
            stage="bogus", recipient_session_id="x",
        ) is False
        assert api.post_calls == []

    def test_transport_failure_prints_stderr_warning(self, capsys):
        # Codex 2026-06-26 dogfood (= Gx0VhYhthfqneAdp4XVS) found the
        # silent ``except`` here was the false-negative source. Ensure
        # the failure is observable on stderr without changing the
        # fire-and-forget contract.
        api = _FakeApi()
        api.post_should_raise = True
        ok = crl.ack_event(
            api, project_id="proj-1", event_id="evt-99",
            stage="opened", recipient_session_id="codex-1-abc",
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "ack opened for evt-99 failed" in captured.err


# ------------------------------------------------------------------ #
# poll_inbox_once
# ------------------------------------------------------------------ #


class TestPollInboxOnce:
    def _event(self, **kw):
        base = {
            "event_id": "evt-1",
            "created_at": "2026-06-25T10:00:00Z",
            "channel": "dm",
            "sender_session_id": "other-sid",
            "payload": {"recipient_session_id": "codex-1-abc", "text": "hi"},
        }
        base.update(kw)
        return base

    def test_persists_event_addressed_to_us(self, tmp_path):
        api = _FakeApi()
        api.get_returns = [[self._event()]]
        latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
        )
        assert n == 1
        assert latest == "2026-06-25T10:00:00Z"
        assert (tmp_path / ".beacon" / "codex" / "inbox" / "evt-1.json").is_file()
        # delivered ack POSTed.
        assert any(p[0].endswith("/ack") for p in api.post_calls)

    def test_drops_self_sent(self, tmp_path):
        api = _FakeApi()
        api.get_returns = [[self._event(sender_session_id="codex-1-abc")]]
        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
        )
        assert n == 0

    def test_drops_addressed_to_other_sid(self, tmp_path):
        api = _FakeApi()
        evt = self._event()
        evt["payload"]["recipient_session_id"] = "someone-else"
        api.get_returns = [[evt]]
        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
        )
        assert n == 0

    def test_dedupes_via_since_watermark(self, tmp_path):
        # Server ignores ?since and returns the same event again — we
        # must NOT persist it twice.
        api = _FakeApi()
        api.get_returns = [
            [self._event(event_id="evt-1", created_at="2026-06-25T10:00:00Z")],
            [self._event(event_id="evt-1", created_at="2026-06-25T10:00:00Z")],
        ]
        latest1, n1 = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
        )
        assert n1 == 1
        latest2, n2 = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since=latest1, cwd=str(tmp_path),
        )
        assert n2 == 0  # filtered by watermark

    def test_empty_response_returns_zero(self, tmp_path):
        api = _FakeApi()
        api.get_returns = [[]]
        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
        )
        assert n == 0

    def test_get_transport_error_does_not_raise(self, tmp_path):
        api = _FakeApi()
        api.get_should_raise = True
        latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="prev", cwd=str(tmp_path),
        )
        assert (latest, n) == ("prev", 0)


# ------------------------------------------------------------------ #
# Hook contract: list + archive
# ------------------------------------------------------------------ #


class TestInboxFileLifecycle:
    def test_list_inbox_events_returns_persisted_entries(self, tmp_path):
        crl.persist_inbox_event(
            {"event_id": "evt-1", "payload": {"text": "a"}},
            cwd=str(tmp_path),
        )
        crl.persist_inbox_event(
            {"event_id": "evt-2", "payload": {"text": "b"}},
            cwd=str(tmp_path),
        )
        entries = crl.list_inbox_events(cwd=str(tmp_path))
        assert len(entries) == 2
        ids = {e["event"]["event_id"] for e in entries}
        assert ids == {"evt-1", "evt-2"}

    def test_archive_moves_file_into_read_subdir(self, tmp_path):
        crl.persist_inbox_event(
            {"event_id": "evt-1", "payload": {"text": "x"}},
            cwd=str(tmp_path),
        )
        entries = crl.list_inbox_events(cwd=str(tmp_path))
        archived = crl.archive_inbox_event(entries[0]["path"], cwd=str(tmp_path))
        assert archived is not None
        assert archived.parent.name == ".read"
        # No longer visible to a fresh list.
        assert crl.list_inbox_events(cwd=str(tmp_path)) == []

    def test_archive_missing_file_returns_none(self, tmp_path):
        result = crl.archive_inbox_event(
            str(tmp_path / "no-such-file.json"), cwd=str(tmp_path),
        )
        assert result is None

    def test_archive_collision_is_idempotent(self, tmp_path):
        # Codex 2026-06-26 blocker #4: daemon restart re-persisted the
        # same event into the top-level inbox; the next hook archive
        # collided with a stale ``.read/<event_id>.json`` and the file
        # was lost. The new contract suffixes the conflicting dest so
        # neither version is silently overwritten.
        crl.persist_inbox_event(
            {"event_id": "evt-1", "payload": {"text": "v1"}},
            cwd=str(tmp_path),
        )
        first_entry = crl.list_inbox_events(cwd=str(tmp_path))[0]
        crl.archive_inbox_event(first_entry["path"], cwd=str(tmp_path))

        # Simulate daemon restart: same event_id reappears.
        crl.persist_inbox_event(
            {"event_id": "evt-1", "payload": {"text": "v2"}},
            cwd=str(tmp_path),
        )
        second_entry = crl.list_inbox_events(cwd=str(tmp_path))[0]
        archived = crl.archive_inbox_event(second_entry["path"], cwd=str(tmp_path))
        assert archived is not None
        # No surviving top-level file (= no double inject next hook).
        assert crl.list_inbox_events(cwd=str(tmp_path)) == []
        # Both versions preserved in .read/.
        read_dir = tmp_path / ".beacon" / "codex" / "inbox" / ".read"
        assert sum(1 for _ in read_dir.iterdir()) == 2


class TestArchiveFiresOpenedAck:
    """Codex 2026-06-26 silent drift #3 fix — opened ack on hook archive."""

    def test_archive_with_api_posts_opened(self, tmp_path):
        api = _FakeApi()
        crl.persist_inbox_event(
            {"event_id": "evt-99", "payload": {"text": "hello"}},
            cwd=str(tmp_path),
        )
        entry = crl.list_inbox_events(cwd=str(tmp_path))[0]
        crl.archive_inbox_event(
            entry["path"], cwd=str(tmp_path),
            api=api, project_id="proj-1",
            recipient_session_id="codex-1-abc",
        )
        opened_posts = [
            (p, b) for (p, b) in api.post_calls
            if b.get("stage") == "opened"
        ]
        assert len(opened_posts) == 1
        path, body = opened_posts[0]
        assert path == "/api/projects/proj-1/bus/evt-99/ack"
        assert body["recipient_session_id"] == "codex-1-abc"

    def test_archive_without_api_skips_ack(self, tmp_path):
        crl.persist_inbox_event(
            {"event_id": "evt-99", "payload": {"text": "hello"}},
            cwd=str(tmp_path),
        )
        entry = crl.list_inbox_events(cwd=str(tmp_path))[0]
        # No api kwarg → no network call; archive still succeeds.
        path = crl.archive_inbox_event(entry["path"], cwd=str(tmp_path))
        assert path is not None


class TestPollInboxOnceDriftFixes:
    """The three silent drifts from Codex 2026-06-26 dogfood."""

    def test_does_not_persist_dm_without_recipient(self, tmp_path):
        # Codex blocker #1 root cause: dm channel + no recipient slipped
        # through as a "broadcast" in the pre-protocol implementation.
        api = _FakeApi()
        api.get_returns = [[{
            "event_id": "evt-bc",
            "created_at": "2026-06-26T01:00:00Z",
            "channel": "dm",
            "sender_session_id": "other",
            "payload": {"text": "broadcast"},  # no recipient_session_id
        }]]
        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
        )
        assert n == 0
        # delivered ack still fires (e-1348 rule), but not opened.
        assert any(p[0].endswith("/ack") for p in api.post_calls)

    def test_does_not_persist_channel_outside_allowlist(self, tmp_path):
        api = _FakeApi()
        api.get_returns = [[{
            "event_id": "evt-test",
            "created_at": "2026-06-26T01:00:00Z",
            "channel": "test-random",
            "sender_session_id": "other",
            "payload": {},
        }]]
        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
            allowed_channels=("dm",),
        )
        assert n == 0
