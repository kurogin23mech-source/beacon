"""Unit tests for ``core.milestone_assignee_add`` (ms-51 / e-932, e-933).

Verifies the multi-assignee semantics introduced for the branch-driven
workflow, while keeping wire compatibility with the historical single-
string assignee field (ms-42 / ms-43).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402


def _project(ms_status: str = "in_progress", assignee=""):
    ms = {
        "id": "ms-51",
        "title": "Test MS",
        "status": ms_status,
        "entries": [],
        "progress": 0,
    }
    if assignee:
        ms["assignee"] = assignee
    return {"name": "t", "milestones": [ms], "summary": ""}


# ---------------------------------------------------------------------------
# Adding the first assignee
# ---------------------------------------------------------------------------

def test_add_first_assignee_keeps_string_form():
    """Single assignee stays a bare string — wire-compat with ms-42 / ms-43 UI."""
    data = _project()
    ms, added = core.milestone_assignee_add(data, "ms-51", "alice")
    assert added is True
    assert ms["assignee"] == "alice"


def test_add_second_assignee_joins_with_comma():
    data = _project(assignee="alice")
    ms, added = core.milestone_assignee_add(data, "ms-51", "bob")
    assert added is True
    assert ms["assignee"] == "alice,bob"


def test_add_third_assignee_appends():
    data = _project(assignee="alice,bob")
    ms, added = core.milestone_assignee_add(data, "ms-51", "charlie")
    assert added is True
    assert ms["assignee"] == "alice,bob,charlie"


# ---------------------------------------------------------------------------
# Duplicate handling (SPEC AC: "duplicate なら no-op")
# ---------------------------------------------------------------------------

def test_duplicate_assignee_is_noop():
    data = _project(assignee="alice")
    ms, added = core.milestone_assignee_add(data, "ms-51", "alice")
    assert added is False
    assert ms["assignee"] == "alice"


def test_duplicate_assignee_in_multi_list_is_noop():
    data = _project(assignee="alice,bob")
    ms, added = core.milestone_assignee_add(data, "ms-51", "bob")
    assert added is False
    assert ms["assignee"] == "alice,bob"


# ---------------------------------------------------------------------------
# State validation (SPEC e-933 AC-4: refuse done/cancelled)
# ---------------------------------------------------------------------------

def test_done_milestone_refuses_assignee():
    data = _project(ms_status="done")
    with pytest.raises(ValueError, match="done"):
        core.milestone_assignee_add(data, "ms-51", "alice")


def test_cancelled_milestone_refuses_assignee():
    data = _project(ms_status="cancelled")
    with pytest.raises(ValueError, match="cancelled"):
        core.milestone_assignee_add(data, "ms-51", "alice")


def test_observing_milestone_accepts_assignee():
    """Observing is a healthy lifecycle state — joining is allowed."""
    data = _project(ms_status="observing")
    ms, added = core.milestone_assignee_add(data, "ms-51", "alice")
    assert added is True
    assert ms["assignee"] == "alice"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_empty_actor_raises():
    data = _project()
    with pytest.raises(ValueError, match="actor"):
        core.milestone_assignee_add(data, "ms-51", "")


def test_whitespace_only_actor_raises():
    data = _project()
    with pytest.raises(ValueError, match="actor"):
        core.milestone_assignee_add(data, "ms-51", "   ")


def test_missing_milestone_raises():
    data = _project()
    with pytest.raises(ValueError, match="Milestone not found"):
        core.milestone_assignee_add(data, "ms-999", "alice")


# ---------------------------------------------------------------------------
# Backward-compat for legacy projects
# ---------------------------------------------------------------------------

def test_list_form_assignee_is_normalised():
    """If a project somehow has assignee=[...], we still de-dup and append."""
    data = _project()
    data["milestones"][0]["assignee"] = ["alice", "bob"]
    ms, added = core.milestone_assignee_add(data, "ms-51", "charlie")
    assert added is True
    assert ms["assignee"] == "alice,bob,charlie"
