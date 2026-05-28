"""Unit tests for review_history (e-609) — the back-and-forth visibility
fix that lets the timeline render
    pending → changes_requested → pending → approved
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402


def _project_with_pr_at_pending():
    return {
        "name": "t",
        "summary": "",
        "milestones": [{
            "id": "ms-1",
            "title": "x",
            "status": "in_progress",
            "entries": [{
                "id": "e-100",
                "type": "pr",
                "description": "PR",
                "status": "in_review",
                "meta": {
                    "url": "https://github.com/x/y/pull/1",
                    "pr_number": 1,
                    "pr_status": "in_review",
                    "review_status": "pending",
                },
            }],
        }],
    }


def _get_pr(data):
    return data["milestones"][0]["entries"][0]


def _hist(data):
    return _get_pr(data)["meta"].get("review_history", [])


# ---------------------------------------------------------------------------
# Each transition records to review_history
# ---------------------------------------------------------------------------

def test_request_review_records_pending():
    data = _project_with_pr_at_pending()
    core.pr_request_review(data, "e-100")
    h = _hist(data)
    assert len(h) == 1
    assert h[0]["status"] == "pending"


def test_request_changes_records_with_rationale():
    data = _project_with_pr_at_pending()
    core.pr_request_changes(data, "e-100", rationale="needs more tests")
    h = _hist(data)
    assert h[-1]["status"] == "changes_requested"
    assert h[-1]["rationale"] == "needs more tests"


def test_approve_records_approved():
    data = _project_with_pr_at_pending()
    core.pr_approve(data, "e-100", rationale="LGTM")
    h = _hist(data)
    assert h[-1]["status"] == "approved"


def test_reject_records_rejected():
    data = _project_with_pr_at_pending()
    core.pr_reject(data, "e-100", rationale="wrong direction")
    h = _hist(data)
    assert h[-1]["status"] == "rejected"


# ---------------------------------------------------------------------------
# Full round-trip is preserved
# ---------------------------------------------------------------------------

def test_full_roundtrip_sequence_preserved():
    """A realistic review back-and-forth must be visible end-to-end."""
    data = _project_with_pr_at_pending()
    core.pr_request_review(data, "e-100")
    core.pr_request_changes(data, "e-100", rationale="fix the test")
    core.pr_request_review(data, "e-100")  # author re-requests after fixing
    core.pr_approve(data, "e-100", rationale="ok now")
    statuses = [h["status"] for h in _hist(data)]
    assert statuses == ["pending", "changes_requested", "pending", "approved"]


def test_history_heals_corrupted_field():
    """If meta.review_history was somehow not a list, we must not crash."""
    data = _project_with_pr_at_pending()
    _get_pr(data)["meta"]["review_history"] = "garbage"
    core.pr_approve(data, "e-100")
    # The corrupt value is replaced with a fresh list containing the new entry
    h = _hist(data)
    assert isinstance(h, list)
    assert h[-1]["status"] == "approved"


# ---------------------------------------------------------------------------
# Helper directly
# ---------------------------------------------------------------------------

def test_append_review_history_adds_at_actor():
    meta = {}
    core._append_review_history(meta, status="pending", rationale="r", actor="alice")
    assert meta["review_history"][0]["status"] == "pending"
    assert meta["review_history"][0]["rationale"] == "r"
    assert meta["review_history"][0]["actor"] == "alice"
    assert "at" in meta["review_history"][0]
