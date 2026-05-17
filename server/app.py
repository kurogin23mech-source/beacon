"""Beacon API - FastAPI backend for project management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

# Add lib/ to path so we can import core
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from fastapi import FastAPI, HTTPException, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

import core
import firestore_client as db

app = FastAPI(title="Beacon API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Audit Logging
# ---------------------------------------------------------------------------

_audit_logger = logging.getLogger("beacon.audit")
_audit_logger.setLevel(logging.INFO)

# Ensure a handler exists (Cloud Run captures stdout)
if not _audit_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_logger.addHandler(_handler)
_audit_logger.propagate = False

# Mutating methods that should be audit-logged
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# All mutations under /api/projects/* and /api/admin/*
import re
_AUDIT_PATHS = re.compile(r"^/api/(?:projects/[^/]+|admin/)")


def _extract_project_id(path: str) -> str:
    m = re.match(r"^/api/projects/([^/]+)", path)
    return m.group(1) if m else ""


_RESOURCE_SINGULAR = {
    "members": "member",
    "documents": "document",
    "milestones": "milestone",
    "entries": "entry",
    "retros": "retro",
}


def _derive_action(method: str, path: str) -> str:
    """Derive a semantic action name from HTTP method + path."""
    if "/admin/users" in path:
        return f"admin.user.{method.lower()}"
    if "/admin/projects" in path:
        return f"admin.project.{method.lower()}"
    for plural, singular in _RESOURCE_SINGULAR.items():
        if f"/{plural}" in path:
            return f"{singular}.{method.lower()}"
    if "/log" in path:
        return "project.log"
    if "/summary" in path:
        return "project.summary"
    return f"project.{method.lower()}"


def _extract_resource(path: str) -> str:
    """Extract the resource type from a path segment."""
    if "/admin/users" in path:
        return "admin.user"
    if "/admin/projects" in path:
        return "admin.project"
    for resource in ("members", "documents", "milestones", "entries", "retros", "log", "summary"):
        if f"/{resource}" in path:
            return resource
    return "project"


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Emit a structured JSON audit log line for security-sensitive mutations."""

    async def dispatch(self, request: Request, call_next):
        if request.method not in _AUDIT_METHODS or not _AUDIT_PATHS.match(request.url.path):
            return await call_next(request)

        start = time.time()
        response: Response = await call_next(request)
        elapsed_ms = int((time.time() - start) * 1000)

        # Extract user info from request state (set by require_auth if called)
        user_id = getattr(request.state, "audit_user_id", "")
        email = getattr(request.state, "audit_email", "")
        path = request.url.path

        log_entry = {
            "severity": "INFO",
            "type": "audit",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "action": _derive_action(request.method, path),
            "resource": _extract_resource(path),
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "user_id": user_id,
            "email": email,
            "project_id": _extract_project_id(path),
            "ip": request.headers.get("x-forwarded-for", request.client.host if request.client else ""),
            "user_agent": request.headers.get("user-agent", ""),
            "elapsed_ms": elapsed_ms,
        }
        _audit_logger.info(json.dumps(log_entry))
        return response


app.add_middleware(AuditLogMiddleware)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)

# Set BEACON_API_AUTH=0 to disable auth (for local dev / testing)
_auth_enabled = os.environ.get("BEACON_API_AUTH", "1") != "0"


def _verify_id_token(token: str) -> dict:
    """Verify a Google ID token and return the claims."""
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests

    try:
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request()
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    return claims


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """FastAPI dependency that enforces Bearer token auth and auto-registers users."""
    if not _auth_enabled:
        request.state.audit_user_id = "dev"
        request.state.audit_email = "dev@local"
        return {"sub": "dev", "email": "dev@local"}
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authorization header required")
    claims = _verify_id_token(credentials.credentials)
    # Auto-register user on first login
    user_id = claims.get("sub", "")
    email = claims.get("email", "")
    if user_id:
        db.get_or_create_user(user_id, email)
    # Store for audit middleware
    request.state.audit_user_id = user_id
    request.state.audit_email = email
    return claims


