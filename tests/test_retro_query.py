"""Tests for the unified retro / retrospect query base (ms-79 / e-1836).

Verifies:
  - Backwards compat: a plain call with no extensions returns the same
    shape as lib/search.search_project (single source of truth holds).
  - Extension flags: include_bus_dm / include_session_logs / include_trek
    each merge their respective entity rows into the result set.
  - Source / actor / claimant filters: post-filter behavior on result
    rows, with meta.source / meta.actor / claim_holder fallback rules.
  - Source facet: counts by human / auto-op / dm / trek / session_log.
"""

from __future__ import annotations

import os
import sys
import pytest

# Make lib/ importable the same way the CLI does.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

import retro_query  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _project_with_one_human_one_autop_commit() -> dict:
    """Two commits on ms-1: one human, one auto-op (= envelope context)."""
    return {
        "milestones": [
            {
                "id": "ms-1",
                "title": "Test MS",
                "status": "in_progress",
                "entries": [
                    {
                        "id": "e-100",
                        "type": "commit",
                        "description": "human commit",
                        "date": "2026-06-15",
                        "created_at": "2026-06-15T10:00:00Z",
                        "status": "done",
                        "meta": {
                            "hash": "abc1234",
                            "message": "human commit",
                            "actor": {"machine": "mac.local", "agent": "claude-mac"},
                            # no source → defaults to "human"
                        },
                    },
                    {
                        "id": "e-101",
                        "type": "commit",
                        "description": "auto-op commit",
                        "date": "2026-06-15",
                        "created_at": "2026-06-15T11:00:00Z",
                        "status": "done",
                        "meta": {
                            "hash": "def5678",
                            "message": "auto-op commit",
                            "actor": {"machine": "mac.local", "agent": "claude-mac"},
                            "source": "auto-op",
                            "envelope_id": "env-1",
                        },
                    },
                ],
            },
        ],
        "operations": [],
    }


# ---------------------------------------------------------------------------
# Backwards compat
# ---------------------------------------------------------------------------

def test_backcompat_empty_filters_returns_search_shape():
    project = _project_with_one_human_one_autop_commit()
    result = retro_query.retro_query(project)
    # Standard shape from search.search_project
    assert set(result.keys()) >= {"results", "total", "limit", "offset", "facets"}
    # Both commits should appear
    ids = {r["id"] for r in result["results"]}
    assert {"e-100", "e-101"}.issubset(ids)


def test_facets_include_source():
    project = _project_with_one_human_one_autop_commit()
    result = retro_query.retro_query(project)
    src_facet = result["facets"]["source"]
    assert src_facet.get("human") == 1
    assert src_facet.get("auto-op") == 1


# ---------------------------------------------------------------------------
# Source / actor / claimant filters (UC10-F2 / F3)
# ---------------------------------------------------------------------------

def test_filter_source_human_only():
    project = _project_with_one_human_one_autop_commit()
    result = retro_query.retro_query(project, source=["human"])
    ids = {r["id"] for r in result["results"]}
    assert ids == {"e-100"}


def test_filter_source_auto_op_only():
    project = _project_with_one_human_one_autop_commit()
    result = retro_query.retro_query(project, source=["auto-op"])
    ids = {r["id"] for r in result["results"]}
    assert ids == {"e-101"}


def test_filter_source_multi_or_combined():
    project = _project_with_one_human_one_autop_commit()
    result = retro_query.retro_query(project, source=["human", "auto-op"])
    ids = {r["id"] for r in result["results"]}
    assert ids == {"e-100", "e-101"}


def test_filter_actor_by_agent():
    project = {
        "milestones": [{
            "id": "ms-1", "title": "T", "status": "in_progress",
            "entries": [
                {"id": "e-1", "type": "commit", "description": "x",
                 "created_at": "2026-06-15",
                 "meta": {"hash": "h1", "actor": {"agent": "alice", "machine": "m"}}},
                {"id": "e-2", "type": "commit", "description": "y",
                 "created_at": "2026-06-15",
                 "meta": {"hash": "h2", "actor": {"agent": "bob", "machine": "m"}}},
            ],
        }],
        "operations": [],
    }
    result = retro_query.retro_query(project, actor="alice")
    ids = {r["id"] for r in result["results"]}
    assert ids == {"e-1"}


