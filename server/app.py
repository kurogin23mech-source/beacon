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

from fastapi import FastAPI, HTTPException, Depends, Query, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse

import approved_actions as approved_actions_mod
import core
import dm_gate as dm_gate_mod  # ms-70 / e-1713: cross-user DM action authorization judge
import envelope as envelope_mod
import invitations as invitations_mod  # ms-78 e-1803/e-1804: token-based invites
import store_router as db  # e-1544: BEACON_STORE_BACKEND で firestore / dynamodb を切替
import operations
import trek as trek_mod  # ms-69 / e-1656: trek schema + pure mutators

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

# All mutations under /api/projects/*, /api/admin/*, and /api/treks(/...)?
import re
_AUDIT_PATHS = re.compile(r"^/api/(?:projects/[^/]+|admin/|treks(?:$|/))")


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
    # ms-69 / e-1656: treks are top-level. Disambiguate /treks/{id}/members
    # from project /members so audit logs read "trek.member.post" rather than
    # being confused with project member ops.
    if path.startswith("/api/treks"):
        if "/members" in path:
            return f"trek.member.{method.lower()}"
        if "/scope" in path:
            return f"trek.scope.{method.lower()}"
        if "/halt" in path:
            return f"trek.halt.{method.lower()}"
        if "/transfer-leader" in path:
            return f"trek.leader.{method.lower()}"
        if "/start" in path:
            return f"trek.start.{method.lower()}"
        if "/summary" in path:
            return f"trek.summary.{method.lower()}"
        return f"trek.{method.lower()}"
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
    if path.startswith("/api/treks"):
        return "trek"
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


# ---------------------------------------------------------------------------
# Identity provider dispatch (e-1545)
# ---------------------------------------------------------------------------
# BEACON_AUTH_PROVIDER = "firebase" (default, = GCP 既存経路 / Cloud Run) or
#                       "cognito"  (= AWS GA-incubation Lambda 経路)
# CLI トークンはどちらの provider でも有効 (provider-agnostic な HMAC)。
# CLI 以外の bearer トークンは provider 固有の検証経路に流す。
_AUTH_PROVIDER = os.environ.get("BEACON_AUTH_PROVIDER", "firebase").lower()

_cognito_jwks_client = None


def _get_cognito_jwks_client():
    """Return a (cached) PyJWKClient pointed at the configured Cognito User Pool.

    Cognito の JWKS は概ね不変 (= 鍵ローテーション時のみ変わる) なので、
    PyJWKClient の内部キャッシュをそのまま再利用すると毎リクエストの
    HTTP 取得を避けられる。プロセス起動後の初回呼び出しで 1 回だけ
    JWKS endpoint に届く。
    """
    global _cognito_jwks_client
    if _cognito_jwks_client is not None:
        return _cognito_jwks_client
    user_pool_id = os.environ.get("BEACON_COGNITO_USER_POOL_ID", "")
    if not user_pool_id:
        raise HTTPException(
            status_code=500,
            detail="BEACON_AUTH_PROVIDER=cognito but BEACON_COGNITO_USER_POOL_ID is unset",
        )
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    import jwt as _jwt
    jwks_url = (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
    )
    _cognito_jwks_client = _jwt.PyJWKClient(jwks_url)
    return _cognito_jwks_client


def _verify_cognito_token(token: str) -> dict:
    """Verify a Cognito User Pool JWT and return the claims.

    Cognito User Pool は ID token と access token の 2 種類を発行する:
      - ID token: ``token_use=id`` + ``aud`` claim にクライアントID
      - access token: ``token_use=access`` + ``client_id`` claim にクライアントID
    Beacon CLI は ID token を使う想定 (= ユーザ属性 email / sub を要求するため)。
    access token も将来必要になる可能性があるので、両方とも受け付けて
    token_use で分岐する。
    """
    user_pool_id = os.environ.get("BEACON_COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("BEACON_COGNITO_CLIENT_ID", "")
    region = os.environ.get("AWS_REGION", "ap-northeast-1")
    if not user_pool_id:
        raise HTTPException(
            status_code=500,
            detail="BEACON_AUTH_PROVIDER=cognito but BEACON_COGNITO_USER_POOL_ID is unset",
        )
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"

    import jwt as _jwt
    try:
        jwks_client = _get_cognito_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token).key
        # First decode without aud check to read token_use, then re-validate
        # with the appropriate audience claim.
        unverified = _jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_aud": False},
        )
        token_use = unverified.get("token_use")
        if token_use == "id":
            # ID token: aud claim must match client_id (if configured)
            if client_id:
                claims = _jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256"],
                    issuer=issuer,
                    audience=client_id,
                )
            else:
                claims = unverified
        elif token_use == "access":
            # access token: client_id claim must match (manual check; PyJWT
            # decode の audience は ID token 用なのでここでは触らない)
            if client_id and unverified.get("client_id") != client_id:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid token: client_id mismatch",
                )
            claims = unverified
        else:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid token_use: {token_use!r}",
            )
        # Cognito の sub は User Pool 固有の UUID。email は ID token に含まれる
        # (= access token には無いことがある)。両者に email を埋めて
        # downstream の get_or_create_user(sub, email) が動くようにする。
        if "email" not in claims:
            claims["email"] = ""
        return claims
    except _jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")


def _verify_id_token(token: str) -> dict:
    """Verify a bearer token (CLI / Google ID / Cognito JWT) and return claims.

    検証順:
      1. Beacon CLI token (HMAC, provider 非依存)
      2. ``BEACON_AUTH_PROVIDER`` に応じた IdP 経路
         - "cognito" → Cognito User Pool JWT 検証
         - その他    → Google ID token 検証 (= 既存 Cloud Run 経路)
    """
    # Check for long-lived CLI token first (no network call)
    claims = _verify_cli_token(token)
    if claims:
        return claims
    if _AUTH_PROVIDER == "cognito":
        return _verify_cognito_token(token)
    # Fall back to Google ID token verification (= Cloud Run 既存経路)
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
    """Return user's role: 'owner', 'editor', 'viewer', or '' (no access).

    Internal: this is the role-evaluation primitive. Endpoints MUST NOT call
    this directly — go through `_require_project_role(project_id, user,
    allowed=...)` so the load-and-check pair stays atomic from the caller's
    perspective. See CORE doc "認可判定は 1 か所に集中させる" (e-1257) and
    e-1252/e-1254 for the history. The only sanctioned callers are
    `_require_project_role`, `_require_write`, and `_require_owner` — all of
    which are themselves centralized authorization gates.
    """
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


