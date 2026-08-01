"""Task↔SPEC last-written-intent tie-breaker for the attainment gate (ms-119 / e-4597).

When a gated backlog task contradicts a re-scoped SPEC, the disposition needs a
principled tie-breaker: the LAST WRITTEN intent wins. These tests pin the PURE logic —
the timestamp comparison and the way it is surfaced in the disposition-required block —
and the invariants that keep it honest: it is EVIDENCE for a human/judge decision, never
a blind auto-supersede, and it degrades to no-hint when a timestamp is missing.

Motivating incident (ms-128 AC4): a stale de-scoped task nearly blocked attainment
because the judge weighed the old task criterion over the newer SPEC intent.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import transition_approval as ta


TASK = "2026-07-27T00:00:00Z"          # filed first
SPEC_NEWER = "2026-07-28T12:00:00.500"  # SPEC re-scoped a day later (no tz, microseconds)
SPEC_OLDER = "2026-07-20T00:00:00Z"     # SPEC predates the task


# --- pure tie-breaker ------------------------------------------------------

def test_spec_newer_leans_superseded():
    tb = ta.authored_timestamp_tiebreaker(TASK, SPEC_NEWER)
    assert tb is not None
    assert tb["spec_newer"] is True
    assert tb["lean"] == "superseded"


def test_task_newer_leans_task_valid():
    tb = ta.authored_timestamp_tiebreaker(TASK, SPEC_OLDER)
    assert tb["spec_newer"] is False
    assert tb["lean"] == "task-valid"  # symmetric — the task is NOT auto-superseded


def test_missing_or_unparseable_timestamp_yields_no_hint():
    assert ta.authored_timestamp_tiebreaker(None, SPEC_NEWER) is None
    assert ta.authored_timestamp_tiebreaker(TASK, "") is None
    assert ta.authored_timestamp_tiebreaker(TASK, "not-a-date") is None
    assert ta.authored_timestamp_tiebreaker("garbage", SPEC_NEWER) is None


def test_tolerates_tz_and_precision_mismatch():
    # task has a Z suffix, spec has microseconds and no tz — must still compare.
    tb = ta.authored_timestamp_tiebreaker("2026-07-27T09:30:00Z",
                                          "2026-07-27T18:00:00.123456")
    assert tb is not None and tb["spec_newer"] is True


# --- surfaced in the disposition block (advisory, with caveat) -------------

def _backlog(created_at):
    return [{"id": "e-4597", "description": "task↔SPEC 矛盾裁定", "created_at": created_at,
             "meta": {"priority": "high"}}]


def test_format_backlog_gap_shows_tiebreaker_when_spec_given():
    out = ta.format_backlog_gap(_backlog(TASK), target_id="ms-119",
                                spec_updated_at=SPEC_NEWER)
    assert "e-4597" in out
    assert "tie-breaker" in out
    assert "superseded 候補" in out          # SPEC newer → lean superseded
    # the coarseness caveat must ride along so nobody blind-applies it
    assert "blind auto-supersede せず" in out
    assert "検出と最終判断は judge/人間" in out


def test_format_backlog_gap_task_newer_direction():
    out = ta.format_backlog_gap(_backlog(TASK), target_id="ms-119",
                                spec_updated_at=SPEC_OLDER)
    assert "タスク側の意図が有効" in out


def test_format_backlog_gap_omits_tiebreaker_without_spec():
    # no spec_updated_at → current behaviour, no tie-breaker, no caveat (unchanged).
    out = ta.format_backlog_gap(_backlog(TASK), target_id="ms-119")
    assert "e-4597" in out
    assert "tie-breaker" not in out
    assert "blind auto-supersede" not in out


def test_format_backlog_gap_omits_tiebreaker_when_task_has_no_created_at():
    # spec present but the task carries no created_at → no wrong hint, no caveat.
    b = [{"id": "e-1", "description": "x", "meta": {"priority": "high"}}]
    out = ta.format_backlog_gap(b, target_id="ms-119", spec_updated_at=SPEC_NEWER)
    assert "tie-breaker" not in out


def test_format_backlog_gap_empty_is_still_empty():
    assert ta.format_backlog_gap([], target_id="ms-119", spec_updated_at=SPEC_NEWER) == ""
