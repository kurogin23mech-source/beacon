"""ms-95 / e-2875 layer 3-A — client-side archived Trek event filter.

Pins the inbox-hook helpers that silently drop trek-scheduler events
whose Trek has already been archived. Complements the server-side 410
Gone guard (tests/test_trek_archived_write_guards.py) which is the
structural boundary; this filter is the noise-reduction layer that
prevents the AI from even seeing the event.

Under test:
  * ``_is_trek_scheduler_event`` — recognises both pre-e-2639
    (dedicated channel) and post-e-2639 (channel="dm" + origin_channel)
    trek-scheduler events, and rejects normal peer DMs.
  * ``_lookup_trek_status`` — memoises per invocation, fail-open on
    lookup errors.
  * ``_filter_archived_trek_events`` — split ``(kept, dropped)`` where
    dropped contains only archived-trek scheduler events.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "bin" / "beacon-bus-inbox-hook.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location(
        "bus_inbox_hook_e2875", HOOK_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bus_inbox_hook_e2875"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# _is_trek_scheduler_event
# ---------------------------------------------------------------------------

class TestIsTrekSchedulerEvent:
    def test_dedicated_channel_hit(self, hook):
        ev = {
            "channel": "trek-progress-check",
            "payload": {"trek_id": "tk-abc"},
        }
        is_trek, trek_id = hook._is_trek_scheduler_event(ev)
        assert is_trek is True
        assert trek_id == "tk-abc"

    def test_dm_channel_with_origin_channel_hit(self, hook):
        # Post-e-2639 dm-transport shape.
        ev = {
            "channel": "dm",
            "payload": {
                "trek_id": "tk-abc",
                "origin_channel": "trek-progress-check",
            },
        }
        is_trek, trek_id = hook._is_trek_scheduler_event(ev)
        assert is_trek is True
        assert trek_id == "tk-abc"

    def test_all_four_scheduler_channels_recognised(self, hook):
        for ch in [
            "trek-progress-check",
            "trek-task-review",
            "trek-leader-digest",
            "trek-trigger",
        ]:
            ev = {"channel": ch, "payload": {"trek_id": "tk-x"}}
            is_trek, _ = hook._is_trek_scheduler_event(ev)
            assert is_trek is True, ch

    def test_normal_peer_dm_not_trek_scheduler(self, hook):
        # Trek member DMing another trek member — has trek_id in some
        # metadata but no origin_channel, and channel is "dm".
        ev = {
            "channel": "dm",
            "payload": {"trek_id": "tk-abc", "text": "hi peer"},
        }
        is_trek, _ = hook._is_trek_scheduler_event(ev)
        assert is_trek is False

    def test_no_trek_id_returns_false(self, hook):
        ev = {
            "channel": "trek-progress-check",
            "payload": {"body": "no trek id"},
        }
        is_trek, trek_id = hook._is_trek_scheduler_event(ev)
        assert is_trek is False
        assert trek_id == ""

    def test_missing_payload_defaults_to_empty(self, hook):
        ev = {"channel": "trek-progress-check"}
        is_trek, _ = hook._is_trek_scheduler_event(ev)
        assert is_trek is False


# ---------------------------------------------------------------------------
# _lookup_trek_status
# ---------------------------------------------------------------------------

class TestLookupTrekStatus:
    def test_returns_status_from_api(self, hook, monkeypatch):
        calls = []

        def fake_get(url, path, token):
            calls.append(path)
            return {"trek_id": "tk-x", "status": "active"}

        monkeypatch.setattr(hook, "_api_get", fake_get)
        cache: dict = {}
        status = hook._lookup_trek_status(
            "https://api", "tk-x", "tok", cache,
        )
        assert status == "active"
        assert cache["tk-x"] == "active"
        assert calls == ["/api/treks/tk-x"]

    def test_caches_per_invocation(self, hook, monkeypatch):
        call_count = {"n": 0}

        def fake_get(url, path, token):
            call_count["n"] += 1
            return {"status": "archived"}

        monkeypatch.setattr(hook, "_api_get", fake_get)
        cache: dict = {}
        assert hook._lookup_trek_status(
            "https://api", "tk-y", "tok", cache,
        ) == "archived"
        assert hook._lookup_trek_status(
            "https://api", "tk-y", "tok", cache,
        ) == "archived"
        # Second call served from cache.
        assert call_count["n"] == 1

    def test_returns_none_on_exception(self, hook, monkeypatch):
        def fake_get(url, path, token):
            raise RuntimeError("boom")

        monkeypatch.setattr(hook, "_api_get", fake_get)
        cache: dict = {}
        status = hook._lookup_trek_status(
            "https://api", "tk-z", "tok", cache,
        )
        assert status is None
        # Caches the failure so subsequent events for the same trek
        # don't re-hit the failing API.
        assert cache["tk-z"] is None

    def test_returns_none_when_response_missing_status(self, hook, monkeypatch):
        def fake_get(url, path, token):
            return {"trek_id": "tk-q"}  # no status key

        monkeypatch.setattr(hook, "_api_get", fake_get)
        cache: dict = {}
        assert hook._lookup_trek_status(
            "https://api", "tk-q", "tok", cache,
        ) is None


# ---------------------------------------------------------------------------
# _filter_archived_trek_events
# ---------------------------------------------------------------------------

class TestFilterArchivedTrekEvents:
    def test_drops_archived_trek_scheduler_event(self, hook, monkeypatch):
        monkeypatch.setattr(
            hook, "_api_get",
            lambda url, path, tok: {"status": "archived"},
        )
        unread = [
            {
                "event_id": "e1",
                "channel": "trek-progress-check",
                "payload": {"trek_id": "tk-arch"},
                "created_at": "2026-07-03T22:00:00Z",
            },
        ]
        kept, dropped = hook._filter_archived_trek_events(
            unread, "https://api", "tok",
        )
        assert kept == []
        assert len(dropped) == 1
        assert dropped[0]["event_id"] == "e1"

    def test_keeps_active_trek_scheduler_event(self, hook, monkeypatch):
        monkeypatch.setattr(
            hook, "_api_get",
            lambda url, path, tok: {"status": "active"},
        )
        unread = [
            {
                "event_id": "e1",
                "channel": "trek-progress-check",
                "payload": {"trek_id": "tk-live"},
                "created_at": "2026-07-03T22:00:00Z",
            },
        ]
        kept, dropped = hook._filter_archived_trek_events(
            unread, "https://api", "tok",
        )
        assert len(kept) == 1
        assert dropped == []

    def test_fail_open_on_lookup_error(self, hook, monkeypatch):
        def blow_up(url, path, tok):
            raise ConnectionError("net down")

        monkeypatch.setattr(hook, "_api_get", blow_up)
        unread = [
            {
                "event_id": "e1",
                "channel": "trek-progress-check",
                "payload": {"trek_id": "tk-x"},
                "created_at": "2026-07-03T22:00:00Z",
            },
        ]
        kept, dropped = hook._filter_archived_trek_events(
            unread, "https://api", "tok",
        )
        # Failing open — server 410 Gone is the structural boundary.
        assert len(kept) == 1
        assert dropped == []

    def test_normal_dm_untouched(self, hook, monkeypatch):
        # Should never even call _api_get for non-trek events.
        called = {"n": 0}

        def counting_get(url, path, tok):
            called["n"] += 1
            return {"status": "active"}

        monkeypatch.setattr(hook, "_api_get", counting_get)
        unread = [
            {
                "event_id": "peer-dm",
                "channel": "dm",
                "payload": {"trek_id": "tk-abc", "text": "hi peer"},
                "created_at": "2026-07-03T22:00:00Z",
            },
        ]
        kept, dropped = hook._filter_archived_trek_events(
            unread, "https://api", "tok",
        )
        assert len(kept) == 1
        assert dropped == []
        assert called["n"] == 0

    def test_mixed_batch_partitions_correctly(self, hook, monkeypatch):
        status_by_trek = {
            "tk-arch": "archived",
            "tk-live": "active",
        }

        def fake_get(url, path, tok):
            # path is /api/treks/<id>
            tid = path.rsplit("/", 1)[-1]
            return {"status": status_by_trek.get(tid, "active")}

        monkeypatch.setattr(hook, "_api_get", fake_get)
        unread = [
            {"event_id": "a", "channel": "trek-progress-check",
             "payload": {"trek_id": "tk-arch"}},
            {"event_id": "b", "channel": "dm",
             "payload": {"text": "peer chat"}},
            {"event_id": "c", "channel": "trek-task-review",
             "payload": {"trek_id": "tk-live"}},
            {"event_id": "d", "channel": "dm",
             "payload": {"trek_id": "tk-arch",
                         "origin_channel": "trek-leader-digest"}},
        ]
        kept, dropped = hook._filter_archived_trek_events(
            unread, "https://api", "tok",
        )
        kept_ids = [e["event_id"] for e in kept]
        dropped_ids = [e["event_id"] for e in dropped]
        assert set(kept_ids) == {"b", "c"}
        assert set(dropped_ids) == {"a", "d"}
