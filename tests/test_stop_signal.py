"""Tests for the stop signal protocol (ms-55 e-1646).

Covers the producer (build), consumer (parse), receiver-side gate
(stop_applies_to_session), and the state-reduction helper
(latest_active_stops). The CLI tests for `beacon stop` itself live in
test_stop_cli.py.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import stop_signal  # noqa: E402


# ---------------------------------------------------------------------------
# build_stop_payload — validation rules
# ---------------------------------------------------------------------------

def test_build_scoped_ms_minimum():
    p = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-1",
        target_kind="ms",
        target_id="ms-55",
    )
    assert p["kind"] == "stop"
    assert p["scope"] == "scoped"
    assert p["target"] == {"kind": "ms", "id": "ms-55"}
    assert p["reason"] == ""
    assert p["reason_kind"] == "manual"
    assert p["issued_by_session_id"] == "sv-1"
    assert "issued_at" in p  # auto-stamped


def test_build_scoped_task_with_reason_kind():
    p = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-1",
        target_kind="task",
        target_id="e-1646",
        reason="build failed 3 retries",
        reason_kind="build_fail",
    )
    assert p["target"] == {"kind": "task", "id": "e-1646"}
    assert p["reason_kind"] == "build_fail"


def test_build_global_no_target():
    p = stop_signal.build_stop_payload(
        scope="global",
        issued_by_session_id="sv-1",
        reason="hostile prompt detected",
        reason_kind="other",
    )
    assert p["scope"] == "global"
    assert "target" not in p  # SPEC §3: global must not carry target


def test_build_global_with_target_rejected():
    """SPEC §3: global stops are explicitly project-wide. Letting a global
    stop carry a target makes the wire format ambiguous (= is the receiver
    supposed to honor the target as a hint, or is global authoritative?)."""
    with pytest.raises(ValueError, match="must not carry target"):
        stop_signal.build_stop_payload(
            scope="global",
            issued_by_session_id="sv-1",
            target_kind="ms",
            target_id="ms-55",
        )


def test_build_scoped_without_target_rejected():
    with pytest.raises(ValueError, match="requires target"):
        stop_signal.build_stop_payload(
            scope="scoped",
            issued_by_session_id="sv-1",
        )


def test_build_invalid_scope_rejected():
    with pytest.raises(ValueError, match="scope must be"):
        stop_signal.build_stop_payload(
            scope="partial",  # not yet defined; e-1670 future work
            issued_by_session_id="sv-1",
        )


def test_build_invalid_target_kind_rejected():
    with pytest.raises(ValueError, match="target_kind must be"):
        stop_signal.build_stop_payload(
            scope="scoped",
            issued_by_session_id="sv-1",
            target_kind="release",
            target_id="v0.37.3",
        )


def test_build_invalid_reason_kind_rejected():
    with pytest.raises(ValueError, match="reason_kind must be"):
        stop_signal.build_stop_payload(
            scope="global",
            issued_by_session_id="sv-1",
            reason_kind="catastrophic",
        )


def test_build_requires_issued_by():
    with pytest.raises(ValueError, match="issued_by_session_id"):
        stop_signal.build_stop_payload(
            scope="global",
            issued_by_session_id="",
        )


def test_build_machine_reason_attached():
    mr = {"retry_count": 3, "last_error": "exit 137"}
    p = stop_signal.build_stop_payload(
        scope="global",
        issued_by_session_id="sv-1",
        reason_kind="build_fail",
        machine_reason=mr,
    )
    assert p["machine_reason"] == mr


def test_build_machine_reason_must_be_dict():
    with pytest.raises(ValueError, match="machine_reason"):
        stop_signal.build_stop_payload(
            scope="global",
            issued_by_session_id="sv-1",
            machine_reason="exit 137",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# build_resume_payload
# ---------------------------------------------------------------------------

def test_build_resume_global():
    p = stop_signal.build_resume_payload(
        scope="global",
        issued_by_session_id="sv-1",
        reason="all clear after manual review",
    )
    assert p["kind"] == "resume"
    assert p["scope"] == "global"
    assert "target" not in p


def test_build_resume_scoped():
    p = stop_signal.build_resume_payload(
        scope="scoped",
        issued_by_session_id="sv-1",
        target_kind="ms",
        target_id="ms-55",
    )
    assert p["target"] == {"kind": "ms", "id": "ms-55"}


def test_build_resume_scoped_requires_target():
    with pytest.raises(ValueError, match="requires target"):
        stop_signal.build_resume_payload(
            scope="scoped",
            issued_by_session_id="sv-1",
        )


# ---------------------------------------------------------------------------
# parse_stop_event — graceful on malformed input
# ---------------------------------------------------------------------------

def _wrap(payload: dict, *, channel: str = "stop-signal", event_id: str = "e-1") -> dict:
    return {
        "event_id": event_id,
        "channel": channel,
        "payload": payload,
        "created_at": "2026-06-15T00:00:00Z",
    }


def test_parse_roundtrip_scoped():
    payload = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-1",
        target_kind="ms",
        target_id="ms-55",
        reason="testing",
    )
    rec = stop_signal.parse_stop_event(_wrap(payload))
    assert rec is not None
    assert rec["scope"] == "scoped"
    assert rec["target"] == {"kind": "ms", "id": "ms-55"}
    assert rec["reason"] == "testing"
    assert rec["event_id"] == "e-1"


def test_parse_roundtrip_global():
    payload = stop_signal.build_stop_payload(
        scope="global",
        issued_by_session_id="sv-1",
    )
    rec = stop_signal.parse_stop_event(_wrap(payload))
    assert rec is not None
    assert rec["scope"] == "global"
    assert "target" not in rec


def test_parse_wrong_channel_returns_none():
    payload = stop_signal.build_stop_payload(
        scope="global",
        issued_by_session_id="sv-1",
    )
    assert stop_signal.parse_stop_event(_wrap(payload, channel="dm")) is None


def test_parse_wrong_kind_returns_none():
    """A resume event on stop-signal channel is not a stop."""
    payload = stop_signal.build_resume_payload(
        scope="global",
        issued_by_session_id="sv-1",
    )
    assert stop_signal.parse_stop_event(_wrap(payload)) is None


def test_parse_missing_scope_returns_none():
    assert stop_signal.parse_stop_event(_wrap({
        "kind": "stop",
        "issued_by_session_id": "sv-1",
    })) is None


def test_parse_unknown_target_kind_returns_none():
    assert stop_signal.parse_stop_event(_wrap({
        "kind": "stop",
        "scope": "scoped",
        "target": {"kind": "release", "id": "v0.1"},
        "issued_by_session_id": "sv-1",
    })) is None


def test_parse_missing_issued_by_returns_none():
    assert stop_signal.parse_stop_event(_wrap({
        "kind": "stop",
        "scope": "global",
        "issued_by_session_id": "",
    })) is None


def test_parse_garbage_event_returns_none():
    assert stop_signal.parse_stop_event(None) is None  # type: ignore[arg-type]
    assert stop_signal.parse_stop_event("hello") is None  # type: ignore[arg-type]
    assert stop_signal.parse_stop_event({}) is None


# ---------------------------------------------------------------------------
# stop_applies_to_session — receiver gate
# ---------------------------------------------------------------------------

def test_global_always_applies():
    payload = stop_signal.build_stop_payload(
        scope="global", issued_by_session_id="sv-A",
    )
    rec = stop_signal.parse_stop_event(_wrap(payload))
    assert stop_signal.stop_applies_to_session(rec, my_session_id="sv-B")
    assert stop_signal.stop_applies_to_session(rec, my_session_id="sv-A")


def test_scoped_session_matches_self_only():
    payload = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-X",
        target_kind="session",
        target_id="sv-A",
    )
    rec = stop_signal.parse_stop_event(_wrap(payload))
    assert stop_signal.stop_applies_to_session(rec, my_session_id="sv-A")
    assert not stop_signal.stop_applies_to_session(rec, my_session_id="sv-B")


def test_scoped_ms_matches_workers_on_that_ms():
    payload = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-X",
        target_kind="ms",
        target_id="ms-55",
    )
    rec = stop_signal.parse_stop_event(_wrap(payload))
    assert stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-A", my_ms_id="ms-55",
    )
    # A worker on a different MS should NOT halt — that's the over-broadcast
    # case SPEC §3 explicitly avoids.
    assert not stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-A", my_ms_id="ms-56",
    )
    # A session not associated with any MS shouldn't halt either.
    assert not stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-A",
    )


def test_scoped_task_matches_only_active_task():
    payload = stop_signal.build_stop_payload(
        scope="scoped",
        issued_by_session_id="sv-X",
        target_kind="task",
        target_id="e-1646",
    )
    rec = stop_signal.parse_stop_event(_wrap(payload))
    assert stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-A", my_task_id="e-1646",
    )
    assert not stop_signal.stop_applies_to_session(
        rec, my_session_id="sv-A", my_task_id="e-9999",
    )


def test_applies_to_session_garbage_safe():
    assert not stop_signal.stop_applies_to_session(
        None, my_session_id="sv-A",  # type: ignore[arg-type]
    )
    assert not stop_signal.stop_applies_to_session(
        {}, my_session_id="sv-A",
    )
    assert not stop_signal.stop_applies_to_session(
        {"scope": "scoped"}, my_session_id="sv-A",  # missing target
    )


# ---------------------------------------------------------------------------
# latest_active_stops — state reduction
# ---------------------------------------------------------------------------

def _stop_event(*, event_id, created_at, scope, target_kind=None,
                target_id=None, issued_by="sv-X"):
    payload = stop_signal.build_stop_payload(
        scope=scope,
        issued_by_session_id=issued_by,
        target_kind=target_kind,
        target_id=target_id,
        issued_at=created_at,
    )
    return {
        "event_id": event_id,
        "channel": "stop-signal",
        "payload": payload,
        "created_at": created_at,
    }


def _resume_event(*, event_id, created_at, scope, target_kind=None,
                  target_id=None, issued_by="sv-X"):
    payload = stop_signal.build_resume_payload(
        scope=scope,
        issued_by_session_id=issued_by,
        target_kind=target_kind,
        target_id=target_id,
        issued_at=created_at,
    )
    return {
        "event_id": event_id,
        "channel": "stop-signal",
        "payload": payload,
        "created_at": created_at,
    }


def test_latest_active_empty_history():
    assert stop_signal.latest_active_stops([]) == []


def test_latest_active_single_stop():
    events = [_stop_event(
        event_id="e1", created_at="2026-06-15T00:00:00Z",
        scope="global",
    )]
    actives = stop_signal.latest_active_stops(events)
    assert len(actives) == 1
    assert actives[0]["scope"] == "global"


def test_resume_clears_matching_stop():
    events = [
        _stop_event(
            event_id="e1", created_at="2026-06-15T00:00:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",
        ),
        _resume_event(
            event_id="e2", created_at="2026-06-15T00:01:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",
        ),
    ]
    assert stop_signal.latest_active_stops(events) == []


def test_resume_does_not_clear_different_scope():
    """A resume on ms-55 must NOT clear a global stop, or a stop on ms-66."""
    events = [
        _stop_event(
            event_id="e1", created_at="2026-06-15T00:00:00Z",
            scope="global",
        ),
        _stop_event(
            event_id="e2", created_at="2026-06-15T00:01:00Z",
            scope="scoped", target_kind="ms", target_id="ms-66",
        ),
        _resume_event(
            event_id="e3", created_at="2026-06-15T00:02:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",  # different
        ),
    ]
    actives = stop_signal.latest_active_stops(events)
    assert len(actives) == 2
    scopes = {(a["scope"], a.get("target", {}).get("id")) for a in actives}
    assert scopes == {("global", None), ("scoped", "ms-66")}


def test_stop_after_resume_re_activates():
    """If a key gets resumed and then stopped again, the latest stop is
    the one to honor. This matters when a flaky failure mode re-triggers
    after a hopeful resume."""
    events = [
        _stop_event(
            event_id="e1", created_at="2026-06-15T00:00:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",
            issued_by="sv-A",
        ),
        _resume_event(
            event_id="e2", created_at="2026-06-15T00:01:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",
        ),
        _stop_event(
            event_id="e3", created_at="2026-06-15T00:02:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",
            issued_by="sv-B",
        ),
    ]
    actives = stop_signal.latest_active_stops(events)
    assert len(actives) == 1
    assert actives[0]["event_id"] == "e3"
    assert actives[0]["issued_by_session_id"] == "sv-B"


def test_latest_active_ignores_other_channels():
    """An event on the dm channel must not count, even if its payload
    looks stop-shaped."""
    payload = stop_signal.build_stop_payload(
        scope="global", issued_by_session_id="sv-A",
    )
    events = [{
        "event_id": "e1",
        "channel": "dm",
        "payload": payload,
        "created_at": "2026-06-15T00:00:00Z",
    }]
    assert stop_signal.latest_active_stops(events) == []


def test_latest_active_sorts_unsorted_input():
    """If events are passed out of order, the function must still
    reconstruct the correct halt state."""
    events = [
        _resume_event(
            event_id="e2", created_at="2026-06-15T00:01:00Z",
            scope="global",
        ),
        _stop_event(
            event_id="e1", created_at="2026-06-15T00:00:00Z",
            scope="global",
        ),
    ]
    # Resume at T=1 clears the stop at T=0, so no active stops.
    assert stop_signal.latest_active_stops(events) == []


def test_latest_active_returns_sorted_by_issued_at():
    events = [
        _stop_event(
            event_id="e2", created_at="2026-06-15T00:02:00Z",
            scope="scoped", target_kind="ms", target_id="ms-66",
        ),
        _stop_event(
            event_id="e1", created_at="2026-06-15T00:01:00Z",
            scope="scoped", target_kind="ms", target_id="ms-55",
        ),
    ]
    actives = stop_signal.latest_active_stops(events)
    assert [a["target"]["id"] for a in actives] == ["ms-55", "ms-66"]
