"""Unit tests for lib/search.search_project (e-622 / UC10-O9).

Pins the search contract documented in SPEC `3ne57ccZegYQXDQA03op`:
  - All entity types are searchable through one function
  - Metadata filters (type/status/priority/scope/ms/op/id/assignee/owner/from/to) are AND-combined
  - q is OR-combined across configured fields per entity type
  - Empty params return the recent activity (no filtering)
  - Date sort is by updated_at (or created_at) desc
  - Pagination via limit/offset works
"""

from __future__ import annotations

import os
import sys

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)

import search  # noqa: E402


def _make_project(**overrides):
    base = {
        "milestones": [
            {
                "id": "ms-1",
                "title": "Onboarding",
                "objective": "Help non-developers get started",
                "status": "in_progress",
                "priority": "high",
                "created_at": "2026-05-10T00:00:00Z",
                "entries": [
                    {"id": "e-100", "type": "task", "status": "todo",
                     "description": "Add Web UI auto-open",
                     "motivation": "Less friction for new users",
                     "priority": "middle",
                     "created_at": "2026-05-11T00:00:00Z"},
                    {"id": "e-101", "type": "commit", "status": "done",
                     "description": "feat(skill): Web UI auto-open",
                     "meta": {"hash": "abc1234", "message": "feat(skill): Web UI auto-open"},
                     "created_at": "2026-05-12T00:00:00Z"},
                ],
            },
            {
                "id": "ms-2",
                "title": "Data integrity",
                "status": "done",
                "priority": "highest",
                "created_at": "2026-05-20T00:00:00Z",
                "entries": [
                    {"id": "e-200", "type": "task", "status": "done",
                     "description": "Introduce apply_operation work layer",
                     "priority": "high",
                     "created_at": "2026-05-25T00:00:00Z"},
                ],
            },
        ],
        "operations": [
            {
                "id": "op-1",
                "title": "profile-extractor monitor",
                "status": "open",
                "activation_hint": "After Fargate deploy",
                "created_at": "2026-04-01T00:00:00Z",
                "entries": [
                    {"id": "e-300", "type": "incident", "status": "open",
                     "title": "Error rate spike", "description": "Sudden 5xx burst",
                     "created_at": "2026-05-15T00:00:00Z"},
                    {"id": "e-301", "type": "run", "status": "ok",
                     "description": "Nightly batch complete",
                     "created_at": "2026-05-20T00:00:00Z"},
                ],
            },
        ],
        "pushes": [
            {"id": "push-1", "summary": "v0.3.0 release", "pushed_at": "2026-05-20T00:00:00Z",
             "ms_id": "ms-1"},
        ],
        "deployments": [
            {"id": "deploy-1", "description": "Prod cloud-run revision 50",
             "deployed_at": "2026-05-21T00:00:00Z"},
        ],
    }
    base.update(overrides)
    return base


def _make_docs():
    return [
        {"doc_id": "core-1", "title": "Memory layer principle",
         "scope": "core", "content": "Memory layer is passive",
         "updated_at": "2026-05-01T00:00:00Z"},
        {"doc_id": "spec-1", "title": "ms-1 SPEC",
         "scope": "spec", "milestone": "ms-1",
         "content": "Onboarding for non-developers",
         "updated_at": "2026-05-11T00:00:00Z"},
    ]


# ---------------------------------------------------------------------------
# q (fulltext) search across entity types
# ---------------------------------------------------------------------------

def test_q_matches_milestone_title():
    project = _make_project()
    out = search.search_project(project, q="Onboarding")
    types = {r["entity_type"] for r in out["results"]}
    assert "milestone" in types


def test_q_matches_task_motivation_not_just_description():
    project = _make_project()
    out = search.search_project(project, q="friction")
    ids = [r["id"] for r in out["results"]]
    assert "e-100" in ids  # found via motivation field, not description


def test_q_matches_commit_meta_hash():
    project = _make_project()
    out = search.search_project(project, q="abc1234")
    ids = [r["id"] for r in out["results"]]
    assert "e-101" in ids


def test_q_matches_document_content():
    project = _make_project()
    out = search.search_project(project, _make_docs(), q="passive")
    ids = [r["id"] for r in out["results"]]
    assert "core-1" in ids


def test_q_matches_incident_title():
    project = _make_project()
    out = search.search_project(project, q="5xx")
    ids = [r["id"] for r in out["results"]]
    assert "e-300" in ids


# ---------------------------------------------------------------------------
# Metadata filters
# ---------------------------------------------------------------------------

def test_type_filter_restricts_to_listed_types():
    project = _make_project()
    out = search.search_project(project, type=["milestone"])
    assert all(r["entity_type"] == "milestone" for r in out["results"])


def test_status_filter_in_progress_only():
    project = _make_project()
    out = search.search_project(project, type=["milestone"], status=["in_progress"])
    ids = [r["id"] for r in out["results"]]
    assert ids == ["ms-1"]


def test_priority_filter():
    project = _make_project()
    out = search.search_project(project, type=["task"], priority=["high"])
    ids = [r["id"] for r in out["results"]]
    assert ids == ["e-200"]


def test_scope_filter_for_documents():
    project = _make_project()
    out = search.search_project(project, _make_docs(),
                                 type=["document"], scope="core")
    ids = [r["id"] for r in out["results"]]
    assert ids == ["core-1"]


def test_ms_filter_restricts_entries():
    project = _make_project()
    out = search.search_project(project, ms="ms-1")
    # All entries returned should be tied to ms-1
    for r in out["results"]:
        if r["entity_type"] in ("task", "commit"):
            assert r["ms_id"] == "ms-1"


