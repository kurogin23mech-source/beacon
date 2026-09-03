"""ms-164 e-5952: occupation-generic project Target count for the project-list card.

The admin project-list card used ``len(data['milestones'])`` as its headline
metric, so any project whose Targets are not milestones (a sales project's
Opportunities) showed an empty card. ``occupation.project_target_count`` counts
the same projected Target set the detail view enriches with, profession-generically.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402


def test_counts_dev_milestones():
    data = {"milestones": [
        {"id": "ms-1", "status": "in_progress", "title": "a"},
        {"id": "ms-2", "status": "done", "title": "b"},
    ]}
    assert occupation.project_target_count(data) == 2


def test_counts_sales_opportunities_not_milestones():
    """The bug this fixes: a sales project has 0 milestones but N opportunities —
    the count must reflect the opportunities, not read 0."""
    data = {"profession": "sales", "opportunities": [
        {"id": "opp-1", "status": "in_progress"},
        {"id": "opp-2", "status": "in_progress"},
        {"id": "opp-3", "status": "won"},
    ]}
    n = occupation.project_target_count(data)
    assert n >= 3  # every opportunity is a Target; milestone-count would be 0


def test_empty_project_is_zero():
    assert occupation.project_target_count({}) == 0
    assert occupation.project_target_count({"milestones": []}) == 0


def test_malformed_returns_zero_not_raise():
    # project_targets over a structurally broken project must not crash the whole
    # project-list — best-effort 0.
    assert occupation.project_target_count({"milestones": "not-a-list"}) == 0