def test_filter_claimant_via_ms_claim_holder():
    project = {
        "milestones": [{
            "id": "ms-1", "title": "T", "status": "in_progress",
            "claim_holder": "alice",
            "entries": [
                {"id": "e-1", "type": "commit", "description": "x",
                 "created_at": "2026-06-15",
                 "meta": {"hash": "h1"}},
            ],
        }],
        "operations": [],
    }
    result = retro_query.retro_query(project, claimant="alice")
    ids = {r["id"] for r in result["results"]}
    # Both the ms itself and its entry should be reachable via claimant.
    assert "e-1" in ids


# ---------------------------------------------------------------------------
# Extension entity merge (UC10-F1 / F4)
# ---------------------------------------------------------------------------

def test_include_session_logs_merges_fork_sessions():
    project = _project_with_one_human_one_autop_commit()
    session_logs = [
        {
            "session_id": "sv-fork-1",
            "summary": "Fork session decided X",
            "created_at": "2026-06-15T12:00:00Z",
            "fork_parent_session_id": "sv-parent-99",
        },
    ]
    result = retro_query.retro_query(
        project,
        include_session_logs=True,
        session_logs=session_logs,
    )
    types = {r["entity_type"] for r in result["results"]}
    assert "session_log" in types
    # marker for "fork 由来" / Skill output
    fork_rows = [r for r in result["results"] if r["entity_type"] == "session_log"]
    assert fork_rows[0]["from_fork"] is True


def test_include_bus_dm_merges_dm_archive():
    project = _project_with_one_human_one_autop_commit()
    bus_archive = [
        {
            "event_id": "ev-1",
            "channel": "dm",
            "payload": "Cross-project DM request",
            "created_at": "2026-06-15T13:00:00Z",
        },
    ]
    result = retro_query.retro_query(
        project,
        include_bus_dm=True,
        bus_archive=bus_archive,
    )
    types = {r["entity_type"] for r in result["results"]}
    assert "bus_event" in types
    dm_rows = [r for r in result["results"] if r["entity_type"] == "bus_event"]
    assert dm_rows[0]["from_dm"] is True


def test_include_trek_merges_trek_summaries():
    project = _project_with_one_human_one_autop_commit()
    trek_summaries = [
        {
            "id": "ts-1",
            "trek_id": "trek-99",
            "text": "Reached consensus on Y",
            "created_at": "2026-06-15T14:00:00Z",
        },
    ]
    result = retro_query.retro_query(
        project,
        include_trek=True,
        trek_summaries=trek_summaries,
    )
    types = {r["entity_type"] for r in result["results"]}
    assert "trek_summary" in types
    trek_rows = [r for r in result["results"] if r["entity_type"] == "trek_summary"]
    assert trek_rows[0]["from_trek"] is True


# ---------------------------------------------------------------------------
# Source label resolution
# ---------------------------------------------------------------------------

def test_session_log_classified_as_session_log_source():
    project = {"milestones": [], "operations": []}
    session_logs = [{"session_id": "s-1", "summary": "x", "created_at": "2026-06-15"}]
    result = retro_query.retro_query(
        project, include_session_logs=True, session_logs=session_logs,
    )
    facet = result["facets"]["source"]
    assert facet.get("session_log") == 1


def test_dm_classified_as_dm_source():
    project = {"milestones": [], "operations": []}
    bus_archive = [{"event_id": "e-1", "channel": "dm", "payload": "hi",
                    "created_at": "2026-06-15"}]
    result = retro_query.retro_query(
        project, include_bus_dm=True, bus_archive=bus_archive,
    )
    facet = result["facets"]["source"]
    assert facet.get("dm") == 1


def test_pagination_after_post_filter():
    """Post-filters then standard pagination — total should reflect filter."""
    project = _project_with_one_human_one_autop_commit()
    result = retro_query.retro_query(project, source=["human"], limit=10)
    assert result["total"] == 1
    assert len(result["results"]) == 1