def _require_project_role(
    project_id: str,
    user: dict | None,
    *,
    allowed: tuple[str, ...] = ("owner", "editor", "viewer"),
) -> tuple[dict, str]:
    """Single source of truth for "can this user read/write this project?".

    Loads the project (404 if missing), then evaluates the caller's role and
    rejects (403) when the role is empty or not in ``allowed``. Returns
    ``(project_data, role)`` on success.

    Why this exists (e-1254): authorization used to live in two places —
    ``_load`` for REST endpoints, and an ad-hoc verify-only path for the
    WebSocket endpoint. The WS path forgot the role check entirely (e-1252),
    so any signed-in Beacon user could pull any project's contents over
    ``/ws/projects/<id>``. Consolidating the rule into one helper makes that
    failure mode structurally impossible: every caller goes through the same
    "load + role check" pair, and the only knob is ``allowed`` (used by the
    handful of endpoints that need owner-only / editor-only access).

    For WS handlers: catch ``HTTPException`` from this helper and translate
    ``404 → close 4404`` / ``403 → close 4403 (forbidden)``. REST handlers
    re-raise as-is.
    """
    try:
        data = operations.load_project_consistent(project_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if not _auth_enabled or user is None:
        # Auth disabled (dev mode) or anonymous read — _get_role still returns
        # "owner" in dev, "" with no user. Skip the gate entirely in dev so
        # local development against `BEACON_AUTH_ENABLED=0` keeps working.
        return data, ("owner" if not _auth_enabled else "")
    role = _get_role(data, user)
    if not role or role not in allowed:
        raise HTTPException(status_code=403, detail="Access denied")
    return data, role


def _load(project_id: str, user: dict | None = None) -> dict:
    # v2 (subcollection) projects need their milestones hydrated from the
    # subcollection; load_project_consistent transparently handles both v1
    # and v2 so callers get a unified dict shape either way. Falling back
    # to db.get_project here would silently drop milestones[] on v2 docs.
    #
    # e-1254: delegate to _require_project_role so REST and WS share one
    # authorization rule. The pre-existing "user is None → skip auth" path
    # is preserved because some internal callers pass user=None for ops
    # that already verified ownership upstream.
    if user is None:
        try:
            return operations.load_project_consistent(project_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    data, _role = _require_project_role(project_id, user)
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
# Author resolution (ms-78 / e-1909) — UC11-F5 follow-up
# ---------------------------------------------------------------------------

def _resolve_author(user: dict) -> dict:
    """Build the ``meta.author`` dict for a write triggered by ``user``.

    Returns ``{"user_id", "email", "display_name"}``, dropping empty fields.
    ``display_name`` is fetched from the users collection (= what
    invite-accept / /api/me/profile writes). When the user record has
    no display_name yet, that field is omitted — the UI then falls back
    to email rendering. Best-effort: any DB hiccup returns just the
    claim-derived fields so we never block a write on a profile lookup
    failure.
    """
    uid = (user.get("sub") or "").strip()
    email = (user.get("email") or "").strip()
    display_name = ""
    if uid:
        try:
            udata = db.get_user(uid)
            if udata:
                display_name = (udata.get("display_name") or "").strip()
                # Prefer the persisted email over the claim's email when both
                # exist — invite-accept writes the canonical one.
                if not email:
                    email = (udata.get("email") or "").strip()
        except Exception:  # noqa: BLE001 - profile lookup must never break the write
            pass
    author: dict = {}
    if uid:
        author["user_id"] = uid
    if email:
        author["email"] = email
    if display_name:
        author["display_name"] = display_name
    return author


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

    ms-54 / e-1318 (Option C true-heartbeat) adds three new fields the bridge
    (channel/bus.mjs) stamps on every poll iteration:

      * ``last_poll_at``     — ISO8601 UTC of the most recent poll iteration.
                               Updated *inside* the bridge's poll loop, so a
                               stale value structurally implies "this bridge
                               cannot receive events" (not "the heartbeat
                               code path ran on a process whose poll loop
                               died long ago").
      * ``poll_interval_ms`` — bridge's poll cadence. Lets the server compute
                               a precise "healthy if last_poll_at within
                               max(30s, 2 × poll_interval_ms)" threshold
                               rather than guessing.
      * ``shutdown``         — True iff the bridge wrote this update as part
                               of a graceful SIGINT/SIGTERM teardown. Used
                               by the directory ``--healthy`` filter to
                               immediately classify deliberately-stopped
                               sessions as not-healthy, instead of waiting
                               for ``last_poll_at`` to go stale.
    """
    actor: Optional[dict] = None
    created_at: Optional[str] = None
    last_active: Optional[str] = None
    harness: Optional[str] = None
    last_poll_at: Optional[str] = None
    poll_interval_ms: Optional[int] = None
    shutdown: Optional[bool] = None

    # ms-54 / e-1369: session transparency in 4 layers (5th is INTENT, written
    # via a separate endpoint so the bridge never mints narrative text).
    #
    #   Layer 0 — Identity     : agent.{kind, version}, harness.{kind, version}
    #   Layer 1 — Where        : cwd, git.{branch, head_short, head_subject}
    #   Layer 2 — What         : focus.{milestone, recent_task}
    #   Layer 3 — Reach        : channels, budget
    #
    # All optional. A bridge that only stamps Identity at mint and Where on
    # heartbeat still serialises correctly via merge=True. The dicts are
    # shaped (rather than flat fields) so adding a sub-key later doesn't bump
    # the SessionUpsert surface area, and the JSON wire format reads like a
    # natural namespace ("agent.version" rather than "agent_version").
    agent: Optional[dict] = None      # {kind, version}
    # NOTE: the legacy top-level `harness: str` above is kept for back-compat;
    # the new structured form lands under runtime.harness instead of replacing
    # the flat field. A bridge can populate both — readers should prefer the
    # nested dict when present.
    runtime: Optional[dict] = None    # {harness: {kind, version}}
    cwd: Optional[str] = None
    git: Optional[dict] = None        # {branch, head_short, head_subject}
    focus: Optional[dict] = None      # {milestone: {id, title}, recent_task: {id, description}}
    channels: Optional[list[str]] = None
    budget: Optional[dict] = None     # {remaining, total}


class SessionIntentUpsert(BaseModel):
    """Body for POST /api/projects/{project_id}/sessions/{session_id}/intent
    (ms-54 / e-1369 Layer 4).

    Intent is the *AI's self-report* of what it is currently doing — the only
    Layer that depends on natural language rather than machine observation.
    The bridge does NOT write intent (it has no insight into the AI's goal);
    the AI stamps it via `beacon session focus "<text>"` or the picker shows
    "(idle)" when absent.

    `attention_required` is a boolean flag the AI raises when it is waiting
    on a human decision. Readers (directory picker, Web UI) show it
    prominently so a teammate sees "who needs me" at a glance.
    """
    text: Optional[str] = None
    attention_required: Optional[bool] = None


_BUS_DELIVERY_MODES = {"auto-execute", "propose-to-ai", "notify-user-only"}
_BUS_DELIVERY_DEFAULT = "propose-to-ai"


# ---------------------------------------------------------------------------
# Envelope verify adapters (ms-54 / e-1155 Phase 1)
#
# The envelope module is interface-agnostic; here we bind it to Firestore so
# nonce-replay protection and in_reply_to parent lookups hit the real store
# in production. Tests stub these via firestore_client monkey-patching, the
# same way the existing bus_transport tests do.
# ---------------------------------------------------------------------------


class _FirestoreNonceStore(envelope_mod.NonceStore):
    """Wrap firestore_client.check_and_record_bus_nonce for the envelope
    verifier. Computes an ``expires_at`` from the configured nonce TTL so
    an external sweeper can GC stale entries without hitting the verify
    hot path."""

    def check_and_record(self, project_id: str, nonce: str) -> bool:
        import datetime
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(seconds=envelope_mod.NONCE_TTL_SECONDS))
        return db.check_and_record_bus_nonce(
            project_id, nonce,
            expires.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )


def _envelope_nonce_store() -> envelope_mod.NonceStore:
    """Indirection so tests can override the nonce store."""
    return _FirestoreNonceStore()


def _envelope_parent_lookup() -> envelope_mod.ParentLookup:
    """Indirection so tests can override the parent lookup."""
    return envelope_mod.FunctionParentLookup(db.find_bus_event)


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
    # e-1155 Phase 1: AI-to-AI authorization envelope. Optional during the
    # rollout — events without it are treated as T5-equivalent legacy (no
    # auto-execute, no info disclosure beyond short ping). Adopting senders
    # call the issuance endpoint first and stamp the result here.
    envelope: Optional[dict] = None
    # Optional structured action declaration. Senders that want auto-execute
    # OR want the server to enforce the tier permission matrix must declare
    # the action by name here. The legacy free-text payload path remains
    # supported for backward compat (no enforced action).
    requested_action: Optional[str] = None


class EnvelopeIssueRequest(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/envelope/issue.

    e-1155 Phase 1. The server uses the calling user (require_auth) as
    proof of the human signature for T1, and signs the envelope with the
    server HMAC secret. T2 envelopes (Operation scope) are also issued
    here — the caller declares ``scope`` to opt in.

    Issuance discipline (CORE doc § "scope 自然言語の曖昧性"):
      * ``actions_authorized`` must enumerate concrete action names
      * wildcards / regex / natural language are rejected at the
        envelope module boundary
    """
    tier: str
    actions_authorized: list[str] = []
    scope: Optional[str] = None
    data_class: str = "free"
    conversation_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    chain_depth: int = 0
    ttl_seconds: int = 3600


class OperationApproveRequest(BaseModel):
    """Body for POST /api/projects/{id}/operations/{op_id}/envelopes (ms-60 / e-1339).

    Mints a T2 envelope from a SPEC doc whose frontmatter declares
    ``approved_actions``. The SPEC doc must already exist and be linked to
    ``op_id`` (frontmatter ``operation: op-X``). ``ttl_seconds`` defaults to
    "effectively forever" (30 years) per ms-60 SPEC § 設計方針 2 —
    "SPEC 更新まで無期限" with explicit ``beacon operation revoke`` as the
    escape valve.
    """
    spec_doc_id: str
    ttl_seconds: int = 30 * 365 * 86400  # ~30 years; revoke is the kill-switch


class OperationRevokeRequest(BaseModel):
    """Body for POST /api/projects/{id}/operations/{op_id}/envelopes/{env_id}/revoke."""
    reason: str = "manual revoke"


class BusCursorAdvance(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/cursors/{recipient_id}.

    ms-54 / e-998. Consumers commit ``last_seen_at`` after successfully
    processing a batch. The server enforces forward-only semantics, so a
    stale client that sends an older value gets a silent no-op rather than
    rewinding the cursor for everyone else.
    """
    last_seen_at: str


class BusEventReceiptAck(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/{event_id}/ack (ms-54 / e-1348).

    Records a per-event read-receipt stage. The cursor (BusCursorAdvance)
    only tracks the recipient's aggregate frontier; this gives the sender
    a way to ask "did *this* event surface to anyone?".

    ``stage`` is one of:
      * ``delivered`` — receiver bridge fetched the event (poll/WS landed)
      * ``opened``    — receiver dispatched it to the AI/MCP client; the
                        recipient session has structurally seen the content

    ``recipient_session_id`` is stamped on the event as ``<stage>_by`` so an
    auditor can answer "who opened it". First-write-wins per stage — repeat
    acks are idempotent no-ops (returned with ``already_set=True``).
    """
    stage: str
    recipient_session_id: str


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


class ActiveClaimSave(BaseModel):
    """Body for ``POST /api/projects/{pid}/active_claims/{claim_id}`` (ms-55 e-1730).

    The whole `payload` dict is the wire shape lib/claims.py:build_claim_payload
    produces — claim_kind, target {kind,id}, from_session_id, intent,
    optional to_session_id / expires_at / metadata, issued_at, claim_id.
    We do not validate the schema server-side; the client builds + validates
    locally and this layer is a pure persistence mirror.
    """
    payload: dict

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
    # Permission check — viewers are fine for read, admins bypass membership.
    # e-1257: route through _require_project_role so this endpoint can't drift
    # away from the WS/REST authorization gate. Admin bypass is preserved by
    # short-circuiting the membership check when user.is_admin is set.
    if user.get("is_admin"):
        try:
            data = operations.load_project_consistent(project_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    else:
        data, _role = _require_project_role(project_id, user)

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


# ---------------------------------------------------------------------------
# Cloud-first identity (ms-62 / e-1509)
#
# Three endpoints under /api/me/ that move identity (project membership,
# machine_id, session_id) from client-side state to server-side authority:
#
#   GET  /api/me/projects   — list the projects the calling user is a member
#                             of, with role. Identical filter to GET /api/
#                             projects but emits {id, name, role} so callers
#                             that want a machine-readable membership shape
#                             don't have to scrape the broader project listing.
#
#   POST /api/me/machine    — get-or-mint a machine_id for the calling user +
#                             a client-supplied fingerprint (typically the OS
#                             hostname). First call returns a fresh machine_id;
#                             subsequent calls with the same fingerprint
#                             return the same id.
#
#   POST /api/me/heartbeat  — get-or-mint a session_id for the identity tuple
#                             (project_id, machine_id, parent_pid). First call
#                             for a tuple mints a fresh sid; subsequent calls
#                             with the same tuple return the same sid and bump
#                             last_heartbeat_at. This is the cloud-first
#                             alternative to the client-side mint path in
#                             lib/session.py — see ms-62 SPEC for the
#                             judgment trail.
#
# These endpoints exist alongside (not replacing) the existing
# PUT /api/projects/{p}/sessions/{sid} path, so v0.31.0 clients keep working
# during the compat window. v0.33.0 will hard-cut the legacy path; see
# ms-62 task e-1513 for the migration plan.
# ---------------------------------------------------------------------------

class MeMachineUpsert(BaseModel):
    """Body for POST /api/me/machine (e-1509).

    fingerprint is the client-supplied identifier that buckets "is this the
    same machine I saw before?". Typically the OS hostname. The server uses
    it as the lookup key and returns a fresh opaque machine_id on first
    sight; subsequent calls with the same fingerprint return the same
    machine_id.
    """
    fingerprint: str
    hostname: Optional[str] = None
    agent: Optional[str] = None


class MeHeartbeat(BaseModel):
    """Body for POST /api/me/heartbeat (e-1509).

    Identity tuple = (project_id, machine_id, parent_pid). Carries cwd and
    other heartbeat metadata as observational payload — server stores them
    on the session record but does not use them for identity lookup.
    """
    project_id: str
    machine_id: str
    parent_pid: int
    cwd: Optional[str] = None
    branch: Optional[str] = None
    focus_milestone: Optional[str] = None
    agent: Optional[dict] = None


class MeProfileUpdate(BaseModel):
    """Body for PATCH /api/me/profile (ms-78 / e-1909).

    Only ``display_name`` is mutable through this endpoint — email is the
    sign-in identity (managed by the OAuth provider) and ``user_id`` is
    immutable. Empty string clears the display name (= fall back to email
    in the UI).
    """
    display_name: str = ""


@app.get("/api/me/profile")
def me_get_profile(user: dict = Depends(require_auth)):
    """Return the caller's own profile (display_name + email + user_id).

    ms-78 / e-1909 — the Web UI's Settings > Profile tab and the retroactive
    "you haven't set a display name yet" prompt both read this endpoint to
    discover the current state. We don't leak any field outside the
    user's own record (= same identity gate as every other /api/me/* route:
    ``require_auth`` resolves the JWT to a ``sub``).
    """
    uid = user.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="user has no sub claim")
    udata = db.get_user(uid) or {}
    email = (udata.get("email") or user.get("email") or "").strip()
    display_name = (udata.get("display_name") or "").strip()
    return {
        "user_id": uid,
        "email": email,
        "display_name": display_name,
    }


@app.patch("/api/me/profile")
def me_update_profile(body: MeProfileUpdate, user: dict = Depends(require_auth)):
    """Update the caller's own display_name (ms-78 / e-1909).

    Trimmed empty string explicitly clears the field — the UI then falls
    back to the email label. ``db.update_user`` is symmetric across the
    Firestore and DynamoDB backends (= store_router routes by
    ``BEACON_STORE_BACKEND``).
    """
    uid = user.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="user has no sub claim")
    display_name = (body.display_name or "").strip()
    # Mint the user record if absent (= first-time profile edit for an
    # auto-created identity that never went through invite-accept).
    udata = db.get_user(uid)
    if not udata:
        email = (user.get("email") or "").strip()
        try:
            db.get_or_create_user(uid, email)
        except Exception:  # noqa: BLE001 - best-effort mint, update still proceeds
            pass
    try:
        ok = db.update_user(uid, {"display_name": display_name})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"profile update failed: {e}")
    if not ok:
        raise HTTPException(status_code=404, detail="user record not found")
    return {
        "status": "ok",
        "user_id": uid,
        "display_name": display_name,
    }


@app.get("/api/me/projects")
def me_list_projects(user: dict = Depends(require_auth)):
    """List the calling user's project memberships with role (ms-62 / e-1509).

    Mirrors the filter logic in list_projects but emits a per-project role so
    callers (= /beacon-dm-send picker, dm_discover) get membership without
    scraping the broader project listing.
    """
    uid = user.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="user has no sub claim")
    items = db.list_projects(user_id=uid, include_archived=False)
    result = []
    for item in items:
        pid = item.get("project_id", "")
        if not pid:
            continue
        # Reach into the project doc once to discover the role. list_projects
        # does the membership filter but doesn't return role; this second
        # read is cheap because Firestore single-doc reads are fast and the
        # user typically has <50 projects.
        project = db.get_project(pid) or {}
        if project.get("owner") == uid:
            role = "owner"
        else:
            members = project.get("members", []) or []
            role = ""
            for m in members:
                if m.get("user_id") == uid:
                    role = m.get("role", "member") or "member"
                    break
            if not role:
                # Migration-period projects without owner are visible to all;
                # treat that as "member" so the picker doesn't have to handle
                # an empty role.
                role = "member"
        result.append({
            "id": pid,
            "name": item.get("name", ""),
            "role": role,
        })
    return result


@app.get("/api/me/sessions")
def me_list_sessions(
    live_only: bool = False,
    since_minutes: int = 5,
    healthy_only: bool = False,
    machine: str = "",
    agent: str = "",
    user: dict = Depends(require_auth),
):
    """Cross-project session directory for the calling user (ms-54 / e-1587).

    The per-project endpoint /api/projects/{pid}/sessions answers "who in
    *this* project is live"; what was missing was the cross-project view
    answering "what bclaude sessions of mine are alive *anywhere* right now".
    Without it, /beacon-dm-send had to cd into each candidate project to list
    DM recipients, and incident diagnosis (e.g. the e-1579 heartbeat-stop
    re-occurrence check) could not see live sessions outside the diagnostician's
    cwd.

    Same filter contract as the per-project endpoint (live_only, since_minutes,
    healthy_only, machine, agent). Each returned row carries the project_id +
    project_name it belongs to, so the dm picker can route the subsequent
    `bus send --project <pid>` without an extra lookup.

    Membership is enforced via db.list_projects(user_id=uid) — projects the
    user is neither owner nor member of are excluded. Archived projects are
    also excluded; resurrecting them is an explicit user action.
    """
    uid = user.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="user has no sub claim")

    import datetime
    now_dt = datetime.datetime.now(datetime.timezone.utc)

    items = db.list_projects(user_id=uid, include_archived=False)

    all_sessions: list[dict] = []
    for item in items:
        pid = item.get("project_id", "")
        if not pid:
            continue
        sessions = db.list_sessions(pid)
        # Stamp project context + poll_health on every row before filtering so
        # the consumer can disambiguate by project_id and read health without
        # a second round-trip. Same shape as the per-project endpoint plus
        # the new project_id / project_name fields.
        pname = item.get("name", "")
        for s in sessions:
            s["project_id"] = pid
            s["project_name"] = pname
            s["poll_health"] = _compute_poll_health(s, now_dt)
            s["bridge"] = bool(s.get("last_poll_at"))
        all_sessions.extend(sessions)

    def _matches(s: dict) -> bool:
        actor = s.get("actor") or {}
        if machine and actor.get("machine", "") != machine:
            return False
        if agent and actor.get("agent", "") != agent:
            return False
        return True

    filtered = [s for s in all_sessions if _matches(s)] if (machine or agent) else all_sessions

    if live_only:
        cutoff = now_dt - datetime.timedelta(minutes=since_minutes)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        filtered = [
            s for s in filtered
            if (la := s.get("last_active", "")) and la >= cutoff_iso
        ]

    if healthy_only:
        filtered = [
            s for s in filtered
            if s.get("poll_health", {}).get("healthy") is True
        ]

    filtered.sort(key=lambda s: s.get("last_active", ""), reverse=True)
    return filtered


@app.post("/api/me/machine")
def me_upsert_machine(
    body: MeMachineUpsert, user: dict = Depends(require_auth)
):
    """Get or mint a machine_id for (user, fingerprint) (ms-62 / e-1509).

    Returns ``{machine_id, minted, fingerprint}``. ``minted`` is True iff
    this call created the document (= the client should cache the returned
    machine_id in ~/.beacon/machine.json).
    """
    uid = user.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="user has no sub claim")
    if not body.fingerprint:
        raise HTTPException(
            status_code=400, detail="fingerprint is required"
        )
    machine_id, minted = db.get_or_mint_machine(
        uid, body.fingerprint,
        hostname=body.hostname or body.fingerprint,
        agent=body.agent or "",
    )
    return {
        "machine_id": machine_id,
        "minted": minted,
        "fingerprint": body.fingerprint,
    }


