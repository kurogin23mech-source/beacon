"""ms-95 / e-2794 — Ownerless project visibility leak fix (2026-07-03).

Before the fix, ``list_projects(user_id=X)`` returned any project whose
``owner`` field was empty/missing to every authenticated user via a
"migration period" fallthrough. A user signed in with a fresh Google ID
saw all ownerless projects in their empty-state — a multi-tenant boundary
violation.

This pins the four changes that close the leak:

1. ``firestore_client.list_projects`` — deny by default when ``owner`` is
   falsy (removes the ``# Projects without owner are visible to all``
   fallthrough).
2. ``app.create_project`` — reject requests whose auth payload lacks
   ``sub`` (previously fell through to ``owner=""``).
3. ``app.me_list_projects`` — remove the ``role = "member"`` migration
   fallback so ownerless projects can't sneak in via the /api/me path.
4. ``/api/admin/projects/ownerless`` — new admin-only audit endpoint that
   lists projects still lacking an owner so operators can backfill or
   archive them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRESTORE_PY = ROOT / "server" / "firestore_client.py"
APP_PY = ROOT / "server" / "app.py"
# ms-127 e-4869: me_list_projects moved out of the app.py god-module into the
# /api/me/* router. The no-member-fallback guard below now lives there.
ROUTERS_ME_PY = ROOT / "server" / "routers_me.py"
# ms-127 e-4869: the admin endpoints (incl. the ownerless audit) moved into the
# /api/admin/* router; the _require_admin gate is injected as `require_admin`.
ROUTERS_ADMIN_PY = ROOT / "server" / "routers_admin.py"
# ms-127 e-4871 (PR1): create_project moved into the /api/projects/* router
# (nested in make_router; guards injected). The sub-required guard now lives there.
ROUTERS_PROJECTS_PY = ROOT / "server" / "routers_projects.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class TestListProjectsDenyByDefault:
    def setup_method(self, _method):
        self.src = _read(FIRESTORE_PY)

    def test_migration_period_fallthrough_removed(self):
        # The exact old comment must be gone. Its presence would mean the
        # leak is back.
        assert (
            "Projects without owner are visible to all (migration period)"
            not in self.src
        ), "leaky fallthrough comment must not resurface"

    def test_deny_by_default_when_owner_missing(self):
        m = re.search(
            r"def list_projects\([^)]*\)[^:]*:(.*?)\n\n\n",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate list_projects body"
        body = m.group(1)
        # Body must contain a `continue` guard that skips rows whose owner
        # is falsy, before the owner/members membership check runs.
        assert re.search(
            r"if\s+not\s+owner\s*:\s*\n(?:.*\n){0,6}?\s*continue",
            body,
        ), "list_projects must skip ownerless projects (deny by default)"


class TestCreateProjectRequiresSub:
    def setup_method(self, _method):
        # create_project now lives in routers_projects.py (ms-127 e-4871 PR1 split).
        self.src = _read(ROUTERS_PROJECTS_PY)

    def test_create_project_rejects_missing_sub(self):
        m = re.search(
            r"def create_project\([^)]*\)[^:]*:(.*?)return\s*\{[^}]*\"status\":\s*\"created\"",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate create_project body"
        body = m.group(1)
        # No more silent fallback to empty string.
        assert 'user.get("sub", "")' not in body, (
            "create_project must not silently fall back to empty owner"
        )
        # There must be an explicit guard that raises when sub is missing.
        assert re.search(
            r"owner_sub\s*=\s*user\.get\(['\"]sub['\"]\)",
            body,
        ), "must read sub via a named variable so it can be checked"
        assert re.search(
            r"if\s+not\s+owner_sub\s*:\s*\n\s*raise\s+HTTPException\(\s*status_code\s*=\s*401",
            body,
        ), "must 401 when sub is missing instead of writing owner=''"


class TestMeListProjectsNoMemberFallback:
    def setup_method(self, _method):
        # me_list_projects now lives in routers_me.py (ms-127 e-4869 split).
        self.src = _read(ROUTERS_ME_PY)

    def test_migration_member_fallback_removed(self):
        m = re.search(
            r"def me_list_projects\([^)]*\)[^:]*:(.*?)return\s+result",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate me_list_projects body"
        body = m.group(1)
        assert "Migration-period projects without owner are visible" not in body, (
            "the legacy migration-period fallback comment must be gone"
        )
        # The new behaviour: unresolved role → warn + continue (skip),
        # never a silent 'member' promotion.
        assert re.search(
            r"if\s+not\s+role\s*:\s*\n(?:.*\n){0,8}?\s*continue",
            body,
        ), "unresolved role must skip the project, not fall back to 'member'"


class TestAdminOwnerlessEndpoint:
    def setup_method(self, _method):
        # admin endpoints now live in routers_admin.py (ms-127 e-4869 split);
        # decorators are @router.* and the admin gate is the injected
        # `require_admin` (app.py owns `_require_admin` and passes it in).
        self.src = _read(ROUTERS_ADMIN_PY)

    def test_endpoint_registered(self):
        assert '@router.get("/api/admin/projects/ownerless")' in self.src, (
            "audit endpoint must be registered so admins can inventory the residue"
        )

    def test_endpoint_requires_admin(self):
        m = re.search(
            r'@router\.get\("/api/admin/projects/ownerless"\)\s*\n'
            r"\s*def\s+admin_list_ownerless_projects\([^)]*\)[^:]*:(.*?)return\s+\{",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate admin_list_ownerless_projects body"
        body = m.group(1)
        assert "require_admin(user)" in body, (
            "endpoint must gate on _require_admin so stolen user tokens "
            "cannot enumerate ownerless projects"
        )


# ---------------------------------------------------------------------------
# ms-158 / e-5757 — the direct-by-id role check must ALSO deny ownerless
# projects. The tests above pin the 2026-07-03 *listing* fix, but `_get_role`
# (the role primitive behind every REST/WS endpoint) still fell through to
# "editor" for any signed-in user hitting an ownerless project by id. A
# stranger who knew or guessed a project_id could therefore read and write it,
# even though it never showed up in their listing. These behavioral tests pin
# the close: deny by default here too, members unaffected.
# ---------------------------------------------------------------------------

class TestGetRoleOwnerlessFallOpenRemoved:
    """Static guard: the fail-open migration relic must not resurface."""

    def setup_method(self, _method):
        self.src = _read(APP_PY)

    def test_migration_comment_removed(self):
        assert "Migration: ownerless projects are accessible to all" not in self.src, (
            "the fail-open ownerless->editor fallthrough comment must be gone "
            "(ms-158 / e-5757)"
        )

    def test_get_role_has_no_ownerless_editor_grant(self):
        m = re.search(
            r"def _get_role\([^)]*\)[^:]*:(.*?)\n\n\ndef ",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate _get_role body"
        body = m.group(1)
        # No branch may return "editor" for a missing/empty owner.
        assert not re.search(
            r"if\s+not\s+data\.get\(['\"]owner['\"]\)\s*:\s*\n\s*return\s+['\"]editor['\"]",
            body,
        ), "_get_role must not grant editor to non-members of ownerless projects"


import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app as app_module  # noqa: E402


class TestGetRoleOwnerlessDenyByDefault:
    """Behavioral pin on the role primitive itself (auth enabled)."""

    def test_stranger_denied_on_ownerless_project(self, monkeypatch):
        # Non-member, non-owner user reaching an ownerless project by id.
        # Previously returned "editor" (fail-open); must now be "" (no access).
        monkeypatch.setattr(app_module, "_auth_enabled", True)
        proj = {"name": "orphan", "members": []}  # no "owner" field at all
        assert app_module._get_role(proj, {"sub": "stranger"}) == "", (
            "ownerless project must not grant editor to a non-member"
        )

    def test_stranger_denied_when_owner_is_blank(self, monkeypatch):
        # `owner` present but empty is still ownerless — same deny.
        monkeypatch.setattr(app_module, "_auth_enabled", True)
        proj = {"name": "orphan", "owner": "", "members": []}
        assert app_module._get_role(proj, {"sub": "stranger"}) == ""

    def test_member_of_ownerless_project_unaffected(self, monkeypatch):
        # A member keeps their role — the members loop runs before the
        # ownerless check, so closing the fail-open never touches them.
        monkeypatch.setattr(app_module, "_auth_enabled", True)
        proj = {"name": "orphan",
                "members": [{"user_id": "u2", "role": "editor"}]}
        assert app_module._get_role(proj, {"sub": "u2"}) == "editor"

    def test_owned_project_stranger_still_denied(self, monkeypatch):
        # Ordinary owned-project path is unchanged (regression guard).
        monkeypatch.setattr(app_module, "_auth_enabled", True)
        proj = {"name": "p", "owner": "u1", "members": []}
        assert app_module._get_role(proj, {"sub": "stranger"}) == ""

    def test_dev_mode_unaffected(self, monkeypatch):
        # Auth disabled (dev/local) → everyone is owner. The fix only changes
        # production (auth-enabled) behavior.
        monkeypatch.setattr(app_module, "_auth_enabled", False)
        proj = {"name": "orphan", "members": []}
        assert app_module._get_role(proj, {"sub": "anyone"}) == "owner"
