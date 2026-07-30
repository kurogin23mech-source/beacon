"""One guarded transition mechanism for lifecycle target entities (ms-120 e-3908).

AX 原則 6 (illegal states unrepresentable): operation / acquisition used to set
status with only an enum check, so any state jumped to any state — including
backward (done→todo) and reopening a terminal record. Now every transition
flows through core.validate_lifecycle_transition (generalized from the proven
trek.validate_transition), which is MONOTONIC-FORWARD: forward moves and skips
are allowed, backward / reopen-terminal are blocked (recovery via purge).
`opportunity` has no table and stays unguarded by design (SPEC §5 human master).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import core  # noqa: E402
import sales_entities as se  # noqa: E402


# --- single source of truth: the two enumerations must not drift (e-4507) ----

def test_acquisition_statuses_match_transition_table():
    """`sales_entities.ACQUISITION_STATUSES` (validation) and the keys of
    `core.LIFECYCLE_TRANSITIONS["acquisition"]` (transition guard) both enumerate
    the valid acquisition states. They are defined in two files; a future change
    that adds/removes a state in one but not the other would split validation from
    the guard (accepted-but-untransitionable, or vice versa). Pin them together so
    that drift fails here loudly rather than at runtime (maintainability review
    e-4507, §単一真実源)."""
    assert set(se.ACQUISITION_STATUSES) == set(core.LIFECYCLE_TRANSITIONS["acquisition"])


# --- unit: the shared guard -------------------------------------------------

@pytest.mark.parametrize("kind,frm,to", [
    ("operation", "todo", "in_progress"),
    ("operation", "todo", "open"),        # forward skip allowed
    ("operation", "in_progress", "open"),
    ("operation", "open", "closed"),
    ("acquisition", "todo", "in_progress"),
    ("acquisition", "todo", "done"),      # forward skip allowed
    ("acquisition", "in_progress", "done"),  # ms-132 e-4507: observing 除去後
])
def test_legal_forward_transitions_pass(kind, frm, to):
    core.validate_lifecycle_transition(kind, frm, to)  # must not raise


@pytest.mark.parametrize("kind,frm,to", [
    ("operation", "closed", "open"),        # reopen terminal
    ("operation", "open", "todo"),          # backward
    ("acquisition", "done", "todo"),        # backward from terminal
    ("acquisition", "in_progress", "todo"), # backward (ms-132 e-4507)
])
def test_illegal_transitions_raise_with_recovery_hint(kind, frm, to):
    with pytest.raises(ValueError) as ei:
        core.validate_lifecycle_transition(kind, frm, to)
    msg = str(ei.value)
    assert "illegal" in msg and kind in msg
    assert "purge" in msg  # recovery path is surfaced (原則3)


def test_same_state_is_a_noop():
    core.validate_lifecycle_transition("operation", "open", "open")  # no raise


def test_opportunity_is_unguarded_by_design():
    # No table for opportunity → any phase move is allowed (human master, §5).
    core.validate_lifecycle_transition("opportunity", "anything", "whatever")


# --- integration: operation_set_status enforces the guard -------------------

def test_operation_set_status_blocks_reopen():
    data = {"operations": [{"id": "op-1", "title": "x", "status": "closed",
                            "meta": {}}]}
    with pytest.raises(ValueError) as ei:
        core.operation_set_status(data, "op-1", "open")
    assert "illegal operation transition" in str(ei.value)


def test_operation_set_status_allows_legal_forward():
    data = {"operations": [{"id": "op-1", "title": "x", "status": "todo",
                            "meta": {}}]}
    core.operation_set_status(data, "op-1", "open")
    assert data["operations"][0]["status"] == "open"


# --- operation_close is on the SAME guard (e-3908: no direct-write backdoor) --

def test_operation_close_routes_through_the_guard(monkeypatch):
    """The `close` verb must flow through validate_lifecycle_transition too, not
    write status directly — else it is a guard-bypassing backdoor. We spy on the
    guard and assert operation_close consults it (closing is always legal, so
    behaviour is unchanged; this pins the *mechanism*, not the outcome)."""
    calls = []
    real = core.validate_lifecycle_transition

    def spy(kind, frm, to):
        calls.append((kind, frm, to))
        return real(kind, frm, to)

    monkeypatch.setattr(core, "validate_lifecycle_transition", spy)
    data = {"operations": [{"id": "op-1", "title": "x", "status": "open",
                            "meta": {}}]}
    core.operation_close(data, "op-1")
    assert ("operation", "open", "closed") in calls
    assert data["operations"][0]["status"] == "closed"


def test_operation_close_already_closed_rejects():
    data = {"operations": [{"id": "op-1", "title": "x", "status": "closed",
                            "meta": {}}]}
    with pytest.raises(ValueError):
        core.operation_close(data, "op-1")
