"""Transient-network resilience for ``beacon bus listen`` (ms-95 / e-1454).

Real-world incident pinned by these tests: 2026-06-11 02:00Z. A
Monitor-armed ``/beacon-bus-armed`` session was streaming events
through ``beacon bus listen --auto-ack`` when its TLS handshake to
the bus backend timed out. Before this fix, the SSL exception bubbled
out of ``cmd_bus_listen``, the process exited, and the Monitor armed
loop died with it — silent end of autonomous DM coverage.

The fix lives in ``lib/commands.cmd_bus_listen``: the per-iteration
``list_unread_bus_events`` call is wrapped in a try/except shield that
catches the four canonical transient-network exception types
(``urllib.error.URLError``, ``ssl.SSLError``, ``TimeoutError``,
``socket.timeout``), logs to stderr, sleeps with exponential backoff
(1s → 2s → 4s → 8s → 16s → 30s cap), and continues the loop. A
successful round-trip resets the backoff so a single hiccup never
permanently slows polling.

Three things are pinned here:

  (a) **single transient error → recovers next iteration**. One
      ``URLError`` raised on poll #1, then a real event on poll #2.
      The event must still reach stdout and the cursor must advance.

  (b) **persistent errors → exponential backoff sequence**. N raises
      in a row must produce the documented 1 → 2 → 4 → 8 → 16 → 30
      cap sleep durations, with a reset to 1 after the next success.

  (c) **non-network exception propagates**. A ``ValueError`` (= bug
      in upstream code) must NOT be swallowed by the retry shield —
      catching everything would mask logic errors and hide regressions.

A ``time.sleep`` monkeypatch records the requested durations into a
list so the test asserts the sequence without sitting on real wall
clock. The same pattern is used by tests/test_bus_cli.py for the
existing poll-interval contract.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import commands  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs and fixtures
# ---------------------------------------------------------------------------

class _ScriptedApiClient:
    """ApiClient stub whose ``list_unread_bus_events`` plays a script.

    The ``script`` list is consumed left-to-right, one entry per call:

      * an exception instance → it is raised
      * a list[dict] of events → returned as the unread batch

    Tests build the script to simulate any sequence of network failure
    + recovery they need to assert on.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.cursors: dict[str, str] = {}

    def list_unread_bus_events(self, project_id, recipient_id, *,
                               channel="", limit=100):
        self.calls += 1
        if not self.script:
            # Defensive: prevent runaway loops. If a test forgets to
            # cap the script, raise a clear error rather than spin.
            raise RuntimeError("scripted client exhausted")
        item = self.script.pop(0)
        # NOTE: BaseException covers KeyboardInterrupt / SystemExit too,
        # not just Exception subclasses (= those inherit from BaseException
        # directly so a narrow ``Exception`` check would miss them).
        if isinstance(item, BaseException):
            raise item
        return item

    def advance_bus_cursor(self, project_id, recipient_id, last_seen_at):
        self.cursors[recipient_id] = last_seen_at
        return {"last_seen_at": last_seen_at}


