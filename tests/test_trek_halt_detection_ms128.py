"""ms-128 方針6 (e-4367): per-target halt 機械検知.

halt is an attribute of a working Target, not a state. The server tick calls
``evaluate_target_halt`` each tick with the owner's current response
fingerprint + commit count; it compares against the values stored from the
previous tick and tags ``progress-stall`` / ``liveness-timeout`` when the
Target stops advancing. These tests pin the two detection faces, the
advance-clears-halt path, and liveness precedence — all with an injected
clock so no real time passes.
"""
from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402


def _dt(h=0, m=0):
    return datetime.datetime(2026, 7, 28, tzinfo=datetime.timezone.utc) + \
        datetime.timedelta(hours=h, minutes=m)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# --- fingerprint -----------------------------------------------------------

def test_fingerprint_stable_for_same_content():
    a = {"last_picked_choice": "continue", "last_state_summary": "on e-1",
         "last_blockers": []}
    b = {"last_picked_choice": "continue", "last_state_summary": "on e-1",
         "last_blockers": []}
    assert trek.compute_response_fingerprint(a) == \
        trek.compute_response_fingerprint(b)


def test_fingerprint_changes_with_content():
    a = {"last_picked_choice": "continue", "last_state_summary": "on e-1"}
    b = {"last_picked_choice": "continue", "last_state_summary": "on e-2"}
    assert trek.compute_response_fingerprint(a) != \
        trek.compute_response_fingerprint(b)


# --- advance clears / seeds ------------------------------------------------

def test_first_observation_seeds_baseline_no_halt():
    doc: dict = {}
    r = trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp1", current_commit_count=0,
        last_pulse_at=_iso(_dt()), now=_dt())
    assert r is None
    assert trek.get_target_halt_reason(doc, "e-1") is None
    assert doc["task_states"]["e-1"]["last_response_fingerprint"] == "fp1"


def test_fingerprint_change_counts_as_advance():
    doc: dict = {}
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0, now=_dt())
    # 5h later but the response changed → advanced, no halt.
    r = trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp2", current_commit_count=0,
        now=_dt(h=5))
    assert r is None
    assert doc["task_states"]["e-1"]["progress_last_advanced_at"] == \
        _iso(_dt(h=5))


def test_commit_increment_counts_as_advance():
    doc: dict = {}
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0, now=_dt())
    r = trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp1", current_commit_count=3,
        now=_dt(h=5))
    assert r is None


# --- stall -----------------------------------------------------------------

def test_no_advance_within_window_is_not_halt():
    doc: dict = {}
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0, now=_dt())
    # 1h later, same response + no commit → idle but under 4h threshold.
    r = trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp1", current_commit_count=0,
        now=_dt(h=1))
    assert r is None


def test_progress_stall_after_timeout():
    doc: dict = {}
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0,
                              last_pulse_at=_iso(_dt(h=5)), now=_dt())
    # 5h later, identical response + zero commit increment → progress-stall.
    r = trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp1", current_commit_count=0,
        last_pulse_at=_iso(_dt(h=5)), now=_dt(h=5))
    assert r == trek.HALT_REASON_STALL
    assert trek.get_target_halt_reason(doc, "e-1") == "progress-stall"


def test_liveness_timeout_takes_precedence():
    doc: dict = {}
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0,
                              last_pulse_at=_iso(_dt()), now=_dt())
    # last pulse was at t0; 5h later still no pulse → liveness (dead), not
    # merely stalled.
    r = trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp1", current_commit_count=0,
        last_pulse_at=_iso(_dt()), now=_dt(h=5))
    assert r == trek.HALT_REASON_LIVENESS


# --- clear -----------------------------------------------------------------

def test_advance_clears_prior_halt():
    doc: dict = {}
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0, now=_dt())
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp1",
                              current_commit_count=0, now=_dt(h=5))
    assert trek.get_target_halt_reason(doc, "e-1") == "progress-stall"
    # Executor resumes: new fingerprint → advance clears the halt tag.
    trek.evaluate_target_halt(doc, "e-1", current_fingerprint="fp2",
                              current_commit_count=0, now=_dt(h=6))
    assert trek.get_target_halt_reason(doc, "e-1") is None


def test_halt_is_attribute_not_state():
    # Tagging halt must not change the Target's state value.
    doc: dict = {"task_states": {"e-1": {"state": "working",
                                         "last_response_fingerprint": "fp1",
                                         "last_commit_count": 0,
                                         "progress_last_advanced_at": _iso(_dt())}}}
    trek.evaluate_target_halt(
        doc, "e-1", current_fingerprint="fp1", current_commit_count=0,
        now=_dt(h=5))
    assert doc["task_states"]["e-1"]["state"] == "working"
    assert doc["task_states"]["e-1"]["halt_reason"] == "progress-stall"
