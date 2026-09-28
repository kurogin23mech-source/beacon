"""ms-177 / e-6640 — 中断 (``interrupted``) state: the contract, and a test-the-test.

ms-177 splits "stopped" into 正常終了 (a human ran ``beacon session end``) and
中断 (the terminal was closed / an API error killed it / it hit the context
limit). The derivation lives in ``lib/bus_liveness.derive_state``; these tests
pin it — AND pin that the pin actually bites.

WHY a test-the-test (SPEC 受入条件7): the change is one branch returning a
different constant. A test asserting "this input yields interrupted" is easy to
write in a way that would ALSO pass against the old behaviour (e.g. asserting
only ``!= running``, or asserting membership in a set that holds both states).
Such a test reads like a guard and guards nothing. So the pinning assertions are
written ONCE as :func:`assert_interrupted_contract`, and
:class:`TestTheTestBitesOnRegression` feeds that same helper deliberately
regressed implementations and requires it to FAIL. If someone reverts the branch
to ``unknown`` (or folds 中断 into ``terminated``), both the contract test and
this proof-of-bite are the ones that go red.

The regressed implementations are built by WRAPPING the real function rather than
by copying its body: a copy would rot silently as ``derive_state`` evolves, and a
rotten "old implementation" would make this proof meaningless.
"""

from __future__ import annotations

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import attention  # noqa: E402
import bus_liveness  # noqa: E402

GRACE = 300  # post-death grace window, in seconds


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fresh(now):
    """A declaration stamped inside the grace window (a just-now death)."""
    return _iso(now - datetime.timedelta(seconds=5))


def _stale(now):
    """A declaration older than the grace window (death we can no longer doubt)."""
    return _iso(now - datetime.timedelta(seconds=GRACE + 600))


# The non-terminal states a real working session declares. Each of these, once
# the transport is gone and the declaration has aged out, IS an accidental death.
_WORKING_DECLARATIONS = sorted(
    bus_liveness.DECLARABLE_STATES - {bus_liveness.STATE_TERMINATED})


def assert_interrupted_contract(derive):
    """Assert the FULL ms-177 contract against ``derive`` (a derive_state-shaped
    callable). Raises ``AssertionError`` on any violation.

    Shared by the real contract test and by the test-the-test, so both judge the
    behaviour by the exact same yardstick — the only way "reverting the branch
    turns this red" is a claim backed by evidence rather than by assertion.
    """
    now = _now()

    # 1. THE NEW BEHAVIOUR (SPEC AC1): worked, transport gone, declaration aged
    #    out ⇒ 中断. Asserted as EQUALITY to the interrupted constant — not
    #    membership in a set, not inequality to something else, because those
    #    weaker forms are exactly what would also pass on the old `unknown`.
    for declared in _WORKING_DECLARATIONS:
        got = derive(declared, _stale(now), False, now, GRACE)
        assert got == bus_liveness.STATE_INTERRUPTED, (
            f"not-live + stale {declared!r} must derive 中断 "
            f"({bus_liveness.STATE_INTERRUPTED!r}), got {got!r}")

    # 2. An undatable / unparseable declaration counts as aged out (we cannot
    #    confirm it), so it is 中断 too — not a silent pass-through.
    for bad_stamp in (None, "", "not-a-date"):
        got = derive(bus_liveness.STATE_RUNNING, bad_stamp, False, now, GRACE)
        assert got == bus_liveness.STATE_INTERRUPTED, (
            f"not-live + undatable declaration ({bad_stamp!r}) must derive 中断, "
            f"got {got!r}")

    # 3. NO REGRESSION — grace window kept (SPEC AC2 / 方針3): a blip or a
    #    just-now death still reads as the declared state, so a reconnect is
    #    never mislabelled 中断.
    for declared in _WORKING_DECLARATIONS:
        got = derive(declared, _fresh(now), False, now, GRACE)
        assert got == declared, (
            f"not-live + FRESH {declared!r} must stay {declared!r} "
            f"(grace window), got {got!r}")

    # 4. NO REGRESSION — a clean exit stays 正常終了 (SPEC AC3). This is the
    #    whole point of the split: 中断 must never swallow a deliberate ending.
    for stamp in (_fresh(now), _stale(now), None):
        got = derive(bus_liveness.STATE_TERMINATED, stamp, False, now, GRACE)
        assert got == bus_liveness.STATE_TERMINATED, (
            f"declared terminated (stamp={stamp!r}) must stay 正常終了, got {got!r}")

    # 5. NO REGRESSION — ``unknown`` keeps its narrowed meaning (SPEC AC4):
    #    live but never said what it is doing. A live session is never 中断.
    for declared in (None, "", "made-up-native-state"):
        got = derive(declared, None, True, now, GRACE)
        assert got == bus_liveness.STATE_UNKNOWN, (
            f"live + undeclared ({declared!r}) must stay unknown, got {got!r}")
    for declared in _WORKING_DECLARATIONS:
        got = derive(declared, _stale(now), True, now, GRACE)
        assert got == declared, (
            f"LIVE + stale {declared!r} must stay {declared!r} (the heartbeat "
            f"re-affirms it), got {got!r}")

    # 6. NO REGRESSION — never declared anything and gone ⇒ terminated
    #    (SPEC 方針4 keeps this a mixed bucket on purpose).
    got = derive(None, None, False, now, GRACE)
    assert got == bus_liveness.STATE_TERMINATED, (
        f"never-declared + not live must stay terminated, got {got!r}")