def _require_admin(user: dict) -> None:
    """Raise 403 if user is not an admin."""
    if not _auth_enabled:
        return
    user_data = db.get_user(user.get("sub", ""))
    if not user_data or user_data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_role(data: dict, user: dict) -> str:
    """Return user's role: 'owner', 'editor', 'viewer', or '' (no access)."""
    if not _auth_enabled:
        return "owner"
    uid = user.get("sub", "")
    if data.get("owner") == uid:
        return "owner"
    for m in data.get("members", []):
        if m.get("user_id") == uid:
            return m.get("role", "viewer")
    # Migration: ownerless projects are accessible to all
    if not data.get("owner"):
        return "editor"
    return ""


def _load(project_id: str, user: dict | None = None) -> dict:
    data = db.get_project(project_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if user and _auth_enabled:
        role = _get_role(data, user)
        if not role:
            raise HTTPException(status_code=403, detail="Access denied")
    return data


def _require_write(data: dict, user: dict) -> None:
    """Raise 403 if user doesn't have write access (editor or owner)."""
    role = _get_role(data, user)
    if role not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Write access required (editor or owner)")


def _save(project_id: str, data: dict) -> None:
    core.validate_project(data)
    db.save_project(project_id, data)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str
    objective: str = ""

class MilestoneCreate(BaseModel):
    title: str
    target_date: str = ""
    description: str = ""

class MilestoneUpdate(BaseModel):
    title: str = ""
    progress: str = ""
    target_date: str = ""
    status: str = ""
    description: str = ""

class EntryCreate(BaseModel):
    description: str
    type: str = "task"
    date: str = ""
    detail: str = ""

class EntryUpdate(BaseModel):
    description: str = ""
    status: str = ""
    detail: str = ""
    date: str = ""

class LogCommit(BaseModel):
    hash: str
    message: str
    date: str
    summary: str = ""
    ms_id: str = ""
    progress: str = ""

class SummaryUpdate(BaseModel):
    text: str

class RetroCreate(BaseModel):
    content: str

class DocumentSave(BaseModel):
    title: str
    content: str
    scope: str | None = None  # core | spec | memo

class MemberInvite(BaseModel):
    email: str
    role: str = "viewer"  # viewer | editor


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def list_projects(include_archived: bool = False, user: dict = Depends(require_auth)):
    """List projects owned by or shared with the current user."""
    return db.list_projects(user_id=user.get("sub"), include_archived=include_archived)


@app.post("/api/projects/{project_id}/archive")
def archive_project(project_id: str, user: dict = Depends(require_auth)):
    """Archive a project (soft delete — hidden from default listing)."""
    data = _load(project_id, user)
    if _get_role(data, user) != "owner":
        raise HTTPException(status_code=403, detail="Only the project owner can archive")
    data["archived"] = True
    _save(project_id, data)
    return {"status": "archived", "project_id": project_id}


@app.post("/api/projects/{project_id}/unarchive")
def unarchive_project(project_id: str, user: dict = Depends(require_auth)):
    """Restore an archived project."""
    data = _load(project_id, user)
    if _get_role(data, user) != "owner":
        raise HTTPException(status_code=403, detail="Only the project owner can unarchive")
    data["archived"] = False
    _save(project_id, data)
    return {"status": "unarchived", "project_id": project_id}


@app.post("/api/projects/{project_id}")
def create_project(project_id: str, body: ProjectCreate,
                   user: dict = Depends(require_auth)):
    """Create a new project (like beacon init)."""
    existing = db.get_project(project_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Project '{project_id}' already exists")
    data = {
        "name": body.name,
        "objective": body.objective,
        "milestones": [],
        "owner": user.get("sub", ""),
        "members": [],
    }
    _save(project_id, data)
    return {"status": "created", "project_id": project_id}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, user: dict = Depends(require_auth)):
    return _load(project_id, user)


@app.put("/api/projects/{project_id}")
def put_project(project_id: str, body: dict,
                user: dict = Depends(require_auth)):
    try:
        core.validate_project(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Auto-set owner if missing (e.g. cloud push from local)
    if not body.get("owner") and _auth_enabled:
        body["owner"] = user.get("sub", "")
    db.save_project(project_id, body)
    return {"status": "ok", "project_id": project_id}


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones")
def create_milestone(project_id: str, body: MilestoneCreate,
                     user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    ms_id = core.milestone_add(data, body.title, body.target_date,
                               description=body.description)
    _save(project_id, data)
    return {"ms_id": ms_id, "title": body.title}


@app.get("/api/projects/{project_id}/milestones/{ms_id}")
def get_milestone(project_id: str, ms_id: str,
                  user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    for ms in data["milestones"]:
        if ms["id"] == ms_id:
            entries = ms.get("entries", [])
            total, done = core.count_task_status(entries)
            return {
                **ms,
                "total_tasks": total,
                "done_tasks": done,
                "entries": core.entries_to_json(entries),
            }
    raise HTTPException(status_code=404, detail=f"Milestone '{ms_id}' not found")


@app.patch("/api/projects/{project_id}/milestones/{ms_id}")
def update_milestone(project_id: str, ms_id: str, body: MilestoneUpdate,
                     user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        ms = core.milestone_update(
            data, ms_id,
            title=body.title, progress=body.progress,
            target_date=body.target_date, status=body.status,
            description=body.description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "title": ms["title"], "status": ms["status"],
            "progress": ms.get("progress", 0)}


@app.post("/api/projects/{project_id}/milestones/{ms_id}/start")
def start_milestone(project_id: str, ms_id: str,
                    user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        ms = core.milestone_start(data, ms_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "title": ms["title"], "status": "in_progress"}


@app.post("/api/projects/{project_id}/milestones/{ms_id}/done")
def done_milestone(project_id: str, ms_id: str,
                   user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        ms = core.milestone_done(data, ms_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "title": ms["title"], "status": "done"}


@app.delete("/api/projects/{project_id}/milestones/{ms_id}")
def delete_milestone(project_id: str, ms_id: str,
                     user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        ms = core.milestone_delete(data, ms_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"id": ms["id"], "status": "cancelled"}


# ---------------------------------------------------------------------------
# Entries (tasks / commits / notes)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones/{ms_id}/entries")
def create_entry(project_id: str, ms_id: str, body: EntryCreate,
                 user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        eid = core.task_add(
            data, ms_id, body.description,
            entry_type=body.type, date=body.date, detail=body.detail,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"entry_id": eid, "description": body.description}


@app.patch("/api/projects/{project_id}/entries/{entry_id}")
def update_entry(project_id: str, entry_id: str, body: EntryUpdate,
                 user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        ms, entry = core.task_update(
            data, entry_id,
            description=body.description, status=body.status,
            detail=body.detail, date=body.date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return core.entries_to_json([entry])[0]


@app.post("/api/projects/{project_id}/entries/{entry_id}/done")
def done_entry(project_id: str, entry_id: str,
               user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    import datetime
    today = datetime.date.today().isoformat()
    try:
        ms, entry = core.task_done(data, entry_id, date=today)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"entry_id": entry_id, "status": "done"}


@app.delete("/api/projects/{project_id}/entries/{entry_id}")
def delete_entry(project_id: str, entry_id: str,
                 user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        entry = core.task_delete(data, entry_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return {"entry_id": entry_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Log (commit recording)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/log")
def log_commit(project_id: str, body: LogCommit,
               user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    try:
        result = core.log_commit(
            data, ms_id=body.ms_id, commit_hash=body.hash,
            message=body.message, date=body.date,
            summary=body.summary, progress=body.progress,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _save(project_id, data)
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@app.patch("/api/projects/{project_id}/summary")
def update_summary(project_id: str, body: SummaryUpdate,
                   user: dict = Depends(require_auth)):
    data = _load(project_id, user)
    _require_write(data, user)
    data["summary"] = body.text
    _save(project_id, data)
    return {"summary": body.text}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/documents")
def list_documents(project_id: str, user: dict = Depends(require_auth)):
    """List all documents for a project."""
    _load(project_id, user)  # access check
    return db.list_documents(project_id)


@app.get("/api/projects/{project_id}/documents/{doc_id}")
def get_document(project_id: str, doc_id: str,
                 user: dict = Depends(require_auth)):
    """Get a specific document."""
    _load(project_id, user)  # access check
    doc = db.get_document(project_id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    return doc


@app.post("/api/projects/{project_id}/documents")
def create_document(project_id: str, body: DocumentSave,
                    user: dict = Depends(require_auth)):
    """Create a new document."""
    data = _load(project_id, user)
    _require_write(data, user)
    doc_id = db.save_document(project_id, "", body.title, body.content, body.scope)
    return {"doc_id": doc_id, "title": body.title}


@app.put("/api/projects/{project_id}/documents/{doc_id}")
def update_document(project_id: str, doc_id: str, body: DocumentSave,
                    user: dict = Depends(require_auth)):
    """Update an existing document."""
    data = _load(project_id, user)
    _require_write(data, user)
    db.save_document(project_id, doc_id, body.title, body.content, body.scope)
    return {"doc_id": doc_id, "title": body.title}


@app.delete("/api/projects/{project_id}/documents/{doc_id}")
def delete_document_endpoint(project_id: str, doc_id: str,
                             user: dict = Depends(require_auth)):
    """Delete a document."""
    data = _load(project_id, user)
    _require_write(data, user)
    if not db.delete_document(project_id, doc_id):
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    return {"doc_id": doc_id, "status": "deleted"}


# ---------------------------------------------------------------------------
# Members (invite / remove)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/members")
def invite_member(project_id: str, body: MemberInvite,
                  user: dict = Depends(require_auth)):
    """Invite a member by email. Only project owner can invite."""
    data = _load(project_id, user)
    # Only owner can invite
    if _auth_enabled and data.get("owner") != user.get("sub"):
        raise HTTPException(status_code=403, detail="Only project owner can invite members")
    if body.role not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'editor'")
    # Find user by email
    found = db.find_user_by_email(body.email)
    if found is None:
        raise HTTPException(status_code=404,
                            detail=f"User '{body.email}' not found. They must sign in to Beacon first.")
    invited_id, invited_data = found
    # Check not already a member
    members = data.get("members", [])
    if any(m.get("user_id") == invited_id for m in members):
        raise HTTPException(status_code=409, detail=f"'{body.email}' is already a member")
    if data.get("owner") == invited_id:
        raise HTTPException(status_code=409, detail=f"'{body.email}' is the project owner")
    members.append({"user_id": invited_id, "email": body.email, "role": body.role})
    data["members"] = members
    _save(project_id, data)
    return {"status": "invited", "email": body.email, "role": body.role}


@app.delete("/api/projects/{project_id}/members/{member_email}")
def remove_member(project_id: str, member_email: str,
                  user: dict = Depends(require_auth)):
    """Remove a member. Only project owner can remove."""
    data = _load(project_id, user)
    if _auth_enabled and data.get("owner") != user.get("sub"):
        raise HTTPException(status_code=403, detail="Only project owner can remove members")
    members = data.get("members", [])
    new_members = [m for m in members if m.get("email") != member_email]
    if len(new_members) == len(members):
        raise HTTPException(status_code=404, detail=f"Member '{member_email}' not found")
    data["members"] = new_members
    _save(project_id, data)
    return {"status": "removed", "email": member_email}


@app.get("/api/projects/{project_id}/members")
def list_members(project_id: str, user: dict = Depends(require_auth)):
    """List project members."""
    data = _load(project_id, user)
    owner_id = data.get("owner", "")
    owner_email = ""
    if owner_id:
        owner_data = db.get_user(owner_id)
        if owner_data:
            owner_email = owner_data.get("email", "")
    members = data.get("members", [])
    return {
        "owner": owner_id,
        "owner_email": owner_email,
        "members": members,
    }


class MemberRoleUpdate(BaseModel):
    role: str  # viewer | editor


@app.patch("/api/projects/{project_id}/members/{member_email}")
def update_member_role(project_id: str, member_email: str, body: MemberRoleUpdate,
                       user: dict = Depends(require_auth)):
    """Update a member's role. Only project owner can change roles."""
    data = _load(project_id, user)
    if _auth_enabled and data.get("owner") != user.get("sub"):
        raise HTTPException(status_code=403, detail="Only project owner can change roles")
    if body.role not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'editor'")
    members = data.get("members", [])
    for m in members:
        if m.get("email") == member_email:
            m["role"] = body.role
            data["members"] = members
            _save(project_id, data)
            return {"email": member_email, "role": body.role}
    raise HTTPException(status_code=404, detail=f"Member '{member_email}' not found")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.get("/api/admin/users")
def admin_list_users(user: dict = Depends(require_auth)):
    """List all users (admin only)."""
    _require_admin(user)
    return db.list_users()


class AdminUserUpdate(BaseModel):
    role: str  # admin | user


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: str, body: AdminUserUpdate,
                      user: dict = Depends(require_auth)):
    """Update a user's system role (admin only)."""
    _require_admin(user)
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'user'")
    if user_id == user.get("sub"):
        raise HTTPException(status_code=400, detail="Cannot change your own admin role")
    if not db.update_user(user_id, {"role": body.role}):
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return {"user_id": user_id, "role": body.role}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, user: dict = Depends(require_auth)):
    """Delete a user (admin only)."""
    _require_admin(user)
    if user_id == user.get("sub"):
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    if not db.delete_user(user_id):
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return {"user_id": user_id, "status": "deleted"}


@app.get("/api/admin/projects")
def admin_list_projects(user: dict = Depends(require_auth)):
    """List all projects summary (admin only). No project content exposed."""
    _require_admin(user)
    return db.list_all_projects()


@app.delete("/api/admin/projects/{project_id}")
def admin_delete_project(project_id: str, user: dict = Depends(require_auth)):
    """Delete a project (admin only)."""
    _require_admin(user)
    if not db.delete_project(project_id):
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return {"project_id": project_id, "status": "deleted"}


class AdminOwnerTransfer(BaseModel):
    new_owner_id: str


@app.patch("/api/admin/projects/{project_id}/owner")
def admin_transfer_owner(project_id: str, body: AdminOwnerTransfer,
                         user: dict = Depends(require_auth)):
    """Transfer project ownership (admin only)."""
    _require_admin(user)
    data = db.get_project(project_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    new_owner = db.get_user(body.new_owner_id)
    if new_owner is None:
        raise HTTPException(status_code=404, detail=f"User '{body.new_owner_id}' not found")
    data["owner"] = body.new_owner_id
    # Remove new owner from members if present
    data["members"] = [m for m in data.get("members", []) if m.get("user_id") != body.new_owner_id]
    db.save_project(project_id, data)
    return {"project_id": project_id, "new_owner": body.new_owner_id, "email": new_owner.get("email", "")}


@app.get("/api/admin/me")
def admin_check(user: dict = Depends(require_auth)):
    """Check if current user is admin."""
    user_data = db.get_user(user.get("sub", ""))
    is_admin = user_data.get("role") == "admin" if user_data else False
    return {"is_admin": is_admin}


# ---------------------------------------------------------------------------
# Retro
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/retros")
def list_retros(project_id: str, user: dict = Depends(require_auth)):
    """List all retrospective documents for a project."""
    return db.list_retros(project_id)


@app.get("/api/projects/{project_id}/retros/{week}")
def get_retro(project_id: str, week: str, user: dict = Depends(require_auth)):
    """Get a specific retrospective document."""
    retro = db.get_retro(project_id, week)
    if retro is None:
        raise HTTPException(status_code=404, detail=f"Retro '{week}' not found")
    return retro


@app.post("/api/projects/{project_id}/retros/{week}")
def save_retro(project_id: str, week: str, body: RetroCreate,
               user: dict = Depends(require_auth)):
    """Save a retrospective document."""
    data = _load(project_id, user)
    _require_write(data, user)
    db.save_retro(project_id, week, body.content)
    return {"week": week, "status": "saved"}


@app.get("/api/projects/{project_id}/search")
def search_project(project_id: str, q: str = "", ms: str = "",
                   user: dict = Depends(require_auth)):
    """Full-text search across milestones, tasks, commits, and saves."""
    data = _load(project_id, user)
    query = q.lower().strip()
    if not query:
        return []

    results = []

    def _search_entries(entries, ms_id, ms_title):
        for e in entries:
            desc = (e.get("description") or "").lower()
            detail = (e.get("detail") or "").lower()
            if query in desc or query in detail:
                results.append({
                    "ms_id": ms_id,
                    "ms_title": ms_title,
                    "entry_id": e.get("id", ""),
                    "type": e.get("type", ""),
                    "status": e.get("status", ""),
                    "description": e.get("description", ""),
                    "date": e.get("date", "") or e.get("created_at", ""),
                })
            _search_entries(e.get("entries", []), ms_id, ms_title)

    for milestone in data.get("milestones", []):
        if ms and milestone["id"] != ms:
            continue
        ms_title = milestone.get("title", "")
        if query in ms_title.lower():
            results.append({
                "ms_id": milestone["id"],
                "ms_title": ms_title,
                "entry_id": milestone["id"],
                "type": "milestone",
                "status": milestone.get("status", ""),
                "description": ms_title,
                "date": "",
            })
        _search_entries(milestone.get("entries", []), milestone["id"], ms_title)

    return results


# ---------------------------------------------------------------------------
# WebSocket (real-time project updates via Firestore on_snapshot)
# ---------------------------------------------------------------------------

_ws_connections: dict[str, set[WebSocket]] = {}
_watchers: dict[str, object] = {}
_event_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _capture_event_loop():
    global _event_loop
    _event_loop = asyncio.get_event_loop()


def _enrich_project(data: dict) -> dict:
    """Add computed fields (total_tasks, done_tasks, entries_to_json) to project."""
    enriched = {**data}
    milestones = []
    for ms in data.get("milestones", []):
        entries = ms.get("entries", [])
        total, done = core.count_task_status(entries)
        milestones.append({
            **ms,
            "entries": core.entries_to_json(entries),
            "total_tasks": total,
            "done_tasks": done,
        })
    enriched["milestones"] = milestones
    return enriched


async def _broadcast(project_id: str, data: dict):
    """Send enriched project data to all WebSocket clients."""
    clients = _ws_connections.get(project_id, set()).copy()
    if not clients:
        return
    enriched = _enrich_project(data)
    msg = {"type": "project", "data": enriched}
    for ws in clients:
        try:
            await ws.send_json(msg)
        except Exception:
            _ws_connections.get(project_id, set()).discard(ws)


def _on_snapshot(project_id: str, doc_snapshot, changes, read_time):
    """Firestore on_snapshot callback (runs in background thread)."""
    for doc in doc_snapshot:
        data = doc.to_dict()
        if _event_loop and _ws_connections.get(project_id):
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, data), _event_loop
            )


def _start_watcher(project_id: str):
    if project_id in _watchers:
        return
    doc_ref = db.get_db().collection(db.COLLECTION).document(project_id)
    unsub = doc_ref.on_snapshot(
        lambda ds, ch, rt: _on_snapshot(project_id, ds, ch, rt)
    )
    _watchers[project_id] = unsub


def _stop_watcher(project_id: str):
    if project_id in _watchers and not _ws_connections.get(project_id):
        _watchers[project_id]()
        del _watchers[project_id]


@app.websocket("/ws/projects/{project_id}")
async def ws_project(websocket: WebSocket, project_id: str):
    """WebSocket endpoint for real-time project monitoring."""
    token = websocket.query_params.get("token")
    if _auth_enabled:
        if not token:
            await websocket.close(code=1008)
            return
        try:
            _verify_id_token(token)
        except HTTPException:
            await websocket.close(code=1008)
            return

    await websocket.accept()

    if project_id not in _ws_connections:
        _ws_connections[project_id] = set()
    _ws_connections[project_id].add(websocket)

    # Send initial enriched data
    raw = db.get_project(project_id)
    if raw:
        await websocket.send_json({"type": "project", "data": _enrich_project(raw)})

    _start_watcher(project_id)

    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        _ws_connections[project_id].discard(websocket)
        _stop_watcher(project_id)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "env": os.environ.get("BEACON_ENV", "dev")}


@app.get("/api/auth/config")
def auth_config():
    """Return OAuth client ID for Web UI login (no auth required)."""
    client_id = os.environ.get("BEACON_OAUTH_CLIENT_ID", "")
    return {"client_id": client_id}



# ---- CLI Auth (Web UI-mediated flow) ----

import secrets
import time

# In-memory pending codes: code -> {sub, email, id_token, expires}
_cli_pending: dict[str, dict] = {}


@app.post("/api/auth/cli-start")
def cli_auth_start():
    """CLI calls this to get a pairing code. No auth required."""
    code = secrets.token_urlsafe(6)[:8].upper()  # Short human-readable code
    _cli_pending[code] = {"expires": time.time() + 300}
    # Cleanup expired
    now = time.time()
    for k in [k for k, v in _cli_pending.items() if v["expires"] < now]:
        del _cli_pending[k]
    return {"code": code, "expires_in": 300, "url": f"https://beacon-ai.dev/cli-auth?code={code}"}


class CliApproveRequest(BaseModel):
    code: str

@app.post("/api/auth/cli-approve")
def cli_auth_approve(body: CliApproveRequest, user: dict = Depends(require_auth), request: Request = None):
    """Web UI calls this (authenticated) to approve a CLI pairing code."""
    code = body.code.upper()
    if code not in _cli_pending:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    entry = _cli_pending[code]
    if time.time() > entry["expires"]:
        del _cli_pending[code]
        raise HTTPException(status_code=410, detail="Code expired")
    # Store the user's auth info for CLI to pick up
    entry["email"] = user.get("email", "")
    entry["sub"] = user.get("sub", "")
    entry["approved"] = True
    # Get the raw token from authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        entry["id_token"] = auth_header[7:]
    return {"status": "approved", "email": entry["email"]}


@app.get("/api/auth/cli-poll")
def cli_auth_poll_get(code: str = ""):
    """CLI polls this to check if the code has been approved. No auth required."""
    code = code.upper()
    if code not in _cli_pending:
        raise HTTPException(status_code=404, detail="Invalid or expired code")
    entry = _cli_pending[code]
    if time.time() > entry["expires"]:
        del _cli_pending[code]
        raise HTTPException(status_code=410, detail="Code expired")
    if not entry.get("approved"):
        return {"status": "pending"}
    # Approved — return token and cleanup
    result = {
        "status": "approved",
        "email": entry.get("email", ""),
        "id_token": entry.get("id_token", ""),
    }
    del _cli_pending[code]
    return result


# ---------------------------------------------------------------------------
# Static files (Web UI)
# ---------------------------------------------------------------------------

from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse

_static_dir = Path(__file__).parent / "static"

if _static_dir.exists():
    @app.get("/")
    def serve_index():
        return FileResponse(
            _static_dir / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/privacy")
    def privacy_policy():
        return FileResponse(_static_dir / "privacy.html")

    @app.get("/admin")
    def serve_admin():
        return FileResponse(_static_dir / "admin.html")

    @app.get("/cli-auth")
    def serve_cli_auth():
        return FileResponse(_static_dir / "cli-auth.html")

    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
