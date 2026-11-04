"""Trek-related document model + reverse lookup tests (ms-69 / e-1663).

Covers:

  * **frontmatter round-trip**: writing ``trek_id`` via --trek CLI flag
    survives _parse_frontmatter → _add_frontmatter → save_document
  * **GET /api/projects/{pid}/milestones/{ms_id}/related-treks** (+ operation
    + entry variants): reverse lookup from a work item to the treks that
    include it in scope. Project-wide scope entries match every item; narrow
    scope entries match only the exact ref.
  * **GET /api/treks/{tid}/documents**: forward lookup from a trek to all
    docs tagged with its trek_id, across all projects in the trek's scope.

The schema change is intentionally tiny (= one optional ``trek_id`` field
on documents, no migration required) so existing docs without ``trek_id``
are unaffected. The reverse / forward lookups are how the e-1664 Related
Treks widget and the future Trek detail page see "what is in this trek".
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


# ---------------------------------------------------------------------------
# Frontmatter round-trip (Python-side, no HTTP)
# ---------------------------------------------------------------------------

class TestFrontmatterRoundTrip:
    """e-1663 schema (1): ``trek_id`` survives _add_frontmatter / _parse_frontmatter."""

    def _import_helpers(self):
        import commands
        return commands._add_frontmatter, commands._parse_frontmatter

    def test_trek_id_is_persisted_when_passed(self):
        add_fm, parse_fm = self._import_helpers()
        body = "# Hello\n\nbody text\n"
        out = add_fm(body, scope="memo", trek_id="tk-abc12345")
        meta, _body = parse_fm(out)
        assert meta.get("trek_id") == "tk-abc12345"

    def test_existing_trek_id_in_frontmatter_survives_update(self):
        add_fm, parse_fm = self._import_helpers()
        # Doc already has trek_id; we re-emit without overriding.
        body = (
            "---\nscope: memo\ntrek_id: tk-existing\n---\n\nBody\n"
        )
        out = add_fm(body, scope="memo")  # no trek_id arg
        meta, _body = parse_fm(out)
        # Existing value preserved (= _add_frontmatter doesn't blank it).
        assert meta.get("trek_id") == "tk-existing"

    def test_passing_trek_id_overrides_existing(self):
        add_fm, parse_fm = self._import_helpers()
        body = (
            "---\nscope: memo\ntrek_id: tk-old\n---\n\nBody\n"
        )
        out = add_fm(body, scope="memo", trek_id="tk-new")
        meta, _body = parse_fm(out)
        assert meta.get("trek_id") == "tk-new"


# ---------------------------------------------------------------------------
# HTTP / endpoint coverage
# ---------------------------------------------------------------------------

_treks: dict[str, dict] = {}
_projects: dict[str, dict] = {}
_docs: dict[str, list[dict]] = {}


def _mock_get_trek(trek_id):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id, data):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    _treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}


def _mock_list_treks(actor_id=None, *, status=None, include_archived=False):
    out = []
    for t in _treks.values():
        if not include_archived and t.get("status") == "archived":
            continue
        if status and t.get("status") != status:
            continue
        if actor_id:
            creator = (t.get("creator_actor") or {}).get("user_id")
            members = [m.get("user_id") for m in t.get("members") or []]
            if creator != actor_id and actor_id not in members:
                continue
        out.append(copy.deepcopy(t))
    return out


def _mock_get_project(project_id):
    return copy.deepcopy(_projects.get(project_id))


def _mock_save_project(project_id, data):
    _projects[project_id] = copy.deepcopy(data)


def _mock_list_documents(project_id):
    return copy.deepcopy(_docs.get(project_id, []))


def _mock_no_op(*args, **kwargs):
    return None


def _mock_find_user_by_email(email):
    return None


def _rebind_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("get_project", _mock_get_project),
        ("save_project", _mock_save_project),
        ("list_documents", _mock_list_documents),
        ("find_user_by_email", _mock_find_user_by_email),
        ("get_or_create_user", _mock_no_op),
        ("get_user", _mock_no_op),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    return prior


def _restore_db(prior):
    db_module = app_module.db
    for k, v in prior.items():
        if v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)


client = TestClient(app_module.app)


LEADER_UID = "uid-leader"
PROJECT_A = "proj-a"
PROJECT_B = "proj-b"


def _impersonate(uid):
    def _fake_auth():
        return {"sub": uid, "email": f"{uid}@x"}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _projects.clear()
    _docs.clear()
    # Project A has LEADER as a member so _load(project_id, user) succeeds.
    _projects[PROJECT_A] = {
        "name": "Project A",
        "owner": LEADER_UID,
        "members": [
            {"user_id": LEADER_UID, "email": "leader@x", "role": "owner"},
        ],
        "milestones": [
            {"id": "ms-99", "title": "Test ms", "status": "in_progress",
             "progress": 0, "target_date": "", "commits": [], "entries": []},
        ],
        "operations": [{"id": "op-99", "status": "open",
                        "title": "Test op", "entries": []}],
    }
    _projects[PROJECT_B] = {
        "name": "Project B",
        "owner": LEADER_UID,
        "members": [
            {"user_id": LEADER_UID, "email": "leader@x", "role": "owner"},
        ],
        "milestones": [],
        "operations": [],
    }
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        _treks.clear()
        _projects.clear()
        _docs.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior)


def _make_trek(trek_id: str, *, scope: list[dict],
               status: str = "active", archived: bool = False) -> None:
    """Seed a trek directly into the in-memory store."""
    if archived:
        status = "archived"
    _treks[trek_id] = {
        "trek_id": trek_id,
        "title": f"Trek {trek_id}",
        "type": "persistent",
        "status": status,
        "creator_actor": {"user_id": LEADER_UID, "email": "leader@x"},
        "leader_session_id": "sv-leader",
        "members": [{"user_id": LEADER_UID, "email": "leader@x",
                     "role": "leader",
                     "invited_at": "2026-06-14T00:00:00Z",
                     "joined_at": "2026-06-14T00:00:00Z"}],
        "scope": scope,
        "halt": None,
        "created_at": "2026-06-14T00:00:00Z",
        "updated_at": "2026-06-14T00:00:00Z",
        "archived_at": "2026-06-14T01:00:00Z" if archived else None,
    }


# ---------------------------------------------------------------------------
# Reverse lookup: /api/projects/{pid}/<item>/related-treks
# ---------------------------------------------------------------------------

class TestReverseLookup_Milestone:
    def test_narrow_scope_match_returned(self):
        _make_trek("tk-narrow", scope=[
            {"project": PROJECT_A, "milestone": "ms-99"},
        ])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/milestones/ms-99/related-treks"
        )
        assert r.status_code == 200
        ids = [t["trek_id"] for t in r.json()]
        assert ids == ["tk-narrow"]

    def test_project_wide_scope_also_matches(self):
        """A scope entry without any narrowing key (= project-wide) counts
        as related to every milestone in that project."""
        _make_trek("tk-wide", scope=[{"project": PROJECT_A}])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/milestones/ms-99/related-treks"
        )
        assert r.status_code == 200
        assert {t["trek_id"] for t in r.json()} == {"tk-wide"}

    def test_wrong_milestone_does_not_match(self):
        _make_trek("tk-other", scope=[
            {"project": PROJECT_A, "milestone": "ms-other"},
        ])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/milestones/ms-99/related-treks"
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_wrong_project_does_not_match(self):
        _make_trek("tk-elsewhere", scope=[
            {"project": PROJECT_B, "milestone": "ms-99"},
        ])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/milestones/ms-99/related-treks"
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_archived_treks_included_by_default(self):
        _make_trek("tk-old", scope=[
            {"project": PROJECT_A, "milestone": "ms-99"},
        ], archived=True)
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/milestones/ms-99/related-treks"
        )
        ids = {t["trek_id"] for t in r.json()}
        assert "tk-old" in ids
        assert r.json()[0]["status"] == "archived"

    def test_stranger_blocked_at_project_level(self):
        _make_trek("tk-x", scope=[
            {"project": PROJECT_A, "milestone": "ms-99"},
        ])
        _impersonate("uid-stranger")
        r = client.get(
            f"/api/projects/{PROJECT_A}/milestones/ms-99/related-treks"
        )
        assert r.status_code == 403


class TestReverseLookup_Operation:
    def test_narrow_operation_match(self):
        _make_trek("tk-op", scope=[
            {"project": PROJECT_A, "operation": "op-99"},
        ])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/operations/op-99/related-treks"
        )
        assert r.status_code == 200
        assert [t["trek_id"] for t in r.json()] == ["tk-op"]

    def test_milestone_scope_does_not_leak_into_operation_lookup(self):
        """Scope entry with milestone narrowing should NOT match the
        operation lookup (= they are sibling refs, not overlapping)."""
        _make_trek("tk-ms-only", scope=[
            {"project": PROJECT_A, "milestone": "ms-99"},
        ])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/operations/op-99/related-treks"
        )
        assert r.json() == []


class TestReverseLookup_Entry:
    def test_task_match(self):
        _make_trek("tk-task", scope=[
            {"project": PROJECT_A, "task": "e-77"},
        ])
        _impersonate(LEADER_UID)
        r = client.get(
            f"/api/projects/{PROJECT_A}/entries/e-77/related-treks"
        )
        assert r.status_code == 200
        assert [t["trek_id"] for t in r.json()] == ["tk-task"]


# ---------------------------------------------------------------------------
# Forward lookup: /api/treks/{tid}/documents
# ---------------------------------------------------------------------------

class TestTrekDocuments:
    def test_returns_only_docs_tagged_with_this_trek(self):
        _make_trek("tk-mine", scope=[{"project": PROJECT_A}])
        _docs[PROJECT_A] = [
            {"doc_id": "d-1", "title": "Mine",
             "scope": "memo", "trek_id": "tk-mine"},
            {"doc_id": "d-2", "title": "Other trek",
             "scope": "memo", "trek_id": "tk-other"},
            {"doc_id": "d-3", "title": "No trek",
             "scope": "memo"},
        ]
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-mine/documents")
        assert r.status_code == 200
        ids = [d["doc_id"] for d in r.json()]
        assert ids == ["d-1"]
        # Source project_id is surfaced so the UI can deep-link.
        assert r.json()[0]["project_id"] == PROJECT_A

    def test_iterates_all_projects_in_trek_scope(self):
        _make_trek("tk-multi", scope=[
            {"project": PROJECT_A}, {"project": PROJECT_B},
        ])
        _docs[PROJECT_A] = [
            {"doc_id": "d-a", "title": "A doc",
             "scope": "memo", "trek_id": "tk-multi"},
        ]
        _docs[PROJECT_B] = [
            {"doc_id": "d-b", "title": "B doc",
             "scope": "memo", "trek_id": "tk-multi"},
        ]
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-multi/documents")
        ids = {d["doc_id"] for d in r.json()}
        assert ids == {"d-a", "d-b"}

    def test_non_member_returns_403(self):
        _make_trek("tk-private", scope=[{"project": PROJECT_A}])
        _impersonate("uid-outsider")
        r = client.get("/api/treks/tk-private/documents")
        assert r.status_code == 403

    def test_missing_trek_returns_404(self):
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-nonexistent/documents")
        assert r.status_code == 404

    def test_no_trek_scope_returns_empty(self):
        _make_trek("tk-empty", scope=[])
        _impersonate(LEADER_UID)
        r = client.get("/api/treks/tk-empty/documents")
        assert r.json() == []
