"""ms-95 / e-2308 — Trek TTL extension primitive.

These tests pin the lib-level semantics of ``trek.extend_task_ttl`` and the
scheduler-side honour of ``ttl_extended_until``. Server endpoint + CLI
wiring live in separate files; this is the pure-data layer so the contract
can be exercised without standing up the FastAPI app or shelling out.

Coverage:

  (a) ``extend_task_ttl`` creates the entry on a previously-unseen task.
  (b) ``extend_task_ttl`` stamps ``ttl_extended_until`` to now + minutes
      and records the optional reason (truncated at 120 chars).
  (c) ``extend_task_ttl`` with ``minutes <= 0`` clears the extension.
  (d) Calling twice replaces the deadline (idempotent in either direction
      — pull in early or push out further).
  (e) ``detect_auto_stalled_tasks`` skips tasks whose extension is in the
      future, even if ``last_activity_at`` is past the TTL.
  (f) Once the extension expires, the task auto-stalls normally on the
      next tick.

The 2026-06-23 incident this fixes: leader dispatched 3 Agent-tool subagents
to work on Trek tk-28479e6b tasks. Each subagent runs in a fresh
``session_id`` that is not joined to the trek, so it cannot call
``beacon trek task-state working`` to refresh ``last_activity_at``. The
leader's main session is busy awaiting subagent return and cannot call
``/beacon-trek-pulse`` either. 12 minutes pass → TTL fires → task is force-
demoted to ``leader_review`` even though work is actively happening.
"""

from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402
import trek_scheduler  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _trek_with_working_task(*, task_id: str, last_activity_at: str,
                            ttl_minutes: int = 12) -> dict:
    return {
        "trek_id": "tk-test",
        "status": "active",
        "halt": None,
        "meta": {"working_ttl_minutes": ttl_minutes},
        "task_states": {
            task_id: {
                "state": "working",
                "updated_at": last_activity_at,
                "last_activity_at": last_activity_at,
                "updated_by_session_id": "sv-exec",
                "note": "",
            },
        },
    }


# ---------------------------------------------------------------------------
# (a) (b) (c) (d) — extend_task_ttl semantics
# ---------------------------------------------------------------------------

