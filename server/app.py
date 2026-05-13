"""Beacon API - FastAPI backend for project management."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

# Add lib/ to path so we can import core
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from fastapi import FastAPI, HTTPException, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

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
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """FastAPI dependency that enforces Bearer token auth and auto-registers users."""
    if not _auth_enabled:
        return {"sub": "dev", "email": "dev@local"}
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authorization header required")
    claims = _verify_id_token(credentials.credentials)
    # Auto-register user on first login
    user_id = claims.get("sub", "")
    email = claims.get("email", "")
    if user_id:
        db.get_or_create_user(user_id, email)
    return claims


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
def list_projects(user: dict = Depends(require_auth)):
    """List projects owned by or shared with the current user."""
    return db.list_projects(user_id=user.get("sub"))


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
        return FileResponse(_static_dir / "index.html")

    @app.get("/privacy")
    def privacy_policy():
        return FileResponse(_static_dir / "privacy.html")

    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
