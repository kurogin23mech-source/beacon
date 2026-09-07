"""ms-159 / e-6243 — the *work-unit state* projection: derive_state().

The 統合オペレーションUI reads a single small frozen set of canonical states
(running / idle / awaiting_human / blocked / terminated + unknown). These tests
pin the authority model of ``lib/bus_liveness.derive_state`` from the two SPECs
(state model ``np2fSUqpE5LSIkOqHLuK`` 判断1/4, slice ``Icb8zFtbnZZ1yXzMsLO6``
方針1/4):

  1. a FRESH self-declaration is authoritative — all 5 canonical states round-trip.
  2. ``terminated`` is terminal — authoritative even when stale / not live.
  3. a STALE non-terminal declaration ⇒ ``unknown`` (方針1 live-but-silent +
     判断4 固着 backstop), regardless of liveness.
  4. NO declaration ⇒ liveness FALLBACK: live ⇒ unknown (never assume running),
     not live ⇒ terminated (gone).
  5. an undatable declaration is treated as stale (safe side ⇒ unknown).
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import bus_liveness  # noqa: E402

WINDOW = 300  # freshness window in seconds


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fresh(now):
    """A declaration timestamp inside the freshness window."""
    return _iso(now - datetime.timedelta(seconds=5))


def _stale(now):
    """A declaration timestamp older than the freshness window."""
    return _iso(now - datetime.timedelta(seconds=WINDOW + 300))


# ===========================================================================
# 1. Fresh self-declaration is authoritative — all 5 canonical states.
# ===========================================================================

class TestFreshDeclarationAuthoritative:
    @pytest.mark.parametrize("state", [
        bus_liveness.STATE_RUNNING,
        bus_liveness.STATE_IDLE,
        bus_liveness.STATE_AWAITING_HUMAN,
        bus_liveness.STATE_BLOCKED,
    ])
    def test_fresh_non_terminal_declaration_round_trips(self, state):
        now = _now()
        # live value must not matter for a fresh declaration.
        assert bus_liveness.derive_state(state, _fresh(now), True, now, WINDOW) == state
        assert bus_liveness.derive_state(state, _fresh(now), False, now, WINDOW) == state

    def test_fresh_declaration_wins_even_when_not_live(self):
        # A liveness stamp can lag behind a just-arrived declaration; the fresh
        # self-report is authoritative (方針1), so don't downgrade to unknown.
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_AWAITING_HUMAN, _fresh(now), False, now, WINDOW
        ) == bus_liveness.STATE_AWAITING_HUMAN


# ===========================================================================
# 2. terminated is terminal — authoritative regardless of age / liveness.
# ===========================================================================

class TestTerminatedIsTerminal:
    def test_terminated_fresh(self):
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_TERMINATED, _fresh(now), True, now, WINDOW
        ) == bus_liveness.STATE_TERMINATED

    def test_terminated_stays_terminated_when_stale(self):
        # A SessionEnd'd session legitimately stops emitting; staleness must NOT
        # flip it to unknown.
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_TERMINATED, _stale(now), False, now, WINDOW
        ) == bus_liveness.STATE_TERMINATED

    def test_terminated_with_no_timestamp(self):
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_TERMINATED, None, True, now, WINDOW
        ) == bus_liveness.STATE_TERMINATED


# ===========================================================================
# 3. Staleness is a POST-DEATH grace window, not a general freshness clock.
#    While LIVE, the heartbeat re-affirms the marker ⇒ trust it regardless of
#    age (this keeps a long-waiting awaiting_human visible). Only once NOT LIVE
#    does a stale declaration flip to unknown (判断4 固着 backstop).
# ===========================================================================

class TestStalenessOnlyAppliesWhenNotLive:
    @pytest.mark.parametrize("state", [
        bus_liveness.STATE_RUNNING,
        bus_liveness.STATE_IDLE,
        bus_liveness.STATE_AWAITING_HUMAN,
        bus_liveness.STATE_BLOCKED,
    ])
    def test_stale_declaration_is_trusted_while_live(self, state):
        # The correction: a live session whose declaration is old is STILL
        # trusted — the heartbeat proves the state is current. Uniform staleness
        # would wrongly hide it.
        now = _now()
        assert bus_liveness.derive_state(state, _stale(now), True, now, WINDOW) == state

    def test_long_waiting_awaiting_human_stays_visible_while_live(self):
        # The MS-critical case: a session parked in awaiting_human for hours
        # (declared_at frozen — no hook fires while waiting) must remain
        # awaiting_human so the attention面 keeps it at the top, not unknown.
        now = _now()
        hours_ago = _iso(now - datetime.timedelta(hours=3))
        assert bus_liveness.derive_state(
            bus_liveness.STATE_AWAITING_HUMAN, hours_ago, True, now, WINDOW
        ) == bus_liveness.STATE_AWAITING_HUMAN

    def test_stale_awaiting_human_not_live_is_unknown_not_frozen(self):
        # 判断4 固着 backstop: once the heartbeat is ALSO gone, a dead session
        # frozen in awaiting_human must NOT keep nagging — it becomes unknown.
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_AWAITING_HUMAN, _stale(now), False, now, WINDOW
        ) == bus_liveness.STATE_UNKNOWN

    def test_fresh_declaration_not_live_is_trusted_grace_window(self):
        # A just-crashed session (fresh declaration, transport just dropped) is
        # trusted through the grace window — indistinguishable from a real pause.
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_RUNNING, _fresh(now), False, now, WINDOW
        ) == bus_liveness.STATE_RUNNING


# ===========================================================================
# 4. No declaration ⇒ liveness fallback (live is load-bearing here).
# ===========================================================================

class TestNoDeclarationLivenessFallback:
    @pytest.mark.parametrize("declared", [None, "", "  ", "made-up-native-state"])
    def test_no_or_unknown_declaration_live_is_unknown(self, declared):
        # 判断4: never silently assume running for an unstated live session.
        now = _now()
        assert bus_liveness.derive_state(declared, None, True, now, WINDOW) \
            == bus_liveness.STATE_UNKNOWN

    @pytest.mark.parametrize("declared", [None, "", "made-up-native-state"])
    def test_no_declaration_not_live_is_terminated(self, declared):
        # No transport and nothing ever declared ⇒ the coarse fallback is gone.
        now = _now()
        assert bus_liveness.derive_state(declared, None, False, now, WINDOW) \
            == bus_liveness.STATE_TERMINATED

    def test_unknown_is_never_declarable(self):
        # 判断4: "unknown" arriving as a declaration is not a canonical
        # self-report — it falls through to the liveness fallback like any
        # unrecognized token, it is NOT echoed back as an authoritative state.
        now = _now()
        assert bus_liveness.STATE_UNKNOWN not in bus_liveness.DECLARABLE_STATES
        # live + declared "unknown" ⇒ fallback ⇒ unknown (by liveness, not by echo)
        assert bus_liveness.derive_state(
            bus_liveness.STATE_UNKNOWN, _fresh(now), True, now, WINDOW
        ) == bus_liveness.STATE_UNKNOWN
        # not-live + declared "unknown" ⇒ fallback ⇒ terminated (proves it was
        # NOT echoed: an echo would have returned "unknown").
        assert bus_liveness.derive_state(
            bus_liveness.STATE_UNKNOWN, _fresh(now), False, now, WINDOW
        ) == bus_liveness.STATE_TERMINATED


# ===========================================================================
# 5. Undatable declaration ⇒ treated as stale — but only decides once NOT LIVE
#    (while live the declaration is trusted regardless of timestamp).
# ===========================================================================

class TestUndatableDeclaration:
    def test_undatable_declaration_is_trusted_while_live(self):
        # Live ⇒ heartbeat re-affirms ⇒ trust the state even with no timestamp.
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_RUNNING, None, True, now, WINDOW
        ) == bus_liveness.STATE_RUNNING

    def test_declaration_without_timestamp_is_stale_when_not_live(self):
        # Not live + a state we cannot date ⇒ can't confirm ⇒ unknown.
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_RUNNING, None, False, now, WINDOW
        ) == bus_liveness.STATE_UNKNOWN

    def test_unparseable_timestamp_is_stale_when_not_live(self):
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_RUNNING, "not-a-date", False, now, WINDOW
        ) == bus_liveness.STATE_UNKNOWN

    def test_staleness_helper_boundaries(self):
        now = _now()
        assert bus_liveness._declaration_is_stale(_stale(now), now, WINDOW) is True
        assert bus_liveness._declaration_is_stale(_fresh(now), now, WINDOW) is False
        assert bus_liveness._declaration_is_stale(None, now, WINDOW) is True
        assert bus_liveness._declaration_is_stale("garbage", now, WINDOW) is True