class TestExtendTaskTtlSemantics:
    def test_creates_entry_on_unseen_task(self):
        t = {"trek_id": "tk-1", "task_states": {}}
        trek.extend_task_ttl(t, task_id="e-100", minutes=30)
        entry = t["task_states"]["e-100"]
        assert entry["state"] == trek.DEFAULT_TASK_STATE
        assert entry["ttl_extended_until"]

    def test_stamps_deadline_to_now_plus_minutes(self):
        t = _trek_with_working_task(
            task_id="e-200", last_activity_at=_iso(_utcnow()),
        )
        before = _utcnow()
        trek.extend_task_ttl(t, task_id="e-200", minutes=15)
        after = _utcnow()
        ext_str = t["task_states"]["e-200"]["ttl_extended_until"]
        ext = datetime.datetime.strptime(
            ext_str, "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=datetime.timezone.utc)
        # Deadline must land within [before + 15min, after + 15min + 1s].
        assert before + datetime.timedelta(minutes=15) <= ext
        assert ext <= after + datetime.timedelta(minutes=15, seconds=1)

    def test_records_truncated_reason(self):
        t = _trek_with_working_task(
            task_id="e-300", last_activity_at=_iso(_utcnow()),
        )
        long_reason = "x" * 500
        trek.extend_task_ttl(
            t, task_id="e-300", minutes=10, reason=long_reason,
        )
        assert t["task_states"]["e-300"]["ttl_extension_reason"] == "x" * 120

    def test_minutes_zero_clears_extension(self):
        t = _trek_with_working_task(
            task_id="e-400", last_activity_at=_iso(_utcnow()),
        )
        trek.extend_task_ttl(t, task_id="e-400", minutes=20, reason="dispatch")
        assert "ttl_extended_until" in t["task_states"]["e-400"]
        trek.extend_task_ttl(t, task_id="e-400", minutes=0)
        assert "ttl_extended_until" not in t["task_states"]["e-400"]
        assert "ttl_extension_reason" not in t["task_states"]["e-400"]

    def test_minutes_negative_also_clears(self):
        t = _trek_with_working_task(
            task_id="e-401", last_activity_at=_iso(_utcnow()),
        )
        trek.extend_task_ttl(t, task_id="e-401", minutes=20)
        trek.extend_task_ttl(t, task_id="e-401", minutes=-5)
        assert "ttl_extended_until" not in t["task_states"]["e-401"]

    def test_followup_extend_replaces_deadline(self):
        """The contract is "the latest call wins" — extending again can
        either push out further or pull in early. Tests both directions."""
        t = _trek_with_working_task(
            task_id="e-500", last_activity_at=_iso(_utcnow()),
        )
        trek.extend_task_ttl(t, task_id="e-500", minutes=30)
        first = t["task_states"]["e-500"]["ttl_extended_until"]
        # Push further out.
        trek.extend_task_ttl(t, task_id="e-500", minutes=60)
        second = t["task_states"]["e-500"]["ttl_extended_until"]
        assert second > first
        # Pull in.
        trek.extend_task_ttl(t, task_id="e-500", minutes=5)
        third = t["task_states"]["e-500"]["ttl_extended_until"]
        assert third < second

    def test_invalid_minutes_raises(self):
        t = {"trek_id": "tk-1", "task_states": {}}
        try:
            trek.extend_task_ttl(t, task_id="e-1", minutes="not-an-int")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for non-integer minutes")

    def test_empty_task_id_raises(self):
        t = {"trek_id": "tk-1", "task_states": {}}
        try:
            trek.extend_task_ttl(t, task_id="", minutes=10)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for empty task_id")


# ---------------------------------------------------------------------------
# (e) (f) — scheduler honours the extension
# ---------------------------------------------------------------------------

class TestSchedulerHonoursExtension:
    def test_skips_task_when_extension_in_future(self):
        """Even with last_activity_at 30 min stale (= past TTL of 12), the
        extension future-dates the safety net so the task is NOT auto-stalled."""
        now = _utcnow()
        stale = now - datetime.timedelta(minutes=30)
        t = _trek_with_working_task(
            task_id="e-600", last_activity_at=_iso(stale),
        )
        trek.extend_task_ttl(t, task_id="e-600", minutes=15)
        stalled = trek_scheduler.detect_auto_stalled_tasks(t, now=now)
        assert stalled == [], (
            f"expected no auto-stall while extension active, got {stalled!r}"
        )

    def test_fires_normally_once_extension_expires(self):
        """The extension is a postponement, not a permanent waiver. Once it
        expires, normal TTL semantics resume and the task auto-stalls."""
        now = _utcnow()
        stale = now - datetime.timedelta(minutes=30)
        t = _trek_with_working_task(
            task_id="e-700", last_activity_at=_iso(stale),
        )
        # Extension expired 1 minute ago.
        past_ext = now - datetime.timedelta(minutes=1)
        t["task_states"]["e-700"]["ttl_extended_until"] = _iso(past_ext)
        stalled = trek_scheduler.detect_auto_stalled_tasks(t, now=now)
        assert len(stalled) == 1
        assert stalled[0]["task_id"] == "e-700"

    def test_no_extension_field_means_normal_ttl(self):
        """Backward compat — pre-e-2308 docs have no ttl_extended_until,
        and the scheduler must continue to evaluate them on last_activity_at
        alone."""
        now = _utcnow()
        stale = now - datetime.timedelta(minutes=20)
        t = _trek_with_working_task(
            task_id="e-800", last_activity_at=_iso(stale),
        )
        # No extension set.
        stalled = trek_scheduler.detect_auto_stalled_tasks(t, now=now)
        assert len(stalled) == 1
        assert stalled[0]["task_id"] == "e-800"

    def test_extension_does_not_resurrect_terminal_task(self):
        """Extending TTL on a done / leader_review task must not flip it back
        into the auto-stall radar (= state filter runs first)."""
        now = _utcnow()
        t = _trek_with_working_task(
            task_id="e-900",
            last_activity_at=_iso(now - datetime.timedelta(minutes=30)),
        )
        t["task_states"]["e-900"]["state"] = "leader_review"
        trek.extend_task_ttl(t, task_id="e-900", minutes=60)
        stalled = trek_scheduler.detect_auto_stalled_tasks(t, now=now)
        # leader_review tasks are never auto-stalled regardless of extension.
        assert stalled == []
