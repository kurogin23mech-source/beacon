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

    def test_stamps_top_level_agent_kind_from_actor(self):
        # e-3091 follow-up: the session doc's top-level ``agent.kind`` must be
        # set (mirroring bus.mjs cold-start) so identity_resolve.agent_kind_of
        # returns "codex" and resolve_stable_identity(agent_kind="codex")
        # matches the Codex row. Derived from actor.agent_kind (the daemon
        # always passes it).
        api = _FakeApi()
        crl.heartbeat_to_server(
            api, project_id="p", session_id="s",
            actor={"machine": "m", "agent": "codex", "agent_kind": "codex"},
        )
        _path, body = api.put_calls[0]
        assert body["agent"] == {"kind": "codex"}

    def test_no_top_level_agent_when_actor_lacks_agent_kind(self):
        # actor.agent (a machine label) alone must NOT populate the structural
        # top-level agent.kind — only the explicit agent_kind field does.
        api = _FakeApi()
        crl.heartbeat_to_server(
            api, project_id="p", session_id="s",
            actor={"machine": "m", "agent": "codex"},
        )
        _path, body = api.put_calls[0]
        assert "agent" not in body


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

    def test_on_kept_event_fires_for_persisted_events_only(self, tmp_path):
        """ms-93 / e-2519 app-server wiring: callback only fires for kept
        events (= post-filter, post-persist). Self-sent / wrong-recipient
        / watermark-suppressed events must NOT trigger the autonomous
        push path because they would have been silently injected into
        the long-lived Codex turn worker."""
        api = _FakeApi()
        kept_event = self._event(event_id="evt-keep")
        self_sent = self._event(
            event_id="evt-self", sender_session_id="codex-1-abc",
        )
        wrong_rcpt = self._event(event_id="evt-other")
        wrong_rcpt["payload"]["recipient_session_id"] = "someone-else"
        api.get_returns = [[kept_event, self_sent, wrong_rcpt]]
        seen = []
        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
            on_kept_event=lambda evt: seen.append(evt.get("event_id")),
        )
        assert n == 1
        assert seen == ["evt-keep"]

    def test_on_kept_event_exception_does_not_break_persistence(self, tmp_path):
        """Pull-on-prompt path is authoritative; an autonomous-path
        callback exception must not roll back the inbox file or skip
        the rest of the events."""
        api = _FakeApi()
        evt_a = self._event(event_id="evt-a", created_at="2026-06-25T10:00:00Z")
        evt_b = self._event(event_id="evt-b", created_at="2026-06-25T10:01:00Z")
        api.get_returns = [[evt_a, evt_b]]

        def _raise_on_a(evt):
            if evt.get("event_id") == "evt-a":
                raise RuntimeError("simulated app-server hiccup")

        _latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
            on_kept_event=_raise_on_a,
        )
        # Both events still persisted (= callback failures swallowed).
        assert n == 2
        assert (tmp_path / ".beacon" / "codex" / "inbox" / "evt-a.json").is_file()
        assert (tmp_path / ".beacon" / "codex" / "inbox" / "evt-b.json").is_file()

    def test_app_server_mode_can_skip_hook_inbox_persistence(self, tmp_path):
        """App-server push mode must not race UserPromptSubmit hook archiving.

        When ``persist_kept=False`` the kept event still reaches the callback,
        but no top-level inbox file is created for the hook to read/archive.
        """
        api = _FakeApi()
        api.get_returns = [[self._event(event_id="evt-push")]]
        seen = []
        latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
            on_kept_event=lambda evt: seen.append(evt.get("event_id")),
            persist_kept=False,
        )
        assert latest == "2026-06-25T10:00:00Z"
        assert n == 0
        assert seen == ["evt-push"]
        assert crl.list_inbox_events(cwd=str(tmp_path)) == []

    def test_app_server_failure_persists_hook_fallback(self, tmp_path):
        """A failed autonomous dispatch must not consume the only DM copy."""
        api = _FakeApi()
        api.get_returns = [[self._event(event_id="evt-fallback")]]

        def _fail(_evt):
            raise ConnectionError("stale app-server websocket")

        latest, n = crl.poll_inbox_once(
            api, project_id="proj-1", session_id="codex-1-abc",
            since="", cwd=str(tmp_path),
            on_kept_event=_fail,
            persist_kept=False,
        )

        assert latest == "2026-06-25T10:00:00Z"
        assert n == 1
        inbox = crl.list_inbox_events(cwd=str(tmp_path))
        assert [row["event"]["event_id"] for row in inbox] == ["evt-fallback"]

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


# ------------------------------------------------------------------ #
# should_persist_kept — inbox fallback vs blackhole (= ms-93 / e-3156)
# ------------------------------------------------------------------ #


class TestShouldPersistKept:
    """The inbox fallback must survive UNLESS the app-server is present AND
    armed. Regression guard for the non-armed app-server DM blackhole where a
    kept DM was dispatched to a silent background turn and never surfaced to
    the human (2026-07-10, reported by Codex)."""

    def test_no_app_server_pull_only_persists(self):
        # Pure pull-on-prompt (no app-server): the inbox IS the only path.
        assert crl.should_persist_kept(has_app_server=False, armed=False) is True

    def test_no_app_server_armed_persists(self):
        # armed without an app-server (= exec-worker path) still needs inbox.
        assert crl.should_persist_kept(has_app_server=False, armed=True) is True

    def test_app_server_not_armed_persists(self):
        # THE BUG: non-armed app-server dispatched a silent turn and dropped
        # the inbox → blackhole. Must now keep the inbox fallback.
        assert crl.should_persist_kept(has_app_server=True, armed=False) is True

    def test_app_server_armed_skips_inbox(self):
        # Only here is the autonomous reply the surfacing path, so the inbox
        # fallback is redundant (= preserve the pre-bug behavior).
        assert crl.should_persist_kept(has_app_server=True, armed=True) is False
