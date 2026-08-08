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
        self.src = _read(APP_PY)

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
        self.src = _read(APP_PY)

    def test_endpoint_registered(self):
        assert '@app.get("/api/admin/projects/ownerless")' in self.src, (
            "audit endpoint must be registered so admins can inventory the residue"
        )

    def test_endpoint_requires_admin(self):
        m = re.search(
            r'@app\.get\("/api/admin/projects/ownerless"\)\s*\n'
            r"def\s+admin_list_ownerless_projects\([^)]*\)[^:]*:(.*?)return\s+\{",
            self.src,
            re.DOTALL,
        )
        assert m, "could not locate admin_list_ownerless_projects body"
        body = m.group(1)
        assert "_require_admin(user)" in body, (
            "endpoint must gate on _require_admin so stolen user tokens "
            "cannot enumerate ownerless projects"
        )