def test_id_filter_exact_match():
    project = _make_project()
    out = search.search_project(project, id="e-100")
    assert len(out["results"]) == 1
    assert out["results"][0]["id"] == "e-100"


def test_date_range_excludes_outside():
    project = _make_project()
    out = search.search_project(
        project, type=["task"],
        from_date="2026-05-12T00:00:00Z",
    )
    ids = [r["id"] for r in out["results"]]
    assert "e-100" not in ids  # 2026-05-11 < from


# ---------------------------------------------------------------------------
# Pagination and sorting
# ---------------------------------------------------------------------------

def test_empty_query_returns_recent_activity():
    project = _make_project()
    out = search.search_project(project)
    # Should include everything, sorted by date desc
    assert out["total"] > 0


def test_results_sorted_by_date_desc():
    project = _make_project()
    out = search.search_project(project, type=["task", "commit", "incident", "run"])
    dates = [(r.get("updated_at") or r.get("created_at") or "") for r in out["results"]]
    assert dates == sorted(dates, reverse=True)


def test_limit_and_offset():
    project = _make_project()
    page1 = search.search_project(project, limit=2, offset=0)
    page2 = search.search_project(project, limit=2, offset=2)
    assert len(page1["results"]) == 2
    assert page1["total"] == page2["total"]
    assert page1["results"][0]["id"] != page2["results"][0]["id"]


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

def test_facets_count_by_type():
    project = _make_project()
    out = search.search_project(project)
    by_type = out["facets"]["type"]
    assert by_type.get("milestone", 0) == 2
    assert by_type.get("operation", 0) == 1


def test_facets_count_by_status():
    project = _make_project()
    out = search.search_project(project, type=["task"])
    statuses = out["facets"]["status"]
    assert statuses.get("todo", 0) == 1
    assert statuses.get("done", 0) == 1


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------

def test_snippet_centers_on_match():
    project = _make_project()
    out = search.search_project(project, q="5xx")
    inc = next(r for r in out["results"] if r["id"] == "e-300")
    assert "5xx" in inc["snippet"]


# ---------------------------------------------------------------------------
# ms-43 e-631: cross-entity search returns documents/push/deploy/operation
# alongside milestones/tasks/commits in a single response.
# ---------------------------------------------------------------------------

def test_cross_entity_search_covers_all_types():
    """A single search with a shared keyword across multiple entity types must
    return results from every contributing entity, so the Web UI can render a
    grouped view (e-631)."""
    project = {
        "milestones": [
            {"id": "ms-1", "title": "Authentication overhaul",
             "objective": "redo auth", "status": "in_progress",
             "created_at": "2026-05-01T00:00:00Z",
             "entries": [
                 {"id": "e-1", "type": "task", "status": "todo",
                  "description": "Add auth middleware",
                  "created_at": "2026-05-02T00:00:00Z"},
                 {"id": "e-2", "type": "commit", "status": "done",
                  "description": "fix(auth): token refresh",
                  "meta": {"hash": "abc1234", "message": "fix(auth): token refresh"},
                  "created_at": "2026-05-03T00:00:00Z"},
             ]},
        ],
        "operations": [
            {"id": "op-1", "title": "Auth monitor",
             "status": "open", "created_at": "2026-05-04T00:00:00Z",
             "entries": [
                 {"id": "e-3", "type": "incident", "status": "open",
                  "title": "Auth 5xx surge", "description": "auth 5xx spike",
                  "created_at": "2026-05-05T00:00:00Z"},
             ]},
        ],
        "pushes": [
            {"id": "p-1", "summary": "deploy auth fix",
             "pushed_at": "2026-05-06T00:00:00Z", "ms_id": "ms-1"},
        ],
        "deployments": [
            {"id": "d-1", "description": "rolled out auth middleware",
             "deployed_at": "2026-05-07T00:00:00Z", "status": "success"},
        ],
    }
    docs = [{"doc_id": "spec-auth", "title": "Auth SPEC",
             "scope": "spec", "content": "auth design",
             "updated_at": "2026-05-08T00:00:00Z"}]

    out = search.search_project(project, docs, q="auth")
    found_types = {r["entity_type"] for r in out["results"]}

    # All five contributing types must appear in the unified result set.
    assert "milestone" in found_types
    assert "task" in found_types
    assert "commit" in found_types
    assert "operation" in found_types
    assert "incident" in found_types
    assert "push" in found_types
    assert "deploy" in found_types
    assert "document" in found_types


def test_cross_entity_facets_count_per_type():
    """Facets must aggregate cross-entity counts so the Web UI can render
    the grouping chips with totals (e-631)."""
    project = _make_project()
    docs = _make_docs()
    out = search.search_project(project, docs)  # empty q → all recent

    type_facet = out["facets"]["type"]
    # We added milestones / tasks / commits / operation / incident / run / push / deploy / document.
    assert type_facet.get("milestone", 0) >= 1
    assert type_facet.get("task", 0) >= 1
    assert type_facet.get("commit", 0) >= 1
    assert type_facet.get("operation", 0) >= 1
    assert type_facet.get("document", 0) >= 1
    assert type_facet.get("push", 0) >= 1


def test_document_scope_filter_excludes_other_scopes():
    """e-616: Documents-tab fulltext+scope filtering. Asking for scope=spec
    must not leak core/memo documents."""
    project = _make_project()
    docs = _make_docs()
    out = search.search_project(project, docs, type=["document"], scope="spec")
    scopes = {r.get("scope") for r in out["results"]}
    assert scopes == {"spec"}, scopes
