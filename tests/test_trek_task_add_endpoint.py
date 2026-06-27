"""Server endpoint tests for cross-project ``task-add`` via Trek scope.

ms-92 / e-2141. The endpoint POST /api/treks/{trek_id}/task-add walks
``trek.check_trek_task_add_allowed`` and either writes the task to the
target project's milestone (with ``meta.trek_id`` stamp for audit) or
returns 403 with the scope-guard reason code.

These tests cover the 3 SPEC AC paths + auth gating:

  1. ``scope MS 下への追加成功`` — allowed scope + member caller → 200
  2. ``scope 外への追加 403`` — project not in scope → 403 with reason
  3. ``scope 単独 task 下への追加 禁止`` — scope-only-task-narrowing → 403
  4. non-member caller → 403 ("only trek members can add tasks…")
  5. malformed target (operation prefix / missing ms-) → 400

We mock the trek store + ``apply_operation`` so the test stays focused
on the scope-guard contract and doesn't drag in the full project
storage backend. The actual ``core.task_add`` write path is exercised
by the existing ``/api/projects/{id}/milestones/{ms}/entries`` tests.
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

sys.modules["firestore_client"] = app_module.db


# In-memory trek storage + user index.
_treks: dict[str, dict] = {}
_users_by_email: dict[str, tuple[str, dict]] = {}
# ms-95 / Trek task-add slug-resolution bug fix: project registry so
# tests can exercise the slug ↔ full project_id canonicalisation that
# the endpoint now performs via ``_resolve_canonical_project_id``.
# Key is the **full project_id** (= ``"<slug>-<hex6>"``); the entry
# carries ``owner`` so the user-scoped ``list_projects`` filter mirrors
# the production firestore_client behaviour.
_projects: dict[str, dict] = {}

# Recorded apply_operation calls so we can assert the write happened
# (or didn't) without needing a real project backend. The recorded
# args also let us verify ``meta.trek_id`` was stamped during the op.
_apply_operation_calls: list[dict] = []


def _mock_get_trek(trek_id: str):
    data = _treks.get(trek_id)
    return copy.deepcopy(data) if data else None


def _mock_save_trek(trek_id: str, data: dict):
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    _treks[trek_id] = {**copy.deepcopy(payload), "trek_id": trek_id}


def _mock_list_treks(*args, **kwargs):
    return [copy.deepcopy(t) for t in _treks.values()]


def _mock_find_user_by_email(email: str):
    return _users_by_email.get(email)


def _mock_get_or_create_user(user_id: str, email: str):
    return None


def _mock_get_user(user_id: str):
    for uid, (_, data) in _users_by_email.items():
        if data.get("sub") == user_id or uid == user_id:
            return data
    return None


def _mock_get_project(project_id: str):
    """Mirror firestore_client.get_project — None when missing."""
    return copy.deepcopy(_projects.get(project_id))


def _mock_list_projects(user_id: Optional[str] = None,
                       include_archived: bool = False) -> list[dict]:
    """Mirror firestore_client.list_projects shape: project_id + name + owner.

    The resolver only reads ``project_id`` from each row so the minimal
    shape is sufficient. We honour ``user_id`` so owner-mismatched rows
    don't leak into a slug expansion the caller can't access.
    """
    rows: list[dict] = []
    for pid, data in _projects.items():
        if user_id and data.get("owner") and data.get("owner") != user_id:
            continue
        rows.append({"project_id": pid, "name": data.get("name", "")})
    return rows


def _register_project(project_id: str, *, owner: str = "") -> None:
    """Test helper — register a fake project so the resolver can find it."""
    _projects[project_id] = {
        "name": project_id.rsplit("-", 1)[0] if "-" in project_id else project_id,
        "milestones": [],
        "owner": owner,
    }


def _fake_apply_operation(project_id: str, op, *,
                          op_name: str = "", actor: str = "",
                          reason: str = "",
                          project_file: Optional[str] = None):
    """Capture the op call and execute the op against a synthetic project doc.

    The op closure does ``core.task_add`` then stamps ``meta.trek_id``;
    by feeding it a minimal data dict we can verify the meta stamp
    landed and the returned payload includes ``trek_id`` for audit.
    """
    # Minimal project data shape matching what core.task_add expects:
    # a milestones list with at least the target MS present.
    target_ms = _apply_operation_calls and _apply_operation_calls[-1].get("target_ms")
    data = {
        "milestones": [{
            "id": "ms-target",
            "title": "Target MS",
            "status": "in_progress",
            "entries": [],
        }],
    }
    # Run the op closure; it may raise HTTPException on ms-81 gate etc.
    new_data, result = op(data)
    _apply_operation_calls.append({
        "project_id": project_id,
        "op_name": op_name,
        "actor": actor,
        "result": copy.deepcopy(result),
        "data_after": copy.deepcopy(new_data),
    })
    return result


def _rebind_db():
    db_module = app_module.db
    prior = {}
    for name, mock in [
        ("get_trek", _mock_get_trek),
        ("save_trek", _mock_save_trek),
        ("list_treks", _mock_list_treks),
        ("find_user_by_email", _mock_find_user_by_email),
        ("get_or_create_user", _mock_get_or_create_user),
        ("get_user", _mock_get_user),
        # ms-95 / Trek task-add slug resolution: the endpoint now reads
        # both ``get_project`` (= fast path full id check) and
        # ``list_projects`` (= slug expansion scan), so the tests need
        # to mock both for the resolver to behave deterministically.
        ("get_project", _mock_get_project),
        ("list_projects", _mock_list_projects),
    ]:
        prior[name] = getattr(db_module, name, None)
        setattr(db_module, name, mock)
    # Mock apply_operation on the operations module imported by app_module.
    import operations as operations_mod
    prior["__apply_operation"] = operations_mod.apply_operation
    operations_mod.apply_operation = _fake_apply_operation
    return prior


def _restore_db(prior):
    db_module = app_module.db
    import operations as operations_mod
    for k, v in prior.items():
        if k == "__apply_operation":
            operations_mod.apply_operation = v
        elif v is None:
            if hasattr(db_module, k):
                delattr(db_module, k)
        else:
            setattr(db_module, k, v)


client = TestClient(app_module.app)


LEADER_UID = "uid-leader"
LEADER_EMAIL = "leader@x"
MEMBER_UID = "uid-member"
MEMBER_EMAIL = "member@x"
STRANGER_UID = "uid-stranger"
STRANGER_EMAIL = "stranger@x"


def _impersonate(uid: str, email: str = "") -> None:
    actual_email = email or f"{uid}@x"

    def _fake_auth():
        return {"sub": uid, "email": actual_email}
    app_module.app.dependency_overrides[app_module.require_auth] = _fake_auth


@pytest.fixture(autouse=True)
def reset_store():
    prior = _rebind_db()
    _treks.clear()
    _users_by_email.clear()
    _projects.clear()
    _apply_operation_calls.clear()
    for uid, email in (
        (LEADER_UID, LEADER_EMAIL),
        (MEMBER_UID, MEMBER_EMAIL),
        (STRANGER_UID, STRANGER_EMAIL),
    ):
        _users_by_email[email] = (uid, {"sub": uid, "email": email})
    # ms-95 / Trek task-add slug resolution: the legacy tests pre-date
    # the resolver and use short ids (``"proj-target"`` / ``"proj-A"`` /
    # ``"proj-OUTSIDE"``) as if they were full project_ids. Register the
    # set the legacy tests reference so the resolver's fast path
    # (``db.get_project`` hit) returns them as-is — that way the legacy
    # scope-rejection assertions still flow through the 403 path rather
    # than getting short-circuited to 404. New slug-resolution tests
    # below register their own projects with the realistic
    # ``<slug>-<hex6>`` shape so the slug-expansion path is exercised.
    for legacy_pid in ("proj-target", "proj-A", "proj-B", "proj-OUTSIDE"):
        _register_project(legacy_pid)
    app_module.app.dependency_overrides.clear()
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    try:
        yield
    finally:
        app_module._auth_enabled = prior_auth
        _treks.clear()
        _users_by_email.clear()
        _projects.clear()
        app_module.app.dependency_overrides.clear()
        _restore_db(prior)


def _seed_trek(scope: list[dict]) -> str:
    """Insert a trek doc with LEADER as leader + MEMBER joined."""
    trek_id = "tk-test-e2141"
    _treks[trek_id] = {
        "trek_id": trek_id,
        "title": "test trek",
        "description": "",
        "type": "persistent",
        "status": "active",
        "creator_actor": {"user_id": LEADER_UID, "email": LEADER_EMAIL},
        "members": [
            {"user_id": LEADER_UID, "email": LEADER_EMAIL, "role": "leader",
             "invited_by": LEADER_UID, "invited_at": "2026-06-22T00:00:00Z",
             "joined_at": "2026-06-22T00:00:00Z"},
            {"user_id": MEMBER_UID, "email": MEMBER_EMAIL, "role": "member",
             "invited_by": LEADER_UID, "invited_at": "2026-06-22T00:00:00Z",
             "joined_at": "2026-06-22T00:00:00Z"},
        ],
        "scope": scope,
        "leader_session_id": "sv-leader",
        "halt": None,
        "task_states": {},
        "archived_at": None,
        "created_at": "2026-06-22T00:00:00Z",
        "updated_at": "2026-06-22T00:00:00Z",
    }
    return trek_id


# ---------------------------------------------------------------------------
# Allowed path — AC #1
# ---------------------------------------------------------------------------


class TestAllowed:
    def test_member_can_add_task_to_milestone_in_scope(self):
        trek_id = _seed_trek([
            {"project": "proj-target", "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-target",
                "target_milestone": "ms-target",
                "description": "do the thing",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["project_id"] == "proj-target"
        assert body["milestone_id"] == "ms-target"
        assert body["trek_id"] == trek_id
        assert body["entry_id"].startswith("e-")
        # The op closure must have stamped meta.trek_id on the entry.
        assert _apply_operation_calls, "apply_operation must have been called"
        last_call = _apply_operation_calls[-1]
        assert last_call["project_id"] == "proj-target"
        assert last_call["op_name"] == "entry.create.trek_scope"
        # Verify meta.trek_id was stamped on the freshly-added entry.
        data_after = last_call["data_after"]
        ms = next(m for m in data_after["milestones"] if m["id"] == "ms-target")
        assert ms["entries"], "task must have been appended"
        added = ms["entries"][-1]
        assert added["meta"]["trek_id"] == trek_id
        assert added["meta"]["origin"] == "trek.task_add"

    def test_member_can_add_task_with_project_wide_scope(self):
        trek_id = _seed_trek([
            {"project": "proj-target"},  # project-wide
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-target",
                "target_milestone": "ms-target",
                "description": "thing via project-wide scope",
            },
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Rejected path — AC #2 (project not in scope)
# ---------------------------------------------------------------------------


class TestRejectedProjectNotInScope:
    def test_project_outside_scope_is_403(self):
        trek_id = _seed_trek([
            {"project": "proj-A", "milestone": "ms-10"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-OUTSIDE",
                "target_milestone": "ms-99",
                "description": "should not land",
            },
        )
        assert r.status_code == 403, r.text
        assert "project_not_in_scope" in r.text
        # And the write must not have happened.
        assert not _apply_operation_calls

    def test_milestone_mismatch_is_403_with_different_reason(self):
        trek_id = _seed_trek([
            {"project": "proj-A", "milestone": "ms-10"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "ms-999",
                "description": "should not land",
            },
        )
        assert r.status_code == 403, r.text
        assert "milestone_not_in_scope" in r.text


# ---------------------------------------------------------------------------
# Rejected path — AC #4 (scope only has task narrowing)
# ---------------------------------------------------------------------------


class TestRejectedScopeOnlyTask:
    def test_scope_only_task_narrowing_is_403(self):
        """SPEC AC #4: scope に単独 task しかない場合の起票禁止."""
        trek_id = _seed_trek([
            {"project": "proj-A", "task": "e-1234"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "ms-10",
                "description": "MS-grain enforcement test",
            },
        )
        assert r.status_code == 403, r.text
        assert "scope_only_has_task_narrowing" in r.text
        assert not _apply_operation_calls

    def test_scope_only_operation_narrowing_is_403(self):
        trek_id = _seed_trek([
            {"project": "proj-A", "operation": "op-7"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "ms-10",
                "description": "x",
            },
        )
        assert r.status_code == 403, r.text
        assert "scope_only_has_task_narrowing" in r.text


# ---------------------------------------------------------------------------
# Auth gating — non-member caller is rejected
# ---------------------------------------------------------------------------


class TestAuth:
    def test_non_member_cannot_add_task(self):
        trek_id = _seed_trek([
            {"project": "proj-A", "milestone": "ms-10"},
        ])
        _impersonate(STRANGER_UID, STRANGER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "ms-10",
                "description": "stranger should not land",
            },
        )
        assert r.status_code == 403, r.text
        # The pre-existing _load_trek_for_read visibility gate fires first
        # ("Not a member of this trek"); our endpoint's explicit check only
        # triggers in test paths where _auth_enabled=False. Either way the
        # stranger gets locked out before any scope walk.
        assert "member" in r.text.lower()


# ---------------------------------------------------------------------------
# Input validation — malformed target / missing fields
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_description_is_400(self):
        trek_id = _seed_trek([
            {"project": "proj-A", "milestone": "ms-10"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "ms-10",
                "description": "",
            },
        )
        assert r.status_code == 400, r.text

    def test_target_with_op_prefix_is_400(self):
        trek_id = _seed_trek([
            {"project": "proj-A", "milestone": "ms-10"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "op-7",  # not ms-
                "description": "should refuse",
            },
        )
        assert r.status_code == 400, r.text
        assert "ms-" in r.text or "MS-grain" in r.text

    def test_unknown_trek_returns_404(self):
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            "/api/treks/tk-does-not-exist/task-add",
            json={
                "target_project": "proj-A",
                "target_milestone": "ms-10",
                "description": "x",
            },
        )
        assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# ms-95 / Trek task-add slug ↔ full project_id resolution
#
# Background — cross-project Trek dogfood (PE + LPS, 2026-06-27) found
# that scopes stored as **slug** (= the human-friendly name part the CLI
# accepts at ``beacon trek plan --add-scope <slug>:<ms>``) crash the
# task-add endpoint because ``operations.apply_operation`` needs the
# **full project_id** (= ``"<slug>-<hex6>"`` minted by ``cmd_cloud_setup``).
# Symptoms before the fix:
#   * slug stored + slug request → 500 (LookupError in apply_operation)
#   * slug stored + full id request → 403 project_not_in_scope (literal
#     string equality miss between scope and request)
# The endpoint now canonicalises both sides via
# ``_resolve_canonical_project_id`` so the scope match runs on equal
# footing and apply_operation always receives a real project_id.
# ---------------------------------------------------------------------------


class TestSlugResolution:
    """Cover the four canonicalisation paths the bug touches."""

    PE_SLUG = "profile-extractor"
    PE_FULL_ID = "profile-extractor-276d28"

    def _seed_pe_project(self, owner: str = "") -> None:
        _register_project(self.PE_FULL_ID, owner=owner)

    def test_slug_stored_scope_accepts_slug_request(self):
        """Failure case 1 from the dogfood report → was 500, must be 200.

        The scope was added through ``beacon trek plan --add-scope
        profile-extractor:ms-target`` which stores the user-typed slug.
        A subsequent ``beacon trek task add tk-... --target
        profile-extractor:ms-target`` sends the same slug. Both sides
        must resolve to the full project_id so the scope match passes
        AND apply_operation sees a real id.
        """
        self._seed_pe_project()
        trek_id = _seed_trek([
            {"project": self.PE_SLUG, "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": self.PE_SLUG,
                "target_milestone": "ms-target",
                "description": "PE dogfood reproduction case",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Response carries the canonical full id, not the slug, so
        # downstream consumers (= meta.trek_id audit, WS broadcast) see
        # the same project_id Firestore is keyed by.
        assert body["project_id"] == self.PE_FULL_ID
        assert body["entry_id"].startswith("e-")
        # apply_operation was invoked with the resolved full id (= no
        # LookupError, no 500).
        assert _apply_operation_calls
        assert _apply_operation_calls[-1]["project_id"] == self.PE_FULL_ID

    def test_slug_stored_scope_accepts_full_id_request(self):
        """Failure case 2 from the dogfood report → was 403, must be 200.

        Same slug-stored scope, but the caller specifies the full
        project_id (e.g. because Web UI fills it in). The scope match
        must canonicalise the scope side too so the comparison
        succeeds.
        """
        self._seed_pe_project()
        trek_id = _seed_trek([
            {"project": self.PE_SLUG, "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": self.PE_FULL_ID,
                "target_milestone": "ms-target",
                "description": "full id request against slug-stored scope",
            },
        )
        assert r.status_code == 200, r.text
        assert _apply_operation_calls[-1]["project_id"] == self.PE_FULL_ID

    def test_full_id_stored_scope_accepts_slug_request(self):
        """Forward-looking: once scope-add canonicalises (= follow-up),
        scope entries will be stored as full ids. Slug requests must
        still resolve to that same id so this symmetry is intentional.
        """
        self._seed_pe_project()
        trek_id = _seed_trek([
            {"project": self.PE_FULL_ID, "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": self.PE_SLUG,
                "target_milestone": "ms-target",
                "description": "slug request against full-id scope",
            },
        )
        assert r.status_code == 200, r.text
        assert _apply_operation_calls[-1]["project_id"] == self.PE_FULL_ID

    def test_ambiguous_slug_is_404(self):
        """Two projects with the same slug prefix → the resolver refuses
        to guess. The user must supply the full id explicitly.

        This guards against silent cross-tenant write (= picking the
        wrong project just because its id sorts first).
        """
        _register_project("profile-extractor-aaaaaa")
        _register_project("profile-extractor-bbbbbb")
        trek_id = _seed_trek([
            {"project": "profile-extractor", "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "profile-extractor",
                "target_milestone": "ms-target",
                "description": "ambiguous slug",
            },
        )
        assert r.status_code == 404, r.text
        assert "ambiguous" in r.text or "not found" in r.text
        assert not _apply_operation_calls

    def test_unresolvable_target_project_is_404_not_500(self):
        """Regression guard for the original 500 path.

        Even when the caller sends a target_project that doesn't exist
        anywhere (= typo, deleted project), the endpoint must return a
        clean 404 rather than crash on a downstream ``LookupError``.
        """
        # No project registered for "ghost-project". The slug-stored
        # scope match would have succeeded pre-fix (literal equality
        # against the scope entry), then apply_operation would crash.
        trek_id = _seed_trek([
            {"project": "ghost-project", "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": "ghost-project",
                "target_milestone": "ms-target",
                "description": "should never reach apply_op",
            },
        )
        assert r.status_code == 404, r.text
        assert not _apply_operation_calls

    def test_resolver_respects_owner_scoping(self):
        """Slug expansion is filtered by the requesting user's
        ``list_projects`` view — strangers' projects can't be reached
        even if their slug happens to match.
        """
        # PE project owned by someone else; the requesting MEMBER must
        # not see it via slug expansion.
        _register_project(self.PE_FULL_ID, owner="someone-else-uid")
        trek_id = _seed_trek([
            {"project": self.PE_SLUG, "milestone": "ms-target"},
        ])
        _impersonate(MEMBER_UID, MEMBER_EMAIL)
        r = client.post(
            f"/api/treks/{trek_id}/task-add",
            json={
                "target_project": self.PE_SLUG,
                "target_milestone": "ms-target",
                "description": "should not reach foreign project",
            },
        )
        assert r.status_code == 404, r.text
        assert not _apply_operation_calls
