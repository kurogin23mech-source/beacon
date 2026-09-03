"""ms-164 実装順序1: ``occupation.resolve_worked_targets`` — the single,
occupation-generic, MULTI-attribution rule that every forward-record write
(session log / note / push / deploy / incident) routes through (SPEC 方針3).

Contrast with the older single-target ``session_log.resolve_worked_target``
(tested in ``test_session_log_worked_target_e5550.py``): that one folds a
cross-target session to ``"ambiguous"`` → project-wide and lets fork.json DROP
the entries. This new rule keeps EVERY worked Target (multi) and UNIONS the fork
Target with whatever else the entries touched.

Pure-function tests over ``data`` + explicit inputs — no CLI, no cloud, no
filesystem (the caller passes ``fork_target_id``).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402


def _data(*targets, collection="milestones"):
    """Build a minimal project dict from (id, status) tuples."""
    return {collection: [{"id": tid, "status": st} for tid, st in targets]}


# --- inferred (no fork): multi is first-class, NOT collapsed to ambiguous -----

def test_inferred_single_target():
    data = _data(("ms-5", "in_progress"))
    got = occupation.resolve_worked_targets(data, entry_target_ids=["ms-5"])
    assert got == {"target_ids": ["ms-5"], "target_source": "inferred"}


def test_inferred_multi_keeps_all_in_first_seen_order():
    """The behavioural heart of ms-164: a session that touched several Targets
    attributes to ALL of them, rather than the old ``ambiguous`` → empty."""
    data = _data(("ms-1", "done"), ("ms-2", "in_progress"), ("ms-3", "todo"))
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=["ms-2", "ms-1", "ms-2"])
    assert got == {"target_ids": ["ms-2", "ms-1"], "target_source": "inferred"}


# --- fork: structural intent UNIONED with entries (not raced) ----------------

def test_fork_unions_with_entries_fork_first():
    data = _data(("ms-153", "in_progress"), ("ms-99", "todo"))
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=["ms-99"], fork_target_id="ms-153")
    # fork Target leads, then the other Target the entries touched — both kept
    # (the OLD single resolver would have dropped ms-99 entirely).
    assert got == {"target_ids": ["ms-153", "ms-99"], "target_source": "fork"}


def test_fork_without_entries_yields_just_the_fork_target():
    data = _data(("ms-153", "in_progress"))
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=[], fork_target_id="ms-153")
    assert got == {"target_ids": ["ms-153"], "target_source": "fork"}


def test_fork_target_deduped_when_also_in_entries():
    data = _data(("ms-7", "in_progress"))
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=["ms-7"], fork_target_id="ms-7")
    assert got == {"target_ids": ["ms-7"], "target_source": "fork"}


def test_stale_fork_target_falls_through_to_inference():
    """fork.json names a Target that no longer exists here → don't stamp a bogus
    id; recover via the session's own entries (matches cmd_log leniency)."""
    data = _data(("ms-7", "in_progress"))
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=["ms-7"], fork_target_id="ms-DELETED")
    assert got == {"target_ids": ["ms-7"], "target_source": "inferred"}


def test_stale_fork_and_no_entries_falls_through_to_active():
    data = _data(("ms-7", "in_progress"))
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=[], fork_target_id="ms-DELETED")
    assert got == {"target_ids": ["ms-7"], "target_source": "active"}


# --- active fallback: "空なら active target にフォールバック" ------------------

def test_active_fallback_single():
    data = _data(("ms-1", "done"), ("ms-2", "in_progress"))
    got = occupation.resolve_worked_targets(data, entry_target_ids=[])
    assert got == {"target_ids": ["ms-2"], "target_source": "active"}


def test_active_fallback_multi_active():
    data = _data(("ms-2", "in_progress"), ("ms-4", "in_progress"))
    got = occupation.resolve_worked_targets(data, entry_target_ids=[])
    assert got == {"target_ids": ["ms-2", "ms-4"], "target_source": "active"}


def test_none_when_nothing_worked_and_nothing_active():
    data = _data(("ms-1", "done"), ("ms-2", "observing"))
    got = occupation.resolve_worked_targets(data, entry_target_ids=[])
    assert got == {"target_ids": [], "target_source": "none"}


def test_empty_project():
    got = occupation.resolve_worked_targets({}, entry_target_ids=[])
    assert got == {"target_ids": [], "target_source": "none"}


# --- occupation-generic: works over a non-milestone Target collection ---------

def test_generic_over_opportunity_collection():
    """The rule reads Targets via ``iter_target_records`` (manifest-driven), so a
    sales project's Opportunities resolve the same way — no ``data['milestones']``
    hardcode. Active fallback must find the in_progress Opportunity."""
    data = {"opportunities": [
        {"id": "opp-1", "status": "in_progress"},
        {"id": "opp-2", "status": "done"},
    ]}
    got = occupation.resolve_worked_targets(data, entry_target_ids=[])
    assert got == {"target_ids": ["opp-1"], "target_source": "active"}


def test_generic_opportunity_fork_union():
    data = {"opportunities": [
        {"id": "opp-1", "status": "in_progress"},
        {"id": "opp-9", "status": "todo"},
    ]}
    got = occupation.resolve_worked_targets(
        data, entry_target_ids=["opp-9"], fork_target_id="opp-1")
    assert got == {"target_ids": ["opp-1", "opp-9"], "target_source": "fork"}
