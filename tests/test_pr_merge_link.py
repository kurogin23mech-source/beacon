"""Unit tests for the e-610 commit↔PR back-link applied by pr_merge.

When a PR is merged, beacon already has the commits recorded twice:
  1. as child entries under the PR (created at `pr_add` time)
  2. as plain commit entries under the MS (created by `beacon log` during
     development)

Until e-610 these two never knew about each other. Now `pr_merge` walks all
commit entries across milestones and tags any whose 7-char hash matches a
PR child commit with `meta.pr_id = <pr-entry-id>`.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import core  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project_with_pr_and_commits():
    """Two commits already logged under ms-42; a PR was opened on top of
    them and is about to be merged."""
    return {
        "name": "t",
        "summary": "",
        "milestones": [
            {
                "id": "ms-42",
                "title": "current",
                "status": "in_progress",
                "entries": [
                    # Logged commits — these are the back-link targets
                    {"id": "e-100", "type": "commit",
                     "description": "feat: thing", "meta": {"hash": "abc1234"}},
                    {"id": "e-101", "type": "commit",
                     "description": "test: thing", "meta": {"hash": "def5678"}},
                    # The PR itself with the same two commits as children
                    {
                        "id": "e-200",
                        "type": "pr",
                        "description": "PR #42",
                        "status": "in_review",
                        "meta": {"url": "https://github.com/x/y/pull/42",
                                 "pr_number": 42},
                        "entries": [
                            {"id": "e-201", "type": "commit",
                             "description": "feat: thing",
                             "meta": {"hash": "abc1234"}},
                            {"id": "e-202", "type": "commit",
                             "description": "test: thing",
                             "meta": {"hash": "def5678"}},
                        ],
                    },
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Back-link is applied to matching commit entries
# ---------------------------------------------------------------------------

def test_pr_merge_links_matching_commits():
    data = _make_project_with_pr_and_commits()
    core.pr_merge(data, "e-200", date="2026-05-28")
    # Both top-level commit entries should now carry meta.pr_id = "e-200"
    commits = data["milestones"][0]["entries"]
    e100 = next(c for c in commits if c["id"] == "e-100")
    e101 = next(c for c in commits if c["id"] == "e-101")
    assert e100["meta"]["pr_id"] == "e-200"
    assert e101["meta"]["pr_id"] == "e-200"


def test_pr_merge_records_linked_count():
    data = _make_project_with_pr_and_commits()
    _, entry = core.pr_merge(data, "e-200", date="2026-05-28")
    assert entry["meta"]["linked_commits"] == 2


def test_pr_merge_does_not_self_link_child_commits():
    """The PR's own child commit entries must NOT get pr_id (would be a
    redundant self-loop). Only sibling beacon-log entries get tagged."""
    data = _make_project_with_pr_and_commits()
    core.pr_merge(data, "e-200", date="2026-05-28")
    pr = next(e for e in data["milestones"][0]["entries"] if e["id"] == "e-200")
    for child in pr["entries"]:
        assert "pr_id" not in (child.get("meta") or {})


def test_pr_merge_ignores_non_matching_commits():
    """An unrelated commit (different hash) must not get tagged."""
    data = _make_project_with_pr_and_commits()
    # Inject an unrelated commit
    data["milestones"][0]["entries"].insert(0, {
        "id": "e-099", "type": "commit",
        "description": "unrelated", "meta": {"hash": "999aaaa"},
    })
    core.pr_merge(data, "e-200", date="2026-05-28")
    unrelated = next(c for c in data["milestones"][0]["entries"] if c["id"] == "e-099")
    assert "pr_id" not in (unrelated.get("meta") or {})


def test_pr_merge_idempotent_relinking():
    """Re-running pr_merge on the same PR must not crash and must keep
    the link stable."""
    data = _make_project_with_pr_and_commits()
    core.pr_merge(data, "e-200", date="2026-05-28")
    core.pr_merge(data, "e-200", date="2026-05-28")
    e100 = next(c for c in data["milestones"][0]["entries"] if c["id"] == "e-100")
    assert e100["meta"]["pr_id"] == "e-200"


def test_pr_merge_handles_pr_without_child_commits():
    """If the PR was registered without commits (intent-only), pr_merge
    must still complete successfully and not write linked_commits."""
    data = _make_project_with_pr_and_commits()
    # Strip child commits from the PR
    pr = next(e for e in data["milestones"][0]["entries"] if e["id"] == "e-200")
    pr["entries"] = []
    _, entry = core.pr_merge(data, "e-200", date="2026-05-28")
    assert entry["meta"]["pr_status"] == "merged"
    # No linked_commits stamp when there's nothing to link
    assert "linked_commits" not in entry["meta"]