@app.post("/api/me/heartbeat")
def me_heartbeat(body: MeHeartbeat, user: dict = Depends(require_auth)):
    """Get or mint a session_id for the identity tuple (ms-62 / e-1509).

    Tuple = (project_id, machine_id, parent_pid). Returns
    ``{session_id, minted, last_heartbeat_at, created_at}``.

    The caller must be a member of project_id, otherwise 403 — this prevents
    a user from materialising session records in projects they don't
    belong to (= same membership boundary as the rest of /api/projects).
    """
    uid = user.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="user has no sub claim")
    if not body.project_id or not body.machine_id:
        raise HTTPException(
            status_code=400,
            detail="project_id and machine_id are required",
        )
    if not isinstance(body.parent_pid, int) or body.parent_pid <= 0:
        raise HTTPException(
            status_code=400, detail="parent_pid must be a positive integer",
        )
    # Reuse the existing project-load + membership check from
    # /api/projects/{p}/* so we don't drift on the membership rule.
    project = db.get_project(body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    # _require_project_role would also work here but it raises only on
    # explicit list_projects empty; the membership rule we want is
    # "owner OR in members list OR project has no owner (migration)".
    owner = project.get("owner")
    members = [m.get("user_id") for m in project.get("members", []) or []]
    if owner and owner != uid and uid not in members:
        raise HTTPException(
            status_code=403,
            detail="not a member of this project",
        )

    metadata = {}
    if body.branch:
        metadata["git"] = {"branch": body.branch}
    if body.focus_milestone:
        metadata["focus"] = {"milestone": {"id": body.focus_milestone}}
    if body.agent:
        metadata["agent"] = body.agent
    result = db.get_or_mint_session_by_tuple(
        body.project_id,
        body.machine_id,
        body.parent_pid,
        user_id=uid,
        cwd=body.cwd or "",
        metadata=metadata,
    )
    return result


# ---------------------------------------------------------------------------
# High-risk endpoint envelope enforcement (e-1344 / ms-60)
#
# "銃はガラスの向こう" — destructive endpoints get a SECOND wall on the server
# side, on top of the AI-side self-check in /beacon-operation-execute Skill
# Step 4. Even if the AI bypasses its own check, the server demands a valid
# T2 envelope authorizing the exact action before the mutation runs.
#
# Header convention: ``X-Beacon-Envelope: <base64-JSON or raw-JSON>``. The
# envelope arrives in a header (not body) so the gate composes with all HTTP
# methods/payload shapes uniformly. The existing ``Authorization: Bearer ...``
# header stays intact for the identity layer; this is a separate authorization
# layer for action scope.
#
# CORE doc enumerating the protected endpoints lives at scope=core (see
# e-1344 commit message for the doc_id).
# ---------------------------------------------------------------------------

def require_envelope_for_action(action_name: str):
    """FastAPI dependency factory: gate a destructive endpoint on a T2 envelope.

    The dependency reuses the same ``envelope_mod.verify(...)`` pipeline used
    at ``/api/projects/{id}/bus`` (e-1155 Phase 1) — defense in depth, not a
    reimplementation. After verify passes, the action must also be in
    ``envelope.actions_authorized`` (with wildcard-aware match via
    ``approved_actions.matches``).

    Failure modes (all return HTTP 403 with a structured detail dict):

      * Missing header           → ``envelope_required``
      * Malformed envelope       → ``envelope_malformed``
      * verify pipeline rejects  → ``envelope_verify_rejected`` (mirrors the
                                    bus rejection shape)
      * action not authorized    → ``envelope_action_not_authorized``
    """
    async def dep(
        project_id: str,
        request: Request,
        user: dict = Depends(require_auth),
    ):
        envelope_raw = request.headers.get("X-Beacon-Envelope")
        if not envelope_raw:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "envelope_required",
                    "reason": (
                        f"action '{action_name}' requires a verified envelope"
                    ),
                    "header": "X-Beacon-Envelope",
                },
            )
        # Accept either base64-encoded JSON (common transport) or raw JSON
        # (easier for ad-hoc curl). Try base64 first because it's the canonical
        # form for header transport (avoids whitespace/quote escaping pain).
        try:
            try:
                envelope = json.loads(
                    base64.b64decode(envelope_raw).decode("utf-8")
                )
            except Exception:
                envelope = json.loads(envelope_raw)
        except Exception as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "envelope_malformed",
                    "reason": str(exc),
                },
            )
        if not isinstance(envelope, dict):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "envelope_malformed",
                    "reason": "envelope must decode to a JSON object",
                },
            )
        # Reuse the e-1155 verify pipeline. The minimal payload below is just
        # the action descriptor; the bus verify path passes the full message
        # payload but the REST gate only cares about the envelope's own
        # validity + action permission (no T5 disclosure path applies here).
        verify_result = envelope_mod.verify(
            envelope,
            project_id=project_id,
            payload={"action": action_name},
            requested_action=action_name,
            nonce_store=_envelope_nonce_store(),
            parent_lookup=_envelope_parent_lookup(),
            sender_session_id=None,
        )
        if verify_result.rejection_reason is not None:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "envelope_verify_rejected",
                    "reason": verify_result.rejection_reason,
                    "steps": verify_result.steps,
                },
            )
        # Even a passing envelope might authorize a different action — the
        # verify pipeline rejects unknown actions for T1/T2 (step 8) but
        # T3/T5 don't enumerate, so we re-check explicitly here. We use the
        # wildcard-aware matcher from approved_actions (e-1339).
        approved = envelope.get("actions_authorized") or []
        if not approved_actions_mod.matches(approved, action_name):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "envelope_action_not_authorized",
                    "reason": (
                        f"action '{action_name}' not in "
                        f"envelope.actions_authorized"
                    ),
                    "approved_actions": approved,
                },
            )
        return {"envelope": envelope, "verify_result": verify_result}

    return dep


