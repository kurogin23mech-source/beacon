"""Beacon API - FastAPI backend for project management."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac as _hmac
import json
import logging
import os
import sys
import time
from typing import Optional

# Add lib/ to path so we can import core
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from fastapi import FastAPI, HTTPException, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse

import core
import firestore_client as db
import operations

# debug=False is the default, but set explicitly to ensure stack traces are
# never included in error responses in production.
app = FastAPI(title="Beacon API", version="0.1.0", debug=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Global exception handler – prevents stack traces from leaking in 500 responses
# ---------------------------------------------------------------------------

_server_logger = logging.getLogger("beacon.server")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler that returns a generic 500 without exposing internals."""
    _server_logger.exception(
        "Unhandled exception: method=%s path=%s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
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

        # Surface request metadata into the operations layer's audit
        # ContextVars so the changelog writer can pick up ip / ua without
        # each endpoint plumbing them through (ms-14 e-825).
        request_ip = request.headers.get(
            "x-forwarded-for",
            request.client.host if request.client else "",
        )
        request_ua = request.headers.get("user-agent", "")
        operations.set_audit_context(ip=request_ip, user_agent=request_ua)

        start = time.time()
        response: Response = await call_next(request)
        elapsed_ms = int((time.time() - start) * 1000)

        # Extract user info from request state (set by require_auth if called)
        user_id = getattr(request.state, "audit_user_id", "")
        email = getattr(request.state, "audit_email", "")
        # require_auth fires inside call_next, so email is now known —
        # propagate it for the changelog writer too.
        if email:
            operations.set_audit_context(email=email)
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


_CLI_TOKEN_PREFIX = "bcli."
_CLI_TOKEN_LIFETIME = 86400 * 30  # 30 days


def _make_cli_token(sub: str, email: str) -> tuple[str, int]:
    """Issue a long-lived CLI token (HMAC-SHA256). Returns (token, expiry_unix)."""
    expiry = int(time.time()) + _CLI_TOKEN_LIFETIME
    payload = json.dumps({"sub": sub, "email": email, "exp": expiry}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    secret = os.environ.get("BEACON_CLI_TOKEN_SECRET", "dev-secret-CHANGE-ME")
    sig = _hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{_CLI_TOKEN_PREFIX}{payload_b64}.{sig}", expiry


def _verify_cli_token(token: str) -> dict | None:
    """Verify a beacon CLI token. Returns claims dict or None if invalid/expired."""
    if not token.startswith(_CLI_TOKEN_PREFIX):
        return None
    try:
        rest = token[len(_CLI_TOKEN_PREFIX):]
        payload_b64, sig = rest.rsplit(".", 1)
        secret = os.environ.get("BEACON_CLI_TOKEN_SECRET", "dev-secret-CHANGE-ME")
        expected = _hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expected):
            return None
        padding = (4 - len(payload_b64) % 4) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _verify_id_token(token: str) -> dict:
    """Verify a Google ID token or Beacon CLI token and return the claims."""
    # Check for long-lived CLI token first (no network call)
    claims = _verify_cli_token(token)
    if claims:
        return claims
    # Fall back to Google ID token verification
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
    # v2 (subcollection) projects need their milestones hydrated from the
    # subcollection; load_project_consistent transparently handles both v1
    # and v2 so callers get a unified dict shape either way. Falling back
    # to db.get_project here would silently drop milestones[] on v2 docs.
    try:
        data = operations.load_project_consistent(project_id)
    except LookupError:
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


def _require_owner(data: dict, user: dict) -> None:
    """Raise 403 if user is not the project owner.

    Used by destructive operations (purge) where editor-level access is
    deliberately insufficient — only the owner can hard-delete records.
    Mirrors `_require_write` shape (data, user) → raises 403.
    """
    role = _get_role(data, user)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Owner access required")


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
    priority: str = ""
    objective: str = ""
    acceptance_criteria: str = ""

class MilestoneUpdate(BaseModel):
    title: str = ""
    progress: str = ""
    target_date: str = ""
    status: str = ""
    description: str = ""
    priority: str = ""
    objective: str = ""
    acceptance_criteria: str = ""

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

class NoteCreate(BaseModel):
    text: str
    context: str = ""
    ts: str = ""
    # ms-57 / e-1036: per-session attribution for the session-log
    # aggregation query. Empty string = "no session" (older clients,
    # pre-ms-57 CLI) and is dropped server-side so Firestore docs stay
    # either "tagged with a real id" or "no field at all".
    session_id: str = ""

class SessionUpsert(BaseModel):
    """Body for PUT /api/projects/{project_id}/sessions/{session_id}.

    Fields mirror lib/session.py's local payload. All optional because heartbeat
    updates only need to bump last_active; first-mint upserts populate the rest.
    server/firestore_client.upsert_session uses merge=True so partial bodies
    are safe.
    """
    actor: Optional[dict] = None
    created_at: Optional[str] = None
    last_active: Optional[str] = None
    harness: Optional[str] = None


_BUS_DELIVERY_MODES = {"auto-execute", "propose-to-ai", "notify-user-only"}
_BUS_DELIVERY_DEFAULT = "propose-to-ai"


class BusEventCreate(BaseModel):
    """Body for POST /api/projects/{project_id}/bus.

    ms-54: starts as e-996 minimal transport, picks up ``delivery`` in e-1135.
    The recipient_session_id / directory routing / subscribe filter fields
    arrive in later tasks (e-1134 directory query, §9 subscribe filter).

    ``delivery`` declares how the recipient daemon should treat the event:

      * ``auto-execute``     — run the embedded action without asking. Reserved
                               for explicit opt-in (e-1136 dogfood enforces it).
      * ``propose-to-ai``    — inject as a proposal for the receiver AI to
                               consider. **Default** — mirrors ms-31's
                               "force is never the default" principle.
      * ``notify-user-only`` — show in UI/terminal only; never inject into the
                               AI context.

    Unknown values get coerced to the default rather than rejected so a
    schema mismatch between an older sender and a newer server never silently
    upgrades to auto-execute.
    """
    channel: str
    sender_session_id: str = ""
    payload: dict = {}
    delivery: str = _BUS_DELIVERY_DEFAULT


class BusCursorAdvance(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/cursors/{recipient_id}.

    ms-54 / e-998. Consumers commit ``last_seen_at`` after successfully
    processing a batch. The server enforces forward-only semantics, so a
    stale client that sends an older value gets a silent no-op rather than
    rewinding the cursor for everyone else.
    """
    last_seen_at: str


class SessionLogUpsert(BaseModel):
    """Body for PUT /api/projects/{project_id}/session_logs/{session_id}.

    ms-57 / e-1037 schema. `summary` is the durable decision-trail content
    (survives entry GC); `*_ids` are best-effort back-references. `recovered`
    is set True only on the first upsert from the rescue path (session-start
    seeing an orphan session) so forensics can tell rescue-born entries from
    session-end ones. All fields optional because rescue and session-end
    write different subsets; firestore_client.upsert_session_log uses
    merge=True so partials are safe.
    """
    summary: Optional[str] = None
    note_ids: Optional[list[str]] = None
    commit_ids: Optional[list[str]] = None
    pr_ids: Optional[list[str]] = None
    created_at: Optional[str] = None
    last_aggregated_at: Optional[str] = None
    recovered: Optional[bool] = None

class DocumentSave(BaseModel):
    title: str
    content: str
    scope: Optional[str] = None  # core | spec | memo

class DeleteRequest(BaseModel):
    reason: str = ""

class PurgeRequest(BaseModel):
    """Body for destructive hard-delete endpoints (milestone/entry/operation purge).

    `reason` is required (audit trail per CORE doc data-immutability-principle).
    `index` (1-based) disambiguates when duplicate IDs exist — set to None when
    only a single record matches.
    """
    reason: str
    index: Optional[int] = None

class MemberInvite(BaseModel):
    email: str
    role: str = "viewer"  # viewer | editor


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

@app.get("/api/version")
def get_server_version():
    """Server-side beacon CLI version + git revision.

    Returned to the Web UI so the header banner can show
        "Beacon 0.4.0 (rev abc1234)"
    and so the client can detect a stale tab when the server is upgraded.

    See e-587 for the UI hookup (server/static/index.html).
    """
    import subprocess
    try:
        # lib/commands.py is the source of truth for the version string.
        from commands import __version__ as cli_version  # type: ignore
    except Exception:
        cli_version = "unknown"

    git_rev = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        if result.returncode == 0:
            git_rev = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return {"cli": cli_version, "git_rev": git_rev}


@app.get("/api/projects/{project_id}/version")
def get_project_version(project_id: str, user: dict = Depends(require_auth)):
    """Per-project version info derived from push records (e-587).

    Returns:
      {
        "latest_pushed_semver":   "v0.4.0"  | "",   # most recent push that
                                                   # carried an explicit semver
        "latest_pushed_at":       "2026-05-28T..." | "",
        "commits_since_release":  N,         # length of pushes after that one
        "total_pushes":           N,
        "tag":                    "v0.4.0" | "",   # convenience alias
      }

    The Web UI displays this as "v0.4.0  +N commits since release". A
    blank `tag` means the project hasn't started using version-rules yet —
    show nothing rather than a misleading "v?".
    """
    data = db.get_project(project_id)
    if not data:
        raise HTTPException(status_code=404, detail="Project not found")
    # Permission check — viewers are fine for read.
    if _get_role(data, user) is None and not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="No access to this project")

    pushes = data.get("pushes") or []
    # `pushes` ordering varies — sort by pushed_at to be safe.
    sortable = []
    for p in pushes:
        if not isinstance(p, dict):
            continue
        sortable.append((p.get("pushed_at", "") or "", p))
    sortable.sort(key=lambda x: x[0], reverse=True)

    latest_semver = ""
    latest_at = ""
    commits_since = 0
    for _, p in sortable:
        meta = p.get("meta") or {}
        sem = p.get("semver") or meta.get("semver") or ""
        if sem and not latest_semver:
            latest_semver = sem
            latest_at = p.get("pushed_at", "")
            break
        commits_since += p.get("commit_count", 0) or 0

    return {
        "latest_pushed_semver": latest_semver,
        "latest_pushed_at": latest_at,
        "commits_since_release": commits_since,
        "total_pushes": len(pushes),
        "tag": latest_semver,
    }


@app.get("/api/projects")
def list_projects(include_archived: bool = False, user: dict = Depends(require_auth)):
    """List projects owned by or shared with the current user."""
    return db.list_projects(user_id=user.get("sub"), include_archived=include_archived)


@app.post("/api/projects/{project_id}/archive")
def archive_project(project_id: str, user: dict = Depends(require_auth)):
    """Archive a project (soft delete — hidden from default listing)."""
    def op(data: dict):
        if _get_role(data, user) != "owner":
            raise HTTPException(status_code=403, detail="Only the project owner can archive")
        data["archived"] = True
        return data, {"status": "archived", "project_id": project_id}
    return operations.apply_operation(
        project_id, op, op_name="project.archive", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/unarchive")
def unarchive_project(project_id: str, user: dict = Depends(require_auth)):
    """Restore an archived project."""
    def op(data: dict):
        if _get_role(data, user) != "owner":
            raise HTTPException(status_code=403, detail="Only the project owner can unarchive")
        data["archived"] = False
        return data, {"status": "unarchived", "project_id": project_id}
    return operations.apply_operation(
        project_id, op, op_name="project.unarchive", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/migrate-to-v2")
def migrate_project_to_v2(project_id: str,
                          user: dict = Depends(require_auth)):
    """One-time migration from v1 (whole-doc) to v2 (subcollection) layout.

    Why this exists: an unbounded `milestones[]` array on a single Firestore
    document hits the 1 MiB document size cap. Once over the cap, every
    growth-direction write (task add / log / new milestone) returns 500
    because the resulting doc would exceed 1 MiB. The escape hatch is the
    migration write itself, which moves milestones out to a subcollection
    and shrinks the project doc to ~100 KiB — well under the cap.

    Restricted to project owner (it is destructive in the sense that it
    rewrites the storage layout; owner == only person who should approve).

    Idempotent: a project already at schema_version=2 returns
    {"status": "already_v2"} without doing anything.

    After migration:
      - Reads via `operations.load_project_consistent` hydrate the project
        from meta + subcollection (transparent to callers).
      - Writes via `apply_operation` go through `_apply_cloud_v2` which
        only touches the affected MS subdoc.
      - Writes via `replace_project` (legacy whole-doc PUT) detect v2 and
        dispatch to `_replace_cloud_v2` which decomposes into subdocs.
    """
    # Owner check before kicking off the transaction. Reading the meta doc
    # is cheap (no milestones loaded) and works on both v1 and v2.
    meta = db.get_project(project_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if _get_role(meta, user) != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only the project owner can migrate schema",
        )

    try:
        result = operations.migrate_v1_to_v2(project_id)
    except LookupError:
        # Race: project was deleted between the owner check and the migration.
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result


@app.post("/api/projects/{project_id}")
def create_project(project_id: str, body: ProjectCreate,
                   user: dict = Depends(require_auth)):
    """Create a new project (like beacon init).

    New projects are created with schema_version=2 (β subcollection layout)
    by default — see SPEC doc gP9pCssCoa3QduuSMGR0 §"新規プロジェクトは
    β スキーマで作る (並列性確保)". This lets concurrent writes to different
    milestones proceed without contending on a single document.

    Existing projects (created before this change) remain on schema_version=1
    (legacy whole-document) and are not auto-migrated; apply_operation
    transparently routes them through the legacy transaction path.
    """
    existing = db.get_project(project_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Project '{project_id}' already exists")
    data = {
        "name": body.name,
        "objective": body.objective,
        "milestones": [],
        "owner": user.get("sub", ""),
        "members": [],
        # SCHEMA_V2_BETA — see lib/operations.py
        "schema_version": operations.SCHEMA_V2_BETA,
    }
    _save(project_id, data)
    return {"status": "created", "project_id": project_id}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, user: dict = Depends(require_auth)):
    # ms-46 e-756: REST もWS pushと同じ enriched shape を返す
    # (total_tasks / done_tasks / entries_to_json)。client がどの経路で
    # データを取っても counts が落ちないように対称化する。
    return _enrich_project(_load(project_id, user))


@app.put("/api/projects/{project_id}")
def put_project(project_id: str, body: dict,
                user: dict = Depends(require_auth)):
    # validate_project is also called inside replace_project, but we pre-call
    # here so the 400 path doesn't open a transaction unnecessarily.
    try:
        core.validate_project(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Auto-set owner if missing (e.g. cloud push from local)
    if not body.get("owner") and _auth_enabled:
        body["owner"] = user.get("sub", "")
    operations.replace_project(
        project_id, body,
        actor=user.get("sub", ""),
        reason="PUT /api/projects (whole-document replace)",
    )
    return {"status": "ok", "project_id": project_id}


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones")
def create_milestone(project_id: str, body: MilestoneCreate,
                     user: dict = Depends(require_auth)):
    def op(data: dict):
        _require_write(data, user)
        try:
            ms_id = core.milestone_add(
                data, body.title, body.target_date,
                description=body.description,
                priority=body.priority,
                objective=body.objective,
                acceptance_criteria=body.acceptance_criteria,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"ms_id": ms_id, "title": body.title}
    return operations.apply_operation(
        project_id, op, op_name="milestone.create", actor=user.get("sub", ""),
    )


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
    def op(data: dict):
        _require_write(data, user)
        try:
            ms = core.milestone_update(
                data, ms_id,
                title=body.title, progress=body.progress,
                target_date=body.target_date, status=body.status,
                description=body.description,
                priority=body.priority,
                objective=body.objective,
                acceptance_criteria=body.acceptance_criteria,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {
            "id": ms["id"], "title": ms["title"], "status": ms["status"],
            "progress": ms.get("progress", 0),
        }
    return operations.apply_operation(
        project_id, op, op_name="milestone.update", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/milestones/{ms_id}/start")
def start_milestone(project_id: str, ms_id: str,
                    user: dict = Depends(require_auth)):
    def op(data: dict):
        _require_write(data, user)
        try:
            ms = core.milestone_start(data, ms_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"id": ms["id"], "title": ms["title"], "status": "in_progress"}
    return operations.apply_operation(
        project_id, op, op_name="milestone.start", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/milestones/{ms_id}/done")
def done_milestone(project_id: str, ms_id: str,
                   user: dict = Depends(require_auth)):
    def op(data: dict):
        _require_write(data, user)
        try:
            ms = core.milestone_done(data, ms_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"id": ms["id"], "title": ms["title"], "status": "done"}
    return operations.apply_operation(
        project_id, op, op_name="milestone.done", actor=user.get("sub", ""),
    )


@app.delete("/api/projects/{project_id}/milestones/{ms_id}")
def delete_milestone(project_id: str, ms_id: str,
                     body: Optional[DeleteRequest] = None,
                     user: dict = Depends(require_auth)):
    reason = (body.reason if body else "") or ""
    def op(data: dict):
        _require_write(data, user)
        try:
            ms = core.milestone_delete(data, ms_id, reason=reason)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"id": ms["id"], "status": "cancelled"}
    return operations.apply_operation(
        project_id, op, op_name="milestone.delete", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/milestones/{ms_id}/purge")
def purge_milestone(project_id: str, ms_id: str,
                    body: PurgeRequest,
                    user: dict = Depends(require_auth)):
    """Hard-delete a milestone record — owner-only (e-1030).

    Unlike soft delete (`DELETE /milestones/{id}`), this physically removes
    the record from the array (Issue #14 duplicate-ID recovery path). Restricted
    to project owner to protect against accidental destruction by editors.
    """
    if not body.reason:
        raise HTTPException(
            status_code=400,
            detail="reason is required for purge (audit trail per "
                   "data-immutability-principle)",
        )
    def op(data: dict):
        _require_owner(data, user)
        try:
            ms = core.milestone_purge(
                data, ms_id, reason=body.reason, index=body.index,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {
            "id": ms["id"], "title": ms.get("title", ""), "purged": True,
        }
    return operations.apply_operation(
        project_id, op, op_name="milestone.purge", actor=user.get("sub", ""),
        reason=body.reason,
    )


# ---------------------------------------------------------------------------
# Entries (tasks / commits / notes)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones/{ms_id}/entries")
def create_entry(project_id: str, ms_id: str, body: EntryCreate,
                 user: dict = Depends(require_auth)):
    def op(data: dict):
        _require_write(data, user)
        try:
            eid = core.task_add(
                data, ms_id, body.description,
                entry_type=body.type, date=body.date, detail=body.detail,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"entry_id": eid, "description": body.description}
    return operations.apply_operation(
        project_id, op, op_name="entry.create", actor=user.get("sub", ""),
    )


@app.patch("/api/projects/{project_id}/entries/{entry_id}")
def update_entry(project_id: str, entry_id: str, body: EntryUpdate,
                 user: dict = Depends(require_auth)):
    def op(data: dict):
        _require_write(data, user)
        try:
            ms, entry = core.task_update(
                data, entry_id,
                description=body.description, status=body.status,
                detail=body.detail, date=body.date,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, core.entries_to_json([entry])[0]
    return operations.apply_operation(
        project_id, op, op_name="entry.update", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/entries/{entry_id}/done")
def done_entry(project_id: str, entry_id: str,
               user: dict = Depends(require_auth)):
    import datetime
    today = datetime.date.today().isoformat()

    def op(data: dict):
        _require_write(data, user)
        try:
            ms, entry = core.task_done(data, entry_id, date=today)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"entry_id": entry_id, "status": "done"}
    return operations.apply_operation(
        project_id, op, op_name="entry.done", actor=user.get("sub", ""),
    )


@app.delete("/api/projects/{project_id}/entries/{entry_id}")
def delete_entry(project_id: str, entry_id: str,
                 body: Optional[DeleteRequest] = None,
                 user: dict = Depends(require_auth)):
    reason = (body.reason if body else "") or ""
    def op(data: dict):
        _require_write(data, user)
        try:
            entry = core.task_delete(data, entry_id, reason=reason)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"entry_id": entry_id, "status": "cancelled"}
    return operations.apply_operation(
        project_id, op, op_name="entry.delete", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/entries/{entry_id}/purge")
def purge_entry(project_id: str, entry_id: str,
                body: PurgeRequest,
                user: dict = Depends(require_auth)):
    """Hard-delete an entry record — owner-only (e-1030).

    Entry-level analogue of milestone purge — Issue #14 / e-863 recovery for
    duplicate entry IDs. Editors cannot purge; only the project owner can.
    """
    if not body.reason:
        raise HTTPException(
            status_code=400,
            detail="reason is required for purge (audit trail per "
                   "data-immutability-principle)",
        )
    def op(data: dict):
        _require_owner(data, user)
        try:
            entry = core.entry_purge(
                data, entry_id, reason=body.reason, index=body.index,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {
            "entry_id": entry.get("id", entry_id),
            "description": entry.get("description", ""),
            "purged": True,
        }
    return operations.apply_operation(
        project_id, op, op_name="entry.purge", actor=user.get("sub", ""),
        reason=body.reason,
    )


@app.post("/api/projects/{project_id}/operations/{op_id}/purge")
def purge_operation(project_id: str, op_id: str,
                    body: PurgeRequest,
                    user: dict = Depends(require_auth)):
    """Hard-delete an operation record — owner-only (e-1030).

    Operation-level analogue of milestone purge — Issue #14 / e-863 recovery
    for duplicate operation IDs. Editors cannot purge; only the project owner
    can.
    """
    if not body.reason:
        raise HTTPException(
            status_code=400,
            detail="reason is required for purge (audit trail per "
                   "data-immutability-principle)",
        )
    def op(data: dict):
        _require_owner(data, user)
        try:
            purged = core.operation_purge(
                data, op_id, reason=body.reason, index=body.index,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {
            "id": purged.get("id", op_id),
            "title": purged.get("title", ""),
            "purged": True,
        }
    return operations.apply_operation(
        project_id, op, op_name="operation.purge", actor=user.get("sub", ""),
        reason=body.reason,
    )


# ---------------------------------------------------------------------------
# Log (commit recording)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/log")
def log_commit(project_id: str, body: LogCommit,
               user: dict = Depends(require_auth)):
    def op(data: dict):
        _require_write(data, user)
        try:
            result = core.log_commit(
                data, ms_id=body.ms_id, commit_hash=body.hash,
                message=body.message, date=body.date,
                summary=body.summary, progress=body.progress,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, result
    return operations.apply_operation(
        project_id, op, op_name="project.log", actor=user.get("sub", ""),
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@app.patch("/api/projects/{project_id}/summary")
def update_summary(project_id: str, body: SummaryUpdate,
                   user: dict = Depends(require_auth)):
    """**Deprecated** (e-1040 completed). Writes are no-op.

    Cross-session hand-off → `beacon session log` (session_logs subcollection).
    Human narrative → `project-vision` CORE doc.

    The endpoint still returns 200 with the currently-stored summary so
    unknown legacy callers (older CLI / external scripts) don't crash —
    they just observe their input was ignored. The `Deprecation` /
    `Sunset` headers signal the contract change machine-readably.
    """
    # Permission check is still useful (do not leak read access to
    # outsiders), but we don't apply the mutation.
    data = _load(project_id, user)
    _require_write(data, user)
    existing = data.get("summary", "")
    response = JSONResponse(
        content={
            "summary": existing,
            "write_ignored": True,
            "deprecated_since": "e-1040",
        }
    )
    # Standard HTTP deprecation signals.
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "see e-1040; endpoint will be removed"
    response.headers["Link"] = (
        '<https://github.com/r-kida2/beacon/blob/main/CLAUDE.md>; '
        'rel="deprecation"; type="text/html"'
    )
    return response


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/documents")
def list_documents(project_id: str,
                   user: dict = Depends(require_auth)):
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
async def create_document(project_id: str, body: DocumentSave,
                          user: dict = Depends(require_auth)):
    """Create a new document.

    ms-43 e-809: emits a ``document_change`` WS frame after the write so the
    Documents tab on every live client refreshes without waiting for the user
    to re-open the tab. Async only because of that broadcast — the DB write
    itself is sync.
    """
    data = _load(project_id, user)
    _require_write(data, user)
    doc_id = db.save_document(project_id, "", body.title, body.content, body.scope)
    await _broadcast_document_change(
        project_id,
        _build_document_change_payload(project_id, doc_id, op="add",
                                       fallback_title=body.title,
                                       fallback_scope=body.scope),
    )
    return {"doc_id": doc_id, "title": body.title}


@app.put("/api/projects/{project_id}/documents/{doc_id}")
async def update_document(project_id: str, doc_id: str, body: DocumentSave,
                          user: dict = Depends(require_auth)):
    """Update an existing document.

    ms-43 e-809: emits a ``document_change`` WS frame post-write so the open
    Documents tab on every client picks up the new title / scope / updated_at
    in-place (instead of staying stale until next tab switch).
    """
    data = _load(project_id, user)
    _require_write(data, user)
    db.save_document(project_id, doc_id, body.title, body.content, body.scope,
                     updated_by=user.get("email", "unknown"))
    await _broadcast_document_change(
        project_id,
        _build_document_change_payload(project_id, doc_id, op="update",
                                       fallback_title=body.title,
                                       fallback_scope=body.scope),
    )
    return {"doc_id": doc_id, "title": body.title}


@app.get("/api/projects/{project_id}/documents/{doc_id}/revisions")
def list_document_revisions(project_id: str, doc_id: str, user: dict = Depends(require_auth)):
    """List revision history of a document."""
    _load(project_id, user)
    return db.list_document_revisions(project_id, doc_id)


@app.get("/api/projects/{project_id}/documents/{doc_id}/revisions/{rev}")
def get_document_revision(project_id: str, doc_id: str, rev: int, user: dict = Depends(require_auth)):
    """Get a specific revision of a document."""
    _load(project_id, user)
    result = db.get_document_revision(project_id, doc_id, rev)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Revision {rev} not found for '{doc_id}'")
    return result


@app.delete("/api/projects/{project_id}/documents/{doc_id}")
async def delete_document_endpoint(project_id: str, doc_id: str,
                                   body: Optional[DeleteRequest] = None,
                                   user: dict = Depends(require_auth)):
    """Soft-delete a document. Optional ``reason`` records why (ms-14 e-991).

    ms-43 e-809: emits a ``document_change`` (op=delete) WS frame so live
    clients drop the entry from their cached ``state.documents`` without
    needing a tab switch to re-fetch. We capture title/scope BEFORE the
    soft-delete because ``list_documents`` filters deleted docs and the
    client may want to render a brief "X was deleted" toast keyed on scope.
    """
    data = _load(project_id, user)
    _require_write(data, user)
    # Snapshot scope/title before delete so the broadcast payload still
    # carries them — once delete_document flips the soft-delete flag,
    # list_documents-style fetches filter the row out, leaving the client
    # without enough context to update its filtered views correctly.
    prior = db.get_document(project_id, doc_id) or {}
    reason = (body.reason if body else "") or ""
    if not db.delete_document(project_id, doc_id,
                              deleted_by=user.get("email", "unknown"),
                              reason=reason):
        raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
    payload = {
        "op": "delete",
        "doc_id": doc_id,
        "title": prior.get("title", ""),
        "scope": prior.get("scope", "memo"),
        "updated_at": "",
    }
    milestone = prior.get("milestone")
    if milestone:
        payload["milestone"] = milestone
    await _broadcast_document_change(project_id, payload)
    return {"doc_id": doc_id, "status": "cancelled"}


@app.get("/api/projects/{project_id}/changelog")
def list_changelog_endpoint(project_id: str,
                            since: Optional[str] = None,
                            limit: int = 100,
                            user: dict = Depends(require_auth)):
    """Project audit trail — append-only changelog (ms-14 e-825).

    Returns entries newest-first. ``since`` is an ISO8601 timestamp; only
    entries with ``ts > since`` are returned, which makes incremental
    polling cheap. ``limit`` is capped at 500 server-side.
    """
    _load(project_id, user)  # access check
    entries = db.list_changelog(project_id, since=since, limit=limit)
    # ``next_since`` is the oldest ts in this page — pass it back as the
    # cursor for the NEXT older page when the UI scrolls. Empty result
    # means there is nothing further back.
    next_since = entries[-1]["ts"] if entries else None
    return {"entries": entries, "next_since": next_since, "limit": limit}


# ---------------------------------------------------------------------------
# Members (invite / remove)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/members")
def invite_member(project_id: str, body: MemberInvite,
                  user: dict = Depends(require_auth)):
    """Invite a member by email. Only project owner can invite."""
    if body.role not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'editor'")
    # Look up the invitee outside the transaction — read-only on the users
    # collection, not the project doc. Safe and avoids extending the txn window.
    found = db.find_user_by_email(body.email)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"User '{body.email}' not found. They must sign in to Beacon first.",
        )
    invited_id, _ = found

    def op(data: dict):
        if _auth_enabled and data.get("owner") != user.get("sub"):
            raise HTTPException(
                status_code=403, detail="Only project owner can invite members"
            )
        members = data.get("members", [])
        if any(m.get("user_id") == invited_id for m in members):
            raise HTTPException(
                status_code=409, detail=f"'{body.email}' is already a member"
            )
        if data.get("owner") == invited_id:
            raise HTTPException(
                status_code=409, detail=f"'{body.email}' is the project owner"
            )
        members.append({"user_id": invited_id, "email": body.email, "role": body.role})
        data["members"] = members
        return data, {"status": "invited", "email": body.email, "role": body.role}

    return operations.apply_operation(
        project_id, op, op_name="member.invite", actor=user.get("sub", ""),
    )


@app.delete("/api/projects/{project_id}/members/{member_email}")
def remove_member(project_id: str, member_email: str,
                  user: dict = Depends(require_auth)):
    """Remove a member. Only project owner can remove."""
    def op(data: dict):
        if _auth_enabled and data.get("owner") != user.get("sub"):
            raise HTTPException(
                status_code=403, detail="Only project owner can remove members"
            )
        members = data.get("members", [])
        new_members = [m for m in members if m.get("email") != member_email]
        if len(new_members) == len(members):
            raise HTTPException(
                status_code=404, detail=f"Member '{member_email}' not found"
            )
        data["members"] = new_members
        return data, {"status": "removed", "email": member_email}

    return operations.apply_operation(
        project_id, op, op_name="member.remove", actor=user.get("sub", ""),
    )


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
    if body.role not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="Role must be 'viewer' or 'editor'")

    def op(data: dict):
        if _auth_enabled and data.get("owner") != user.get("sub"):
            raise HTTPException(
                status_code=403, detail="Only project owner can change roles"
            )
        members = data.get("members", [])
        for m in members:
            if m.get("email") == member_email:
                m["role"] = body.role
                data["members"] = members
                return data, {"email": member_email, "role": body.role}
        raise HTTPException(
            status_code=404, detail=f"Member '{member_email}' not found"
        )

    return operations.apply_operation(
        project_id, op, op_name="member.update_role", actor=user.get("sub", ""),
    )


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


@app.post("/api/admin/trash/sweep")
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
    _require_admin(user)
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
            per_proj_result = operations.apply_operation(
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


class AdminOwnerTransfer(BaseModel):
    new_owner_id: str


@app.patch("/api/admin/projects/{project_id}/owner")
def admin_transfer_owner(project_id: str, body: AdminOwnerTransfer,
                         user: dict = Depends(require_auth)):
    """Transfer project ownership (admin only)."""
    _require_admin(user)
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

    return operations.apply_operation(
        project_id, op, op_name="admin.transfer_owner", actor=user.get("sub", ""),
    )


@app.get("/api/admin/me")
def admin_check(user: dict = Depends(require_auth)):
    """Check if current user is admin."""
    user_data = db.get_user(user.get("sub", ""))
    is_admin = user_data.get("role") == "admin" if user_data else False
    return {"is_admin": is_admin}


# ---------------------------------------------------------------------------
# Retro
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/notes")
def list_notes(project_id: str, user: dict = Depends(require_auth)):
    """List session notes from Firestore."""
    _load(project_id, user)
    return db.list_notes(project_id)


@app.post("/api/projects/{project_id}/notes")
def add_note(project_id: str, body: NoteCreate, user: dict = Depends(require_auth)):
    """Add a session note."""
    import datetime
    _load(project_id, user)
    note = {
        "ts": body.ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "text": body.text,
    }
    if body.context:
        note["context"] = body.context
    if body.session_id:
        note["session_id"] = body.session_id
    note_id = db.add_note(project_id, note)
    return {"note_id": note_id, **note}


@app.delete("/api/projects/{project_id}/notes")
def clear_notes(project_id: str, user: dict = Depends(require_auth)):
    """Clear all session notes."""
    data = _load(project_id, user)
    _require_write(data, user)
    db.clear_notes(project_id)
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# Session registry (ms-57 / e-1063)
# ---------------------------------------------------------------------------

@app.put("/api/projects/{project_id}/sessions/{session_id}")
def upsert_session(
    project_id: str,
    session_id: str,
    body: SessionUpsert,
    user: dict = Depends(require_auth),
):
    """Upsert a session registry entry.

    Heartbeat path: CLI sends only `last_active` to refresh liveness without
    overwriting the original mint metadata. Initial mint path: CLI sends the
    full payload. Firestore merge=True (in db.upsert_session) handles both.
    """
    _load(project_id, user)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        # Nothing to write — surface as a no-op rather than a 422, so callers
        # debouncing client-side don't need to special-case empty bodies.
        return {"status": "noop"}
    db.upsert_session(project_id, session_id, payload)
    return {"status": "ok", "session_id": session_id}


@app.get("/api/projects/{project_id}/sessions")
def list_sessions(
    project_id: str,
    user_id: str = "",
    machine: str = "",
    agent: str = "",
    live_only: bool = False,
    since_minutes: int = 5,
    user: dict = Depends(require_auth),
):
    """List sessions for a project, with optional directory-query filters.

    ms-54 / e-1134: the rendezvous CLI (e-999) needs to look up "which session
    of this user/machine/agent is currently live" so a sender can pick a DM
    target without knowing the exact session_id out-of-band.

    Filters (all opt-in; the no-arg call still returns everything, ordered by
    last_active desc, to preserve the ms-57 rescue and Web UI 'who is active'
    behavior):

      * ``user_id``       — match ``actor.email`` (user identity surfaces as the
                            email field per session.py's mint convention).
      * ``machine``       — match ``actor.machine`` exactly.
      * ``agent``         — match ``actor.agent`` exactly.
      * ``live_only``     — drop sessions whose ``last_active`` is older than
                            ``since_minutes`` ago. Heartbeat-based liveness, so
                            a session that crashed without session-end is
                            correctly classified as not-live once its heartbeat
                            goes stale (≥ ms-57 heartbeat cadence + slack).
      * ``since_minutes`` — threshold for live_only. Default 5 matches the
                            session heartbeat cadence; raise it for "active in
                            last hour" style queries.

    Filtering is in-memory after load. The sessions/ subcollection is bounded
    (single-digit to a few dozen docs per project in practice), so we avoid
    Firestore composite-index requirements for what is fundamentally an
    interactive picker query.
    """
    _load(project_id, user)
    sessions = db.list_sessions(project_id)
    if not (user_id or machine or agent or live_only):
        return sessions

    def _matches(s: dict) -> bool:
        actor = s.get("actor") or {}
        if user_id and actor.get("email", "") != user_id:
            return False
        if machine and actor.get("machine", "") != machine:
            return False
        if agent and actor.get("agent", "") != agent:
            return False
        return True

    filtered = [s for s in sessions if _matches(s)]

    if live_only:
        import datetime
        cutoff = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=since_minutes)
        )
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        def _is_live(s: dict) -> bool:
            la = s.get("last_active", "")
            # Compare ISO8601 strings lexicographically — same wire format on
            # both sides (server-stamped UTC microseconds).
            return bool(la) and la >= cutoff_iso

        filtered = [s for s in filtered if _is_live(s)]

    return filtered


# ---------------------------------------------------------------------------
# Session log (ms-57 / e-1037)
# ---------------------------------------------------------------------------

@app.put("/api/projects/{project_id}/session_logs/{session_id}")
def upsert_session_log(
    project_id: str,
    session_id: str,
    body: SessionLogUpsert,
    user: dict = Depends(require_auth),
):
    """Upsert a session log entry keyed by session_id (merge=True).

    Both session-end (e-1038) and rescue (e-1039) call this with their own
    subset of fields; merge semantics make the calls commutative — last
    writer wins per field, but no field gets nulled by a partial body.
    """
    _load(project_id, user)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    if not payload:
        return {"status": "noop"}
    db.upsert_session_log(project_id, session_id, payload)
    return {"status": "ok", "session_id": session_id}


@app.get("/api/projects/{project_id}/session_logs/{session_id}")
def get_session_log(
    project_id: str,
    session_id: str,
    user: dict = Depends(require_auth),
):
    """Fetch a single session log entry. Returns 404 if absent."""
    _load(project_id, user)
    doc = db.get_session_log(project_id, session_id)
    if doc is None:
        raise HTTPException(404, detail=f"session_log not found: {session_id}")
    return doc


@app.get("/api/projects/{project_id}/session_logs")
def list_session_logs(
    project_id: str,
    limit: int = 0,
    user: dict = Depends(require_auth),
):
    """List session logs by last_aggregated_at desc. ``limit=0`` means all."""
    _load(project_id, user)
    return db.list_session_logs(project_id, limit=limit or None)


# ---------------------------------------------------------------------------
# Bus events (ms-54 / e-996)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/bus")
async def post_bus_event(
    project_id: str,
    body: BusEventCreate,
    user: dict = Depends(require_auth),
):
    """Append a bus event. Server stamps ``created_at`` so all clients agree
    on the wall-clock ordering (clients' local clocks would diverge across
    machines, defeating the cursor semantics).

    Async handler so we can `await` the WS fan-out on the same event loop
    instead of bouncing through `run_coroutine_threadsafe` (e-997). The
    Firestore call is sync-blocking but bus posts are low-frequency, so the
    event-loop stall is acceptable at this slice; promote to `asyncio.to_thread`
    if/when traffic justifies it.
    """
    import datetime
    _load(project_id, user)
    # Coerce unknown delivery modes to the safe default rather than 422'ing.
    # Rationale: a sender ahead of the server (or a typo) MUST NOT trip a wire
    # error that the calling agent silently retries forever. Coercion to the
    # conservative default keeps the bus flowing without ever auto-elevating.
    delivery = body.delivery if body.delivery in _BUS_DELIVERY_MODES else _BUS_DELIVERY_DEFAULT
    data = {
        "channel": body.channel,
        "sender_session_id": body.sender_session_id,
        "payload": body.payload,
        "delivery": delivery,
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    }
    event_id = db.append_bus_event(project_id, data)
    event = {"event_id": event_id, **data}
    # e-997: push to all WS subscribers of this project. Multi-replica delivery
    # (events posted on another Cloud Run instance) is out of scope here —
    # Firestore on_snapshot or a pub/sub layer would solve it but adds cost;
    # the single-replica path covers UC1/UC2 dogfood.
    if _ws_connections.get(project_id):
        await _broadcast_bus_event(project_id, event)
    return event


@app.get("/api/projects/{project_id}/bus")
def list_bus_events(
    project_id: str,
    since: str = "",
    channel: str = "",
    limit: int = 100,
    user: dict = Depends(require_auth),
):
    """List bus events ordered by created_at. Use ``since=<last_seen_iso>``
    for polling-style catch-up; ``channel`` for server-side routing filter."""
    _load(project_id, user)
    return db.list_bus_events(project_id, since=since, channel=channel, limit=limit)


# e-1209: DM channels are 1:1 unicast by default. Without server-side
# enforcement, a `dm` event posted to project P fans out to every session in
# P that subscribes to `dm` — the receiver-side filter in channel/bus.mjs
# treated missing `payload.recipient_session_id` as "broadcast", and the
# sender path (cmd_bus_send) never stamped that field. Net effect: every DM
# was a broadcast to all dm-subscribed sessions in the project.
#
# Fix is server-authoritative (not just receiver-side) because:
#   1. older receiver builds in the wild still treat missing recipient as
#      broadcast; the server is the only point where every client converges.
#   2. defense-in-depth — a bug in any single receiver shouldn't be able to
#      smuggle DMs to the wrong session.
#
# Routing rules below are intentionally channel-aware:
#   * ``dm`` channel: missing recipient → drop (legacy senders that don't
#     stamp must be treated as malformed rather than broadcast). Mismatched
#     recipient → drop. Matching recipient → pass.
#   * other channels (default broadcast semantics for non-DM): missing
#     recipient → pass (broadcast), mismatched recipient → drop, matching
#     recipient → pass. This keeps `notify`/`ops`/etc broadcast-friendly
#     until ms-54 follow-ups give them their own routing rules.
#
# Self-loop guard (sender == recipient → drop) is also enforced here so
# even a buggy or absent receiver-side filter cannot deliver an event to
# its own author.
_DM_CHANNELS = {"dm"}


def _bus_event_addressed_to(event: dict, recipient_id: str) -> bool:
    """Return True iff ``event`` should be delivered to ``recipient_id``.

    See the module-level rationale on _DM_CHANNELS above. This helper is
    the single source of truth for DM routing; both the /unread endpoint
    and any future WS fan-out filter must funnel through it.
    """
    sender = str(event.get("sender_session_id") or "")
    if sender and sender == recipient_id:
        # Self-sent: never deliver to the author. Receivers also guard
        # against this, but we drop server-side too so a misconfigured
        # consumer can't echo-loop itself into the budget gate.
        return False
    channel = event.get("channel") or ""
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        # Malformed payload (non-dict) cannot encode a recipient — treat as
        # broadcast for non-DM channels and as malformed-drop for DM.
        return channel not in _DM_CHANNELS
    intended = str(payload.get("recipient_session_id") or "")
    if intended:
        return intended == recipient_id
    # No recipient stamp: DM channels require explicit unicast, others
    # default to broadcast.
    return channel not in _DM_CHANNELS


@app.get("/api/projects/{project_id}/bus/unread")
def list_unread_bus_events(
    project_id: str,
    recipient_id: str,
    channel: str = "",
    limit: int = 100,
    user: dict = Depends(require_auth),
):
    """List events the recipient has not yet acknowledged via their cursor.

    Like ``GET /bus`` except ``since`` is resolved server-side from
    ``bus_cursors/{recipient_id}`` AND results are filtered through
    :func:`_bus_event_addressed_to` so DM channels do not fan out to
    every subscriber (e-1209). The endpoint **does not advance** the
    cursor — callers POST to ``/bus/cursors/{recipient_id}`` after processing.
    Splitting read and acknowledge lets crashing consumers get at-least-once
    delivery (events stay readable until acknowledged) while structurally
    preventing duplicate delivery once they acknowledge.

    We over-fetch from the store (``limit * 4`` capped at 400) before applying
    the recipient filter so a high-volume channel doesn't starve the
    requested recipient when most of the window is addressed to others.
    The cursor still advances by ``created_at`` across the full set, so
    skipped events are not redelivered on the next round.
    """
    _load(project_id, user)
    cursor = db.get_bus_cursor(project_id, recipient_id)
    since = cursor.get("last_seen_at", "")
    # Over-fetch so the recipient filter does not blank out a noisy window.
    raw_limit = min(max(limit * 4, limit), 400) if limit else 400
    raw = db.list_bus_events(
        project_id, since=since, channel=channel, limit=raw_limit,
    )
    filtered = [e for e in raw if _bus_event_addressed_to(e, recipient_id)]
    if limit:
        filtered = filtered[:limit]
    return filtered


@app.post("/api/projects/{project_id}/bus/cursors/{recipient_id}")
def advance_bus_cursor(
    project_id: str,
    recipient_id: str,
    body: BusCursorAdvance,
    user: dict = Depends(require_auth),
):
    """Forward-only acknowledge of bus events for ``recipient_id``.

    The server discards advance requests that would rewind the cursor (see
    firestore_client.advance_bus_cursor for the structural reasoning). The
    response is the cursor state *after* the call, so the client can verify
    its commit landed.
    """
    _load(project_id, user)
    return db.advance_bus_cursor(project_id, recipient_id, body.last_seen_at)


@app.get("/api/projects/{project_id}/bus/cursors/{recipient_id}")
def get_bus_cursor(
    project_id: str,
    recipient_id: str,
    user: dict = Depends(require_auth),
):
    """Return the current cursor for ``recipient_id`` ({} if unset)."""
    _load(project_id, user)
    return db.get_bus_cursor(project_id, recipient_id)


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
def search_project(
    project_id: str,
    q: str = "",
    type: Optional[str] = None,        # CSV (task,commit,...) — required by CORE doc
    status: Optional[str] = None,      # CSV
    priority: Optional[str] = None,    # CSV
    scope: str = "",
    ms: str = "",
    op: str = "",
    id: str = "",
    assignee: str = "",
    owner: str = "",
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_auth),
):
    """Unified search across all Beacon entities.

    See CORE doc 'Beacon 検索基盤の原則' and SPEC '3ne57ccZegYQXDQA03op' for
    the design contract. This endpoint delegates to lib/search.search_project
    so the CLI, server, and Skills all share the same logic.
    """
    import sys as _sys, os as _os
    _LIB = _os.path.join(_os.path.dirname(__file__), "..", "lib")
    if _LIB not in _sys.path:
        _sys.path.insert(0, _LIB)
    import search as _search  # noqa: PLC0415

    data = _load(project_id, user)
    # Hydrate documents from Firestore subcollection.
    documents = db.list_documents(project_id)

    def _split(s: Optional[str]) -> Optional[list[str]]:
        if not s:
            return None
        return [x.strip() for x in s.split(",") if x.strip()]

    return _search.search_project(
        data,
        documents,
        q=q,
        type=_split(type),
        status=_split(status),
        priority=_split(priority),
        scope=scope,
        ms=ms,
        op=op,
        id=id,
        assignee=assignee,
        owner=owner,
        from_date=from_ or "",
        to_date=to or "",
        limit=limit,
        offset=offset,
    )


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


async def _broadcast_bus_event(project_id: str, event: dict):
    """Push a single bus event to all WS clients subscribed to this project.

    ms-54 / e-997: per-channel/per-recipient subscribe filtering lives client-
    side at this slice — every connected client sees every event for the
    project and decides what to do with it. Server-side filtering arrives
    with e-1134 (directory query) + §9 subscribe filter.
    """
    clients = _ws_connections.get(project_id, set()).copy()
    if not clients:
        return
    msg = {"type": "bus_event", "data": event}
    for ws in clients:
        try:
            await ws.send_json(msg)
        except Exception:
            _ws_connections.get(project_id, set()).discard(ws)


def _build_document_change_payload(project_id: str, doc_id: str, op: str,
                                   fallback_title: str = "",
                                   fallback_scope: str | None = None) -> dict:
    """Construct the WS payload for a document add/update event.

    We re-read the saved doc via ``db.get_document`` so the broadcast carries
    the *post-write* values (especially ``updated_at`` which the server stamps
    and ``scope`` which may be normalized from frontmatter). If the read
    fails for any reason — racing delete, transient Firestore error — we
    degrade to the request body values so clients still get something to
    insert/update on, rather than silently dropping the event.
    """
    saved = db.get_document(project_id, doc_id) or {}
    scope = saved.get("scope") or fallback_scope or "memo"
    payload = {
        "op": op,
        "doc_id": doc_id,
        "title": saved.get("title", fallback_title),
        "scope": scope,
        "updated_at": saved.get("updated_at", ""),
    }
    milestone = saved.get("milestone")
    if milestone:
        payload["milestone"] = milestone
    return payload


async def _broadcast_document_change(project_id: str, payload: dict):
    """Push a document add/update/delete notification to all WS clients of this
    project (ms-43 e-809).

    Rationale: Documents are not part of the project doc snapshot stream
    (they live in a Firestore subcollection that ``_on_snapshot`` doesn't
    watch), so a write here is invisible to clients until they re-open the
    Documents tab and trigger ``loadDocuments`` from scratch. That breaks
    parity with Milestones / Tasks which already render reactively via the
    project broadcast.

    Rather than expand the project snapshot to embed documents (would balloon
    every WS frame), we mirror the ``_broadcast_bus_event`` pattern with a
    distinct message type. The payload is a *single-document delta* — clients
    apply it to ``state.documents`` regardless of whether the Documents tab
    is currently active, so re-opening the tab shows the latest list with no
    network round trip.

    Payload schema (locked-in for client compatibility):
      op        : "add" | "update" | "delete"
      doc_id    : str — Firestore document id
      title     : str — current title (empty string on delete is OK)
      scope     : str — "core" | "spec" | "memo" | "report"
      updated_at: str — ISO timestamp from the write (empty on delete)
      milestone : str (optional) — present iff doc has a milestone frontmatter
    """
    clients = _ws_connections.get(project_id, set()).copy()
    if not clients:
        return
    msg = {"type": "document_change", "data": payload}
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
    """WebSocket endpoint for real-time project monitoring.

    Close codes used by this endpoint (clients must distinguish them — e-639):

      4401  TOKEN MISSING   — no token query param. Client should not retry
                              silently; redirect to login.
      4403  TOKEN EXPIRED   — token presented but rejected. Client should
                              attempt a refresh (where supported) and either
                              re-connect with a fresh token or surface a
                              "please log in again" notice.

    Both codes are in the application-private range (4000–4999) so they do
    not collide with standard WebSocket close codes. The browser exposes them
    via CloseEvent.code, which makes the retry decision deterministic on the
    client side (no more silent 1008 + infinite reconnect loop).
    """
    token = websocket.query_params.get("token")
    if _auth_enabled:
        if not token:
            # Reason text helps server-side audit logs; clients should rely on code.
            await websocket.close(code=4401, reason="token_missing")
            return
        try:
            _verify_id_token(token)
        except HTTPException:
            await websocket.close(code=4403, reason="token_expired_or_invalid")
            return

    await websocket.accept()

    if project_id not in _ws_connections:
        _ws_connections[project_id] = set()
    _ws_connections[project_id].add(websocket)

    # Send initial enriched data. load_project_consistent hydrates v2
    # projects from the subcollection so the client receives the full
    # milestones[] regardless of schema layout.
    try:
        raw = operations.load_project_consistent(project_id)
    except LookupError:
        raw = None
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
    # version is exposed so release.yml can assert that the bump commit has
    # actually reached the Cloud Run revision (e-953 AC 2): without this the
    # downstream deploy could "succeed" while still serving the old image.
    #
    # Use the standalone beacon_cli/_version.py file — importing lib.commands
    # transitively pulls peer modules (`from store import get_store` etc.)
    # that bin/beacon normally places on sys.path; in the FastAPI runtime the
    # uvicorn worker doesn't, so the import raises ModuleNotFoundError and
    # /health silently reports "unknown" forever. Caught by the v0.9.0 dogfood
    # (ms-52 e-960 finding).
    try:
        from beacon_cli._version import __version__ as _beacon_version
    except Exception:
        _beacon_version = "unknown"
    return {
        "status": "ok",
        "env": os.environ.get("BEACON_ENV", "dev"),
        "version": _beacon_version,
    }


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
    # Approved — issue a long-lived CLI token and return it
    sub = entry.get("sub", "")
    email = entry.get("email", "")
    cli_token, token_expiry = _make_cli_token(sub, email)
    result = {
        "status": "approved",
        "email": email,
        "id_token": cli_token,
        "token_expiry": token_expiry,
    }
    del _cli_pending[code]
    return result


# ---------------------------------------------------------------------------
# TrailNode capability registry (sister product, see server/trailnode.py)
# ---------------------------------------------------------------------------

from trailnode import make_router as _make_trailnode_router
from trailnode_orgs import make_router as _make_trailnode_orgs_router

# Org router mounts under /api/trailnode/orgs (ms-6). It must be included
# before the capabilities router so that /orgs paths win over the more
# permissive `{capability_id:path}` matcher.
app.include_router(_make_trailnode_orgs_router(require_auth))
app.include_router(_make_trailnode_router(require_auth))


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
    @app.get("/privacy.html")
    def privacy_policy():
        return FileResponse(_static_dir / "privacy.html")

    @app.get("/terms")
    @app.get("/terms.html")
    def terms_of_service():
        return FileResponse(_static_dir / "terms.html")

    @app.get("/admin")
    def serve_admin():
        return FileResponse(_static_dir / "admin.html")

    @app.get("/cli-auth")
    def serve_cli_auth():
        return FileResponse(_static_dir / "cli-auth.html")

    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
