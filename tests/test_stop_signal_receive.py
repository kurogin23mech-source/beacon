"""Receive-side halt protocol tests (ms-55 e-1721).

The pure parser + sender helpers are in test_stop_signal.py. This file
covers what happens AFTER a stop event lands:

  * `process_inbox_events` writes a halt-request.json when a matching
    stop signal applies to this session
  * scoped halts only bind when the target matches (= session_id /
    ms_id / task_id)
  * global halts always bind
  * a matching resume clears a pending halt
  * `render_halt_inject` formats the request for PostToolUse
  * `halt_inject_needed` suppresses already-acknowledged requests
  * `acknowledge_halt_request` / `clear_halt_request` lifecycle
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import stop_signal  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ev(channel, payload, *, created_at, event_id="ev-X"):
    return {
        "channel": channel,
        "payload": payload,
        "created_at": created_at,
        "event_id": event_id,
    }


def _stop_payload(*, scope="global", target_kind=None, target_id=None,
                  reason="halt", reason_kind="manual",
                  issued_by="sv-sender", issued_at="2026-06-15T01:00:00Z"):
    if scope == "global":
        return stop_signal.build_stop_payload(
            scope=scope,
            issued_by_session_id=issued_by,
            reason=reason,
            reason_kind=reason_kind,
            issued_at=issued_at,
        )
    return stop_signal.build_stop_payload(
        scope=scope,
        issued_by_session_id=issued_by,
        target_kind=target_kind,
        target_id=target_id,
        reason=reason,
        reason_kind=reason_kind,
        issued_at=issued_at,
    )


def _resume_payload(*, scope="global", target_kind=None, target_id=None,
                    issued_by="sv-sender", issued_at="2026-06-15T02:00:00Z"):
    if scope == "global":
        return stop_signal.build_resume_payload(
            scope=scope,
            issued_by_session_id=issued_by,
            issued_at=issued_at,
        )
    return stop_signal.build_resume_payload(
        scope=scope,
        issued_by_session_id=issued_by,
        target_kind=target_kind,
        target_id=target_id,
        issued_at=issued_at,
    )


# ---------------------------------------------------------------------------
# process_inbox_events — write
# ---------------------------------------------------------------------------

def test_global_stop_writes_halt_request(tmp_path):
    events = [
        _ev("stop-signal", _stop_payload(scope="global"),
            created_at="2026-06-15T01:00:00Z"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", beacon_dir=str(tmp_path),
    )
    assert result is not None
    assert result["scope"] == "global"
    # File on disk reflects the request.
    path = Path(stop_signal.halt_request_path("sv-A", beacon_dir=str(tmp_path)))
    assert path.exists()
    saved = json.loads(path.read_text())
    assert saved["scope"] == "global"
    assert saved["reason_kind"] == "manual"
    # raw_event should be stripped to keep the file small.
    assert "raw_event" not in saved
    # received_at is stamped on write.
    assert saved["received_at"]


def test_scoped_stop_matches_session(tmp_path):
    events = [
        _ev("stop-signal",
            _stop_payload(scope="scoped", target_kind="session",
                          target_id="sv-target"),
            created_at="2026-06-15T01:00:00Z"),
    ]
    # Session sv-other should NOT be halted.
    result = stop_signal.process_inbox_events(
        events, session_id="sv-other", beacon_dir=str(tmp_path),
    )
    assert result is None
    path = Path(stop_signal.halt_request_path(
        "sv-other", beacon_dir=str(tmp_path)))
    assert not path.exists()


def test_scoped_stop_matches_ms(tmp_path):
    events = [
        _ev("stop-signal",
            _stop_payload(scope="scoped", target_kind="ms",
                          target_id="ms-55", reason="build broke"),
            created_at="2026-06-15T01:00:00Z"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", ms_id="ms-55",
        beacon_dir=str(tmp_path),
    )
    assert result is not None
    assert result["target"] == {"kind": "ms", "id": "ms-55"}


def test_scoped_stop_no_ms_match_no_write(tmp_path):
    events = [
        _ev("stop-signal",
            _stop_payload(scope="scoped", target_kind="ms",
                          target_id="ms-55"),
            created_at="2026-06-15T01:00:00Z"),
    ]
    # Session is on a different MS — should NOT halt.
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", ms_id="ms-99",
        beacon_dir=str(tmp_path),
    )
    assert result is None


def test_scoped_stop_matches_task(tmp_path):
    events = [
        _ev("stop-signal",
            _stop_payload(scope="scoped", target_kind="task",
                          target_id="e-1721"),
            created_at="2026-06-15T01:00:00Z"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", task_id="e-1721",
        beacon_dir=str(tmp_path),
    )
    assert result is not None
    assert result["target"]["id"] == "e-1721"


# ---------------------------------------------------------------------------
# process_inbox_events — resume
# ---------------------------------------------------------------------------

def test_resume_clears_pending_halt(tmp_path):
    # First, drop a stop on disk so we can verify clear.
    stop_signal.write_halt_request(
        {"scope": "global", "reason": "first", "reason_kind": "manual",
         "issued_by_session_id": "sv-sender", "issued_at": "01:00"},
        session_id="sv-A", beacon_dir=str(tmp_path),
    )
    events = [
        _ev("stop-signal", _resume_payload(scope="global",
                                            issued_at="02:00:00Z"),
            created_at="2026-06-15T02:00:00Z"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", beacon_dir=str(tmp_path),
    )
    assert result is None
    path = Path(stop_signal.halt_request_path(
        "sv-A", beacon_dir=str(tmp_path)))
    assert not path.exists()


def test_scoped_resume_only_clears_matching(tmp_path):
    """A resume for ms-99 should NOT clear a halt issued on ms-55."""
    stop_signal.write_halt_request(
        {"scope": "scoped", "target": {"kind": "ms", "id": "ms-55"},
         "reason": "first", "reason_kind": "manual",
         "issued_by_session_id": "sv-sender", "issued_at": "01:00"},
        session_id="sv-A", beacon_dir=str(tmp_path),
    )
    events = [
        _ev("stop-signal",
            _resume_payload(scope="scoped", target_kind="ms",
                            target_id="ms-99"),
            created_at="2026-06-15T02:00:00Z"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", ms_id="ms-55",
        beacon_dir=str(tmp_path),
    )
    # We didn't write any new halt, but the existing file should remain.
    assert result is None
    path = Path(stop_signal.halt_request_path(
        "sv-A", beacon_dir=str(tmp_path)))
    assert path.exists()


def test_stop_then_resume_in_same_batch_cancels(tmp_path):
    events = [
        _ev("stop-signal", _stop_payload(scope="global"),
            created_at="2026-06-15T01:00:00Z", event_id="ev-1"),
        _ev("stop-signal", _resume_payload(scope="global"),
            created_at="2026-06-15T02:00:00Z", event_id="ev-2"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", beacon_dir=str(tmp_path),
    )
    assert result is None
    path = Path(stop_signal.halt_request_path(
        "sv-A", beacon_dir=str(tmp_path)))
    assert not path.exists()


def test_resume_then_stop_keeps_latest_halt(tmp_path):
    """Even if a resume appeared first in the batch, a later stop wins."""
    events = [
        _ev("stop-signal", _resume_payload(scope="global"),
            created_at="2026-06-15T01:00:00Z", event_id="ev-1"),
        _ev("stop-signal",
            _stop_payload(scope="global", reason="new"),
            created_at="2026-06-15T02:00:00Z", event_id="ev-2"),
    ]
    result = stop_signal.process_inbox_events(
        events, session_id="sv-A", beacon_dir=str(tmp_path),
    )
    assert result is not None
    assert result["reason"] == "new"


# ---------------------------------------------------------------------------
# halt-request file lifecycle
# ---------------------------------------------------------------------------

def test_read_returns_none_when_missing(tmp_path):
    result = stop_signal.read_halt_request(
        "sv-missing", beacon_dir=str(tmp_path),
    )
    assert result is None


def test_acknowledge_stamps_and_returns_true(tmp_path):
    stop_signal.write_halt_request(
        {"scope": "global", "reason": "x", "reason_kind": "manual",
         "issued_by_session_id": "sv-s", "issued_at": "01:00"},
        session_id="sv-A", beacon_dir=str(tmp_path),
    )
    assert stop_signal.acknowledge_halt_request(
        "sv-A", beacon_dir=str(tmp_path), note="ok",
    )
    data = stop_signal.read_halt_request(
        "sv-A", beacon_dir=str(tmp_path),
    )
    assert data["acknowledged_at"]
    assert data["acknowledgement_note"] == "ok"


def test_acknowledge_missing_returns_false(tmp_path):
    assert stop_signal.acknowledge_halt_request(
        "sv-missing", beacon_dir=str(tmp_path),
    ) is False


def test_clear_halt_request(tmp_path):
    stop_signal.write_halt_request(
        {"scope": "global", "reason": "x", "reason_kind": "manual",
         "issued_by_session_id": "sv-s", "issued_at": "01:00"},
        session_id="sv-A", beacon_dir=str(tmp_path),
    )
    assert stop_signal.clear_halt_request(
        "sv-A", beacon_dir=str(tmp_path),
    ) is True
    assert stop_signal.clear_halt_request(
        "sv-A", beacon_dir=str(tmp_path),
    ) is False  # already gone


# ---------------------------------------------------------------------------
# PostToolUse render + suppression
# ---------------------------------------------------------------------------

def test_render_global_halt():
    record = {
        "scope": "global",
        "reason": "build fail 3 retries",
        "reason_kind": "build_fail",
        "issued_by_session_id": "sv-sender",
        "issued_at": "2026-06-15T01:00:00Z",
        "received_at": "2026-06-15T01:00:01Z",
    }
    out = stop_signal.render_halt_inject(record)
    assert "STOP SIGNAL" in out
    assert "GLOBAL" in out
    assert "build fail 3 retries" in out
    assert "build_fail" in out
    assert "sv-sender" in out


def test_render_scoped_halt():
    record = {
        "scope": "scoped",
        "target": {"kind": "ms", "id": "ms-55"},
        "reason": "test fail",
        "reason_kind": "test_fail",
        "issued_by_session_id": "sv-sender",
        "issued_at": "2026-06-15T01:00:00Z",
        "received_at": "2026-06-15T01:00:01Z",
    }
    out = stop_signal.render_halt_inject(record)
    assert "ms:ms-55" in out
    assert "test_fail" in out


def test_render_malformed_returns_empty():
    assert stop_signal.render_halt_inject(None) == ""
    assert stop_signal.render_halt_inject("not a dict") == ""


def test_inject_needed_false_when_acknowledged():
    record = {
        "scope": "global",
        "reason": "x",
        "acknowledged_at": "2026-06-15T01:30:00Z",
    }
    assert stop_signal.halt_inject_needed(record) is False


def test_inject_needed_true_when_pending():
    record = {
        "scope": "global",
        "reason": "x",
    }
    assert stop_signal.halt_inject_needed(record) is True


def test_inject_needed_false_for_none():
    assert stop_signal.halt_inject_needed(None) is False


# ---------------------------------------------------------------------------
# Public API surface — pin so refactors don't drop the receive hook.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "stop_applies_to_session",
    "halt_request_path",
    "write_halt_request",
    "read_halt_request",
    "acknowledge_halt_request",
    "clear_halt_request",
    "process_inbox_events",
    "render_halt_inject",
    "halt_inject_needed",
])
def test_receive_surface_exports(name: str) -> None:
    assert hasattr(stop_signal, name), (
        f"lib/stop_signal.py missing {name!r} — receive-side halt "
        "protocol is incomplete"
    )