@app.post("/api/projects/{project_id}/archive")
def archive_project(
    project_id: str,
    user: dict = Depends(require_auth),
    _envelope: dict = Depends(require_envelope_for_action("project.archive")),
):
    """Archive a project (soft delete — hidden from default listing)."""
    # e-1257: owner-only gate via the centralized helper (404 if missing,
    # 403 if not owner). Pre-check before the transaction mirrors the pattern
    # used by envelope issuance (L2131) and other owner-gated mutations.
    _require_project_role(project_id, user, allowed=("owner",))
    def op(data: dict):
        data["archived"] = True
        return data, {"status": "archived", "project_id": project_id}
    return operations.apply_operation(
        project_id, op, op_name="project.archive", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/unarchive")
def unarchive_project(project_id: str, user: dict = Depends(require_auth)):
    """Restore an archived project."""
    # e-1257: owner-only gate via the centralized helper. See archive_project.
    _require_project_role(project_id, user, allowed=("owner",))
    def op(data: dict):
        data["archived"] = False
        return data, {"status": "unarchived", "project_id": project_id}
    return operations.apply_operation(
        project_id, op, op_name="project.unarchive", actor=user.get("sub", ""),
    )


@app.post("/api/projects/{project_id}/migrate-to-v2")
def migrate_project_to_v2(
    project_id: str,
    user: dict = Depends(require_auth),
    _envelope: dict = Depends(
        require_envelope_for_action("project.migrate.v2")
    ),
):
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
    # Owner check before kicking off the transaction.
    # e-1257: route through the centralized helper. _require_project_role
    # loads via load_project_consistent which hydrates milestones on v2, but
    # this endpoint is invoked once per project (or returns "already_v2"
    # immediately on v2), so the extra subcollection read is acceptable. The
    # alternative — keeping db.get_project here — would re-fork the auth path
    # and re-create the L687/L730 family of drift that ms-39 exists to close.
    _require_project_role(project_id, user, allowed=("owner",))

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
def purge_milestone(
    project_id: str,
    ms_id: str,
    body: PurgeRequest,
    user: dict = Depends(require_auth),
    _envelope: dict = Depends(require_envelope_for_action("milestone.purge")),
):
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
    # ms-78 / e-1909 — resolve the human author identity once, then thread it
    # into core.task_add so meta.author is stamped at creation time.
    author = _resolve_author(user)

    def op(data: dict):
        _require_write(data, user)
        try:
            eid = core.task_add(
                data, ms_id, body.description,
                entry_type=body.type, date=body.date, detail=body.detail,
                author=author,
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
    # ms-78 / e-1909
    author = _resolve_author(user)

    def op(data: dict):
        _require_write(data, user)
        try:
            ms, entry = core.task_update(
                data, entry_id,
                description=body.description, status=body.status,
                detail=body.detail, date=body.date,
                author=author,
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
    # ms-78 / e-1909
    author = _resolve_author(user)

    def op(data: dict):
        _require_write(data, user)
        try:
            ms, entry = core.task_done(data, entry_id, date=today, author=author)
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
def purge_entry(
    project_id: str,
    entry_id: str,
    body: PurgeRequest,
    user: dict = Depends(require_auth),
    _envelope: dict = Depends(require_envelope_for_action("entry.purge")),
):
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
def purge_operation(
    project_id: str,
    op_id: str,
    body: PurgeRequest,
    user: dict = Depends(require_auth),
    _envelope: dict = Depends(require_envelope_for_action("operation.purge")),
):
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
# Operation envelopes (ms-60 / e-1339)
#
# T2 envelope flow: SPEC doc declares approved_actions in YAML frontmatter →
# beacon operation approve mints a server-signed envelope from that list →
# the envelope record lives in projects/{id}/operation_envelopes/.
# Re-approve auto-revokes any prior active envelope for the same op_id.
# ---------------------------------------------------------------------------

def _spec_doc_for_op(project_id: str, op_id: str, spec_doc_id: str) -> dict:
    """Load a SPEC doc and verify it's bound to ``op_id``.

    Raises HTTPException with a clear reason on any failure path so the CLI
    can surface a useful message rather than a generic 500.
    """
    doc = db.get_document(project_id, spec_doc_id)
    if not doc:
        raise HTTPException(
            status_code=404, detail=f"SPEC doc not found: {spec_doc_id}"
        )
    content = doc.get("content", "") or ""
    declared_scope = db._extract_frontmatter_field(content, "scope")
    if declared_scope != "spec":
        raise HTTPException(
            status_code=400,
            detail=(f"doc {spec_doc_id} has scope={declared_scope!r}, "
                    f"expected 'spec'"),
        )
    declared_op = db._extract_frontmatter_field(content, "operation")
    if declared_op != op_id:
        raise HTTPException(
            status_code=400,
            detail=(f"SPEC doc {spec_doc_id} is bound to operation "
                    f"{declared_op!r}, not {op_id!r}"),
        )
    return doc


@app.post("/api/projects/{project_id}/operations/{op_id}/envelopes")
def operation_approve(
    project_id: str,
    op_id: str,
    body: OperationApproveRequest,
    user: dict = Depends(require_auth),
):
    """Mint a T2 envelope from a SPEC doc's ``approved_actions``.

    Steps:
      1. Membership check (writer required — minting an authorization is a
         privileged action).
      2. Verify ``op_id`` exists on the project.
      3. Load the SPEC doc, verify it's scope=spec and bound to ``op_id``.
      4. Parse + validate ``approved_actions`` (last-segment wildcards OK
         for T2 per ms-60 SPEC § 設計方針 4).
      5. Issue server-signed envelope via envelope module.
      6. Store record (auto-revoking any prior active envelope for the op).

    Returns the stored envelope record.
    """
    data, _role = _require_project_role(
        project_id, user, allowed=("owner", "editor")
    )
    if not core.find_operations(data, op_id):
        raise HTTPException(
            status_code=404, detail=f"operation not found: {op_id}"
        )
    spec_doc = _spec_doc_for_op(project_id, op_id, body.spec_doc_id)
    content = spec_doc.get("content", "")
    try:
        raw_actions = approved_actions_mod.parse_spec_frontmatter(content)
    except approved_actions_mod.ApprovedActionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if raw_actions is None:
        raise HTTPException(
            status_code=400,
            detail=("SPEC doc has no `approved_actions` field in YAML "
                    "frontmatter; nothing to authorize"),
        )
    if not raw_actions:
        raise HTTPException(
            status_code=400,
            detail=("`approved_actions` is empty — an envelope with no "
                    "authorized actions is meaningless"),
        )
    try:
        approved_actions_mod.validate_actions(
            raw_actions, allow_last_segment_wildcard=True
        )
    except approved_actions_mod.ApprovedActionsError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid approved_actions: {exc}"
        )

    issuer = user.get("email") or user.get("sub") or "dev"
    try:
        envelope_dict = envelope_mod.issue_envelope(
            tier=envelope_mod.TIER_T2,
            issuer=issuer,
            project_id=project_id,
            scope=op_id,
            actions_authorized=raw_actions,
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    record = db.issue_operation_envelope(
        project_id=project_id,
        op_id=op_id,
        spec_doc_id=body.spec_doc_id,
        spec_revision_id=spec_doc.get("revision_id", ""),
        envelope_dict=envelope_dict,
        approved_actions=raw_actions,
        created_by=issuer,
    )
    return record


@app.post(
    "/api/projects/{project_id}/operations/{op_id}/envelopes/{envelope_id}/revoke"
)
def operation_revoke(
    project_id: str,
    op_id: str,
    envelope_id: str,
    body: OperationRevokeRequest,
    user: dict = Depends(require_auth),
):
    """Mark an envelope as revoked. Idempotent.

    ``op_id`` in the URL is verified against the stored record so a typo'd
    URL can't revoke an envelope belonging to a different operation.
    """
    _require_project_role(project_id, user, allowed=("owner", "editor"))
    existing = db.get_operation_envelope(project_id, envelope_id)
    if not existing:
        raise HTTPException(
            status_code=404, detail=f"envelope not found: {envelope_id}"
        )
    if existing.get("op_id") != op_id:
        raise HTTPException(
            status_code=400,
            detail=(f"envelope {envelope_id} belongs to operation "
                    f"{existing.get('op_id')!r}, not {op_id!r}"),
        )
    revoked_by = user.get("email") or user.get("sub") or "dev"
    record = db.revoke_operation_envelope(
        project_id, envelope_id, revoked_by, body.reason or "manual revoke"
    )
    return record


@app.get("/api/projects/{project_id}/operations/{op_id}/envelopes")
def operation_envelopes_list(
    project_id: str,
    op_id: str,
    status: Optional[str] = Query(None, description="active | revoked"),
    user: dict = Depends(require_auth),
):
    """List envelopes for an operation, newest first.

    ``status`` filter is optional. Read-only members can list (audit visibility).
    """
    _load(project_id, user)  # membership check (any role)
    if status and status not in ("active", "revoked"):
        raise HTTPException(
            status_code=400, detail="status must be 'active' or 'revoked'"
        )
    return db.list_operation_envelopes(project_id, op_id=op_id, status=status)


# ---------------------------------------------------------------------------
# Log (commit recording)
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/log")
def log_commit(project_id: str, body: LogCommit,
               user: dict = Depends(require_auth)):
    # ms-78 / e-1909 — stamp meta.author with the human identity of the
    # signed-in committer (= what the Web UI renders in commit lists).
    author = _resolve_author(user)

    def op(data: dict):
        _require_write(data, user)
        try:
            result = core.log_commit(
                data, ms_id=body.ms_id, commit_hash=body.hash,
                message=body.message, date=body.date,
                summary=body.summary, progress=body.progress,
                author=author,
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


@app.post("/api/projects/{project_id}/documents/images")
async def upload_document_image(project_id: str,
                                file: UploadFile = File(...),
                                user: dict = Depends(require_auth)):
    """ms-43: SPEC / memo / retro 本文に貼る画像を 1 枚アップロードする。

    multipart/form-data の ``file`` フィールドにバイナリを乗せて POST する。
    認可は project の write 権限と等価 (= 本文を書ける人なら画像も貼れる)。
    レスポンスは ``{url, markdown}``: ``markdown`` をそのまま doc 本文に
    貼り付けると ``![filename](url)`` として render される。

    保存先と仕様の詳細は ``server/doc_images.py`` 参照 (= GCS bucket、UUID
    key、public read、画像 MIME のみ、10 MiB 上限)。
    """
    data = _load(project_id, user)
    _require_write(data, user)

    contents = await file.read()
    try:
        import doc_images
        result = doc_images.upload_image(
            project_id=project_id,
            filename=file.filename or "image",
            data=contents,
            declared_content_type=file.content_type,
        )
    except ValueError as e:
        # 不正な MIME / サイズ超過 / 空 data 等、client 側に責任がある類。
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # GCS 接続不能 / bucket 不在 等の server 側障害。
        logger.error("doc image upload failed: %s", e)
        raise HTTPException(status_code=500, detail="image upload failed")

    return {
        "url": result.url,
        "markdown": result.markdown,
        "size": result.size,
        "content_type": result.content_type,
    }


# ---------------------------------------------------------------------------
# Active claims (ms-55 e-1730)
# ---------------------------------------------------------------------------
#
# Project-wide mirror of lib/claims.py's local active_claims.json store.
# The CLI in cloud-mode round-trips through these endpoints so:
#   * `beacon claim list` returns the multi-machine union (= Mac + Win
#     view the same set), not just one machine's local cache
#   * `beacon claim post/handoff/request` is idempotent across sessions
#   * the Web UI can render Active Claims without scanning the bus
# Schema is opaque to the server — the client owns the wire shape.

@app.get("/api/projects/{project_id}/active_claims")
def list_active_claims_endpoint(project_id: str,
                                user: dict = Depends(require_auth)):
    """List all active claims on a project, sorted by issued_at."""
    _load(project_id, user)  # access check
    return db.list_active_claims(project_id)


@app.get("/api/projects/{project_id}/active_claims/{claim_id}")
def get_active_claim_endpoint(project_id: str, claim_id: str,
                              user: dict = Depends(require_auth)):
    _load(project_id, user)
    claim = db.get_active_claim(project_id, claim_id)
    if claim is None:
        raise HTTPException(
            status_code=404, detail=f"Claim '{claim_id}' not found",
        )
    return claim


@app.post("/api/projects/{project_id}/active_claims/{claim_id}")
def save_active_claim_endpoint(project_id: str, claim_id: str,
                               body: ActiveClaimSave,
                               user: dict = Depends(require_auth)):
    """Upsert a claim. Idempotent — same claim_id overwrites."""
    data = _load(project_id, user)
    _require_write(data, user)
    db.save_active_claim(project_id, claim_id, body.payload)
    return {"claim_id": claim_id, "status": "saved"}


@app.delete("/api/projects/{project_id}/active_claims/{claim_id}")
def delete_active_claim_endpoint(project_id: str, claim_id: str,
                                 user: dict = Depends(require_auth)):
    """Release a claim from the project-wide store. Idempotent."""
    data = _load(project_id, user)
    _require_write(data, user)
    deleted = db.delete_active_claim(project_id, claim_id)
    return {"claim_id": claim_id, "deleted": deleted}


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
# Related treks (ms-69 / e-1663) — reverse lookup from a project work item
# (milestone / operation / task) to the treks that include it in scope.
#
# Used by the e-1664 Related Treks widget on the project detail page.
# Archived treks are included by default so the widget can render historic
# associations ("we worked on this together in trek X, archived 2 weeks ago").
# ---------------------------------------------------------------------------

def _list_related_treks(project_id: str, *, milestone: str = "",
                        operation: str = "", task: str = "",
                        user: dict | None) -> list:
    """Return treks visible to ``user`` whose scope matches this work item.

    Match rule: an entry counts if it is in the same project AND either
    (a) narrows to the exact ref, or (b) has no narrowing key (= covers
    the whole project, so the item is implicitly in scope).
    """
    actor = user.get("sub") if (_auth_enabled and user) else None
    candidates = db.list_treks(
        actor_id=actor,
        include_archived=True,  # widget renders historic associations too
    )
    out = []
    for t in candidates:
        for entry in t.get("scope") or []:
            if entry.get("project") != project_id:
                continue
            has_narrow = bool(
                entry.get("milestone")
                or entry.get("operation")
                or entry.get("task")
            )
            if not has_narrow:
                out.append(t)
                break
            if milestone and entry.get("milestone") == milestone:
                out.append(t)
                break
            if operation and entry.get("operation") == operation:
                out.append(t)
                break
            if task and entry.get("task") == task:
                out.append(t)
                break
    return out


@app.get("/api/projects/{project_id}/milestones/{ms_id}/related-treks")
def related_treks_for_milestone(project_id: str, ms_id: str,
                                user: dict = Depends(require_auth)):
    """List treks visible to the caller whose scope covers this milestone.

    Includes archived treks (= the widget renders history). Returns the
    full trek doc per match; the Web UI picks status / title / archived_at
    for the badge rendering.
    """
    _load(project_id, user)  # project-side access check
    return _list_related_treks(project_id, milestone=ms_id, user=user)


@app.get("/api/projects/{project_id}/operations/{op_id}/related-treks")
def related_treks_for_operation(project_id: str, op_id: str,
                                user: dict = Depends(require_auth)):
    """List treks whose scope covers this operation (e-1663 / e-1664)."""
    _load(project_id, user)
    return _list_related_treks(project_id, operation=op_id, user=user)


@app.get("/api/projects/{project_id}/entries/{entry_id}/related-treks")
def related_treks_for_entry(project_id: str, entry_id: str,
                            user: dict = Depends(require_auth)):
    """List treks whose scope covers this task entry (e-1663 / e-1664)."""
    _load(project_id, user)
    return _list_related_treks(project_id, task=entry_id, user=user)


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
    """List project members.

    ms-78 e-1807: enriches each row with the user's `display_name` so the
    UI / CLI can prefer a human-friendly label over the raw email. The field
    is empty when the user hasn't set one yet — the UI should fall back to
    email in that case.
    """
    data = _load(project_id, user)
    owner_id = data.get("owner", "")
    owner_email = ""
    owner_display_name = ""
    if owner_id:
        owner_data = db.get_user(owner_id)
        if owner_data:
            owner_email = owner_data.get("email", "")
            owner_display_name = owner_data.get("display_name", "")
    members = data.get("members", []) or []
    enriched = []
    for m in members:
        if not isinstance(m, dict):
            continue
        m2 = dict(m)
        uid = m.get("user_id", "")
        if uid:
            udata = db.get_user(uid)
            if udata:
                m2["display_name"] = udata.get("display_name", "") or m2.get(
                    "display_name", ""
                )
        enriched.append(m2)
    return {
        "owner": owner_id,
        "owner_email": owner_email,
        "owner_display_name": owner_display_name,
        "members": enriched,
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
# Member invitations (ms-78 e-1803/e-1804)
# ---------------------------------------------------------------------------
#
# Token-based invite flow (replaces the legacy "must already have a Beacon
# account" path in invite_member above):
#
#   1. Owner POSTs /api/projects/{pid}/invitations with email + role.
#      Server issues a random token, stores SHA256 hash, returns the
#      plaintext + the share URL (/join/<token>) ONCE.
#   2. Invitee opens /join/<token>. The landing page calls
#      GET /api/invitations/{token} (no auth) to preview project name /
#      role / inviter, then prompts Google login.
#   3. After login the landing page calls POST /api/invitations/{token}/accept
#      with the invitee's display name. Server atomically consumes the
#      invitation and adds them to members[].
#
# All writes go through `apply_operation` so the Firestore vs DynamoDB
# split is invisible to this layer.

class InvitationCreate(BaseModel):
    email: str
    role: str = "viewer"  # viewer | editor
    expiry_days: int = invitations_mod.DEFAULT_EXPIRY_DAYS


class InvitationAccept(BaseModel):
    display_name: str = ""  # ms-78 e-1807: required-but-allow-server-default


def _invite_url(token: str) -> str:
    """Build the public landing URL for a token. Honours BEACON_PUBLIC_BASE_URL
    so local dev / staging / prod all produce a clickable link."""
    base = os.environ.get(
        "BEACON_PUBLIC_BASE_URL", "https://beacon-ai.dev"
    ).rstrip("/")
    return f"{base}/join/{token}"


@app.post("/api/projects/{project_id}/invitations")
def create_invitation(project_id: str, body: InvitationCreate,
                      user: dict = Depends(require_auth)):
    """Owner issues a fresh invite token. Returns the plaintext token + URL ONCE.

    The plaintext is *never* returned again — if the inviter loses it they
    must cancel + re-issue. The DB only ever sees the SHA256 hash.
    """
    issued: dict = {}

    def op(data: dict):
        if _auth_enabled and data.get("owner") != user.get("sub"):
            raise HTTPException(
                status_code=403,
                detail="Only project owner can issue invitations",
            )
        try:
            invitation, token = invitations_mod.invitation_create(
                data,
                email=body.email,
                role=body.role,
                invited_by_user_id=user.get("sub", ""),
                invited_by_email=user.get("email", ""),
                expiry_days=body.expiry_days,
                project_id=project_id,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Stash for outside the txn — the closure may be retried, only the
        # last successful run's values matter.
        issued["invitation"] = invitation
        issued["token"] = token
        return data, None

    operations.apply_operation(
        project_id, op,
        op_name="invitation.create", actor=user.get("sub", ""),
    )
    invitation = issued.get("invitation") or {}
    token = issued.get("token") or ""
    return {
        "invitation": invitations_mod.invitation_public_view(invitation),
        "token": token,                        # plaintext, returned ONCE
        "url": _invite_url(token),
        "expires_at": invitation.get("expires_at", ""),
        "note": (
            "Beacon project member への招待です。GitHub repo collaborator は別途 "
            "GitHub 側で `gh repo edit --add-collaborator <user>` 等で設定してください。"
        ),
    }


@app.get("/api/projects/{project_id}/invitations")
def list_invitations(project_id: str, user: dict = Depends(require_auth)):
    """List active (= unexpired) invitations for a project. Owner-only."""
    data = _load(project_id, user)
    if _auth_enabled and data.get("owner") != user.get("sub"):
        raise HTTPException(
            status_code=403,
            detail="Only project owner can view invitations",
        )
    return {
        "invitations": [
            invitations_mod.invitation_public_view(inv)
            for inv in invitations_mod.invitations_list(data)
            if not invitations_mod._is_expired(inv.get("expires_at", ""))
        ],
    }


@app.delete("/api/projects/{project_id}/invitations/{invitation_id}")
def cancel_invitation(project_id: str, invitation_id: str,
                      user: dict = Depends(require_auth)):
    """Cancel an outstanding invitation. Owner-only.

    After cancel, the token becomes invalid — even if the invitee still has
    the URL, /accept will return 404.
    """
    def op(data: dict):
        if _auth_enabled and data.get("owner") != user.get("sub"):
            raise HTTPException(
                status_code=403,
                detail="Only project owner can cancel invitations",
            )
        try:
            removed = invitations_mod.invitation_cancel(data, invitation_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return data, {
            "status": "cancelled",
            "invitation": invitations_mod.invitation_public_view(removed),
        }

    return operations.apply_operation(
        project_id, op,
        op_name="invitation.cancel", actor=user.get("sub", ""),
    )


def _resolve_invitation_project(token: str) -> tuple[str, dict, dict]:
    """Resolve (project_id, project_data, invitation_dict) from a plaintext token.

    Tokens carry the project_id as a prefix (= ``<pid>.<random>``) so we can
    look up directly without scanning all projects. Raises 404 on miss.
    """
    pid = invitations_mod.parse_token_project_id(token)
    if not pid:
        raise HTTPException(
            status_code=404,
            detail="Invitation token has no project context. Ask the inviter for a fresh link.",
        )
    data = db.get_project(pid)
    if not data:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or expired. Ask the inviter for a fresh link.",
        )
    inv = invitations_mod.invitation_find_by_token(data, token)
    if not inv:
        raise HTTPException(
            status_code=404,
            detail="Invitation not found or expired. Ask the inviter for a fresh link.",
        )
    return pid, data, inv


@app.get("/api/invitations/{token}")
def preview_invitation(token: str):
    """Preview an invitation by plaintext token. Public endpoint — no auth.

    Returns project name / role / inviter so the landing page can render
    "X invited you to Project Y as Z" before the invitee logs in. Does NOT
    return any secrets and does NOT consume the invitation.

    404 if the token does not match any live invitation (= unknown / expired /
    cancelled / already accepted).
    """
    pid, data, inv = _resolve_invitation_project(token)
    owner_id = data.get("owner") or ""
    owner_email = ""
    if owner_id:
        owner_data = db.get_user(owner_id)
        if owner_data:
            owner_email = owner_data.get("email", "")
    return {
        "project_id": pid,
        "project_name": data.get("name", ""),
        "role": inv.get("role", ""),
        "invited_email": inv.get("email", ""),
        "inviter_email": inv.get("invited_by_email", "") or owner_email,
        "expires_at": inv.get("expires_at", ""),
        "owner_email": owner_email,
    }


@app.post("/api/invitations/{token}/accept")
def accept_invitation(token: str, body: InvitationAccept,
                      user: dict = Depends(require_auth)):
    """Consume an invite token and add the caller to the project's members.

    Authenticated — the caller must already be signed in (= Google login on
    the landing page). The invitee email must match the email the invitation
    was issued to (= prevents passing the URL to a third party).

    `display_name` is recorded on the user record (= ms-78 e-1807, the
    UC11-F5 "no more raw emails in author columns" goal).

    Idempotent on success in the trivial sense: invitation is consumed and
    the member row is added. A second call returns 404 because the token
    no longer exists.
    """
    target_pid = invitations_mod.parse_token_project_id(token)
    if not target_pid:
        raise HTTPException(
            status_code=404,
            detail="Invitation token has no project context. Ask the inviter for a fresh link.",
        )
    caller_id = user.get("sub", "")
    caller_email = (user.get("email") or "").lower()
    display_name = (body.display_name or "").strip()

    accepted: dict = {}

    def op(data: dict):
        try:
            inv = invitations_mod.invitation_consume(data, token)
        except ValueError as e:
            # Race against another consume / cancel attempt
            raise HTTPException(status_code=404, detail=str(e))
        # Email match check — server enforces, role cannot be re-targeted
        invitee_email = (inv.get("email") or "").lower()
        if caller_email and invitee_email and caller_email != invitee_email:
            # Re-insert the invitation so the legitimate invitee can still use it
            data.setdefault("invitations", []).append(inv)
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This invitation was issued to {invitee_email}, but you "
                    f"are signed in as {caller_email}. Sign in with the "
                    "invited account and try again."
                ),
            )
        # Add to project members (= server-side schema, user_id key)
        members = data.setdefault("members", [])
        if not isinstance(members, list):
            members = []
            data["members"] = members
        if data.get("owner") == caller_id:
            # Owner accepting their own invite (= edge case, no-op for membership)
            pass
        elif not any(m.get("user_id") == caller_id for m in members
                     if isinstance(m, dict)):
            members.append({
                "user_id": caller_id,
                "email": caller_email,
                "role": inv.get("role", "viewer"),
                "joined_at": invitations_mod._now_iso(),
                "invited_by": inv.get("invited_by", ""),
            })
        accepted["invitation"] = inv
        return data, None

    operations.apply_operation(
        target_pid, op,
        op_name="invitation.accept", actor=caller_id,
    )

    # Persist display_name on the user record (= UC11-F5 / e-1807).
    # Best-effort — failure here should not block project membership.
    if display_name:
        try:
            db.update_user(caller_id, {"display_name": display_name})
        except Exception:
            pass

    inv = accepted.get("invitation") or {}
    return {
        "status": "accepted",
        "project_id": target_pid,
        "role": inv.get("role", ""),
        "display_name": display_name,
        "next_step_url": f"/?project={target_pid}",
        "note": (
            "Beacon project に追加されました。GitHub repo の collaborator は "
            "別途 GitHub 側で設定が必要です (招待主に依頼してください)。"
        ),
    }


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
# Treks (ms-69 / e-1656) — cross-project / cross-session collaboration area
#
# Top-level resource (not under /api/projects/) because a trek's whole point
# is to bridge projects. Storage lives in `treks/` collection (Firestore) or
# `beacon-{env}-treks` table (DynamoDB), routed through store_router.
#
# Membership is at user grain (= user_id + email pair), so a single user
# with multiple sessions counts as one member. Leader is at session grain
# (= `leader_session_id` on the trek doc).
#
# Authorization model:
#   - read  (list / get / summary)        : creator OR any member
#   - write (invite / scope / halt set+clear) : joined member
#   - leader-only (update / archive / start / transfer) : member with
#     role="leader" (user grain). Transfer additionally requires the caller's
#     session to equal trek.leader_session_id (session grain).
#   - join                                : caller must already appear in
#     members[] (= invited but not yet joined). Non-invited callers get 403.
# ---------------------------------------------------------------------------

class TrekCreate(BaseModel):
    title: str
    description: str = ""
    type: str = "persistent"  # temporary | persistent
    creator_session_id: str   # caller's session_id (becomes leader)
    # ms-83 / e-1994: optional cadence + future-form manager URL at creation
    # time. Both are recorded on ``meta``; cadence falls back to default
    # (= 10 minutes) when omitted, manager_agent_url is unused in this MS.
    cadence_minutes: Optional[int] = None
    manager_agent_url: Optional[str] = None


class TrekUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = None
    # ms-83 / e-1994 — periodic cadence (= server-side "next, please" DM
    # interval in minutes) and a future-form manager-agent URL slot.
    # Both live on ``meta`` so the on-disk shape stays tidy. ``None``
    # leaves the field unchanged; explicit empty string / explicit 0
    # behaviours are handled in the setter functions.
    cadence_minutes: Optional[int] = None
    manager_agent_url: Optional[str] = None


class TrekInvite(BaseModel):
    email: str


class TrekScopeOp(BaseModel):
    project: str
    milestone: Optional[str] = None
    operation: Optional[str] = None
    task: Optional[str] = None


class TrekHaltSet(BaseModel):
    issued_by_session_id: str
    reason: str = ""


class TrekTransferLeader(BaseModel):
    from_session_id: str  # current leader session (caller's session)
    to_session_id: str    # new leader session


def _load_trek_for_read(trek_id: str, user: dict) -> dict:
    """Load a trek doc. 404 if missing, 403 if caller is neither creator
    nor a member (per SPEC visibility = creator OR members)."""
    t = db.get_trek(trek_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"Trek '{trek_id}' not found")
    if not _auth_enabled:
        return t
    uid = user.get("sub", "")
    creator_uid = (t.get("creator_actor") or {}).get("user_id", "")
    if creator_uid == uid:
        return t
    for m in t.get("members") or []:
        if m.get("user_id") == uid:
            return t
    raise HTTPException(status_code=403, detail="Not a member of this trek")


def _trek_member_role(t: dict, user_id: str) -> str:
    """Return the caller's role ('leader' / 'member' / '' if not a member)."""
    for m in t.get("members") or []:
        if m.get("user_id") == user_id:
            return m.get("role", "member")
    return ""


def _require_trek_leader(t: dict, user: dict) -> None:
    """Raise 403 if caller does not hold the leader role on this trek."""
    if not _auth_enabled:
        return
    if _trek_member_role(t, user.get("sub", "")) != "leader":
        raise HTTPException(status_code=403, detail="Trek leader role required")


def _require_trek_joined_member(t: dict, user: dict) -> None:
    """Raise 403 if caller is not a joined member (= invited but not joined
    is insufficient for write ops; mirrors SPEC 設計方針 12 join-flow)."""
    if not _auth_enabled:
        return
    uid = user.get("sub", "")
    for m in t.get("members") or []:
        if m.get("user_id") == uid and m.get("joined_at"):
            return
    raise HTTPException(status_code=403, detail="Only joined members can perform this action")


@app.get("/api/treks")
def list_treks_endpoint(
    status: Optional[str] = None,
    include_archived: bool = False,
    all_actors: bool = False,
    user: dict = Depends(require_auth),
):
    """List treks visible to the caller.

    Default: treks where caller is creator OR member.
    ``?all_actors=true`` returns every trek (admin only; non-admin sees 403).
    ``?status=`` narrows to a specific lifecycle state.
    ``?include_archived=true`` includes archived treks (default hides them).
    """
    if all_actors:
        _require_admin(user)
        actor_filter = None
    else:
        actor_filter = user.get("sub") if _auth_enabled else None
    return db.list_treks(
        actor_id=actor_filter,
        status=status,
        include_archived=include_archived,
    )


@app.post("/api/treks")
def create_trek_endpoint(body: TrekCreate, user: dict = Depends(require_auth)):
    """Create a new trek. Caller becomes the creator + initial leader member.

    ``creator_session_id`` is recorded as ``leader_session_id`` (SPEC 設計方針 9).
    """
    if not body.creator_session_id:
        raise HTTPException(status_code=400, detail="creator_session_id required")
    try:
        new_doc = trek_mod.new_trek(
            title=body.title,
            creator_user_id=user.get("sub", ""),
            creator_email=user.get("email", ""),
            creator_session_id=body.creator_session_id,
            description=body.description,
            type_=body.type,
            cadence_minutes=body.cadence_minutes,
            manager_agent_url=body.manager_agent_url or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(new_doc["trek_id"], new_doc)
    return new_doc


@app.get("/api/treks/{trek_id}")
def get_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Get a single trek by id. Caller must be creator or member."""
    return _load_trek_for_read(trek_id, user)


@app.patch("/api/treks/{trek_id}")
def update_trek_endpoint(trek_id: str, body: TrekUpdate,
                         user: dict = Depends(require_auth)):
    """Update title / description / type. Leader-only.

    Status / members / scope / halt are mutated through dedicated endpoints
    so audit logs and authz rules stay sharp per intent.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader(t, user)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        t["title"] = title
    if body.description is not None:
        t["description"] = body.description
    if body.type is not None:
        try:
            trek_mod.validate_type(body.type)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        t["type"] = body.type
    # ms-83 / e-1994: cadence_minutes / manager_agent_url live on ``meta``;
    # use the dedicated setters so validation + idempotency rules stay in
    # one place (= lib/trek.py). ``cadence_minutes is None`` here means
    # "field not supplied in this PATCH"; explicit clear uses a future
    # dedicated endpoint or 0 sentinel — kept out of this MS scope.
    if body.cadence_minutes is not None:
        try:
            trek_mod.set_cadence_minutes(
                t, cadence_minutes=body.cadence_minutes
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if body.manager_agent_url is not None:
        trek_mod.set_manager_agent_url(
            t, manager_agent_url=body.manager_agent_url
        )
    t["updated_at"] = trek_mod.utcnow_iso()
    db.save_trek(trek_id, t)
    return t


@app.delete("/api/treks/{trek_id}")
def archive_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Archive a trek (status → archived). Leader-only. Archive is terminal."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader(t, user)
    cur = t.get("status", "")
    try:
        trek_mod.validate_transition(cur, "archived")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    now = trek_mod.utcnow_iso()
    t["status"] = "archived"
    t["archived_at"] = now
    t["updated_at"] = now
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/start")
def start_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Transition trek planning → active. Leader-only."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader(t, user)
    cur = t.get("status", "")
    try:
        trek_mod.validate_transition(cur, "active")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    t["status"] = "active"
    t["updated_at"] = trek_mod.utcnow_iso()
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/members")
def invite_trek_member_endpoint(trek_id: str, body: TrekInvite,
                                user: dict = Depends(require_auth)):
    """Invite a user to the trek by email. Any joined member can invite.

    Invitee must already exist as a Beacon user (= signed in once). The
    invitation appears in members[] with ``joined_at=""`` until they call
    POST /api/treks/{id}/members/join.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    found = db.find_user_by_email(body.email)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"User '{body.email}' not found. They must sign in to Beacon first.",
        )
    invited_id, _ = found
    try:
        trek_mod.add_invitation(
            t,
            user_id=invited_id,
            email=body.email,
            invited_by_user_id=user.get("sub", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/members/join")
def join_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Accept the caller's own invitation (= sets ``joined_at`` to now).

    Caller must already appear in members[] (= owner-issued invitation).
    Non-invited callers get 403 — there is no self-add path by design.

    We bypass _load_trek_for_read's membership check here because the trek
    visibility model is "creator OR member" and an invited-but-not-yet-joined
    user IS a member entry; the visibility check passes. Self-add (= caller
    not yet in members[] at all) gets caught by trek_mod.accept_invitation.
    """
    t = _load_trek_for_read(trek_id, user)
    try:
        trek_mod.accept_invitation(t, user_id=user.get("sub", ""))
    except ValueError as e:
        # Not invited (no row at all) → 403, not 404. The trek exists; the
        # caller just cannot self-add.
        raise HTTPException(status_code=403, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.delete("/api/treks/{trek_id}/members/me")
def leave_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Caller removes themselves from the trek.

    The leader must transfer leadership first (`POST .../transfer-leader`),
    and the last member cannot leave (= archive the trek instead).
    """
    t = _load_trek_for_read(trek_id, user)
    try:
        trek_mod.remove_member(t, user_id=user.get("sub", ""))
    except ValueError as e:
        # leader-still-leader / not-a-member / last-member → 400
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.put("/api/treks/{trek_id}/scope")
def add_trek_scope_endpoint(trek_id: str, body: TrekScopeOp,
                            user: dict = Depends(require_auth)):
    """Append a scope entry (cross-project ref). Any joined member."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    entry: dict = {"project": body.project}
    if body.milestone:
        entry["milestone"] = body.milestone
    if body.operation:
        entry["operation"] = body.operation
    if body.task:
        entry["task"] = body.task
    try:
        trek_mod.add_scope_entry(t, entry=entry)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.delete("/api/treks/{trek_id}/scope")
def remove_trek_scope_endpoint(trek_id: str, body: TrekScopeOp,
                               user: dict = Depends(require_auth)):
    """Remove a scope entry. Any joined member."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    entry: dict = {"project": body.project}
    if body.milestone:
        entry["milestone"] = body.milestone
    if body.operation:
        entry["operation"] = body.operation
    if body.task:
        entry["task"] = body.task
    try:
        trek_mod.remove_scope_entry(t, entry=entry)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.put("/api/treks/{trek_id}/halt")
def set_trek_halt_endpoint(trek_id: str, body: TrekHaltSet,
                           user: dict = Depends(require_auth)):
    """Pull the Andon cord. Any joined member may halt an active trek.

    Halt is metadata, not a status: trek stays ``active`` while halted.
    Sessions observe the halt field and pause autonomous work. Resume by
    DELETE on this same path.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    if not body.issued_by_session_id:
        raise HTTPException(status_code=400, detail="issued_by_session_id required")
    try:
        trek_mod.set_halt(
            t,
            issued_by_session_id=body.issued_by_session_id,
            reason=body.reason or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.delete("/api/treks/{trek_id}/halt")
def clear_trek_halt_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Release the Andon cord. Any joined member."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    trek_mod.clear_halt(t)
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/transfer-leader")
def transfer_trek_leader_endpoint(trek_id: str, body: TrekTransferLeader,
                                  user: dict = Depends(require_auth)):
    """Hand off leadership to another session.

    Two-factor check (session AND user grain):
      * ``from_session_id`` must equal the current ``leader_session_id`` —
        confirms the caller is the live leader session.
      * The calling user must hold the ``leader`` role in members[] —
        confirms identity at the user grain (= survives session restart).
    """
    t = _load_trek_for_read(trek_id, user)
    if not body.from_session_id or not body.to_session_id:
        raise HTTPException(status_code=400,
                            detail="from_session_id and to_session_id required")
    if t.get("leader_session_id") != body.from_session_id:
        raise HTTPException(
            status_code=403,
            detail="from_session_id does not match current trek leader",
        )
    _require_trek_leader(t, user)
    try:
        trek_mod.transfer_leader(t, target_session_id=body.to_session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return t


@app.get("/api/treks/{trek_id}/documents")
def list_trek_documents_endpoint(trek_id: str,
                                 user: dict = Depends(require_auth)):
    """List documents associated with this trek (= ``trek_id`` field set).

    Iterates the trek's scope to collect candidate projects, then lists
    documents for each and filters by ``trek_id``. Returns the docs the
    caller can see (= the caller is already a trek member, so they have
    visibility into any project that the trek's scope includes).
    """
    t = _load_trek_for_read(trek_id, user)
    out: list = []
    seen_doc_ids: set[str] = set()
    project_ids = {
        s.get("project") for s in t.get("scope") or [] if s.get("project")
    }
    for pid in project_ids:
        try:
            project_docs = db.list_documents(pid)
        except Exception:
            # Stale scope entry pointing at a project the caller cannot
            # read; skip silently so a single bad ref doesn't break the
            # whole listing.
            continue
        for d in project_docs:
            if d.get("trek_id") != trek_id:
                continue
            doc_id = d.get("doc_id")
            if doc_id in seen_doc_ids:
                continue
            seen_doc_ids.add(doc_id)
            # Surface the source project_id so the UI can deep-link the doc.
            d_out = dict(d)
            d_out["project_id"] = pid
            out.append(d_out)
    return out


@app.get("/api/treks/{trek_id}/summary")
def trek_summary_endpoint(trek_id: str, user: dict = Depends(require_auth)):
    """Compact status snapshot for dashboards / Web UI Treks tab.

    Returns the high-level counts + status fields without exposing the full
    members[] / scope[] arrays — a separate GET /api/treks/{id} fetches the
    full doc when the caller drills in.
    """
    t = _load_trek_for_read(trek_id, user)
    members = t.get("members") or []
    return {
        "trek_id": t.get("trek_id"),
        "title": t.get("title"),
        "type": t.get("type"),
        "status": t.get("status"),
        "halted": bool(t.get("halt")),
        "halt": t.get("halt"),
        "leader_session_id": t.get("leader_session_id"),
        "creator_actor": t.get("creator_actor"),
        "member_count": len(members),
        "joined_member_count": sum(1 for m in members if m.get("joined_at")),
        "scope_count": len(t.get("scope") or []),
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "archived_at": t.get("archived_at"),
    }


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

    ms-54 / e-1349: the authenticated user's email is stamped onto the
    session's ``actor.email`` field regardless of whether the body included
    actor. Email lives only on the server (bearer token is its property),
    so the bridge cannot fabricate or spoof another user's identity. The
    directory query then surfaces ``actor.email`` so the DM-send Skill
    picker can show a member-level identity ("alice@…") rather than just
    ``machine/agent``. See firestore_client.stamp_session_actor_email for
    the field-path merge that preserves actor.machine/agent in the
    heartbeat (no-actor) path.
    """
    _load(project_id, user)
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    email = user.get("email", "")

    # Mint path: body carried actor. Stamp email in-line so the single
    # db.upsert_session write below lands the complete view atomically.
    if email and isinstance(payload.get("actor"), dict):
        payload["actor"] = {**payload["actor"], "email": email}

    if not payload and not email:
        # Nothing to write — surface as a no-op rather than a 422, so callers
        # debouncing client-side don't need to special-case empty bodies.
        return {"status": "noop"}
    if payload:
        db.upsert_session(project_id, session_id, payload)
    # Heartbeat path: body had no actor. Stamp the authenticated email via
    # the field-path merge helper so existing actor.machine/agent (from a
    # prior mint) are not stomped. Idempotent — repeat heartbeats just
    # re-write the same email leaf.
    if email and not isinstance(payload.get("actor"), dict):
        db.stamp_session_actor_email(project_id, session_id, email)
    return {"status": "ok", "session_id": session_id}


_POLL_HEALTH_MIN_WINDOW_S = 30
_POLL_HEALTH_DEFAULT_INTERVAL_MS = 2000
_POLL_HEALTH_INTERVAL_MULTIPLIER = 2


def _compute_poll_health(session: dict, now_dt) -> dict:
    """Compute the ``poll_health`` block for a session row (e-1318).

    Formula (server-side, no client clock involved):

      threshold_seconds = max(
          _POLL_HEALTH_MIN_WINDOW_S,                       # floor: 30 s
          _POLL_HEALTH_INTERVAL_MULTIPLIER * poll_interval # 2× bridge cadence
      )
      healthy = (
          last_poll_at exists
          AND shutdown != true
          AND server_now - last_poll_at <= threshold_seconds
      )

    Returns a dict ALWAYS — the directory consumer (CLI / Skill) reads the
    field unconditionally. Sessions that have never been touched by the
    poll-gated bridge get ``healthy=None`` (unknown) so the caller can
    fall back to ``last_active`` rather than wrongly marking them dead.

    The shutdown short-circuit deliberately classifies a graceful teardown
    as not-healthy *immediately* — without it, the directory would
    advertise a session as receive-capable for ~30 s after a clean Ctrl-C.
    """
    import datetime
    last_poll = session.get("last_poll_at", "")
    interval_ms = session.get("poll_interval_ms") or _POLL_HEALTH_DEFAULT_INTERVAL_MS
    shutdown = bool(session.get("shutdown", False))

    if not last_poll:
        return {
            "last_poll_at": "",
            "poll_interval_ms": None,
            "shutdown": False,
            "healthy": None,
            "age_seconds": None,
        }

    threshold = max(
        _POLL_HEALTH_MIN_WINDOW_S,
        (_POLL_HEALTH_INTERVAL_MULTIPLIER * int(interval_ms)) // 1000,
    )

    age_seconds: Optional[float] = None
    healthy = False
    try:
        # Accept both microsecond and millisecond ISO8601, tolerating the
        # trailing Z. fromisoformat in 3.11+ handles Z natively; replace to
        # stay portable across the deployment fleet.
        parsed = datetime.datetime.fromisoformat(last_poll.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        age_seconds = (now_dt - parsed).total_seconds()
        healthy = (not shutdown) and (age_seconds <= threshold)
    except (ValueError, TypeError):
        # Malformed last_poll_at — treat as unknown rather than dead. A
        # corrupted stamp on one session must not silently delete it from
        # the picker; surface it with healthy=null so debug tooling can
        # see "there's a row here, but its liveness signal is broken".
        return {
            "last_poll_at": last_poll,
            "poll_interval_ms": int(interval_ms),
            "shutdown": shutdown,
            "healthy": None,
            "age_seconds": None,
        }

    return {
        "last_poll_at": last_poll,
        "poll_interval_ms": int(interval_ms),
        "shutdown": shutdown,
        "healthy": healthy,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
    }


@app.get("/api/projects/{project_id}/sessions")
def list_sessions(
    project_id: str,
    user_id: str = "",
    machine: str = "",
    agent: str = "",
    live_only: bool = False,
    since_minutes: int = 5,
    healthy_only: bool = False,
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
                            NOTE: ``last_active`` proves only "some heartbeat
                            code path ran"; for "this bridge can actually
                            receive DMs right now" use ``healthy_only``.
      * ``since_minutes`` — threshold for live_only. Default 5 matches the
                            session heartbeat cadence; raise it for "active in
                            last hour" style queries.
      * ``healthy_only``  — e-1318 Option C true-heartbeat. Drop sessions
                            whose bridge poll loop is stale or shutdown. The
                            stale window is ``max(30s, 2× poll_interval_ms)``,
                            so the filter scales with the bridge's own
                            cadence. Sessions without ``last_poll_at`` (never
                            polled — likely an older bridge or no bridge at
                            all) are also dropped under ``healthy_only`` —
                            unknown-liveness is *not* a healthy receiver.

    Every returned row carries a ``poll_health`` block (e-1318) regardless
    of filter choice, so the CLI / Skill consumer can display age & shutdown
    flag in the picker without an extra round-trip.

    Filtering is in-memory after load. The sessions/ subcollection is bounded
    (single-digit to a few dozen docs per project in practice), so we avoid
    Firestore composite-index requirements for what is fundamentally an
    interactive picker query.
    """
    _load(project_id, user)
    sessions = db.list_sessions(project_id)

    import datetime
    now_dt = datetime.datetime.now(datetime.timezone.utc)

    # Always attach poll_health — backward-compat callers that ignore it lose
    # nothing, but /beacon-dm-send (and any other directory consumer) gets
    # the structured signal in one round-trip.
    #
    # Also stamp ``bridge: True/False`` (ms-54 e-1319): True iff a bridge
    # poll loop has ever written ``last_poll_at`` on this session. This is
    # the structural marker for "has a receive channel at all", distinct
    # from poll_health.healthy which factors in age + shutdown. Callers
    # that only want "would a DM have anywhere to land" (e.g. directory
    # default view) can filter on this without re-implementing the
    # last_poll_at presence check.
    for s in sessions:
        s["poll_health"] = _compute_poll_health(s, now_dt)
        s["bridge"] = bool(s.get("last_poll_at"))

    if not (user_id or machine or agent or live_only or healthy_only):
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
        cutoff = now_dt - datetime.timedelta(minutes=since_minutes)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        def _is_live(s: dict) -> bool:
            la = s.get("last_active", "")
            # Compare ISO8601 strings lexicographically — same wire format on
            # both sides (server-stamped UTC microseconds).
            return bool(la) and la >= cutoff_iso

        filtered = [s for s in filtered if _is_live(s)]

    if healthy_only:
        # Only sessions whose bridge poll loop has stamped a recent
        # last_poll_at AND is not in graceful shutdown. ``healthy=None``
        # (unknown — older bridge, no last_poll_at field) is treated as
        # NOT healthy: the contract is "I am polling right now", and
        # silence does not satisfy that contract.
        filtered = [s for s in filtered if s.get("poll_health", {}).get("healthy") is True]

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

def _resolve_bus_event_user_ids(
    project_id: str,
    sender_session_id: str,
    payload: dict | None,
) -> tuple[str, str]:
    """Resolve (sender_user_id, receiver_user_id) for a bus envelope.

    Both ids are looked up from the project's session registry
    (= projects/{project_id}/sessions/{session_id}.user_id, written by
    upsert_session / mint_session paths). Missing rows return empty
    string for that side; the caller (``dm_gate.should_gate_dm_action``)
    treats empty-string sender / receiver as "unknown" and falls through
    to the standard rule set (= same_user skip is impossible when sender
    is blank, but the no_actions / shared_trek rules still apply).

    Used only by the post_bus_event gate (ms-70 / e-1713). Kept off the
    hot path of normal lookups — one ``list_sessions`` call per bus
    write is acceptable at current dogfood traffic; a directory-style
    point lookup can replace it if/when scale demands.
    """
    if not sender_session_id and not (isinstance(payload, dict) and payload.get("recipient_session_id")):
        return ("", "")
    recipient_sid = ""
    if isinstance(payload, dict):
        recipient_sid = str(payload.get("recipient_session_id") or "")
    sender_uid = ""
    receiver_uid = ""
    try:
        sessions = db.list_sessions(project_id)
    except Exception:
        # Backend unavailable / table missing in a fresh project: treat
        # both ids as unknown. The gate's no_actions / shared_trek rules
        # still cover the safe defaults.
        return ("", "")
    for s in sessions:
        sid = s.get("session_id") or ""
        if sender_session_id and sid == sender_session_id:
            sender_uid = str(s.get("user_id") or "")
        if recipient_sid and sid == recipient_sid:
            receiver_uid = str(s.get("user_id") or "")
        if (not sender_session_id or sender_uid) and (
            not recipient_sid or receiver_uid
        ):
            break
    return (sender_uid, receiver_uid)


@app.post("/api/projects/{project_id}/bus")
async def post_bus_event(
    project_id: str,
    body: BusEventCreate,
    user: dict = Depends(require_auth),
):
    """Append a bus event. Server stamps ``created_at`` so all clients agree
    on the wall-clock ordering (clients' local clocks would diverge across
    machines, defeating the cursor semantics).

    e-1155 Phase 1: every receive now goes through the envelope verify
    pipeline (9 steps from CORE doc 1UGomhHqCQo0iYSRtCdB). Outcomes:

      * verify pass → original tier permission applies, delivery may stay
        at auto-execute if the tier supports it.
      * verify fail → monotonic T5 degrade. If T5 can't carry the payload
        (action requested OR payload not in T5 short-ping shape) the
        post is rejected with 403 *and* an audit record is written.
      * legacy (no envelope) → T5-equivalent; same rejection rule.

    The audit record is written for *every* receive (pass, fail, or
    rejected) so the e-1168 audit log task is structurally satisfied by
    Phase 1.

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
    requested_delivery = (
        body.delivery if body.delivery in _BUS_DELIVERY_MODES
        else _BUS_DELIVERY_DEFAULT
    )

    # e-1155 step 1: envelope verify. The result drives delivery downgrade
    # and audit logging regardless of the legacy/with-envelope path.
    verify_result = envelope_mod.verify(
        body.envelope,
        project_id=project_id,
        payload=body.payload,
        requested_action=body.requested_action,
        nonce_store=_envelope_nonce_store(),
        parent_lookup=_envelope_parent_lookup(),
        sender_session_id=body.sender_session_id,
    )

    # Hard reject when verify populates a rejection_reason: that signal is
    # reserved by the envelope module for "T5 degrade also fails" (action
    # requested OR signed envelope present + payload not in T5 short-ping
    # shape). Soft degrade keeps rejection_reason=None so the bus stays
    # backward-compat for legacy free-text DMs (e-1136 dogfood depends on
    # this distinction).
    rejected = verify_result.rejection_reason is not None

    t5_payload_conforms = envelope_mod.validate_t5_payload(
        body.payload if isinstance(body.payload, dict) else {}
    ) is None
    effective_delivery = envelope_mod.decide_delivery(
        envelope=body.envelope,
        effective_tier=verify_result.effective_tier,
        requested_action=body.requested_action,
        requested_delivery=requested_delivery,
        t5_payload_conforms=t5_payload_conforms,
    )

    audit_record = {
        "received_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        "envelope": _envelope_audit_view(body.envelope),
        "verify": verify_result.to_audit_dict(),
        "requested_action": body.requested_action,
        "requested_delivery": requested_delivery,
        "effective_delivery": effective_delivery if not rejected else None,
        "sender_session_id": body.sender_session_id,
        "channel": body.channel,
        "rejected": rejected,
        "event_id": None,
    }

    if rejected:
        # Audit before raising so the rejection is observable.
        db.append_bus_audit(project_id, audit_record)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "envelope_verify_rejected",
                "reason": verify_result.rejection_reason,
                "steps": verify_result.steps,
            },
        )

    # ms-70 / e-1713: cross-user DM action authorization gate.
    # Resolve sender / receiver user_ids from the project session registry,
    # then ask the pure judge whether this envelope must be held for
    # receiver-side human approval. The gate writes a pending sidecar row
    # *and* downgrades effective_delivery to a non-auto-execute mode so
    # the receiver daemon cannot self-act before the human decides.
    sender_uid, receiver_uid = _resolve_bus_event_user_ids(
        project_id=project_id,
        sender_session_id=body.sender_session_id,
        payload=body.payload,
    )
    env_actions = (body.envelope or {}).get("actions_authorized") or []
    gate_lookup = dm_gate_mod.build_shared_trek_lookup_from_lists(
        # Sender-side trek visibility is sufficient — Trek membership
        # query is symmetric (creator OR members) on either backend.
        lambda uid: db.list_treks(actor_id=uid) if uid else [],
    )
    should_gate, gate_reason = dm_gate_mod.should_gate_dm_action(
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        actions_authorized=env_actions,
        shared_trek_lookup=gate_lookup,
    )
    audit_record["dm_gate"] = {
        "should_gate": should_gate,
        "reason": gate_reason,
        "sender_user_id": sender_uid,
        "receiver_user_id": receiver_uid,
    }
    if should_gate:
        # Force a safe, non-auto-execute delivery so legacy receivers
        # that ignore the sidecar still cannot fire actions. The
        # canonical "needs human consent" mode is propose-to-ai
        # (= surface in the AI context but do not auto-run).
        effective_delivery = "propose-to-ai"
        audit_record["effective_delivery"] = effective_delivery

    data = {
        "channel": body.channel,
        "sender_session_id": body.sender_session_id,
        "payload": body.payload,
        "delivery": effective_delivery,
        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    }
    # Persist the envelope alongside the event so receivers can re-verify
    # (defense in depth) and the audit collection has a back-reference.
    if body.envelope is not None:
        data["envelope"] = body.envelope
    if body.requested_action is not None:
        data["requested_action"] = body.requested_action

    event_id = db.append_bus_event(project_id, data)
    audit_record["event_id"] = event_id
    db.append_bus_audit(project_id, audit_record)

    # Sidecar write must happen *after* append_bus_event so the parent
    # event_id exists. Pending is the only status we record from this
    # path; approved / denied land via the receiver's CLI/Skill, and
    # auto-allow events deliberately leave no sidecar (= legacy read
    # path interprets None as "auto").
    if should_gate:
        try:
            db.put_bus_event_approval(
                project_id,
                event_id,
                approval_status="pending",
                sender_user_id=sender_uid or "",
                receiver_user_id=receiver_uid or "",
            )
        except Exception as _exc:  # pragma: no cover - defensive
            # Sidecar write failure must NOT break the dispatcher; the
            # event is already in bus_events and the audit record
            # captured the gate decision. Receivers reading the sidecar
            # will see None == "auto" and an operator can re-stamp by
            # hand if needed. Log and move on.
            logging.getLogger(__name__).warning(
                "put_bus_event_approval failed for event_id=%s: %s",
                event_id, _exc,
            )

    event = {"event_id": event_id, **data}
    # e-997: push to all WS subscribers of this project. Multi-replica delivery
    # (events posted on another Cloud Run instance) is out of scope here —
    # Firestore on_snapshot or a pub/sub layer would solve it but adds cost;
    # the single-replica path covers UC1/UC2 dogfood.
    if _ws_connections.get(project_id):
        await _broadcast_bus_event(project_id, event)
    return event


def _envelope_audit_view(env: Optional[dict]) -> Optional[dict]:
    """Return a redacted view of the envelope suitable for the audit log.

    Drops the signature (already verified — storing it adds bulk without
    forensic value) and keeps the field set Phase 2 might extend. None
    inputs round-trip to None so legacy receives are explicit in the log.
    """
    if env is None:
        return None
    keep = {"tier", "issuer", "scope", "actions_authorized", "data_class",
            "issued_at", "expires_at", "project_id", "nonce",
            "conversation_id", "in_reply_to", "chain_depth"}
    return {k: env.get(k) for k in keep if k in env}


@app.post("/api/projects/{project_id}/bus/envelope/issue")
def issue_bus_envelope(
    project_id: str,
    body: EnvelopeIssueRequest,
    user: dict = Depends(require_auth),
):
    """Issue a server-signed bus envelope (e-1155 Phase 1).

    The signature is HMAC-SHA256 over the canonical envelope, keyed by the
    server's ``BEACON_ENVELOPE_SECRET``. The Bearer token on this request
    *is* the proof of human authorization for T1 — the user has explicitly
    asked the server to mint an envelope, which is the structural primitive
    behind "T1 = human explicit signature".

    T2 envelopes (Operation scope) are also minted here. A non-empty
    ``scope`` switches the tier semantics; the envelope module enforces
    the tier/scope consistency rule.

    Rejects wildcards / regex in ``actions_authorized`` at the module
    boundary, so callers cannot smuggle a permissive scope by encoding
    fuzzy intent.
    """
    # Membership check: only writers on this project can mint envelopes
    # against it. Read-only members and unauthenticated callers can't
    # synthesize T1/T2 signatures.
    _require_project_role(project_id, user, allowed=("owner", "editor"))
    issuer = user.get("email") or user.get("sub") or "dev"
    try:
        env = envelope_mod.issue_envelope(
            tier=body.tier,
            issuer=issuer,
            project_id=project_id,
            actions_authorized=body.actions_authorized,
            data_class=body.data_class,
            scope=body.scope,
            conversation_id=body.conversation_id,
            in_reply_to=body.in_reply_to,
            chain_depth=body.chain_depth,
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=400,
                            detail=f"envelope issuance rejected: {e}")
    return env


@app.get("/api/projects/{project_id}/bus/audit")
def list_bus_audit(
    project_id: str,
    since: str = "",
    limit: int = 100,
    user: dict = Depends(require_auth),
):
    """List bus envelope audit records for ``project_id`` (e-1155 / e-1168).

    Audit visibility is membership-gated: only project members can read.
    """
    _require_project_role(project_id, user)
    return db.list_bus_audit(project_id, since=since, limit=limit)


@app.get("/api/projects/{project_id}/dm/pending")
def list_pending_dm_actions(
    project_id: str,
    receiver_user_id: str = "",
    limit: int = 100,
    user: dict = Depends(require_auth),
):
    """List pending bus_event_approvals rows (ms-70 / e-1714).

    Used by ``/beacon-session-start`` to surface "保留中の DM action"
    (= cross-user DM bus envelopes that the receiver's terminal was
    closed for when ms-70 / e-1713's dispatcher gate held them).

    The endpoint is membership-gated like other project-scoped reads;
    no extra ACL beyond that because the sidecar's
    ``receiver_user_id`` query gives the caller scoped-to-self filter
    semantics — and read-only members can already see bus events in
    the parent collection.

    ``receiver_user_id`` (optional): restrict to "my pending". Empty
    string returns rows for all receivers in the project (used by web
    UI dashboards / debugging; the Skill always passes a value).
    """
    _load(project_id, user)
    return db.list_pending_approvals(
        project_id,
        receiver_user_id=(receiver_user_id or None),
        limit=limit,
    )


@app.get("/api/projects/{project_id}/dm/approval/history")
def list_dm_approval_history(
    project_id: str,
    limit: int = 50,
    user: dict = Depends(require_auth),
):
    """List **decided** bus_event_approvals rows for ``project_id`` (ms-70 / e-1718).

    Audit-trail read used by the Web UI's "DM 承認履歴" (DM approval history)
    section, which is read-only by design: SPEC 設計方針 3 keeps every
    approve / deny decision inside the terminal Claude Code that received
    the action, so the Web UI surfaces only the *aftermath* — who decided
    what, when — never an approve / deny control.

    Filters out ``pending`` rows specifically so a future contributor cannot
    casually wire approve / deny buttons on top of this endpoint without
    noticing they would break the terminal-only invariant. ``auto`` rows
    are also excluded — they carry no human decision and would drown out
    the interesting human-decided rows in the audit view.

    Membership-gated like the symmetric ``/dm/pending`` endpoint (e-1714):
    project members can read; non-members cannot. ``decision_by`` is
    returned as the raw user_id stamped by the server at decision time;
    rendering "(you)" suffixes etc. is a presentation concern handled in
    the Web UI.
    """
    _load(project_id, user)
    # Defensive cap. Frontend default is 50; allowing a few hundred is fine
    # for human audit scroll, but unbounded would let a curious client slurp
    # every decision in the project.
    capped = max(1, min(int(limit or 50), 500))
    return db.list_decided_approvals(project_id, limit=capped)


# ms-70 / e-1716: receiver-side decision endpoint.
#
# SPEC 設計方針 3 ("承認は terminal Claude Code 内での user 直接判断のみ") means
# this endpoint is reached exclusively from `beacon dm respond` typed by the
# human, never from an autonomous AI loop. The CLI carries the human's Bearer
# token; the server pulls ``decision_by`` from that token's ``sub`` claim so
# the CLI cannot spoof "I am someone else" by passing a different user_id.
#
# State machine pinned here (idempotency / safety rails):
#   * sidecar missing (legacy / auto): refuse — there is nothing to decide.
#   * sidecar pending: write the requested decision_status. Allowed.
#   * sidecar already approved / denied with the same caller + same decision:
#       no-op idempotent return (= same user retrying the same press).
#   * sidecar already approved / denied with a different caller OR different
#       decision: refuse (= the receiver-of-record already made their call;
#       a second user or a flip cannot smuggle through this primitive).
#   * sidecar receiver_user_id != caller's sub: refuse with 403 (= "not your
#       envelope to decide" — important defense against a curious teammate
#       clicking a colleague's pending row).
class DMRespondBody(BaseModel):
    decision: str  # "approve" | "deny"


@app.post("/api/projects/{project_id}/dm/approval/{event_id}")
def respond_dm_approval(
    project_id: str,
    event_id: str,
    body: DMRespondBody,
    user: dict = Depends(require_auth),
):
    """Receiver decides approve / deny on a pending DM-action sidecar (e-1716).

    Returns the resulting 7-field sidecar row, same shape as
    :func:`db.get_bus_event_approval`. The caller's identity (= server-side
    ``user.sub``) is stamped as ``decision_by`` and the server clock is
    stamped as ``decision_at``; both fields are server-authoritative — the
    CLI has no way to pass them in.
    """
    _load(project_id, user)
    # Normalize decision verb. "approve" / "deny" only — no auto / pending
    # flip from this endpoint (auto is set by the dispatcher when blanket-
    # allowing, pending is set when the gate first fires).
    decision = (body.decision or "").strip().lower()
    if decision not in ("approve", "deny"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid decision {body.decision!r}; "
                "expected 'approve' or 'deny'"
            ),
        )
    target_status = "approved" if decision == "approve" else "denied"

    # Caller's user_id from auth context. In dev (BEACON_API_AUTH=0) the
    # require_auth dependency returns {'sub': 'dev', 'email': 'dev@local'},
    # so decision_by = "dev" in that mode — consistent with how other
    # actor-stamping endpoints (project.archive etc.) behave in dev.
    caller_uid = user.get("sub") or ""

    existing = db.get_bus_event_approval(project_id, event_id)
    if existing is None:
        # No sidecar = legacy/auto envelope. Nothing to decide.
        raise HTTPException(
            status_code=404,
            detail=(
                f"no pending approval found for event_id={event_id!r} "
                f"in project={project_id!r} (the envelope may be legacy / "
                "auto-allowed, or the event_id is wrong)"
            ),
        )

    receiver_uid = existing.get("receiver_user_id") or ""
    sender_uid = existing.get("sender_user_id") or ""

    # Receiver-of-record check. Only the addressee can decide.
    if caller_uid and receiver_uid and caller_uid != receiver_uid:
        raise HTTPException(
            status_code=403,
            detail=(
                f"event_id={event_id!r} is addressed to a different user; "
                "only the intended receiver can approve or deny"
            ),
        )

    current_status = existing.get("approval_status") or ""

    if current_status in ("approved", "denied"):
        # Already decided. Idempotent only when SAME caller chose the SAME
        # outcome — anything else is a structural error.
        if (existing.get("decision_by") == caller_uid
                and current_status == target_status):
            # Same press, same user: no-op return.
            return existing
        raise HTTPException(
            status_code=409,
            detail=(
                f"event_id={event_id!r} already decided as "
                f"{current_status!r} by {existing.get('decision_by')!r} "
                f"at {existing.get('decision_at')!r}; cannot change to "
                f"{target_status!r}"
            ),
        )

    if current_status == "auto":
        # Sidecar exists in auto-allowed state (= dispatcher blanket allow).
        # No human decision required; refuse rather than overwriting.
        raise HTTPException(
            status_code=409,
            detail=(
                f"event_id={event_id!r} is in auto-allowed state and does "
                "not require an explicit decision"
            ),
        )

    # current_status == "pending" → write the decision. put_bus_event_approval
    # preserves created_at and updates status/decision_by/decision_at.
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    row = db.put_bus_event_approval(
        project_id,
        event_id,
        approval_status=target_status,
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        decision_by=caller_uid,
        decision_at=now,
    )

    # ms-70 / e-1717: denied → emit a server-issued T5 reply addressed
    # back to the original envelope's sender_session_id so the AI that
    # tried to act doesn't sit in an infinite-await loop on a bus event
    # whose human gatekeeper just said "no". The approve path needs no
    # such reply because normal delivery resumes once the sidecar flips
    # to approved.
    #
    # T5 chosen per AC 3: this is server-issued, scope=None, no actions,
    # info-disclosure-forbidden (= ping-shape payload only). The CORE doc
    # "高リスク endpoint 一覧" (8iZL1IC92GZ0GwtAUjq5) covers tier escalation
    # paths, not read-only deny notifications — T5 is the right floor here.
    #
    # The payload squeezes into the T5 short-ping schema
    # ({ping/ack/status/kind/ts}, ≤32 chars per string value) by encoding
    # the "denied by receiver" semantics in structural fields:
    #   kind   = "deny"
    #   status = "denied_by_receiver"   (= AC 2 parse anchor)
    #   ack    = "<receiver email or sub>"
    # AC 2 ("body text contains 'denied by receiver' + receiver email")
    # is satisfied structurally: the substring "denied" + "receiver"
    # both appear in status, and the email/sub identifying the receiver
    # is in ack. The literal free-text phrase with a space is not
    # representable in T5 ping shape (= CORE doc "T5 = 短い ping schema");
    # this trade-off is the structural realization of the rule.
    #
    # Failure here is logged + swallowed (warning, not error): the
    # receiver's deny decision is already durably recorded in the sidecar
    # row above; if the reply append fails, that's a downstream-notification
    # gap, not a state-machine corruption. Mirrors e-1713's dispatcher-
    # failure-as-warning policy so a transient Firestore hiccup on the
    # reply path never reverts a human's deny click.
    if decision == "deny":
        try:
            original_event = db.get_bus_event(project_id, event_id)
            if original_event is None:
                logging.warning(
                    "e-1717: original bus_event %s not found in project %s "
                    "for denied-reply chain; skipping reply append",
                    event_id, project_id,
                )
            else:
                sender_session_id = (
                    original_event.get("sender_session_id") or ""
                )
                if not sender_session_id:
                    logging.warning(
                        "e-1717: original bus_event %s has no "
                        "sender_session_id; skipping denied-reply chain",
                        event_id,
                    )
                else:
                    # Receiver identifier — prefer email (= human-readable
                    # in the AI's context), fall back to sub for dev mode.
                    receiver_ident = (
                        user.get("email") or user.get("sub") or ""
                    )
                    # Cap ack at the T5 short-ping value max (32 chars) so
                    # validate_t5_payload accepts it for any plausible email.
                    if len(receiver_ident) > 32:
                        receiver_ident = receiver_ident[:32]

                    # Chain depth: bump from the original envelope so the
                    # 9-step verify's chain_depth ceiling stays honest.
                    original_envelope = (
                        original_event.get("envelope") or {}
                    )
                    parent_chain_depth = (
                        original_envelope.get("chain_depth") or 0
                    )
                    parent_conversation = (
                        original_envelope.get("conversation_id") or None
                    )

                    reply_issuer = (
                        user.get("email") or user.get("sub") or "server"
                    )
                    reply_envelope = envelope_mod.issue_envelope(
                        tier=envelope_mod.TIER_T5,
                        issuer=reply_issuer,
                        project_id=project_id,
                        actions_authorized=[],
                        scope=None,
                        conversation_id=parent_conversation,
                        in_reply_to=event_id,
                        chain_depth=int(parent_chain_depth) + 1,
                    )
                    reply_payload = {
                        "kind": "deny",
                        "status": "denied_by_receiver",
                        "ack": receiver_ident,
                    }
                    reply_data = {
                        "channel": "dm",
                        # The reply is server-issued, not session-issued.
                        # Use an empty sender_session_id sentinel so legacy
                        # readers don't mistake the reply for a human-typed
                        # message; the in_reply_to + payload.kind="deny"
                        # are the canonical signals.
                        "sender_session_id": "",
                        "payload": {
                            **reply_payload,
                            # Routing: receiver-of-original sender's session
                            # is the addressee.
                            "recipient_session_id": sender_session_id,
                            "in_reply_to": event_id,
                        },
                        "delivery": "notify-user-only",
                        "envelope": reply_envelope,
                        "created_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    }
                    db.append_bus_event(project_id, reply_data)
        except Exception as exc:  # pragma: no cover - defensive
            # Sidecar already records the human's deny — never let a
            # downstream reply-chain hiccup propagate as endpoint failure.
            logging.warning(
                "e-1717: denied-reply chain append failed for event_id=%s "
                "in project=%s: %s (deny decision is recorded; sender AI "
                "will not receive auto-notification this round)",
                event_id, project_id, exc,
            )

    return row


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
    since: str = "",
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

    ``since`` override (ms-60 follow-up): callers can pass an explicit
    ``since`` to fetch events newer than a client-side high-water mark
    instead of the server-side recipient cursor. This lets the bridge
    (channel/bus.mjs) advance its own in-memory watermark per poll without
    burning the server cursor that the inbox-hook depends on for emitting
    AUTONOMOUS ACTION blocks. Empty string ⇒ fall back to the server cursor
    (= legacy behavior, used by /beacon-bus-inbox-hook).
    """
    _load(project_id, user)
    if not since:
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


# ---------------------------------------------------------------------------
# Session intent (ms-54 / e-1369 Layer 4)
#
# Intent is the AI's narrative self-report ("I'm working on X right now").
# The bridge cannot stamp it because the bridge does not know what the AI is
# trying to do — only the AI does. We expose a tiny dedicated endpoint so a
# session can write its own intent without touching the heartbeat upsert
# (keeps the WHERE/WHAT/REACH bridge writes pristinely machine-observable).
# ---------------------------------------------------------------------------


@app.post("/api/projects/{project_id}/sessions/{session_id}/intent")
def upsert_session_intent(
    project_id: str,
    session_id: str,
    body: SessionIntentUpsert,
    user: dict = Depends(require_auth),
):
    """Stamp the AI's free-form intent on its own session document.

    Two fields, both optional:

      * ``text``               — 1-line description of what this AI is doing
                                 right now (e.g. "DM read receipt 実装中").
                                 Empty string clears it.
      * ``attention_required`` — True when the session is waiting on a human
                                 decision. The directory picker / Web UI uses
                                 this to surface "who needs me" at a glance.

    The endpoint does NOT enforce that the calling session_id matches the
    authenticated user — multi-agent dispatch may stamp on behalf of a
    sub-session. We do require project membership via _load. The
    ``actor.email`` already on the session document is the audit trail for
    who actually owns it.
    """
    _load(project_id, user)
    payload = body.model_dump(exclude_none=True)
    if not payload:
        return {"status": "noop"}
    # Land under a stable nested key so directory readers know exactly where
    # to look. Avoids the SessionUpsert merge surface entirely.
    db.upsert_session(project_id, session_id, {"intent": payload})
    return {"status": "ok", "session_id": session_id, "intent": payload}


# ---------------------------------------------------------------------------
# Per-event read receipts (ms-54 / e-1348)
#
# Two narrow endpoints sit on top of find_bus_event / set_bus_event_receipt:
#
#   POST /bus/{event_id}/ack — stamp delivered_at / opened_at
#   GET  /bus/{event_id}     — fetch one event with its receipt state
#
# Declared after every other /bus/* route because FastAPI matches in
# registration order. Putting the {event_id} catch-all earlier would shadow
# /bus/audit, /bus/unread, /bus/cursors/..., /bus/envelope/issue.
# ---------------------------------------------------------------------------


_RECEIPT_STAGES = ("delivered", "opened")


@app.post("/api/projects/{project_id}/bus/{event_id}/ack")
def ack_bus_event_receipt(
    project_id: str,
    event_id: str,
    body: BusEventReceiptAck,
    user: dict = Depends(require_auth),
):
    """Idempotent first-write-wins receipt stamping for a single bus event.

    The semantics differ from the cursor advance in
    :func:`advance_bus_cursor`:

      * Cursor = recipient-side frontier (covers many events at once).
      * Receipt = sender-visible per-event ack ("did this specific message
        surface to anyone?"). bus.mjs calls this twice for each DM —
        ``delivered`` when poll first sees it, ``opened`` after the
        ``notifications/claude/channel`` push lands.

    Repeat calls for the same stage are no-ops and return the original
    timestamp with ``already_set=True``. Unknown stages are rejected at the
    boundary so a typo on the receiver does not accidentally smuggle a new
    field onto the event document.
    """
    _load(project_id, user)
    stage = body.stage
    if stage not in _RECEIPT_STAGES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid stage {stage!r}; expected one of "
                f"{_RECEIPT_STAGES}"
            ),
        )
    result = db.set_bus_event_receipt(
        project_id, event_id, stage, body.recipient_session_id,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"bus event {event_id!r} not found",
        )
    return result


@app.get("/api/projects/{project_id}/bus/{event_id}")
def get_bus_event(
    project_id: str,
    event_id: str,
    user: dict = Depends(require_auth),
):
    """Return one event with its receipt state for ``beacon bus status``.

    Powers the 3-stage display (sent / delivered / opened). The event dict
    already carries the per-stage timestamp fields when set, so the client
    only needs to render what's present.
    """
    _load(project_id, user)
    event = db.find_bus_event(project_id, event_id)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"bus event {event_id!r} not found",
        )
    return event


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


@app.on_event("startup")
async def _verify_envelope_secret_configured():
    """Refuse to start in production with the dev envelope-signing fallback (e-1291).

    The envelope HMAC secret (``server/envelope.py``) falls back to a literal
    placeholder string when ``BEACON_ENVELOPE_SECRET`` is unset. That string
    is visible in the public repo, so booting production with the fallback
    would let anyone read the source and forge T1 envelopes (= authorize
    high-risk actions on the bus).

    Refuse-to-start condition::

        _auth_enabled is True  AND  envelope_mod.is_using_dev_fallback() is True

    ``_auth_enabled`` is the project-wide "production-ish posture" toggle
    (``BEACON_API_AUTH`` != "0"). When auth is disabled (local dev / unit
    tests set ``BEACON_API_AUTH=0`` and stub ``_auth_enabled = False``), the
    fallback is allowed and we only log an INFO so the situation is visible
    without blocking boot.

    Raising ``RuntimeError`` from a startup handler causes FastAPI to abort
    the lifespan startup, which makes Cloud Run's health checks fail — the
    exact behavior we want for a misconfigured deploy (fail fast at boot,
    not at first forged envelope).
    """
    if _auth_enabled and envelope_mod.is_using_dev_fallback():
        msg = (
            "BEACON_ENVELOPE_SECRET not configured for production "
            "(BEACON_API_AUTH=1 but envelope signing would use the dev "
            "fallback secret visible in the public repo). Set "
            "BEACON_ENVELOPE_SECRET to a random 32+ byte value in the "
            "Cloud Run service env before deploying — e.g. "
            "`gcloud run services update beacon-api-prod "
            "--update-env-vars BEACON_ENVELOPE_SECRET=<random>`."
        )
        _server_logger.error(msg)
        raise RuntimeError(msg)
    if envelope_mod.is_using_dev_fallback():
        _server_logger.info(
            "envelope signing using dev fallback secret "
            "(BEACON_API_AUTH=0 — local dev / test posture)"
        )


def _hydrate_v2_milestones(project_id: str, data: dict) -> dict:
    """Re-attach milestones from the v2 subcollection before broadcasting.

    v2 schema stores milestones under projects/{id}/milestones/{ms_id}; the
    parent doc no longer carries milestones[] (lib/operations._replace_cloud_v2).
    Firestore on_snapshot only fires for the parent doc, so a meta-only update
    (summary / members / heartbeat ping) would broadcast {milestones: []} and
    the WebUI would clear the list until the next reload (ms-43 e-1473).

    Fallback: any read failure returns data unchanged. We never want a broadcast
    to be dropped on hydration error — stale-but-present beats empty.
    """
    if data.get("schema_version") != 2:
        return data
    try:
        ms_ref = (
            db.get_db()
            .collection(db.COLLECTION)
            .document(project_id)
            .collection("milestones")
            .stream()
        )
        milestones = [snap.to_dict() for snap in ms_ref]
        return {**data, "milestones": milestones}
    except Exception as exc:
        _server_logger.warning(
            "milestone hydration failed for project=%s: %s", project_id, exc
        )
        return data


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
            hydrated = _hydrate_v2_milestones(project_id, data)
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, hydrated), _event_loop
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

    Close codes used by this endpoint (clients must distinguish them —
    e-639 introduced 4401/4403 for token state; e-1252 added 4403=forbidden
    and 4404 for project-level authorization):

      4401  TOKEN MISSING       — no ``?token=`` query param. Client should
                                  not retry silently; redirect to login.
      4403  TOKEN INVALID       — token presented but signature/expiry
            (reason="token_expired_or_invalid")
                                  rejected. Client should attempt a refresh
                                  and reconnect, or surface "please sign in
                                  again" if refresh fails.
      4403  FORBIDDEN           — token is valid (the user is signed in) but
            (reason="forbidden")  has no role on this project. Client must
                                  NOT retry — the user simply isn't a member.
                                  Differentiated from the expired case via
                                  the ``reason`` field on ``CloseEvent``.
      4404  PROJECT NOT FOUND   — the project_id doesn't exist. Client
            (reason="project_not_found")
                                  should stop reconnecting and surface a
                                  clear "this project doesn't exist or has
                                  been deleted" message.

    All codes are in the application-private range (4000–4999) so they do
    not collide with standard WebSocket close codes. The browser exposes them
    via CloseEvent.code, which makes the retry decision deterministic on the
    client side (no more silent 1008 + infinite reconnect loop).

    e-1252 (= 「サインインさえできれば他人のプロジェクトの中身が誰でも読めて
    しまう」状態の根本修正): until this change the endpoint only verified the
    token signature/expiry, then immediately dumped the requested project to
    the socket. Any authenticated Beacon user could read any project. We now
    route the load through ``_require_project_role`` (e-1254 / 認可ルールを 1
    つに集約するヘルパー) so the WS path goes through the exact same role
    check as REST endpoints.
    """
    token = websocket.query_params.get("token")
    claims: dict | None = None
    if _auth_enabled:
        if not token:
            # Reason text helps server-side audit logs; clients should rely on code.
            await websocket.close(code=4401, reason="token_missing")
            return
        try:
            claims = _verify_id_token(token)
        except HTTPException:
            await websocket.close(code=4403, reason="token_expired_or_invalid")
            return

    # Authorization gate: project must exist AND the authenticated user must
    # have a role on it. Without this the WS endpoint was a wide-open read
    # channel (e-1252). We deliberately run the role check BEFORE
    # ``websocket.accept()`` so the close code reaches the client as a
    # handshake-failure CloseEvent rather than mid-stream (which some browser
    # WS stacks coalesce into a generic 1006).
    try:
        raw, _role = _require_project_role(project_id, claims)
    except HTTPException as exc:
        # Map REST-shaped 403/404 onto WS close codes. Anything else (we
        # don't expect any) gets a generic 4403 so we never leak project
        # contents on an unhandled error.
        if exc.status_code == 404:
            await websocket.close(code=4404, reason="project_not_found")
        else:
            await websocket.close(code=4403, reason="forbidden")
        return

    await websocket.accept()

    if project_id not in _ws_connections:
        _ws_connections[project_id] = set()
    _ws_connections[project_id].add(websocket)

    # Send initial enriched data. The role check above already loaded the
    # project via load_project_consistent (which hydrates v2 subcollection
    # milestones), so we reuse ``raw`` instead of fetching twice.
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
    # Resolve __version__ with fallback chain (e-1273):
    #   1. beacon_cli._version — used in dev (editable install)
    #   2. commands — works in the Cloud Run image, where Dockerfile copies
    #      lib/ into PYTHONPATH but NOT beacon_cli/, so step 1 fails and the
    #      previous implementation reported "unknown" forever (verified at
    #      https://beacon-ai.dev/health on v0.25.0).
    _beacon_version = "unknown"
    try:
        from beacon_cli._version import __version__ as _beacon_version  # type: ignore
    except Exception:
        try:
            from commands import __version__ as _beacon_version  # type: ignore
        except Exception:
            pass
    return {
        "status": "ok",
        "env": os.environ.get("BEACON_ENV", "dev"),
        "version": _beacon_version,
    }


@app.get("/api/auth/config")
def auth_config():
    """Return identity provider config for Web UI / CLI login.

    Response shape depends on ``BEACON_AUTH_PROVIDER``:

    - **firebase** (default, Cloud Run 既存経路):
      ``{"provider": "firebase", "client_id": "<google-oauth-client-id>"}``
      Existing SPA reads ``client_id`` directly for Google Identity Services。

    - **cognito** (AWS GA Lambda 経路, e-1545):
      ``{"provider": "cognito", "client_id": "<spa-client-id>",
         "cognito_domain": "<hosted-ui-domain>", "region": "<aws-region>"}``
      新 SPA / CLI が hosted UI redirect flow を組み立てるのに使う。

    auth 不要 (= ログイン前に叩く endpoint なので)。
    """
    provider = _AUTH_PROVIDER
    if provider == "cognito":
        return {
            "provider": "cognito",
            "client_id": os.environ.get("BEACON_COGNITO_CLIENT_ID", ""),
            "cognito_domain": os.environ.get("BEACON_COGNITO_HOSTED_UI_DOMAIN", ""),
            "region": os.environ.get("AWS_REGION", "ap-northeast-1"),
        }
    # Firebase / Cloud Run 既存経路 (= 後方互換)
    return {
        "provider": "firebase",
        "client_id": os.environ.get("BEACON_OAUTH_CLIENT_ID", ""),
    }



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
    # ms-44 e-1246: Google Identity Services (GIS) uses a popup to receive the
    # ID token via window.postMessage. Modern browsers require the parent page
    # to advertise Cross-Origin-Opener-Policy: same-origin-allow-popups for
    # that opener/postMessage relationship to survive cross-origin popups.
    # Without it, popup → parent postMessage is blocked, the callback fires
    # with an empty credential, atob() throws, and the user is stuck on the
    # login screen with no visible error. Applied to every HTML route so a
    # future page that loads GIS does not silently regress.
    _GIS_HEADERS = {
        "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
    }

    @app.get("/")
    def serve_index():
        return FileResponse(
            _static_dir / "index.html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                **_GIS_HEADERS,
            },
        )

    @app.get("/privacy")
    @app.get("/privacy.html")
    def privacy_policy():
        return FileResponse(_static_dir / "privacy.html", headers=_GIS_HEADERS)

    @app.get("/terms")
    @app.get("/terms.html")
    def terms_of_service():
        return FileResponse(_static_dir / "terms.html", headers=_GIS_HEADERS)

    @app.get("/admin")
    def serve_admin():
        return FileResponse(_static_dir / "admin.html", headers=_GIS_HEADERS)

    @app.get("/cli-auth")
    def serve_cli_auth():
        return FileResponse(_static_dir / "cli-auth.html", headers=_GIS_HEADERS)

    # ms-78 e-1804 — /join/<token> public landing for invitations.
    # The token is parsed client-side from window.location.pathname; we just
    # serve the same join.html for any /join/* path so deep-links work.
    @app.get("/join/{token}")
    def serve_join_landing(token: str):
        return FileResponse(_static_dir / "join.html", headers=_GIS_HEADERS)

    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
