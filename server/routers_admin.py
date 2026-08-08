"""Instance-admin ("/api/admin/*") router.

ms-127 e-4869 (B フェーズ): the admin endpoints extracted from the server/app.py
god-module, following the auth-router型 established by server/routers_me.py and
server/routers_orgs.py (make_router(require_auth, *, ...injected helpers)).

Pure move: every route body is verbatim from app.py's ``admin_*`` handlers —
same paths, same shapes, no behavior change. All mutating routes keep the
``require_admin`` gate (so a stolen non-admin token can't reach them).

Injected dependencies (owned by app.py, passed in to avoid an import cycle —
app.py imports this module, so this module must not import app.py back):

- ``require_auth``            — the host app's identity dependency.
- ``require_admin``           — ``app._require_admin``. Raises 403 unless the
                                caller is an admin (and short-circuits in
                                auth-disabled mode). Still owned by app.py
                                because a non-admin endpoint there also calls it,
                                so the admin rule stays single-sourced. It reads
                                app.py's ``_auth_enabled`` internally, so passing
                                the function (not a snapshot) keeps the dev-mode
                                bypass correct per-request.
- ``apply_op_and_broadcast``  — ``app._apply_op_and_broadcast``. The
                                transactional project-mutation + broadcast helper
                                used by ~25 endpoints; injected so the trash
                                sweep / owner transfer keep the identical write
                                path without duplicating it or importing app.py.

``db`` mirrors app.py's binding — ``store_router as db`` (e-1544 backend
routing). ``core`` / ``operations`` are the same modules app.py imports.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import core
import operations
import store_router as db  # e-1544: same backend-routing binding app.py uses


class AdminUserUpdate(BaseModel):
    role: str  # admin | user


class AdminOwnerTransfer(BaseModel):
    new_owner_id: str


def make_router(
    require_auth: Callable,
    *,
    require_admin: Callable[[dict], None],
    apply_op_and_broadcast: Callable[..., Any],
) -> APIRouter:
    """Build the /api/admin/* router with the host app's auth + admin helpers.

    Called once from app.py and mounted via::

        app.include_router(make_router(
            require_auth,
            require_admin=_require_admin,
            apply_op_and_broadcast=_apply_op_and_broadcast,
        ))

    Keyword-only + ``Callable``-typed injected helpers, with a construction-time
    callability check (same rationale as routers_orgs.make_router): a mis-wire
    fails at mount rather than at request time.
    """
    for _name, _dep in (
        ("require_auth", require_auth),
        ("require_admin", require_admin),
        ("apply_op_and_broadcast", apply_op_and_broadcast),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_admin.make_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    @router.get("/api/admin/users")
    def admin_list_users(user: dict = Depends(require_auth)):
        """List all users (admin only)."""
        require_admin(user)
        return db.list_users()

    @router.patch("/api/admin/users/{user_id}")
    def admin_update_user(user_id: str, body: AdminUserUpdate,
                          user: dict = Depends(require_auth)):
        """Update a user's system role (admin only)."""
        require_admin(user)
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
        if user_id == user.get("sub"):
            raise HTTPException(status_code=400, detail="Cannot change your own admin role")
        if not db.update_user(user_id, {"role": body.role}):
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        return {"user_id": user_id, "role": body.role}

    @router.delete("/api/admin/users/{user_id}")
    def admin_delete_user(user_id: str, user: dict = Depends(require_auth)):
        """Delete a user (admin only)."""
        require_admin(user)
        if user_id == user.get("sub"):
            raise HTTPException(status_code=400, detail="Cannot delete yourself")
        if not db.delete_user(user_id):
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
        return {"user_id": user_id, "status": "deleted"}

    @router.get("/api/admin/projects")
    def admin_list_projects(user: dict = Depends(require_auth)):
        """List all projects summary (admin only). No project content exposed."""
        require_admin(user)
        return db.list_all_projects()

    @router.get("/api/admin/projects/ownerless")
    def admin_list_ownerless_projects(user: dict = Depends(require_auth)):
        """Audit endpoint: list projects that have no owner field (ms-95 / e-2794).

        Before the 2026-07-03 fix, ownerless projects leaked into every user's
        project listing via the ``list_projects`` migration-period fallthrough.
        That path is now closed (deny by default). This endpoint lets an admin
        inventory the residue so owners can be backfilled or the projects
        archived. Returns rows with minimal metadata — no milestones / entries.
        """
        require_admin(user)
        rows = []
        for p in db.list_all_projects():
            if p.get("owner"):
                continue
            pid = p.get("project_id", "")
            full = db.get_project(pid) or {} if pid else {}
            rows.append({
                "project_id": pid,
                "name": p.get("name", "") or full.get("name", ""),
                "objective": (full.get("objective") or "")[:200],
                "archived": bool(full.get("archived", False)),
                "member_count": p.get("member_count", 0),
                "milestone_count": p.get("milestone_count", 0),
                "updated_at": p.get("updated_at", ""),
            })
        return {"count": len(rows), "projects": rows}

    @router.delete("/api/admin/projects/{project_id}")
    def admin_delete_project(project_id: str, user: dict = Depends(require_auth)):
        """Delete a project (admin only)."""
        require_admin(user)
        if not db.delete_project(project_id):
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
        return {"project_id": project_id, "status": "deleted"}

    @router.post("/api/admin/trash/sweep")
    def admin_trash_sweep(days: int = 30,
                          dry_run: bool = False,
                          user: dict = Depends(require_auth)):
        """30-day soft-delete auto-purge for the whole instance (ms-14 e-826/e-991).

        Walks every project and hard-deletes milestones / tasks / documents
        whose ``cancelled_at`` (or ``deleted_at`` for docs) is older than
        ``days``. For each project we write ONE changelog entry summarising
        the swept ids so the audit trail survives the data being gone.

        Intended caller: a Cloud Scheduler job firing daily. Admin role is
        required so a stolen user token can't trigger destructive sweeps.

        ``dry_run=true`` returns the would-be counts without writing.
        """
        require_admin(user)
        days = max(1, days)
        summary = {
            "days": days,
            "dry_run": dry_run,
            "projects_scanned": 0,
            "ms_purged": 0,
            "task_purged": 0,
            "doc_purged": 0,
            "projects": [],
        }
        for proj in db.list_all_projects():
            pid = proj["project_id"]
            summary["projects_scanned"] += 1
            # MS + task sweep: in dry_run we read once and compute counts;
            # in apply mode we mutate the project doc transactionally so
            # concurrent writes can't tear the array.
            if dry_run:
                # Use load_project_consistent so v2 (subcollection) projects
                # report accurate counts — db.get_project alone would return
                # the meta doc with no milestones[] and dry-run would always
                # report 0 sweepable items.
                try:
                    data_now = operations.load_project_consistent(pid)
                except LookupError:
                    data_now = {}
                per_proj_result = core.sweep_trashed_in_project(
                    data_now, days=days, apply=False,
                )
            else:
                def _sweep_op(data, _days=days):
                    result = core.sweep_trashed_in_project(
                        data, days=_days, apply=True,
                    )
                    return data, result
                per_proj_result = apply_op_and_broadcast(
                    pid, _sweep_op,
                    op_name="trash.sweep",
                    actor="system",
                    reason=f"{days}d auto-purge",
                )
            # Doc sweep: docs live in the subcollection, separate path.
            doc_purged = db.sweep_trashed_documents(pid, days=days, dry_run=dry_run)
            ms_ids = per_proj_result.get("ms_purged_ids", [])
            task_ids = per_proj_result.get("task_purged_ids", [])
            summary["ms_purged"] += len(ms_ids)
            summary["task_purged"] += len(task_ids)
            summary["doc_purged"] += len(doc_purged)
            if ms_ids or task_ids or doc_purged:
                # Audit entry per project. The standard apply_operation hook
                # already wrote a 'trash.sweep' entry without ids; this adds
                # the item-level detail so a later reader can answer
                # "what was purged from project X on YYYY-MM-DD?".
                if not dry_run:
                    db.append_changelog(pid, {
                        "op": "trash.sweep.detail",
                        "actor": "system",
                        "reason": f"{days}d auto-purge",
                        "project_id": pid,
                        "payload": {
                            "days": days,
                            "milestone_ids": ms_ids,
                            "task_ids": task_ids,
                            "doc_ids": doc_purged,
                        },
                    })
                summary["projects"].append({
                    "project_id": pid,
                    "ms_purged": ms_ids,
                    "task_purged": task_ids,
                    "doc_purged": doc_purged,
                })
        return summary

    @router.patch("/api/admin/projects/{project_id}/owner")
    def admin_transfer_owner(project_id: str, body: AdminOwnerTransfer,
                             user: dict = Depends(require_auth)):
        """Transfer project ownership (admin only)."""
        require_admin(user)
        # Validate the new owner exists before entering the transaction so we
        # can return a clean 404 without aborting a txn.
        new_owner = db.get_user(body.new_owner_id)
        if new_owner is None:
            raise HTTPException(
                status_code=404, detail=f"User '{body.new_owner_id}' not found"
            )

        def op(data: dict):
            data["owner"] = body.new_owner_id
            # Remove new owner from members if present
            data["members"] = [
                m for m in data.get("members", []) if m.get("user_id") != body.new_owner_id
            ]
            return data, {
                "project_id": project_id,
                "new_owner": body.new_owner_id,
                "email": new_owner.get("email", ""),
            }

        return apply_op_and_broadcast(
            project_id, op, op_name="admin.transfer_owner", actor=user.get("sub", ""),
        )

    @router.get("/api/admin/me")
    def admin_check(user: dict = Depends(require_auth)):
        """Check if current user is admin."""
        user_data = db.get_user(user.get("sub", ""))
        is_admin = user_data.get("role") == "admin" if user_data else False
        return {"is_admin": is_admin}

    return router
