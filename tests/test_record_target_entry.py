"""Unit tests for occupation.record_target_entry — the class-abstraction (L2)
side-effect recording seam (ms-134 e-4720, hardened per maintainability review
2026-08-02 Maint#2/#4a).

Pure-function tests (no I/O, no CLI): they hand ``record_target_entry`` a plain
dict and assert the dispatch + no-op behaviour directly, so a future edit to the
dispatch logic gets fast feedback without spinning up a subprocess CLI.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402


def _dev(active=True):
    ms = {"id": "ms-1", "status": "in_progress" if active else "todo",
          "label": "MVP", "entries": []}
    return {"profession": "dev", "milestones": [ms]}


# --- development: unchanged behaviour ---------------------------------------

def test_empty_target_records_onto_active_milestone():
    data = _dev()
    rec = occupation.record_target_entry(data, "", description="doc add: x",
                                         source="auto", date="2026-08-02",
                                         revision_id="d1")
    assert rec["recorded"] is True
    assert rec["target"] == "ms-1"
    assert len(data["milestones"][0]["entries"]) == 1


def test_explicit_milestone_records_onto_that_milestone():
    data = {"profession": "dev", "milestones": [
        {"id": "ms-1", "status": "done", "label": "A", "entries": []},
        {"id": "ms-2", "status": "todo", "label": "B", "entries": []}]}
    rec = occupation.record_target_entry(data, "ms-2", description="x",
                                         source="auto", date="2026-08-02")
    assert rec["recorded"] is True and rec["target"] == "ms-2"
    assert len(data["milestones"][1]["entries"]) == 1


def test_operation_records_onto_operation_entries():
    data = {"profession": "dev", "milestones": [],
            "operations": [{"id": "op-1", "entries": []}]}
    rec = occupation.record_target_entry(data, "op-1", description="x",
                                         source="auto", date="2026-08-02")
    assert rec["recorded"] is True
    assert len(data["operations"][0]["entries"]) == 1


# --- the milestone-less-project no-op (the e-4710 fix) ----------------------

def test_empty_target_no_milestone_is_noop_not_error():
    data = {"profession": "sales", "milestones": [], "opportunities": []}
    rec = occupation.record_target_entry(data, "", description="doc add: x",
                                         source="auto", date="2026-08-02")
    assert rec["recorded"] is False
    assert rec["reason"] == "no-milestone"


def test_sales_target_is_noop():
    data = {"profession": "sales", "milestones": [],
            "opportunities": [{"id": "opp-1", "label": "X"}]}
    rec = occupation.record_target_entry(data, "opp-1", description="x",
                                         source="auto", date="2026-08-02")
    assert rec["recorded"] is False
    assert rec["reason"] == "opportunity-no-changelog"


# --- unknown / descriptor prefix must NOT fall through to a milestone -------

def test_unknown_prefix_target_never_records_onto_active_milestone():
    """A doc linked to a descriptor-defined / unrecognised target (e.g. ``ct-9``)
    must NO-OP even in a project that HAS an active milestone — it must never
    silently record onto a *different* target (Maint#2)."""
    data = _dev()  # has an active milestone
    rec = occupation.record_target_entry(data, "ct-9", description="x",
                                         source="auto", date="2026-08-02")
    assert rec["recorded"] is False
    assert rec["reason"] == "unknown-no-changelog"
    assert data["milestones"][0]["entries"] == []  # milestone untouched


def test_trek_target_is_noop():
    data = _dev()
    rec = occupation.record_target_entry(data, "tk-abc", description="x",
                                         source="auto", date="2026-08-02")
    assert rec["recorded"] is False
    assert data["milestones"][0]["entries"] == []


# --- real user errors still surface (not swallowed) ------------------------

def test_bad_explicit_milestone_id_raises():
    data = _dev()
    import pytest
    with pytest.raises(ValueError):
        occupation.record_target_entry(data, "ms-999", description="x",
                                       source="auto", date="2026-08-02")