def _clear_bus_env(monkeypatch):
    """Match the housekeeping done in test_bus_cli._clear_bus_env."""
    for key in ("BEACON_BUS_CHANNEL", "BEACON_BUS_ONCE", "BEACON_BUS_AUTO_ACK",
                "BEACON_BUS_INTERVAL", "BEACON_BUS_RECIPIENT"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def listen_env(monkeypatch):
    """Set up env vars + sleep capture for cmd_bus_listen testing.

    Returns a dict with ``sleeps`` (list of recorded sleep durations,
    in order). ``time.sleep`` is patched at the ``commands.time``
    binding (imported inside the function) so we trap the exact
    sleeps the listen loop emits.
    """
    _clear_bus_env(monkeypatch)
    monkeypatch.setenv("BEACON_BUS_RECIPIENT", "R1")
    # Tiny interval so the per-iteration trailing sleep doesn't dominate
    # the recorded sequence we want to assert on.
    monkeypatch.setenv("BEACON_BUS_INTERVAL", "0.25")
    monkeypatch.setenv("BEACON_BUS_ONCE", "1")

    sleeps: list[float] = []

    def _record_sleep(seconds):
        sleeps.append(seconds)
        # Do NOT actually sleep — tests must run fast.

    # The listen function does ``import time`` inside its body, so the
    # ``time`` module attribute we need to patch is the module itself.
    import time as _time
    monkeypatch.setattr(_time, "sleep", _record_sleep)

    return {"sleeps": sleeps}


# ---------------------------------------------------------------------------
# (a) single transient error → recovers next iteration
# ---------------------------------------------------------------------------

def test_single_url_error_recovers_and_streams_event(
    monkeypatch, capsys, listen_env
):
    """One URLError on poll #1, real event on poll #2 → event reaches
    stdout, cursor (if auto-ack) advances. Before the fix the URLError
    bubbled out and the process exited mid-poll."""
    monkeypatch.setenv("BEACON_BUS_AUTO_ACK", "1")
    event = {
        "event_id": "ev-recover",
        "channel": "dm",
        "sender_session_id": "S",
        "payload": {"text": "hello"},
        "delivery": "propose-to-ai",
        "created_at": "2026-06-11T02:00:00.000000Z",
    }
    stub = _ScriptedApiClient([
        urllib.error.URLError("ssl handshake timeout"),
        [event],
    ])
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    # Avoid invoking sidecar lookup (= would call into the real client and
    # explode in this stub). The pending-DM banner path is tested in
    # test_bus_listen_dm_banner.py — here we just need the network shield.
    monkeypatch.setattr(commands, "_fetch_pending_dm_lookup", lambda c, p: {})

    commands.cmd_bus_listen()

    out_lines = capsys.readouterr().out.strip().splitlines()
    assert len(out_lines) == 1
    parsed = json.loads(out_lines[0])
    assert parsed["event_id"] == "ev-recover"
    # Cursor advanced via auto-ack on the successful round-trip.
    assert stub.cursors.get("R1") == "2026-06-11T02:00:00.000000Z"
    # The retry backoff (1s) was emitted as the first recorded sleep.
    assert listen_env["sleeps"][0] == 1


# ---------------------------------------------------------------------------
# (b) persistent errors → exponential backoff sequence
# ---------------------------------------------------------------------------

def test_persistent_errors_grow_backoff_and_reset_on_success(
    monkeypatch, capsys, listen_env
):
    """Five transient errors in a row should produce 1, 2, 4, 8, 16 sleeps
    (= the documented exponential schedule), followed by a reset to 1 on
    the next successful round-trip. The cap (30) is asserted in the
    separate cap test below to keep this assertion focused."""
    errs = [
        ssl.SSLError("handshake timeout"),
        urllib.error.URLError("temporary failure in name resolution"),
        TimeoutError("read timeout"),
        socket.timeout("recv timeout"),
        urllib.error.URLError("connection reset by peer"),
    ]
    event = {
        "event_id": "ev-after-storm",
        "channel": "dm",
        "sender_session_id": "S",
        "payload": {},
        "delivery": "propose-to-ai",
        "created_at": "2026-06-11T02:01:00.000000Z",
    }
    # 5 errors, then success, then another error (should backoff from 1 again).
    stub = _ScriptedApiClient(
        list(errs) + [[event], urllib.error.URLError("late blip"), [event]],
    )
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(commands, "_fetch_pending_dm_lookup", lambda c, p: {})

    # Disable --once so the loop reaches the second success. We cap via
    # the script: once the script is exhausted the stub raises RuntimeError
    # which propagates out as a normal exception (not a network type).
    monkeypatch.delenv("BEACON_BUS_ONCE", raising=False)
    monkeypatch.setenv("BEACON_BUS_AUTO_ACK", "1")

    with pytest.raises(RuntimeError, match="exhausted"):
        commands.cmd_bus_listen()

    # The recorded sleeps interleave retry-backoff sleeps with the
    # per-iteration ``interval`` sleep (0.25). Pull just the retry
    # sleeps (= the integer ones we control) for the assertion.
    retry_sleeps = [s for s in listen_env["sleeps"] if s != 0.25]
    # 5 consecutive errors → 1, 2, 4, 8, 16
    # then a success (= reset to 1)
    # then 1 more error → 1 again
    assert retry_sleeps == [1, 2, 4, 8, 16, 1]


def test_backoff_caps_at_thirty_seconds(monkeypatch, capsys, listen_env):
    """After 1 → 2 → 4 → 8 → 16, the next failure would compute 32 but
    must clamp to 30. Subsequent failures stay at 30. Without the cap a
    long outage would push the retry interval into the minutes range
    and the session would feel hung even after the network recovered."""
    # 8 consecutive errors. Expected sleeps: 1, 2, 4, 8, 16, 30, 30, 30.
    errs = [urllib.error.URLError(f"err-{i}") for i in range(8)]
    stub = _ScriptedApiClient(list(errs))
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(commands, "_fetch_pending_dm_lookup", lambda c, p: {})
    monkeypatch.delenv("BEACON_BUS_ONCE", raising=False)

    with pytest.raises(RuntimeError, match="exhausted"):
        commands.cmd_bus_listen()

    retry_sleeps = [s for s in listen_env["sleeps"] if s != 0.25]
    assert retry_sleeps == [1, 2, 4, 8, 16, 30, 30, 30]


# ---------------------------------------------------------------------------
# (c) non-network exception propagates
# ---------------------------------------------------------------------------

def test_value_error_propagates_unchanged(monkeypatch, capsys, listen_env):
    """A logic bug (ValueError) must NOT be swallowed by the retry shield.
    Catching everything would mask regressions — only the four documented
    transient-network types are caught and retried."""
    stub = _ScriptedApiClient([ValueError("unexpected payload shape")])
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(commands, "_fetch_pending_dm_lookup", lambda c, p: {})

    with pytest.raises(ValueError, match="unexpected payload shape"):
        commands.cmd_bus_listen()

    # No retry sleep was emitted (= shield did not fire on this type).
    retry_sleeps = [s for s in listen_env["sleeps"] if s != 0.25]
    assert retry_sleeps == []


def test_keyboard_interrupt_still_clean_exit(monkeypatch, capsys, listen_env):
    """``KeyboardInterrupt`` (= user Ctrl-C) must still produce a clean
    return, not be retried. The existing ``except KeyboardInterrupt``
    around the outer loop owns that behavior; this test pins that the
    new network shield does not accidentally swallow it."""
    stub = _ScriptedApiClient([KeyboardInterrupt()])
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(commands, "_fetch_pending_dm_lookup", lambda c, p: {})

    # Should return None (= clean exit), not raise.
    result = commands.cmd_bus_listen()
    assert result is None


# ---------------------------------------------------------------------------
# (d) stderr visibility
# ---------------------------------------------------------------------------

def test_transient_error_logged_to_stderr(monkeypatch, capsys, listen_env):
    """The retry path must emit a stderr line per failure so a human
    tailing the log (or a debug-mode supervisor) can see what's
    happening. The exact prefix is part of the documented contract."""
    event = {
        "event_id": "ev-after",
        "channel": "dm",
        "sender_session_id": "",
        "payload": {},
        "delivery": "propose-to-ai",
        "created_at": "2026-06-11T02:02:00.000000Z",
    }
    stub = _ScriptedApiClient([
        ssl.SSLError("handshake timeout"),
        [event],
    ])
    monkeypatch.setattr(
        commands, "_get_api_client",
        lambda: (stub, {"project_id": "proj-1"}),
    )
    monkeypatch.setattr(commands, "_fetch_pending_dm_lookup", lambda c, p: {})

    commands.cmd_bus_listen()

    err = capsys.readouterr().err
    assert "[bus listen] transient network error" in err
    assert "SSLError" in err
    assert "handshake timeout" in err