# ---------------------------------------------------------------------------
# Deliberately regressed implementations, expressed as TRANSFORMS of the real
# one (no copied body ⇒ they cannot rot out of step with derive_state).
# ---------------------------------------------------------------------------

def _regressed_to_unknown(*args, **kwargs):
    """The pre-ms-177 behaviour: 中断 folded back into ``unknown``."""
    got = bus_liveness.derive_state(*args, **kwargs)
    return bus_liveness.STATE_UNKNOWN if got == bus_liveness.STATE_INTERRUPTED else got


def _regressed_to_terminated(*args, **kwargs):
    """The other plausible wrong turn: an accidental death filed as a clean
    exit, which is the exact confusion ms-177 exists to end."""
    got = bus_liveness.derive_state(*args, **kwargs)
    return bus_liveness.STATE_TERMINATED if got == bus_liveness.STATE_INTERRUPTED else got


def _regressed_grace_dropped(declared_state, declared_at, live, now, grace):
    """A third wrong turn: 中断 declared the instant transport drops, losing the
    grace window — a reconnect would flash as an interruption."""
    got = bus_liveness.derive_state(declared_state, declared_at, live, now, grace)
    if (not live and declared_state in _WORKING_DECLARATIONS
            and got == declared_state):
        return bus_liveness.STATE_INTERRUPTED
    return got


class TestInterruptedContract:
    def test_real_derivation_satisfies_the_contract(self):
        assert_interrupted_contract(bus_liveness.derive_state)


class TestTheTestBitesOnRegression:
    """Proof that the contract above is a guard, not decoration (SPEC AC7)."""

    # Each case carries the probe that shows its regression is REAL (a wrapper
    # that silently changed nothing would make the "must fail" half vacuous):
    # the input where it diverges from the real derivation, and what it wrongly
    # returns there.
    @pytest.mark.parametrize("regressed,probe_stamp,wrong_verdict,label", [
        (_regressed_to_unknown, _stale, bus_liveness.STATE_UNKNOWN,
         "中断 → unknown (the pre-ms-177 branch)"),
        (_regressed_to_terminated, _stale, bus_liveness.STATE_TERMINATED,
         "中断 → terminated (accidental death read as clean exit)"),
        (_regressed_grace_dropped, _fresh, bus_liveness.STATE_INTERRUPTED,
         "grace window dropped (a blip flashes as 中断)"),
    ])
    def test_contract_fails_on_regression(self, regressed, probe_stamp,
                                          wrong_verdict, label):
        # a. The contract bites: the same assertions that pass on the real
        #    derivation must raise on the regressed one.
        with pytest.raises(AssertionError):
            assert_interrupted_contract(regressed)
        # b. The regression is real: at its probe input it returns the wrong
        #    verdict, while the real derivation does not.
        now = _now()
        args = (bus_liveness.STATE_RUNNING, probe_stamp(now), False, now, GRACE)
        assert regressed(*args) == wrong_verdict, (
            f"{label}: the regressed impl should return {wrong_verdict!r} at its "
            f"probe input, got {regressed(*args)!r} — the wrapper is a no-op and "
            f"this proof is vacuous")
        assert bus_liveness.derive_state(*args) != wrong_verdict, (
            f"{label}: the REAL derivation also returns {wrong_verdict!r} here, "
            f"so this probe does not distinguish regression from correct behaviour")

    def test_an_unchanged_passthrough_does_NOT_fail(self):
        # Control: a wrapper that changes nothing must PASS. Without this, the
        # test above could be green merely because the helper always throws.
        assert_interrupted_contract(
            lambda *a, **k: bus_liveness.derive_state(*a, **k))


class TestInterruptedIsWiredIntoTheStateVocabulary:
    def test_interrupted_is_in_the_canonical_range(self):
        assert bus_liveness.STATE_INTERRUPTED in bus_liveness.ALL_STATES

    def test_interrupted_is_not_declarable(self):
        # A session killed by a closed terminal cannot file a report on its way
        # out, so 中断 is server-raised only (like unknown). A session claiming
        # it must NOT be echoed back as authoritative.
        assert bus_liveness.STATE_INTERRUPTED not in bus_liveness.DECLARABLE_STATES
        now = _now()
        assert bus_liveness.derive_state(
            bus_liveness.STATE_INTERRUPTED, _fresh(now), True, now, GRACE
        ) == bus_liveness.STATE_UNKNOWN

    def test_roster_ranks_interrupted_above_a_clean_exit(self):
        # An accidental death deserves the human's eye before a clean exit does;
        # that is the whole reason for splitting the bucket.
        order = attention._ROSTER_STATE_ORDER
        assert order[bus_liveness.STATE_INTERRUPTED] < order[bus_liveness.STATE_TERMINATED]
