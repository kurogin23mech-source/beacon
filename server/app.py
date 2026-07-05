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
import phantom_done_evidence as phantom_done_mod  # ms-95 / e-2726: task done evidence gate
import store_router as db  # e-1544: BEACON_STORE_BACKEND で firestore / dynamodb を切替
import operations
import trek as trek_mod  # ms-69 / e-1656: trek schema + pure mutators
import trek_scheduler as trek_scheduler_mod  # ms-83 / e-1997: progress-check cadence logic

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

# Set BEACON_LOCAL_DEV=1 to enable the IdP-free local dev login (= /api/auth/dev-login
# mints a bcli token for an arbitrary email, so multiple people can use a
# locally-running server under separate accounts without Google/Cognito).
# HARD off by default: production (Cloud Run) never sets this env, so the
# dev-login endpoint returns 404 there and can never be reached.
_local_dev_enabled = os.environ.get("BEACON_LOCAL_DEV", "0") == "1"


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
    hydrate_milestones: bool = True,
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

    ``hydrate_milestones`` (cost-reduction knob, added for the
    ~60% Firestore read reduction on high-frequency polling endpoints):

      * ``True``  (default) — call ``load_project_consistent`` which streams
        the ``milestones`` subcollection (97 reads for the Beacon project).
        Every existing caller kept its current behavior.
      * ``False`` — call ``load_project_meta_only`` which fetches ONLY the
        project meta doc (1 read) and returns the SAME dict shape with
        ``milestones=[]``. Use for endpoints whose handler body never
        reads ``data["milestones"]`` (bus polling, session intent, cursor
        updates, per-event acks). The auth check runs on the meta doc alone
        because ``owner`` / ``members`` live at the top level — unaffected
        by whether milestones are hydrated.

    Keeping this behind one flag on the single auth helper preserves the
    "authorization rule lives in one place" invariant this function was
    created to enforce.

    For WS handlers: catch ``HTTPException`` from this helper and translate
    ``404 → close 4404`` / ``403 → close 4403 (forbidden)``. REST handlers
    re-raise as-is.
    """
    try:
        if hydrate_milestones:
            data = operations.load_project_consistent(project_id)
        else:
            data = operations.load_project_meta_only(project_id)
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


def _load_meta_only(project_id: str, user: dict | None = None) -> dict:
    """Meta-only variant of :func:`_load` — same auth rule, no milestones hydration.

    Every API call passing through ``_load`` triggered
    ``operations.load_project_consistent`` which streams the entire
    ``milestones`` subcollection (97 reads for the Beacon project) even when
    the handler only needed a role check on the project meta doc. For
    high-frequency polling endpoints (bus/unread, cursor advance, session
    intent, per-event ack) this multiplied Firestore reads by ~97x — one of
    the dominant contributors to the GCP bill discovered during the
    cost-reduction sweep.

    This helper is the drop-in for those handlers:

      * Same 404 / 403 behavior as ``_load`` (the auth check is delegated to
        ``_require_project_role`` so the rule stays in ONE place).
      * Returns the same dict shape as ``_load`` EXCEPT
        ``data["milestones"]`` is guaranteed to be ``[]``. Any accidental
        ``for ms in data["milestones"]`` is a silent no-op — the caller does
        not KeyError, and there is no silently truncated milestone list to
        mislead downstream code.
      * The ``user is None`` branch mirrors ``_load``'s dev-mode /
        internal-caller path so behavior is consistent between the two
        entry points.

    Do NOT switch endpoints that read ``data["milestones"]`` (like
    ``GET /api/projects/{project_id}`` or milestone / task CRUD) to this
    helper — they would silently receive an empty list.
    """
    if user is None:
        try:
            return operations.load_project_meta_only(project_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    data, _role = _require_project_role(project_id, user, hydrate_milestones=False)
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

def _resolve_canonical_project_id(
    maybe: str, *, user_id: str
) -> Optional[str]:
    """Resolve a slug or full project_id to its canonical full project_id.

    ms-95 / Trek task-add cross-project bug: scope entries can be stored
    using the user-friendly **slug** (= just ``"profile-extractor"``)
    because that's what users type at the CLI (``beacon trek plan
    --add-scope profile-extractor:ms-5``). But ``operations.apply_operation``
    requires the **full project_id** (= ``"profile-extractor-276d28"`` with
    the 6-char md5 path-hash suffix the CLI mints in ``cmd_cloud_setup``).
    Without this resolver, the task-add endpoint passes the slug down to
    ``db.get_project`` → returns None → ``LookupError`` → uncaught → HTTP
    500. This helper canonicalizes both the request side and (caller's
    copy of) the scope side so the scope match works on equal footing and
    the downstream apply_operation call always sees the full id.

    Returns:
      * the full project_id on **unique** match
      * ``None`` if not found OR ambiguous (= multiple slug expansions)

    Resolution strategy:
      1. Fast path — assume ``maybe`` is already the full id. If
         ``db.get_project(maybe)`` returns a doc, return ``maybe`` as-is.
      2. Slug path — scan projects accessible to ``user_id`` and pick
         those whose project_id starts with ``f"{maybe}-"`` (= slug prefix
         used by ``cmd_cloud_setup`` when minting ids as ``<slug>-<hex6>``).
         Return the id when exactly one matches, else ``None``.

    Failure modes (intentional, returned as ``None`` so the caller picks
    the right HTTP code):
      * Project does not exist → 404 candidate
      * Multiple projects share the slug prefix → 409 candidate (the
        caller should surface "ambiguous slug" so the user can supply
        the full id explicitly)

    Performance: slug path is O(N) over the user's projects per call.
    No cache — keep the code simple; if this shows up in latency traces
    later, memoize per-request.
    """
    if not maybe:
        return None
    # Fast path: maybe is already a full project_id.
    try:
        if db.get_project(maybe) is not None:
            return maybe
    except Exception:  # noqa: BLE001 - db hiccup falls through to slug path
        pass
    if not user_id:
        # Cannot scope the slug scan without a user — refuse rather than
        # leak cross-tenant ids.
        return None
    try:
        rows = db.list_projects(user_id=user_id)
    except Exception:  # noqa: BLE001 - on db failure, refuse to guess
        return None
    prefix = f"{maybe}-"
    matches = [
        r.get("project_id") for r in (rows or [])
        if (r.get("project_id") or "").startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_trek_scope_project_ids(trek_doc: dict) -> list[str]:
    """Resolve every project in ``trek_doc.scope[]`` to canonical full ids.

    ms-95 / e-2639 — Trek scheduler tick was migrated to per-member dm
    fanout (= ms-97 SPEC AC16 / 中心原則 6 「Wake 経路は DM と完全同一」)。
    The single-project ``_resolve_trek_target_project_id`` helper that
    resolved only ``scope[0]['project']`` was removed: it forced the tick
    to post into one project bus, leaving members whose home project sat
    in ``scope[1..N]`` permanently deaf to Trek progress-check / leader-
    digest events. The new helper canonicalises *every* unique project
    listed in scope so the caller can walk all candidate home buses when
    fanning a dm out to each member's live session.

    Resolution strategy mirrors the retired single-project helper: each
    project value is canonicalised through the leader user's project
    access list (= leader is guaranteed to have access to every project
    in scope, so this is the safe identity for scheduler-side slug
    expansion when no end-user request context exists). Slugs that
    cannot be resolved are returned as-is so downstream code can still
    attempt list_sessions / append_bus_event against the raw value —
    matching the pre-e-2639 graceful-degradation behaviour.

    Behaviour:
      * Returns canonical full project_ids in scope order, de-duplicated.
      * Unresolvable slugs are returned raw (= we never raise; the
        scheduler loop must continue across treks).
      * Empty scope returns ``[]``.
    """
    scope = trek_doc.get("scope") or []
    if not scope:
        return []
    leader_user_id = ""
    for m in trek_doc.get("members") or []:
        if (m.get("role") or "") == "leader" and m.get("user_id"):
            leader_user_id = m.get("user_id") or ""
            break
    seen: set[str] = set()
    out: list[str] = []
    for entry in scope:
        raw = (entry.get("project") or "").strip()
        if not raw:
            continue
        canonical = raw
        if leader_user_id:
            resolved = _resolve_canonical_project_id(
                raw, user_id=leader_user_id,
            )
            if resolved:
                canonical = resolved
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    return out


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


class OperationFireClaimRequest(BaseModel):
    """Body for POST /api/projects/{id}/operation-fires/{op_id}/claim (ms-95).

    Atomic per-day claim used by the CLI scheduler to dedup operation
    triggers across parallel bclaude sessions in the same project. See
    ``claim_operation_fire_if_new`` in firestore_client for the gate
    semantics. ``session_id`` is informational (= which session won the
    race today) and may be empty when the caller has no bridge mint.
    """
    session_id: str = ""


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
            # ms-95 / e-2794 (2026-07-03): 旧「migration-period 対応で無 owner
            # project を member 扱い」フォールバックを削除。上流の list_projects
            # が既に ownerless project を deny by default で除外しているため、
            # ここに到達する時点で role が決まらないケース (= owner でも member
            # でもない) は認可データの不整合であり、silent に member 化しない。
            if not role:
                _server_logger.warning(
                    "me_list_projects: project %s reached me-endpoint without "
                    "resolvable role for user %s; skipping",
                    pid, uid,
                )
                continue
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    # ms-95 / e-2794 (2026-07-03): owner を必須化。以前は sub 欠落時に空文字
    # フォールバックで owner="" の project が silent に生まれ、list_projects の
    # migration-period fallthrough と組み合わさって全ユーザー可視の穴を作って
    # いた。deny by default 側の fix と両輪で塞ぐ。
    owner_sub = user.get("sub")
    if not owner_sub:
        raise HTTPException(status_code=401, detail="Authenticated user has no sub claim")
    data = {
        "name": body.name,
        "objective": body.objective,
        "milestones": [],
        "owner": owner_sub,
        "members": [],
        # schema_version: v2 (β subcollection layout) は Firestore 専用
        # (1 MiB / doc cap を避ける設計)。dynamodb / mysql は 1 MiB 制約が無く、
        # v2 経路が Firestore を直呼び (operations.py / _hydrate_v2_milestones)
        # するため非 Firestore backend では動かない。よって非 Firestore では
        # v1 unified で作る (ms-96 e-2379)。
        "schema_version": (
            operations.SCHEMA_V2_BETA
            if os.environ.get("BEACON_STORE_BACKEND", "firestore").lower() == "firestore"
            else 1
        ),
    }
    _save(project_id, data)
    return {"status": "created", "project_id": project_id}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, slim: bool = False,
                user: dict = Depends(require_auth)):
    # ms-46 e-756: REST もWS pushと同じ enriched shape を返す
    # (total_tasks / done_tasks / entries_to_json)。client がどの経路で
    # データを取っても counts が落ちないように対称化する。
    # ms-84 / e-2326: ?slim=true で entries[] を落とした軽量応答を返す
    # (= Web UI 初期 fetch 用、 entries は MS expand 時に lazy fetch)。
    # default は従来通り full 応答 (= CLI / Tauri IPC 等の既存 consumer 互換)。
    raw = _load(project_id, user)
    return _enrich_project_slim(raw) if slim else _enrich_project(raw)


@app.get("/api/projects/{project_id}/milestones/{milestone_id}/entries")
def get_milestone_entries(project_id: str, milestone_id: str,
                          user: dict = Depends(require_auth)):
    """Return entries[] for a single milestone (ms-84 / e-2326).

    Pair endpoint for the slim WS broadcast: Web UI requests this per-MS
    when the user expands a card. The recursive entry tree is serialized
    via core.entries_to_json so the shape matches the legacy full payload.
    Returns 404 if the milestone is not present in the project.
    """
    raw = _load(project_id, user)
    for ms in raw.get("milestones", []):
        if ms.get("id") == milestone_id:
            entries = ms.get("entries", [])
            return {
                "milestone_id": milestone_id,
                "entries": core.entries_to_json(entries),
            }
    raise HTTPException(status_code=404, detail="milestone not found")


# ms-84 / e-2326 follow-up — tab-scoped REST endpoints. The slim WS broadcast
# drops these arrays so the frame fits inside Cloud Run's WS tolerance; the
# Web UI / Tauri fetch them lazily when the user switches to the matching
# tab (= Releases / Worktree). Each returns the raw array under a top-level
# key so the client can drop it straight into state.project.{name}.

@app.get("/api/projects/{project_id}/pushes")
def get_project_pushes(project_id: str, user: dict = Depends(require_auth)):
    raw = _load(project_id, user)
    return {"pushes": raw.get("pushes", [])}


@app.get("/api/projects/{project_id}/deployments")
def get_project_deployments(project_id: str, user: dict = Depends(require_auth)):
    raw = _load(project_id, user)
    return {"deployments": raw.get("deployments", [])}


@app.get("/api/projects/{project_id}/worktree-sessions")
def get_project_worktree_sessions(project_id: str,
                                  user: dict = Depends(require_auth)):
    raw = _load(project_id, user)
    return {"worktree_sessions": raw.get("worktree_sessions", [])}


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
    # ms-43 / e-2128 — explicit WS broadcast after every write. ms-84 / e-2325:
    # the Firestore on_snapshot listener was disabled (see _start_watcher
    # docstring) because it produced duplicate broadcasts for every write
    # (over-broadcast bug). Single-instance Cloud Run posture makes the
    # explicit broadcast self-sufficient.
    _broadcast_project_after_write(project_id)
    return {"status": "ok", "project_id": project_id}


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/milestones")
def create_milestone(project_id: str, body: MilestoneCreate,
                     user: dict = Depends(require_auth)):
    # ms-43 / e-2246 — resolve the human author identity (= user_id / email /
    # display_name) once, then thread it into core.milestone_add so meta.author
    # is stamped at creation time. Mirrors the create_entry contract from
    # ms-78 / e-1909 so MS lists / detail can surface a creator label.
    author = _resolve_author(user)

    def op(data: dict):
        _require_write(data, user)
        try:
            ms_id = core.milestone_add(
                data, body.title, body.target_date,
                description=body.description,
                priority=body.priority,
                objective=body.objective,
                acceptance_criteria=body.acceptance_criteria,
                author=author,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return data, {"ms_id": ms_id, "title": body.title}
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
        project_id, op, op_name="entry.update", actor=user.get("sub", ""),
    )


def _mirror_task_done_to_treks(entry_id: str) -> list[str]:
    """ms-88 / e-2167 — task pool ↔ Trek stamp 同期 (= mirror sync).

    task pool 側で task が ``done`` に成った瞬間に、 active な Trek の
    ``task_states[<entry_id>]`` が non-terminal で stamp 済 なら自動で
    ``done`` に mirror する。 「task pool で done だが Trek stamp は
    waiting-review / leader_review / working で残ってる」 stuck 状態 (=
    2026-06-19 dogfood の e-2045 14h 放置事例) を構造的に排除する。

    state transition validation は bypass する (= 直接書き換え)。 これは
    server-forced reconciliation であり、 「executor / leader / user が
    意図的に state を進める」 normal transition とは性質が違う。

    Returns trek_ids that were touched.
    """
    touched: list[str] = []
    try:
        all_treks = db.list_treks(actor_id=None)
    except Exception:
        return touched
    for t in all_treks:
        if t.get("status") != "active":
            continue
        states = t.get("task_states") or {}
        existing = states.get(entry_id)
        if not existing:
            continue
        try:
            current_state = trek_mod.get_task_state(t, entry_id)
        except Exception:
            current_state = (existing or {}).get("state") or ""
        if current_state in trek_mod.TERMINAL_TASK_STATES:
            continue
        # Direct mirror write (= bypass transition validation).
        now_iso = trek_mod.utcnow_iso()
        states[entry_id] = {
            **existing,
            "state": "done",
            "updated_at": now_iso,
            "last_activity_at": now_iso,
            "updated_by_session_id": "task-pool-mirror",
            "note": (
                "task pool で done 化、 mirror 同期 (= ms-88 / e-2167)"
            ),
        }
        t["task_states"] = states
        t["updated_at"] = now_iso
        try:
            db.save_trek(t.get("trek_id", ""), t)
            touched.append(t.get("trek_id", ""))
        except Exception:
            continue
    return touched


# ms-95 / e-2726 — phantom-done evidence gate.
#
# When ``done_entry`` succeeds, compare the just-done task's keywords
# against the most-recent N commits. If no commit references the task by
# id AND keyword overlap is below threshold, emit a phantom-done warning
# to Cloud Logging (structured json line, severity=WARNING). Done itself
# is ALLOWED — the gate is a flag, not a filter ("動かしながら考える"
# philosophy from the e-2726 task spec).
#
# Background: 2026-06-28 dogfood observed two phantom dones (= e-710 in
# the PE project, e-2567 here). The CORE doc ``task-done-judgment-principle``
# / ms-97 AC10 expects done to be backed by physical evidence, but there
# was no server-side check. This is the implementation gap closer.
#
# Companion to e-2650 (= "Trek slot done requires project task done"):
#   * e-2650 protects the Trek-view layer (= slot done cannot precede
#     project task done).
#   * e-2726 (this) protects the project pool layer (= project task done
#     should have commit evidence).
# Stacked, the two close the loop: Trek slot done → project task done
# (e-2650) → commit evidence flagged when missing (e-2726).
def _check_phantom_done_evidence(
    project_id: str,
    entry_id: str,
    user: dict,
    request: Optional[Request] = None,
) -> Optional[dict]:
    """Evaluate a freshly-done task for commit evidence; emit warning if missing.

    Returns the assessment dict (or ``None`` on lookup failure / disabled).
    Failure is silently swallowed — the warning emission is purely
    observational and must not block the task done response. The returned
    assessment is folded into the endpoint response so the CLI can surface
    the warning to the user in the same turn.

    Disabled via env ``BEACON_PHANTOM_DONE_GATE=0`` (escape hatch for
    Cloud Run rollback without a redeploy). Default = enabled.
    """
    if os.environ.get("BEACON_PHANTOM_DONE_GATE", "1") == "0":
        return None
    try:
        data = operations.load_project_consistent(project_id)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    found = core.find_entry(data, entry_id)
    if not found:
        return None
    _, _, entry, _ = found
    recent_commits = phantom_done_mod.collect_recent_commits(data)
    assessment = phantom_done_mod.evaluate_done_evidence(entry, recent_commits)
    if assessment.get("has_evidence"):
        # All good — task has a matching commit OR has no keywords to
        # judge against. Do not pollute Cloud Logging with negatives.
        return assessment
    # Phantom done detected — emit structured warning.
    sid = ""
    if request is not None:
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
    uid = ""
    if isinstance(user, dict):
        uid = user.get("sub") or user.get("email") or ""
    log_record = {
        "evt": "task.done.phantom_done_warning",
        "severity": "WARNING",
        "phantom_done_warning": True,  # flag for Cloud Logging filter
        "task_id": entry_id,
        "project_id": project_id,
        "user_id": uid,
        "session_id": sid,
        "task_description": (entry.get("description") or "")[:200],
        "task_keywords_sample": assessment.get("task_keywords", [])[:20],
        "commit_window": assessment.get("window", 0),
        "threshold": assessment.get("threshold"),
        "match_type": assessment.get("match_type", "none"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        # severity=WARNING so Cloud Logging splits this from the bulk
        # INFO-level audit traffic. The json payload is what aggregation
        # queries key off.
        _audit_logger.warning(json.dumps(log_record, ensure_ascii=False))
    except Exception:
        pass
    return assessment


@app.post("/api/projects/{project_id}/entries/{entry_id}/done")
def done_entry(project_id: str, entry_id: str, request: Request,
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
    result = _apply_op_and_broadcast(
        project_id, op, op_name="entry.done", actor=user.get("sub", ""),
    )
    # ms-88 / e-2167 — mirror task pool done into Trek task_states.
    # Best-effort: failure does not block the task done response.
    try:
        _mirror_task_done_to_treks(entry_id)
    except Exception:
        pass
    # ms-95 / e-2726 — phantom-done evidence gate. Flag only, no reject.
    # Failure is silently swallowed inside the helper; we surface the
    # assessment in the response so the CLI can echo the warning to the
    # operator in the same turn.
    try:
        assessment = _check_phantom_done_evidence(
            project_id, entry_id, user, request=request,
        )
        if (
            isinstance(result, dict)
            and isinstance(assessment, dict)
            and not assessment.get("has_evidence", True)
        ):
            result = {
                **result,
                "phantom_done_warning": {
                    "task_id": entry_id,
                    "match_type": assessment.get("match_type", "none"),
                    "commit_window": assessment.get("window", 0),
                    "threshold": assessment.get("threshold"),
                    "message": (
                        "No commit in the recent window references this "
                        "task. Done allowed (= flag, not filter), but the "
                        "lack of physical evidence is logged for audit "
                        "(ms-95 / e-2726)."
                    ),
                },
            }
    except Exception:
        pass
    return result


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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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
    return _apply_op_and_broadcast(
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


@app.post("/api/projects/{project_id}/operation-fires/{op_id}/claim")
def operation_fire_claim(
    project_id: str,
    op_id: str,
    body: OperationFireClaimRequest,
    user: dict = Depends(require_auth),
):
    """Atomically claim "I'm firing op-<id> today" for this project (ms-95).

    First-write-wins per ``(project, op, today)``. Subsequent callers see the
    prior claim and skip their bus push. The CLI scheduler
    (``_auto_fire_operation_triggers`` in lib/commands.py) hits this endpoint
    before posting the operation-trigger bus event so cross-cwd /
    cross-machine parallel bclaude sessions in the same project no longer
    each fire independently (= e-1668 N-multiplied fires / e-2350 4-6 min
    retrigger storms when ``run_record`` lands locally but cloud sync lag
    makes the next scheduler tick still see "no run_record yet").

    Response: ``{claimed: bool, claimed_by: str, claimed_at: str}``. Date is
    server clock (UTC) so all sessions agree on the calendar day boundary
    even across timezone-mixed machines.

    Any project member (= owner / editor / viewer) may claim. The claim
    itself is not a privileged action; the gate exists to dedup honest
    parallel writers, not to enforce access.
    """
    _load(project_id, user)  # membership check (any role)
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return db.claim_operation_fire_if_new(
        project_id, op_id, today, body.session_id or ""
    )


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
    return _apply_op_and_broadcast(
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

    return _apply_op_and_broadcast(
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

    return _apply_op_and_broadcast(
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

    return _apply_op_and_broadcast(
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

    _apply_op_and_broadcast(
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

    return _apply_op_and_broadcast(
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

    _apply_op_and_broadcast(
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


@app.get("/api/admin/projects/ownerless")
def admin_list_ownerless_projects(user: dict = Depends(require_auth)):
    """Audit endpoint: list projects that have no owner field (ms-95 / e-2794).

    Before the 2026-07-03 fix, ownerless projects leaked into every user's
    project listing via the ``list_projects`` migration-period fallthrough.
    That path is now closed (deny by default). This endpoint lets an admin
    inventory the residue so owners can be backfilled or the projects
    archived. Returns rows with minimal metadata — no milestones / entries.
    """
    _require_admin(user)
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
            per_proj_result = _apply_op_and_broadcast(
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

    return _apply_op_and_broadcast(
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


class TrekTaskStateSet(BaseModel):
    """ms-75 / e-2048 — Trek-internal task state declaration.

    ``task_id`` is the project entry id (e-XXXX) the executor is stamping.
    ``state`` is one of ``working``, ``done``, ``waiting-review``. ``note``
    is a short freeform string (≤500 chars) attached to the state record
    so the leader review surface can show executor rationale without a
    separate DM round-trip.
    """
    task_id: str
    state: str
    note: Optional[str] = ""


class TrekHaltSet(BaseModel):
    issued_by_session_id: str
    reason: str = ""


# ms-92 / e-2141 — cross-project task add via Trek scope.
# Body for POST /api/treks/{trek_id}/task-add. The server walks
# trek.check_trek_task_add_allowed and either writes the task to the
# target project's milestone (stamping meta.trek_id for audit) or
# returns 403 with the scope-guard reason code so the CLI can show
# the right remediation hint.
class TrekTaskAddRequest(BaseModel):
    target_project: str
    target_milestone: str
    description: str
    type: str = "task"
    priority: str = ""
    motivation: str = ""
    acceptance_criteria: str = ""


class TrekTransferLeader(BaseModel):
    from_session_id: str  # current leader session (caller's session)
    to_session_id: str    # new leader session


# ms-88 / e-2089 — fresh session take-over (= dead leader_session_id 引き継ぎ)
class TrekTakeOver(BaseModel):
    session_id: str  # 新 leader_session_id (= 呼び出し session の sid)


# ms-88 / e-2106 — pulse-ack body (= /beacon-trek-pulse Skill self-report).
# ms-92 / e-2165 — structured payload fields added so the leader-digest
# (e-2164) can mechanically aggregate "stuck=N idle=M" counts without
# parsing natural-language notes. All structured fields are optional and
# backward-compat: pre-e-2165 callers (= bridge versions / scripts that
# only set picked_choice + note) keep working.
class TrekPulseAck(BaseModel):
    session_id: str
    picked_choice: str = ""  # 5-choice token, see lib/trek.VALID_PULSE_PICKED_CHOICES (ms-88 / e-2139)
    note: str = ""
    # Structured fields — see lib/trek.record_pulse_ack docstring for shape.
    state_summary: str = ""
    blockers: list[str] = []
    needs_leader_judgment: bool = False
    time_on_task_seconds: int = 0


# ms-88 / e-2138 — kickoff completion body (= /beacon-trek-pulse Step 0 が呼ぶ)
class TrekKickoff(BaseModel):
    session_id: str
    kickoff_dm_event_id: str = ""  # bus.send 結果の event_id (= audit trace)


# ms-97 / Phase 7-B / e-2684 — leader succession consent body (= candidate
# accept / decline 1 hop response). caller_session_id は X-Beacon-Session
# header から取るので body には載せない。
class TrekSuccessionConsent(BaseModel):
    decision: str  # "accept" | "decline"


# ms-95 / e-2308 — extend TTL on a single task (= leader hints "I delegated
# this to a subagent that can't stamp activity itself"). See
# lib/trek.extend_task_ttl docstring for semantics.
class TrekExtendTtl(BaseModel):
    task_id: str
    minutes: int  # 0 or negative clears the extension
    reason: str = ""  # short audit string (e.g. "dispatched to subagent X")


# ms-97 / Phase 7-C / AC24, e-2603 — blanket scope approval body.
class TrekBlanketCategory(BaseModel):
    category: str  # See lib/trek._normalised_blanket_category for shape


def _trek_find_member_dual(
    t: dict, *, user_id: str, session_id: str = "",
) -> dict | None:
    """Phase-gated member lookup (= ms-97 / e-2658 Phase 1 AC6 dual-mode).

    Phase A+ trek (= members[] が session_id keyed) で caller の session_id
    が分かっている時は session-grain で lookup。 caller_sid が空 (= 旧
    bridge / curl など header 未送) の時、 または pre-A trek の時は
    legacy user_id grain で lookup。 dual-mode 経路として cutover 期間
    の互換性を保つ。
    """
    if session_id and trek_mod.is_session_id_keyed(t):
        m = trek_mod.find_member(t, session_id=session_id)
        if m is not None:
            return m
        # Fallback to user_id grain in case the calling session predates
        # the migration (= header set but session not yet registered).
        # This avoids hard-locking honest callers out during the cutover
        # window; AC13 (= strict session_id hard-check) lands separately.
    if not user_id:
        return None
    return trek_mod.find_member(t, user_id=user_id)


def _load_trek_for_read(
    trek_id: str, user: dict, request: "Request | None" = None,
) -> dict:
    """Load a trek doc. 404 if missing, 403 if caller is neither creator
    nor a member (per SPEC visibility = creator OR members).

    ms-97 / e-2658 Phase 1 (AC6) — ``request`` から ``X-Beacon-Session``
    を取り、 phase A+ trek の時は session_id grain で membership check
    する dual-mode 経路。 ``request`` 省略時は user_id grain のみ。
    """
    t = db.get_trek(trek_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"Trek '{trek_id}' not found")
    if not _auth_enabled:
        return t
    uid = user.get("sub", "")
    creator_uid = (t.get("creator_actor") or {}).get("user_id", "")
    if creator_uid == uid:
        return t
    caller_sid = ""
    if request is not None:
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
    if _trek_find_member_dual(t, user_id=uid, session_id=caller_sid) is not None:
        return t
    raise HTTPException(status_code=403, detail="Not a member of this trek")


def _trek_member_role(
    t: dict, user_id: str, session_id: str = "",
) -> str:
    """Return the caller's role ('leader' / 'member' / '' if not a member).

    ms-97 / e-2658 Phase 1 — phase A+ trek の時は session_id grain 優先、
    pre-A trek または session_id 不明時は user_id grain。 同 user_id で
    複数 session が居る場合 (= phase A+ silent expand 状況) は session_id
    grain hit が真値、 user_id grain は best-effort first-match。
    """
    member = _trek_find_member_dual(
        t, user_id=user_id, session_id=session_id,
    )
    if member is None:
        return ""
    return member.get("role", "member")


def _require_trek_leader(
    t: dict, user: dict, request: "Request | None" = None,
) -> None:
    """Raise 403 if caller does not hold the leader role on this trek.

    ms-97 / e-2658 Phase 1 — original user_id grain (+ phase A+ で session
    grain 優先) の role check entry-point。 Phase 4 (= AC13 hard-check) で
    leader-only endpoints は ``_require_trek_leader_session`` 経由に切り
    替わるが、 この helper 自身は role check 限定の従来 contract を維持
    する (= 並列で残しておくと既存 caller の影響範囲を最小化できる)。
    """
    if not _auth_enabled:
        return
    sid = ""
    if request is not None:
        sid = request.headers.get("X-Beacon-Session", "") or ""
    if _trek_member_role(t, user.get("sub", ""), sid) != "leader":
        raise HTTPException(status_code=403, detail="Trek leader role required")


def _require_trek_leader_session(
    t: dict, user: dict, request: "Request | None" = None,
) -> None:
    """Raise 403 unless caller is BOTH the leader role AND the live leader session (ms-97 / AC13).

    Two-layer check (= role at user grain + session at session grain):

      1. ``_require_trek_leader`` — caller has ``role == "leader"`` in
         the trek's members[]. Survives session restart of the same
         user.
      2. session_id hard-check    — caller's ``X-Beacon-Session`` header
         equals ``trek.leader_session_id``. Blocks a second session of
         the same user from impersonating leader actions (= the gap
         that AC13 closes; pre-Phase-4 the role check alone allowed
         any session of the leader user to mutate).

    Layer 2 only fires on phase A+ trek (= ``is_session_id_keyed``
    True). Pre-A trek invariance: the helper degrades to the
    role-only check, matching the legacy contract. Without the
    ``X-Beacon-Session`` header (= legacy CLI / smoke test) we also
    fall through to role-only — the header is opt-in, not a hard
    requirement, so we don't break callers that pre-date the
    session-grain key.

    The 403 detail surfaces a session prefix on both sides of the
    mismatch so the operator can tell "wrong-session of leader user"
    from "non-leader user" at a glance (= dogfood ergonomics).
    """
    _require_trek_leader(t, user, request)
    if not _auth_enabled:
        return
    if request is None:
        return
    caller_sid = request.headers.get("X-Beacon-Session", "") or ""
    if not caller_sid:
        # No session header → can't apply session grain; the role check
        # above is the available authoritative gate. Mirrors the
        # legacy CLI behaviour (= header is opt-in).
        return
    if not trek_mod.is_session_id_keyed(t):
        # Pre-A trek: members[] is keyed by user_id only, so the
        # leader_session_id field may be stale or absent. Don't fail
        # the call — pre-A invariance is contractually preserved.
        return
    leader_sid = t.get("leader_session_id") or ""
    if not leader_sid:
        # Phase A+ trek with no live leader session (= e.g. just
        # archived or stamped null) — the role check above is the
        # available gate; refusing here would prevent ``take-over``
        # from binding a fresh session post-leader-death.
        return
    if caller_sid != leader_sid:
        raise HTTPException(
            status_code=403,
            detail=(
                "leader action requires the stamped leader session "
                f"(caller={caller_sid[:8]}.., leader={leader_sid[:8]}..)"
            ),
        )


def _require_trek_joined_member(
    t: dict, user: dict, request: "Request | None" = None,
) -> None:
    """Raise 403 if caller is not a joined member (= invited but not joined
    is insufficient for write ops; mirrors SPEC 設計方針 12 join-flow).

    ms-97 / e-2658 Phase 1 — phase A+ trek の時は session_id grain 優先。
    placeholder (= session_id 未設定 + joined_at 空) は invited-but-not-joined
    として常に reject される。
    """
    if not _auth_enabled:
        return
    uid = user.get("sub", "")
    sid = ""
    if request is not None:
        sid = request.headers.get("X-Beacon-Session", "") or ""
    member = _trek_find_member_dual(t, user_id=uid, session_id=sid)
    if member is not None and member.get("joined_at"):
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
def update_trek_endpoint(trek_id: str, body: TrekUpdate, request: Request,
                         user: dict = Depends(require_auth)):
    """Update title / description / type. Leader-only.

    Status / members / scope / halt are mutated through dedicated endpoints
    so audit logs and authz rules stay sharp per intent.

    ms-97 Phase 4 / AC13 — leader hard-check via
    ``_require_trek_leader_session`` (= role + session_id grain on phase
    A+ trek). Pre-A invariance preserved.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
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
def archive_trek_endpoint(trek_id: str, request: Request,
                          user: dict = Depends(require_auth)):
    """Archive a trek (status → archived). Leader-only. Archive is terminal.

    ms-97 Phase 4 / AC13 — leader hard-check via
    ``_require_trek_leader_session`` (= role + session_id grain on phase
    A+ trek). Pre-A invariance preserved.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
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
def start_trek_endpoint(trek_id: str, request: Request,
                        user: dict = Depends(require_auth)):
    """Transition trek planning → active. Leader-only.

    ms-97 Phase 4 / AC13 — leader hard-check via
    ``_require_trek_leader_session`` (= role + session_id grain on phase
    A+ trek). Pre-A invariance preserved.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
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


def emit_leader_succession_consent_dm(
    trek_doc: dict,
    candidate_session_id: str,
    *,
    former_leader_session_id: str = "",
    deadline_seconds: int = 1800,
    candidate_home_project_id: str = "",
) -> Optional[str]:
    """Post a 1 hop leader succession consent DM to ``candidate_session_id``.

    ms-97 / Phase 6 (AC15) — structural placeholder for the (Phase 7) AC22
    auto-succession algorithm. AC22 itself is not written here; this helper
    exists so AC22 can call into a stable, signed-envelope DM emit path
    without re-deriving routing / envelope / payload concerns.

    Contract:

    * Payload kind is ``trek-leader-succession-consent``.
    * Required payload fields: ``trek_id``, ``candidate_session_id``,
      ``deadline_seconds``, ``former_leader_session_id``.
    * Envelope is T1-system scoped to the trek, authorizing the single
      action ``trek.leader_succession_consent``.
    * Bus event is posted to ``candidate_home_project_id`` if provided,
      otherwise the first ``trek_doc.scope[].project`` is used (= the
      candidate's working project is what the bus inbox watches).
    * Delivery mode is ``auto-execute`` so the candidate session wakes
      via the bus-armed loop and the user's terminal Claude surfaces the
      DM through the normal /beacon-dm-respond path (= 1 hop consent).
    * Returns the bus event id, or ``None`` if no target bus is
      resolvable (= empty scope and no explicit override) — the caller
      can degrade gracefully without a thrown exception.

    Raises ``ValueError`` only for callable-misuse cases (= empty
    ``candidate_session_id``), so the AC22 algorithm gets a loud signal
    if it forgets to plumb the candidate routing through.
    """
    if not candidate_session_id:
        raise ValueError(
            "candidate_session_id is required (= the session that the "
            "consent DM is addressed to)"
        )
    trek_id = trek_doc.get("trek_id") or ""

    target_project_id = candidate_home_project_id or ""
    if not target_project_id:
        scope = trek_doc.get("scope") or []
        if scope:
            first = scope[0] if isinstance(scope[0], dict) else {}
            target_project_id = first.get("project") or ""
    if not target_project_id:
        # No bus to post to. Caller (= AC22) can fall back to a different
        # candidate or escalate to user. Return None rather than throwing
        # because routing failure is an expected runtime state, not a
        # caller bug.
        return None

    try:
        env = envelope_mod.issue_t1_system_envelope(
            project_id=target_project_id,
            trek_id=trek_id,
            actions_authorized=["trek.leader_succession_consent"],
            data_class="free",
            ttl_seconds=max(int(deadline_seconds), 60),
        )
    except ValueError:
        env = None

    payload = {
        "kind": "trek-leader-succession-consent",
        "trek_id": trek_id,
        "candidate_session_id": candidate_session_id,
        "deadline_seconds": int(deadline_seconds),
        "former_leader_session_id": former_leader_session_id or "",
        "recipient_session_id": candidate_session_id,
        "created_at": trek_mod.utcnow_iso(),
        "body": (
            f"[Trek leader succession consent] trek_id={trek_id}\n"
            "現 leader session が不応状態に陥ったため、 あなたが次期 leader "
            "候補として選ばれました。\n"
            f"deadline: {int(deadline_seconds)} 秒以内に accept / decline を "
            "明示してください。\n"
            "辞退した場合、 次の candidate に escalate されます。"
        ),
    }
    bus_data = {
        "channel": "dm",
        "sender_session_id": "",
        "recipient_session_id": candidate_session_id,
        "payload": payload,
        "envelope": env,
        "delivery": "auto-execute",
        "created_at": trek_mod.utcnow_iso(),
    }
    try:
        return db.append_bus_event(target_project_id, bus_data)
    except Exception:
        # Best-effort delivery; AC22 will observe a missing event via
        # downstream check and re-escalate to the next candidate.
        return None


@app.post("/api/treks/{trek_id}/members/join")
def join_trek_endpoint(trek_id: str, request: Request,
                       user: dict = Depends(require_auth)):
    """Accept the caller's own invitation (= sets ``joined_at`` to now).

    Caller must already appear in members[] (= owner-issued invitation).
    Non-invited callers get 403 — there is no self-add path by design.

    We bypass _load_trek_for_read's membership check here because the trek
    visibility model is "creator OR member" and an invited-but-not-yet-joined
    user IS a member entry; the visibility check passes. Self-add (= caller
    not yet in members[] at all) gets caught by trek_mod.accept_invitation.

    ms-86 / e-2225 — also writes a session_history entry on the trek doc
    so the Trek keeps a cumulative record of every session that has ever
    joined. The session id is taken from the ``X-Beacon-Session`` header
    (= the same channel the task-state endpoint uses). When the header is
    missing the join still succeeds; the session_history entry is just
    skipped for that call.
    """
    t = _load_trek_for_read(trek_id, user)
    caller_sid = request.headers.get("X-Beacon-Session", "") or ""
    user_id = user.get("sub", "")
    try:
        trek_mod.accept_invitation(
            t, user_id=user_id, session_id=caller_sid,
        )
    except ValueError as e:
        # Not invited (no row at all) → 403, not 404. The trek exists; the
        # caller just cannot self-add.
        raise HTTPException(status_code=403, detail=str(e))
    # ms-97 / e-2637 — welcome tick bootstrap. After a successful join, if
    # this session has not yet received a welcome tick, fire one into its
    # home project bus so the fresh joiner (= claim ゼロ, AC33 lazy start
    # 不該当) gets a wake-up event that primes the kickoff ritual via
    # /beacon-trek-execute Skill. Idempotent: ``meta.welcome_tick_fired_at``
    # tracks per-session_id stamps so retries / re-joins do not re-fire.
    # The stamp + the bus event write share the same save_trek transaction
    # below so the audit trail is consistent.
    welcome_event_id = ""
    if caller_sid and trek_mod.should_fire_welcome_tick(
        t, session_id=caller_sid,
    ):
        welcome_event_id = _fire_welcome_tick(
            trek_doc=t, trek_id=trek_id, session_id=caller_sid,
        )
        if welcome_event_id:
            trek_mod.mark_welcome_tick_fired(t, session_id=caller_sid)
    db.save_trek(trek_id, t)
    # ms-97 / Phase 6 (AC15) — surface the accident-time leader candidate
    # notice on the join response so clients (= CLI / Skill / future UI) can
    # display the pre-notice to the invitee without re-deriving the text. The
    # member entry now carries ``meta.leader_candidate_notice_shown_at`` as
    # the audit stamp (= trek_mod.accept_invitation does that write).
    response = dict(t)
    response["leader_candidate_notice"] = (
        trek_mod.build_leader_candidate_notice(t)
    )
    if welcome_event_id:
        response["_welcome_tick_event_id"] = welcome_event_id
    return response


def _fanout_welcome_ticks_for_pending_members(
    *, trek_doc: dict, trek_id: str,
    scope_project_ids: list[str],
) -> None:
    """ms-97 / e-2637 — Scheduler-side welcome tick safety net.

    Walks ``trek_doc.members[]`` (phase A+ only) and fires a one-shot
    welcome tick for every session whose stamp is missing
    (= ``should_fire_welcome_tick`` True). On success, stamps
    ``meta.welcome_tick_fired_at[session_id]``.

    The join endpoint fires the welcome tick at join time as the primary
    path. This scheduler-side sweep is the safety net that catches:
      * Sessions that joined before this code was deployed.
      * Sessions whose join-time fire failed (network / DB hiccup).
      * Sessions whose welcome tick was lost in a bus replay.

    The trek_doc is mutated in place; the surrounding tick loop's
    save_trek persists the stamps alongside any other mutations made on
    this tick.
    """
    members = trek_doc.get("members") or []
    if not members:
        return
    # ms-97 / e-2637 — Leader session is excluded from welcome tick.
    # The leader created the trek (= already knows about it); welcome
    # tick is for fresh joiners who need the AC28 manual + kickoff
    # primer. Stamping the leader implicitly here would also produce
    # noise (= leader receives an unnecessary dm on every fresh deploy).
    leader_sid = trek_doc.get("leader_session_id") or ""
    for m in members:
        msid = m.get("session_id") or ""
        if not msid:
            continue
        if leader_sid and msid == leader_sid:
            # Stamp the leader so future ticks skip immediately, but
            # do NOT fire a dm to them.
            trek_mod.mark_welcome_tick_fired(trek_doc, session_id=msid)
            continue
        if (m.get("role") or "") == "leader":
            trek_mod.mark_welcome_tick_fired(trek_doc, session_id=msid)
            continue
        if not trek_mod.should_fire_welcome_tick(trek_doc, session_id=msid):
            continue
        event_id = _fire_welcome_tick(
            trek_doc=trek_doc, trek_id=trek_id, session_id=msid,
            scope_project_ids=scope_project_ids,
        )
        if event_id:
            trek_mod.mark_welcome_tick_fired(trek_doc, session_id=msid)


def _fire_welcome_tick(*, trek_doc: dict, trek_id: str,
                       session_id: str,
                       scope_project_ids: list[str] | None = None) -> str:
    """ms-97 / e-2637 — Post a one-shot welcome tick to the joiner's home bus.

    Resolution: walk ``trek_doc.scope`` for the first project whose
    session registry contains ``session_id`` (= same approach as
    ``_build_executor_targets_session_grain``). If no scope project
    surfaces the session, fall back to ``scope[0]`` so the event is
    still written somewhere observable (= the bridge layer's dm
    delivery filter keys on ``recipient_session_id``, so as long as
    the receiver is on this bus the message lands).

    Returns the event_id on success, empty string on failure. Failures
    must never break the join transaction — the welcome tick is a
    bootstrap optimisation, not a load-bearing path.
    """
    if scope_project_ids is None:
        try:
            scope_project_ids = _resolve_trek_scope_project_ids(trek_doc)
        except Exception:
            scope_project_ids = []
    if not scope_project_ids:
        return ""
    target_pid = ""
    for pid in scope_project_ids:
        try:
            project_sessions = db.list_sessions(pid)
        except Exception:
            project_sessions = []
        for s in project_sessions:
            if (s.get("session_id") or "") == session_id:
                target_pid = pid
                break
        if target_pid:
            break
    if not target_pid:
        # Best-effort fallback: deliver to scope[0]. The bridge filters
        # by recipient_session_id so cross-project posts are safe.
        target_pid = scope_project_ids[0]
    payload = trek_mod.build_welcome_tick_payload(
        trek_doc, session_id=session_id,
    )
    try:
        env = envelope_mod.issue_t1_system_envelope(
            project_id=target_pid,
            trek_id=trek_id,
            actions_authorized=["trek.welcome"],
            data_class="free",
            ttl_seconds=3600,
        )
    except Exception:
        env = None
    bus_data = {
        "channel": "dm",
        "sender_session_id": "",
        "recipient_session_id": session_id,
        "payload": payload,
        "envelope": env,
        "delivery": "auto-execute",
        "created_at": trek_mod.utcnow_iso(),
    }
    try:
        return db.append_bus_event(target_pid, bus_data)
    except Exception as exc:
        print(
            f"warn[ms-97 e-2637]: welcome tick append failed for trek "
            f"{trek_id} session {session_id}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ""


@app.delete("/api/treks/{trek_id}/members/me")
def leave_trek_endpoint(trek_id: str, request: Request,
                        user: dict = Depends(require_auth)):
    """Caller removes themselves from the trek.

    The leader must transfer leadership first (`POST .../transfer-leader`),
    and the last member cannot leave (= archive the trek instead).

    ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek (= members[] が session_id
    keyed) で ``X-Beacon-Session`` header があれば、 該当 1 session の
    member entry だけを削除する (= 同 user の他 session entries は残る、
    session-grain leave)。 header 不在 / pre-A trek は従来の user-grain
    leave (= 同 user の全 entries を削除)。
    """
    t = _load_trek_for_read(trek_id, user, request)
    user_id = user.get("sub", "")
    caller_sid = request.headers.get("X-Beacon-Session", "") or ""
    try:
        if caller_sid and trek_mod.is_session_id_keyed(t):
            # session-grain leave (= 自分の 1 session のみ抜ける)。
            # session 不在なら user-grain にフォールバック (= placeholder
            # session_id 未設定 entry の挙動を保つ)。
            target = trek_mod.find_member(t, session_id=caller_sid)
            if target is not None:
                trek_mod.remove_member(t, session_id=caller_sid)
            else:
                trek_mod.remove_member(t, user_id=user_id)
        else:
            trek_mod.remove_member(t, user_id=user_id)
    except ValueError as e:
        # leader-still-leader / not-a-member / last-member → 400
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return t


# ms-95 / e-2320 — structured audit log for Trek scope mutations.
# Background: 2026-06-23 (memo doc kINfY5a9LLnxHWWhtbZJ Finding 4) a Trek
# scope went from 3 narrow MS entries back to including a project-wide
# entry 1-2 hours after the leader had explicitly removed it. The leader
# could not identify who or what re-added the project-wide entry. A grep
# of the entire codebase (lib/ / server/ / scripts/ / channel/ / skills/)
# found ZERO automated callers of ``add_scope_entry`` other than:
#   (1) cmd_trek_plan in lib/commands.py (= user-typed `beacon trek plan
#       --add-scope` only)
#   (2) this server endpoint (= the only HTTP path for scope writes)
# Both require explicit caller action. So the re-add was either: a stale
# CLI from another window, a manual mistake the leader forgot, or a race
# we couldn't see without log evidence. The "封鎖" (= structural block)
# for e-2320 is therefore observability: every scope mutation gets a
# structured log line carrying caller user_id / session / entry so the
# next occurrence can be traced from Cloud Logging instead of guessed at.
# (e-2315 is the orthogonal "reject project-wide entries at the parser
# layer" task — together they close the foot-gun.)

def _log_trek_scope_audit(*, action: str, trek_id: str, user: dict,
                          request: Request, entry: dict) -> None:
    """Emit a structured audit line for trek scope add/remove.

    Format mirrors the JSON-line shape Cloud Logging already parses from
    other server modules. Failure to log is silently swallowed — audit is
    observational, not load-bearing.
    """
    try:
        import json as _json
        sid = ""
        try:
            sid = request.headers.get("X-Beacon-Session", "") or ""
        except Exception:
            sid = ""
        uid = ""
        if isinstance(user, dict):
            uid = user.get("sub") or user.get("email") or ""
        record = {
            "evt": "trek.scope.audit",
            "action": action,  # "add" or "remove"
            "trek_id": trek_id,
            "user_id": uid,
            "session_id": sid,
            "entry": entry,
        }
        print(_json.dumps(record, ensure_ascii=False), flush=True)
    except Exception:
        pass


@app.put("/api/treks/{trek_id}/scope")
def add_trek_scope_endpoint(trek_id: str, body: TrekScopeOp,
                            request: Request,
                            user: dict = Depends(require_auth)):
    """Stage a scope-entry add for user approval (ms-97 / e-2626, AC23).

    Pre-e-2626 this was an immediate mutation (= ``scope[]`` grew before
    any explicit user OK). With AC23 the scope-add must mirror the
    scope-remove approval flow (= ``pending_user_approval`` state on
    ``trek_doc.pending_scope_ops[]``). The actual ``scope[]`` mutation
    lands only when the user runs ``beacon trek scope-approve
    <pending_id>`` (= POST ``/api/treks/{trek_id}/scope/approve/
    {pending_id}``).

    The legacy audit log (ms-95 / e-2320) still emits, but now with
    ``action="add_pending"`` so the Cloud Logging filter can tell the
    request stage (``pending``) from the apply stage
    (``scope_add_approved``).

    AC24 (= blanket pre-approval, e-2603) is layered on top in a
    follow-up task. This endpoint stages unconditionally; the
    auto-commit on blanket-approved categories lands separately.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    # ms-97 / e-2694 dogfood fix: expand short project names (=
    # ``life-plan-simulator``) to canonical full ids (=
    # ``life-plan-simulator-68c5df``) BEFORE we persist the scope entry.
    # Without this, scope rows storing the short name silently mismatch
    # against the registry-keyed session lookups (= ``list_sessions`` and
    # DM fanout key off the full id), so members in those projects
    # disappear from the fanout target list.
    requesting_user_id = (user.get("sub") or "").strip()
    canonical_pid = body.project
    if requesting_user_id:
        resolved = _resolve_canonical_project_id(
            body.project, user_id=requesting_user_id,
        )
        if resolved:
            canonical_pid = resolved
    entry: dict = {"project": canonical_pid}
    if body.milestone:
        entry["milestone"] = body.milestone
    if body.operation:
        entry["operation"] = body.operation
    if body.task:
        entry["task"] = body.task
    # ms-97 / e-2659 (AC7 server layer): explicit strict-mode validation
    # so the project-wide rejection surfaces as HTTPException 400 with the
    # user-facing message, distinct from the 409 "already present" path
    # below. add_pending_scope_op will also run strict mode internally,
    # this front-load is for the clean status code split.
    try:
        entry = trek_mod.normalize_scope_entry(entry, strict=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Resolve the requesting session id from the X-Beacon-Session header so
    # the pending record carries the "who asked" attribution. Falls back to
    # empty string when the header is absent (= same null behaviour as the
    # audit helper, callers without a session header still get to stage).
    sid = ""
    try:
        sid = request.headers.get("X-Beacon-Session", "") or ""
    except Exception:
        sid = ""
    # ms-97 / Phase 7-C / AC24 — blanket pre-approval auto-commit. When the
    # leader has registered a matching category (= e.g. "operation" or
    # "milestone:ms-97"), bypass ``add_pending_scope_op`` and apply the
    # add directly via ``add_scope_entry``. This is the "no DM hop" path.
    if trek_mod.is_blanket_approved(t, entry):
        try:
            trek_mod.add_scope_entry(t, entry=entry)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        db.save_trek(trek_id, t)
        _log_trek_scope_audit(
            action="add_blanket_auto_commit", trek_id=trek_id, user=user,
            request=request, entry=entry,
        )
        out = dict(t)
        out["pending_op"] = None
        out["auto_committed"] = True
        out["committed_via"] = "blanket_approval"
        return out
    try:
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_ADD,
            entry=entry,
            requested_by_session_id=sid,
        )
    except ValueError as e:
        # Normalisation failure (= bad entry shape). 409 to match the
        # legacy contract on the immediate path.
        raise HTTPException(status_code=409, detail=str(e))
    db.save_trek(trek_id, t)
    _log_trek_scope_audit(
        action="add_pending", trek_id=trek_id, user=user,
        request=request, entry=entry,
    )
    # Return the trek doc unchanged in shape, plus the pending record so
    # callers can immediately reference the ``pending_id`` for approve /
    # reject. Existing clients that ignored the body keep working.
    out = dict(t)
    out["pending_op"] = rec
    out["auto_committed"] = False
    return out


@app.delete("/api/treks/{trek_id}/scope")
def remove_trek_scope_endpoint(trek_id: str, body: TrekScopeOp,
                               request: Request,
                               user: dict = Depends(require_auth)):
    """Stage a scope-entry removal for user approval (ms-97 / e-2611, AC25).

    Pre-e-2611 this was an immediate mutation. With AC25 the scope-remove
    must mirror the scope-add approval flow (= ``pending_user_approval``
    state on ``trek_doc.pending_scope_ops[]``). The actual ``scope[]``
    mutation lands only when the user runs
    ``beacon trek scope-approve <pending_id>`` (= POST
    ``/api/treks/{trek_id}/scope/approve/{pending_id}``).

    The legacy audit log (ms-95 / e-2320) still emits, but now with
    ``action="remove_pending"`` so the Cloud Logging filter can tell the
    request stage (``pending``) from the apply stage (``remove_approved``).
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    entry: dict = {"project": body.project}
    if body.milestone:
        entry["milestone"] = body.milestone
    if body.operation:
        entry["operation"] = body.operation
    if body.task:
        entry["task"] = body.task
    # Resolve the requesting session id from the X-Beacon-Session header so
    # the pending record carries the "who asked" attribution. Falls back to
    # empty string when the header is absent (= same null behaviour as the
    # audit helper, callers without a session header still get to stage).
    sid = ""
    try:
        sid = request.headers.get("X-Beacon-Session", "") or ""
    except Exception:
        sid = ""
    try:
        rec = trek_mod.add_pending_scope_op(
            t,
            action=trek_mod.PENDING_SCOPE_ACTION_REMOVE,
            entry=entry,
            requested_by_session_id=sid,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.save_trek(trek_id, t)
    _log_trek_scope_audit(
        action="remove_pending", trek_id=trek_id, user=user,
        request=request, entry=entry,
    )
    # Return the trek doc unchanged in shape, plus the pending record so
    # callers can immediately reference the ``pending_id`` for approve /
    # reject. Existing clients that ignored the body keep working.
    out = dict(t)
    out["pending_op"] = rec
    return out


@app.post("/api/treks/{trek_id}/scope/approve/{pending_id}")
def approve_trek_scope_op_endpoint(trek_id: str, pending_id: str,
                                   request: Request,
                                   user: dict = Depends(require_auth)):
    """Approve a pending scope op (= commit add or remove).

    ms-97 / e-2611 — Mirror of the scope-remove staging endpoint. Any
    joined member of the trek may approve; the philosophy is that
    ``pending_user_approval`` is a structural pause for an explicit
    "yes do that" rather than a per-actor authorisation check. The same
    ``trek.scope.audit`` log line is emitted with
    ``action="<scope_add|remove>_approved"`` so the apply step is
    observable in Cloud Logging alongside the original request stage.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    rec = trek_mod.find_pending_scope_op(t, pending_id=pending_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail=f"pending scope op not found: {pending_id}",
        )
    try:
        applied_entry = trek_mod.approve_pending_scope_op(
            t, pending_id=pending_id,
        )
    except ValueError as e:
        # The drop happened before raise (see lib helper), so save the
        # now-cleaned doc and 409 the caller. This matches the
        # add_scope_entry / remove_scope_entry 409 contract on add.
        db.save_trek(trek_id, t)
        raise HTTPException(status_code=409, detail=str(e))
    db.save_trek(trek_id, t)
    audit_action = f"{rec.get('action', 'scope_op')}_approved"
    _log_trek_scope_audit(
        action=audit_action, trek_id=trek_id, user=user,
        request=request, entry=applied_entry,
    )
    return t


@app.post("/api/treks/{trek_id}/scope/reject/{pending_id}")
def reject_trek_scope_op_endpoint(trek_id: str, pending_id: str,
                                  request: Request,
                                  user: dict = Depends(require_auth)):
    """Reject a pending scope op (= drop without applying).

    ms-97 / e-2611 — Companion to ``approve``. Any joined member may
    reject. Audit line carries
    ``action="<scope_add|remove>_rejected"`` so the rejection is just as
    traceable as approve / apply.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    rec = trek_mod.find_pending_scope_op(t, pending_id=pending_id)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail=f"pending scope op not found: {pending_id}",
        )
    try:
        dropped = trek_mod.reject_pending_scope_op(
            t, pending_id=pending_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.save_trek(trek_id, t)
    audit_action = f"{dropped.get('action', 'scope_op')}_rejected"
    _log_trek_scope_audit(
        action=audit_action, trek_id=trek_id, user=user,
        request=request, entry=dropped.get("entry") or {},
    )
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


@app.post("/api/treks/{trek_id}/summary-sent")
def trek_summary_sent_endpoint(
    trek_id: str, request: Request,
    user: dict = Depends(require_auth),
):
    """Stamp ``meta.summary_sent_at`` after the leader sent the user
    summary DM (ms-97 / Phase 7-A / AC21).

    Leader-only via ``_require_trek_leader_session`` (= role + session_id
    grain on phase A+ trek、 pre-A invariance preserved)。 stamp 後は
    leader-digest tick が停止条件に入る (= completion_notified_at と
    summary_sent_at の両方 stamp で leader-digest fire を skip する、
    AC21)。

    Idempotent: re-calling stamps the latest timestamp but the leader-tick
    stop condition only cares about "non-null". Returns the updated trek
    doc so callers can echo ``meta.summary_sent_at`` back to the user.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
    meta = t.setdefault("meta", {})
    meta["summary_sent_at"] = trek_mod.utcnow_iso()
    t["updated_at"] = trek_mod.utcnow_iso()
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/migrate-members-session-keyed")
def migrate_trek_members_session_keyed_endpoint(
    trek_id: str,
    request: Request,
    dry_run: bool = False,
    user: dict = Depends(require_auth),
):
    """Migrate trek.members[] from user_id keyed to session_id keyed (ms-97 / e-2658 AC6).

    Leader-only via ``_require_trek_leader_session``. Applies the pure
    mutator ``trek_mod.migrate_members_to_session_keyed`` and persists
    via ``db.save_trek``. Live Firestore trek (= tk-XXXXXXXX) を CLI から
    cloud-mode で書き換えるための endpoint。 local-mode は CLI 側で別経路
    (= scripts/migrate_members_session_keyed.py local fallback)。

    Idempotency:
        Returns 409 if the trek is already past pre-A phase (= refuse
        double migration to keep the inverse rollback unambiguous).

    Args:
        dry_run: ``?dry_run=true`` で migrate 後の trek_doc を返すが
            ``db.save_trek`` は呼ばない (= 事前確認用)。
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
    try:
        trek_mod.migrate_members_to_session_keyed(t)
    except ValueError as exc:
        # Double-migration refusal (= already at phase A/B/C) returns 409
        # Conflict so the CLI / operator can distinguish "already done"
        # from a 4xx validation error.
        raise HTTPException(status_code=409, detail=str(exc))
    if not dry_run:
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


@app.post("/api/treks/{trek_id}/take-over")
def take_over_trek_endpoint(trek_id: str, body: TrekTakeOver,
                            user: dict = Depends(require_auth)):
    """Fresh-session leader take-over (ms-88 / e-2089).

    Unlike ``transfer-leader`` which requires the **live** prior leader
    session to authorize, take-over only checks the user-grain leader role
    — so a fresh bclaude session of the same user can recover when the
    original leader session is dead (= Mac restart, terminal closed,
    bclaude relaunched). This closes the dogfood Finding 1 silent-ack
    path: a dead ``leader_session_id`` was stale-but-non-null, scheduler
    fan-out kept aiming at it, and there was no way to re-bind without
    going through the dead session.

    Auth (user grain only, no from_session check):
      * Caller must be a joined member (= ``find_member`` non-null AND
        ``joined_at`` non-empty)
      * Caller must hold the ``leader`` role (= ``_require_trek_leader``)

    Idempotent: re-binding to the same ``session_id`` is a no-op
    (``trek_mod.transfer_leader`` already handles the equality case).
    """
    t = _load_trek_for_read(trek_id, user)
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    _require_trek_leader(t, user)
    # joined check is implicit in _require_trek_leader (= role only set
    # after joining), but we re-affirm here so the error message is precise
    # if a future refactor splits role from joined_at.
    if _auth_enabled:
        uid = user.get("sub") or ""
        member = trek_mod.find_member(t, user_id=uid)
        if not member or not member.get("joined_at"):
            raise HTTPException(
                status_code=403,
                detail="take-over requires a joined leader member",
            )
    try:
        trek_mod.transfer_leader(t, target_session_id=body.session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # ms-88 / e-2138 — take-over した fresh session は kickoff_pending=true に
    # 強制 reset。 前 session の kickoff が完了済でも、 新 session は別の plan
    # / worktree を持つ可能性があるので、 peer に再度 announce する義務を持つ。
    uid_for_reset = (user.get("sub") if _auth_enabled else "") or ""
    trek_mod.reset_kickoff_pending(
        t, session_id=body.session_id, user_id=uid_for_reset,
    )
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/succession-consent")
def trek_succession_consent_endpoint(
    trek_id: str,
    body: TrekSuccessionConsent,
    request: Request,
    user: dict = Depends(require_auth),
):
    """AC22 1 hop consent endpoint — candidate accepts / declines leadership.

    ms-97 / Phase 7-B / e-2684. Called by the candidate session that
    received a ``trek-leader-succession-consent`` DM. Strict caller gate:

      * Caller session_id (= ``X-Beacon-Session`` header) MUST equal
        ``meta.succession_pending_candidate`` (= the session the
        orchestrator nominated this cycle). Any other session is 403.
      * ``decision`` must be ``"accept"`` or ``"decline"``.

    On accept:
      * ``leader_session_id`` を caller_sid に transfer (= ``transfer_leader``)
      * ``session_history`` に role_at_join="leader" を upsert
      * ``meta.succession_pending_*`` / ``succession_declined`` /
        ``succession_escalated_at`` を全てクリア (= clear_succession_state)
      * caller の ``kickoff_status`` を pending=True に reset (= 新 leader
        は peer に再度 plan を宣言する義務、 ms-88 / e-2138 互換)

    On decline:
      * ``meta.succession_declined`` に caller_sid を追加 (= 次 tick で
        別 candidate を選ぶ)
      * ``meta.succession_pending_*`` をクリア
      * leader 役はそのまま (= 引き続き不応のままなら次 tick で再 nominate)
    """
    t = _load_trek_for_read(trek_id, user, request)
    decision = (body.decision or "").strip().lower()
    if decision not in ("accept", "decline"):
        raise HTTPException(
            status_code=400,
            detail="decision must be 'accept' or 'decline'",
        )
    caller_sid = ""
    if request is not None:
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
    if not caller_sid:
        raise HTTPException(
            status_code=400,
            detail="X-Beacon-Session header required for succession consent",
        )
    meta = t.get("meta") or {}
    pending_sid = (
        meta.get(trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY) or ""
    )
    if not pending_sid:
        raise HTTPException(
            status_code=400,
            detail="no succession candidate pending for this trek",
        )
    if caller_sid != pending_sid:
        raise HTTPException(
            status_code=403,
            detail=(
                "only the pending succession candidate can respond "
                f"(caller={caller_sid[:8]}.., pending={pending_sid[:8]}..)"
            ),
        )
    member = trek_mod.find_member(t, session_id=caller_sid)
    if member is None:
        raise HTTPException(
            status_code=403,
            detail="caller is not a member of this trek",
        )
    if decision == "accept":
        try:
            trek_mod.transfer_leader(t, target_session_id=caller_sid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Promote member role + record session_history at role_at_join=leader.
        member["role"] = "leader"
        trek_mod.upsert_session_history(
            t,
            session_id=caller_sid,
            user_id=member.get("user_id") or "",
            email=member.get("email") or "",
            role_at_join="leader",
        )
        # Fresh leader session must re-kickoff (= announce plan to peers).
        trek_mod.reset_kickoff_pending(
            t,
            session_id=caller_sid,
            user_id=member.get("user_id") or "",
        )
        trek_mod.clear_succession_state(t)
        db.save_trek(trek_id, t)
        return t
    # Decline path
    trek_mod.record_succession_decline(t, caller_sid)
    db.save_trek(trek_id, t)
    return t


@app.post("/api/treks/{trek_id}/kickoff")
def trek_kickoff_endpoint(trek_id: str, body: TrekKickoff,
                          user: dict = Depends(require_auth)):
    """Mark a session's kickoff DM as sent (ms-88 / e-2138).

    Called by ``/beacon-trek-pulse`` Skill's Step 0 after it has generated
    and sent the kickoff DM to the trek leader. Until this endpoint is
    called, ``pulse-ack`` rejects further progress for the same session
    with HTTP 400 ``kickoff_required``.

    Auth: caller must be a trek member at the user grain. The endpoint
    itself does not verify the DM was actually sent — that is a Skill /
    audit-side concern. The structural enforcement is "no pulse-ack
    progress until kickoff endpoint is called", which is sufficient to
    force the executor through the Skill body's Step 0.

    Returns the updated per-session kickoff_status entry so the Skill can
    echo "stamped" back to the user.
    """
    t = _load_trek_for_read(trek_id, user)
    if _auth_enabled:
        uid = user.get("sub") or ""
        if not trek_mod.find_member(t, user_id=uid):
            raise HTTPException(
                status_code=403,
                detail="only trek members can mark kickoff completed",
            )
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    try:
        uid_for_stamp = (user.get("sub") if _auth_enabled else "") or ""
        trek_mod.mark_kickoff_completed(
            t,
            session_id=body.session_id,
            user_id=uid_for_stamp,
            kickoff_dm_event_id=body.kickoff_dm_event_id or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return (t.get(trek_mod.KICKOFF_HISTORY_KEY) or {}).get(body.session_id) or {}


@app.get("/api/treks/{trek_id}/kickoff")
def trek_kickoff_status_endpoint(trek_id: str,
                                  user: dict = Depends(require_auth)):
    """Per-session kickoff summary (ms-88 / e-2138)."""
    t = _load_trek_for_read(trek_id, user)
    if _auth_enabled:
        uid = user.get("sub") or ""
        if not trek_mod.find_member(t, user_id=uid):
            raise HTTPException(
                status_code=403,
                detail="only trek members can read kickoff status",
            )
    return trek_mod.summarize_kickoff_status(t)


# ---------------------------------------------------------------------------
# ms-97 / Phase 7-C / AC24, e-2603 — blanket scope approval endpoints.
# Leader-only. The category string is validated by trek_mod helpers.
# ---------------------------------------------------------------------------

@app.post("/api/treks/{trek_id}/blanket-approve")
def trek_blanket_approve_endpoint(trek_id: str, body: TrekBlanketCategory,
                                  request: Request,
                                  user: dict = Depends(require_auth)):
    """Register ``category`` as a blanket pre-approval (AC24).

    Subsequent scope-add requests whose entry matches the category will
    auto-commit (= bypass pending stage). Leader-only.
    """
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
    try:
        trek_mod.add_blanket_approval(t, body.category)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return {
        "trek_id": trek_id,
        "blanket_scope_approvals": trek_mod.list_blanket_approvals(t),
    }


@app.post("/api/treks/{trek_id}/blanket-revoke")
def trek_blanket_revoke_endpoint(trek_id: str, body: TrekBlanketCategory,
                                 request: Request,
                                 user: dict = Depends(require_auth)):
    """Remove ``category`` from blanket pre-approvals (AC24). Leader-only."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_leader_session(t, user, request)
    try:
        trek_mod.remove_blanket_approval(t, body.category)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return {
        "trek_id": trek_id,
        "blanket_scope_approvals": trek_mod.list_blanket_approvals(t),
    }


# ---------------------------------------------------------------------------
# ms-97 / Phase 7-C / AC26 + AC27, e-2603 — structured logs read API.
# Write paths live next to the relevant endpoints (= tick, pulse-ack,
# task-state) and call ``db.append_trek_log`` directly.
# ---------------------------------------------------------------------------

def _append_trek_log_safe(trek_id: str, entry: dict) -> None:
    """Append a trek log entry, swallowing any backend error.

    Logs are observability, not a transactional concern — a failure to
    persist the log row must never break the original write (= tick /
    pulse-ack / task-state). Errors are printed to stderr for visibility.
    """
    try:
        db.append_trek_log(trek_id, entry)
    except Exception as exc:
        print(
            f"warn[ms-97 e-2603]: append_trek_log failed for trek "
            f"{trek_id}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


@app.get("/api/treks/{trek_id}/logs.jsonl")
def trek_logs_jsonl_endpoint(trek_id: str, request: Request,
                             since: str = "",
                             user: dict = Depends(require_auth)):
    """Stream the trek's structured logs as NDJSON (AC27).

    One JSON object per line, ascending by ``created_at``. RBAC: any
    joined member (= same surface as ``_load_trek_for_read``). Optional
    ``?since=<ISO8601>`` filters older rows so callers can resume.
    """
    t = _load_trek_for_read(trek_id, user, request)
    # Cap at a generous limit to keep streaming bounded; clients that
    # need everything can page via ``since``.
    rows = db.list_trek_logs(trek_id, limit=10000, since=since or "")
    # FastAPI StreamingResponse with NDJSON content type.
    from fastapi.responses import StreamingResponse
    import json as _json

    def _generator():
        for row in rows:
            try:
                yield _json.dumps(row, ensure_ascii=False) + "\n"
            except Exception:
                # Skip un-serialisable rows defensively (= one bad row
                # must not break the stream).
                continue
    # Make sure auth side-effects on ``t`` aren't accidentally serialised.
    del t  # noqa: F841
    return StreamingResponse(_generator(), media_type="application/x-ndjson")


@app.post("/api/treks/{trek_id}/pulse-ack")
def trek_pulse_ack_endpoint(trek_id: str, body: TrekPulseAck,
                            user: dict = Depends(require_auth)):
    """Record /beacon-trek-pulse Skill invocation (ms-88 / e-2106).

    Layer 2 (= observability) of the 3-layer trek autonomy harness
    (CORE doc 5nfTSmCDVUzD4SLzIhI5). The Skill calls this endpoint as its
    very first Step so the server has **ground truth** about whether the
    Skill actually fired in response to a scheduler tick. This closes the
    "Skill marker visible in executor terminal" verification hole that
    dogfood (= tk-40b0b27c) could not check directly.

    Auth: caller must be a trek member (= user grain, same as task-state).
    The Skill is invoked by the executor session, so the calling user is
    naturally a joined member.

    Returns the updated pulse_acks entry for the caller's session so the
    Skill can echo the recorded state back to the user.
    """
    t = _load_trek_for_read(trek_id, user)
    if _auth_enabled:
        uid = user.get("sub") or ""
        if not trek_mod.find_member(t, user_id=uid):
            raise HTTPException(
                status_code=403,
                detail="only trek members can pulse-ack",
            )
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    # ms-88 / e-2138 — Kickoff Ritual physical gate. 自セッションが kickoff DM を
    # 送信していなければ pulse-ack を拒否し、 Skill side の Step 0 (= kickoff DM
    # 自動生成 + leader 宛送信) を走らせる。 narrative ではなく server-side
    # validation で「kickoff 未送信なら progress 不可」 を物理的に閉じる。
    if trek_mod.get_kickoff_pending(t, session_id=body.session_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "kickoff_required: this session has not yet sent its kickoff DM. "
                "Run /beacon-trek-pulse Step 0 to send the kickoff DM to the trek "
                "leader (= self-info + plan + worktree + non-touch range), then "
                "POST /api/treks/{trek_id}/kickoff to mark it completed, then "
                "retry pulse-ack."
            ),
        )
    try:
        trek_mod.record_pulse_ack(
            t,
            session_id=body.session_id,
            picked_choice=body.picked_choice or "",
            note=body.note or "",
            # ms-92 / e-2165 — structured payload. Server passes them
            # through as-is; record_pulse_ack handles normalisation +
            # truncation. Pre-e-2165 bridges omit these (Pydantic
            # default-fills empty values), so behaviour is unchanged
            # for legacy callers.
            state_summary=body.state_summary or "",
            blockers=body.blockers or [],
            needs_leader_judgment=bool(body.needs_leader_judgment),
            time_on_task_seconds=int(body.time_on_task_seconds or 0),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    # ms-97 / Phase 7-C / AC26 — pulse-ack log row. The Skill marker
    # (= record_pulse_ack already mutated the trek doc); the log row
    # is the durable observability artefact.
    _append_trek_log_safe(trek_id, {
        "kind": "pulse-ack",
        "session_id": body.session_id,
        "payload": {
            "picked_choice": body.picked_choice or "",
            "note": (body.note or "")[:500],
            "state_summary": (body.state_summary or "")[:500],
            "blockers": list(body.blockers or [])[:20],
            "needs_leader_judgment": bool(body.needs_leader_judgment),
            "time_on_task_seconds": int(body.time_on_task_seconds or 0),
        },
        "created_at": trek_mod.utcnow_iso(),
    })
    return (t.get("pulse_acks") or {}).get(body.session_id) or {}


@app.post("/api/treks/{trek_id}/extend-ttl")
def extend_trek_task_ttl_endpoint(trek_id: str, body: TrekExtendTtl,
                                  user: dict = Depends(require_auth)):
    """Postpone the TTL safety net deadline on a single task (ms-95 / e-2308).

    Leader-side primitive for the Agent-tool subagent dispatch path.
    When ``/beacon-dispatch`` launches a subagent that cannot stamp
    ``last_activity_at`` itself (= different ``session_id``, not joined
    to the trek), the leader calls this endpoint to push the auto-stall
    deadline forward by ``minutes`` so the TTL check skips the task
    while delegation is in progress. ``minutes=0`` (or negative) clears
    the extension and lets normal TTL semantics resume.

    Auth: caller must be a trek member. Leader-only is **not** enforced
    so that any joined member can call this on their own behalf (= a
    fork session dispatching a sub-agent for its task without leader
    handoff still has access). Server-side write goes through
    ``trek_mod.extend_task_ttl`` which validates the integer cast.

    Returns the updated ``task_states[task_id]`` entry so the caller
    can echo the new ``ttl_extended_until`` back to the user.
    """
    t = _load_trek_for_read(trek_id, user)
    if _auth_enabled:
        uid = user.get("sub") or ""
        if not trek_mod.find_member(t, user_id=uid):
            raise HTTPException(
                status_code=403,
                detail="only trek members can extend task TTL",
            )
    if not body.task_id:
        raise HTTPException(status_code=400, detail="task_id required")
    try:
        trek_mod.extend_task_ttl(
            t,
            task_id=body.task_id,
            minutes=body.minutes,
            reason=body.reason or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    return (t.get("task_states") or {}).get(body.task_id) or {}


@app.get("/api/treks/{trek_id}/pulse-acks")
def list_trek_pulse_acks_endpoint(trek_id: str,
                                  user: dict = Depends(require_auth)):
    """Per-session pulse-ack summary for dashboards (ms-88 / e-2108).

    Returns the compact summary built by ``trek_mod.summarize_pulse_acks``
    so the Phase 4 Trek detail page can render compliance widgets without
    pulling the full trek doc. Any joined member may read.
    """
    t = _load_trek_for_read(trek_id, user)
    if _auth_enabled:
        uid = user.get("sub") or ""
        if not trek_mod.find_member(t, user_id=uid):
            raise HTTPException(
                status_code=403,
                detail="only trek members can read pulse-ack stats",
            )
    return trek_mod.summarize_pulse_acks(t)


@app.patch("/api/treks/{trek_id}/task-state")
def set_trek_task_state_endpoint(trek_id: str, body: TrekTaskStateSet,
                                 request: Request,
                                 user: dict = Depends(require_auth)):
    """Stamp Trek-internal task state (ms-75 / e-2048).

    Executor sessions call this after each commit / chunk completion
    to declare whether they are still ``working`` on the task, have
    reached ``done``, or need ``waiting-review`` (= user judgment).

    Side effects:
      * Update ``trek_doc.task_states[task_id]`` (= validated transition)
      * Persist trek doc
      * When the stamped state is terminal (= done / waiting-review),
        emit a one-time ``trek-task-review`` bus event addressed to the
        leader's home project so ``/beacon-trek-review`` Skill can
        surface the transition to the human leader (= push model that
        spares the leader from polling).

    Authorisation: caller must be a trek member (= identity at user
    grain). Leader-only is not required — any member session can stamp
    on behalf of the work they performed.
    """
    t = _load_trek_for_read(trek_id, user, request)
    # Member check. Determine the calling session_id (best-effort —
    # bridges include X-Beacon-Session header; CLI/curl callers may
    # omit it). ms-97 / e-2658 Phase 1 (AC6) — phase A+ trek の時は
    # session_id grain で member check、 pre-A は user_id grain。
    user_id = user.get("sub") or ""
    caller_sid = request.headers.get("X-Beacon-Session", "") or ""
    if not _trek_find_member_dual(t, user_id=user_id, session_id=caller_sid):
        raise HTTPException(
            status_code=403,
            detail="only trek members can stamp task state",
        )
    if not body.task_id:
        raise HTTPException(status_code=400, detail="task_id required")
    try:
        trek_mod.validate_task_state(body.state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # ms-97 / e-2650 — phantom done 構造防御。 Trek slot を done に flip
    # する前に、 真値源である project pool の task status が done である
    # ことを必須条件として check する (= 「view 側だけ done になる経路」 を
    # server で構造的に reject)。 状態が done 以外なら check skip (= todo
    # / working / *_review への遷移は従来通り 5-state machine だけで判定、
    # done だけが evidence-backed terminal なので明示防御の対象)。
    # 旧コード (= 2026-06-28 以前) ではこの check が無く、 e-710 のような
    # 「commit ゼロで Trek slot だけ done」 が成立していた。 ms-97 SPEC
    # AC10 / AC30 補強の構造実装、 詳細は lib/trek.py
    # ``check_slot_done_precondition`` の docstring を参照。
    if body.state == "done":
        allowed, reason_code, message = trek_mod.check_slot_done_precondition(
            t,
            task_id=body.task_id,
            get_project=db.get_project,
        )
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": reason_code,
                    "message": message,
                    "trek_id": trek_id,
                    "task_id": body.task_id,
                },
            )
    try:
        trek_mod.set_task_state(
            t,
            task_id=body.task_id,
            state=body.state,
            updated_by_session_id=caller_sid,
            note=body.note or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.save_trek(trek_id, t)
    # If the new state is terminal, fan out a review-required notice to
    # the leader. The leader's review Skill (/beacon-trek-review)
    # surfaces this as a forced 3-choice action. We use the trek's home
    # project (= first scope entry's project) as the bus event target,
    # and address it to the current leader_session_id so only the
    # responsible session sees it (= no project-wide broadcast).
    # ms-97 / e-2706 (AC1) — review notify trigger 拡張。
    # 旧コードは `TERMINAL_TASK_STATES` (= done / user_review) のみで判定し、
    # `leader_review` 遷移時に trek-task-review event が永久に発火しない構造
    # bug を持っていた (= ms-88 e-2107 で waiting-review → leader_review に
    # 5-state 移行した際の check 条件 drift)。 REVIEW_TRIGGER_STATES (= done /
    # user_review / leader_review) に置き換えて leader への review 通知を
    # 正常化する。 outcome log row は元来 terminal 状態にのみ書く design なので
    # 同じ trigger 集合に乗せる (= leader_review も「leader 判断要請」 という
    # 意味で 1 つの outcome event として記録に値する)。
    if body.state in trek_mod.REVIEW_TRIGGER_STATES:
        # ms-97 / Phase 7-C / AC26 — outcome log row at review-trigger state.
        # Recorded BEFORE the DM fanout so the log row exists even if
        # leader notification fails (= durable audit trail).
        _append_trek_log_safe(trek_id, {
            "kind": "outcome",
            "session_id": caller_sid,
            "payload": {
                "task_id": body.task_id,
                "state": body.state,
                "note": (body.note or "")[:500],
            },
            "created_at": trek_mod.utcnow_iso(),
        })
        leader_sid = t.get("leader_session_id") or ""
        scope = t.get("scope") or []
        target_pid = scope[0].get("project") if scope else ""
        # ms-88 / e-2168 — self-loop suppress: leader が自分で stamp した
        # transition では、 leader 宛 review event を mint しない。 「自分が判断
        # したものを自分にもう一度 review 依頼する」 ループによる envelope mint +
        # DM 配送 + 通知 noise を構造的に排除。 state 遷移自体は normal に
        # 保存されており、 suppress した事実だけ meta.review_suppressions に
        # 残して後から audit 可能化する。
        self_judgment = bool(
            caller_sid and leader_sid and caller_sid == leader_sid
        )
        if self_judgment:
            meta = t.setdefault("meta", {})
            suppressions = meta.setdefault("review_suppressions", [])
            suppressions.append({
                "task_id": body.task_id,
                "state": body.state,
                "suppression_reason": "self_judgment",
                "suppressed_at": trek_mod.utcnow_iso(),
                "caller_session_id": caller_sid,
            })
            try:
                db.save_trek(trek_id, t)
            except Exception:
                # Best-effort audit trail; suppression decision still
                # holds even if save fails.
                pass
        elif leader_sid and target_pid:
            try:
                envelope = envelope_mod.issue_t1_system_envelope(
                    project_id=target_pid,
                    trek_id=trek_id,
                    actions_authorized=["trek.task_review"],
                    data_class="free",
                    ttl_seconds=3600,
                )
            except Exception:
                envelope = None
            review_payload = {
                "kind": "trek-task-review",
                "trek_id": trek_id,
                "task_id": body.task_id,
                "state": body.state,
                "note": body.note or "",
                "updated_by_session_id": caller_sid,
                "recipient_session_id": leader_sid,
                "body": (
                    f"[Trek task review required] trek_id={trek_id} "
                    f"task_id={body.task_id} state={body.state}\n"
                    f"executor note: {(body.note or '').strip()[:200]}\n"
                    f"次の action: /beacon-trek-review {trek_id} {body.task_id} "
                    f"で approve / re-work / forward-to-user の 3 択を実行してください。"
                ),
                "created_at": trek_mod.utcnow_iso(),
            }
            bus_data = {
                "channel": "trek-task-review",
                "sender_session_id": "",
                "payload": review_payload,
                "envelope": envelope,
                "delivery": "auto-execute",
                "created_at": trek_mod.utcnow_iso(),
            }
            try:
                db.append_bus_event(target_pid, bus_data)
            except Exception:
                # Best-effort notification; the leader can still discover
                # the terminal state via `beacon trek show` polling.
                pass
    return t


@app.post("/api/treks/{trek_id}/task-add")
def add_trek_task_endpoint(
    trek_id: str,
    body: TrekTaskAddRequest,
    request: Request,
    user: dict = Depends(require_auth),
):
    """Cross-project task add through Trek scope (ms-92 / e-2141).

    Walks ``trek.check_trek_task_add_allowed(t, target_project,
    target_milestone)`` first. On allowed: writes the task to the
    target project's milestone via the existing ``core.task_add``
    primitive, stamping ``meta.trek_id`` for audit traceability. On
    rejected: 403 with the scope-guard reason code so callers can
    show the right remediation hint.

    Authorisation: caller must be a trek member (= same as
    ``task-state``). The scope-guard does the cross-project
    authorisation; the membership check is the "are you allowed to
    drive this Trek at all" gate.

    Reason codes returned in the 403 detail (= mirror the lib/trek.py
    constants so the CLI / docs share a vocabulary):
      * ``project_not_in_scope`` — target_project absent from scope
      * ``milestone_not_in_scope`` — project present, MS not enumerated
      * ``scope_only_has_task_narrowing`` — AC #4 of e-2141 (= MS-grain
        enforcement; task-level scope entries don't sprout sideways)
    """
    t = _load_trek_for_read(trek_id, user)
    user_id = user.get("sub") or ""
    if _auth_enabled and not trek_mod.find_member(t, user_id=user_id):
        raise HTTPException(
            status_code=403,
            detail="only trek members can add tasks through trek scope",
        )
    if not body.target_project:
        raise HTTPException(status_code=400, detail="target_project required")
    if not body.target_milestone:
        raise HTTPException(status_code=400, detail="target_milestone required")
    if not body.description:
        raise HTTPException(status_code=400, detail="description required")
    if not body.target_milestone.startswith("ms-"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"target_milestone {body.target_milestone!r} must start with "
                "'ms-' (operations / single tasks are not valid task-add "
                "targets — see SPEC ms-92 e-2141 AC #4 MS-grain enforcement)"
            ),
        )
    # ms-95 / Trek task-add slug ↔ full project_id resolution.
    # Two storage forms can appear in trek scope: full id (= canonical,
    # what apply_operation needs) or slug (= what users type at
    # ``beacon trek plan --add-scope <slug>:<ms>``). Canonicalise both
    # the request side and an in-memory copy of the scope so the scope
    # match runs on equal footing and the downstream apply_op call
    # always receives a real project_id. Without this the bug from PE
    # + LPS dogfood reports manifests as:
    #   * slug stored + slug request → scope match ok, then 500
    #     (LookupError from apply_operation seeing an unknown id)
    #   * slug stored + full id request → 403 project_not_in_scope
    #     (literal string equality miss)
    requesting_user_id = user.get("sub") or ""
    resolved_target_project = _resolve_canonical_project_id(
        body.target_project, user_id=requesting_user_id,
    )
    if resolved_target_project is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"target_project {body.target_project!r} not found or "
                "ambiguous (no exact full project_id match and no unique "
                "slug expansion). Pass the full project_id (e.g. "
                "'<slug>-<hex6>') if multiple projects share the slug."
            ),
        )
    # Build a scope-copy where each entry's ``project`` field is
    # canonicalised. Unresolvable entries (= stale slug for a deleted
    # project, or a slug shared by multiple of the caller's projects)
    # are kept as-is so they cleanly fail the scope match instead of
    # crashing the endpoint.
    canonical_scope: list[dict] = []
    for s in t.get("scope") or []:
        original = s.get("project") or ""
        resolved = (
            _resolve_canonical_project_id(original, user_id=requesting_user_id)
            or original
        )
        canonical_scope.append({**s, "project": resolved})
    canonical_t = {**t, "scope": canonical_scope}
    # ms-97 Phase 4 / AC14 — executor の home project check.
    #
    # Trek scope を介した slot 起票は、 caller (= executor) が「自分の
    # home project に slot を生やす」 経路に限定する。 すなわち caller
    # の live session_id が登録されている scope project = 起票先 project
    # でなければならない。 違反例 (= executor A が executor B の project に
    # slot を生やす) は executor 間で意図しない作業押し付けを生むので、
    # server side で物理的に塞ぐ (= プロンプト notice ではなく code 制約)。
    #
    # 適用範囲: phase A+ trek のみ (= is_session_id_keyed True)。
    # Pre-A trek は session_id を持たない時代の trek なので、 caller
    # session_id を home に逆引きする手立てが無い → legacy 振る舞い継続
    # (= scope guard だけが authorisation の砦)。
    #
    # X-Beacon-Session header が空の caller (= CLI smoke / 古い client)
    # は home check を skip。 header は opt-in、 hard-require には
    # しない (= AC13 の helper と同じ stance)。
    #
    # leader (= role == "leader") は home check 免除。 leader は全
    # scope projects に対して slot 起票する authority を持つ (=
    # cross-project authority は leader / scope policy 側で行使)。
    if _auth_enabled and trek_mod.is_session_id_keyed(canonical_t):
        caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        if caller_sid:
            caller_role = _trek_member_role(
                canonical_t, user_id=user_id, session_id=caller_sid,
            )
            if caller_role != "leader":
                scope_project_ids: list[str] = []
                for s in canonical_scope:
                    pid = s.get("project") or ""
                    if pid and pid not in scope_project_ids:
                        scope_project_ids.append(pid)
                home_pid = trek_mod.resolve_session_home_project(
                    caller_sid,
                    scope_project_ids,
                    db.list_sessions,
                )
                if not home_pid:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "executor session is not registered in any "
                            "scope project; cannot resolve home project "
                            "for slot 起票 (AC14)"
                        ),
                    )
                if home_pid != resolved_target_project:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"executor can only add a slot in their home "
                            f"project (home={home_pid}, "
                            f"target={resolved_target_project}); leader "
                            "must originate cross-project slots (AC14)"
                        ),
                    )
    allowed, reason = trek_mod.check_trek_task_add_allowed(
        canonical_t,
        target_project=resolved_target_project,
        target_milestone=body.target_milestone,
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"trek scope rejects task add: {reason}",
        )
    # Scope check passed. Write to the target project via the existing
    # task_add op. We stamp meta.trek_id on the entry (= audit trail).
    author = _resolve_author(user)

    def op(data: dict):
        # ms-81 / e-1916 write-status gate is intentionally NOT applied
        # here — the Trek scope authorisation is the relevant gate for
        # cross-project writes, and Trek scope only enumerates active
        # work. If the target MS is in a bad write status the entry-add
        # CLI surfaces that locally; cross-project we trust the Trek
        # scope owner's intent.
        try:
            eid = core.task_add(
                data, body.target_milestone, body.description,
                entry_type=body.type, priority=body.priority,
                motivation=body.motivation,
                acceptance_criteria=body.acceptance_criteria,
                author=author,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Stamp meta.trek_id on the freshly-created entry so future
        # /beacon-retrospect queries can answer "which tasks were
        # sprouted via Trek X". core.task_add returns just the id, so
        # we re-find the entry and patch its meta.
        find_result = core.find_entry(data, eid)
        if find_result:
            _, _, entry, _ = find_result
            meta = entry.setdefault("meta", {})
            meta["trek_id"] = trek_id
            meta["origin"] = "trek.task_add"
        return data, {
            "entry_id": eid,
            "project_id": resolved_target_project,
            "milestone_id": body.target_milestone,
            "trek_id": trek_id,
        }
    return _apply_op_and_broadcast(
        resolved_target_project,
        op,
        op_name="entry.create.trek_scope",
        actor=user.get("sub", ""),
    )


class TrekReconcileRequest(BaseModel):
    apply: bool = False


@app.post("/api/treks/{trek_id}/reconcile")
def reconcile_trek_endpoint(trek_id: str, body: TrekReconcileRequest,
                            user: dict = Depends(require_auth)):
    """ms-88 / e-2167 — Trek task_states を task pool と整合させる reconcile.

    Trek 内に既に stamp 済の各 task について scope project の task pool を
    引いて、 「pool 上は done だが Trek stamp は non-terminal で残ってる」
    stuck 状態を一覧化する。 ``apply=true`` なら mirror で done に書き換え。
    ``apply=false`` (= default) は dry-run、 diff だけ返す。

    Authorisation: caller must be a trek member.
    """
    t = _load_trek_for_read(trek_id, user)
    user_id = user.get("sub") or ""
    if not trek_mod.find_member(t, user_id=user_id):
        raise HTTPException(
            status_code=403,
            detail="only trek members can reconcile task state",
        )
    states = t.get("task_states") or {}
    scope_project_ids = [
        s.get("project") for s in t.get("scope") or [] if s.get("project")
    ]
    # Build a single dict of entry_id → pool_status across scope projects.
    pool_status: dict[str, str] = {}
    for pid in scope_project_ids:
        try:
            project_data = db.get_project(pid)
        except Exception:
            continue
        if not project_data:
            continue
        for entry_id in states.keys():
            if entry_id in pool_status:
                continue
            found = core.find_entry(project_data, entry_id)
            if found:
                _, _, entry, _ = found
                pool_status[entry_id] = (entry or {}).get("status") or ""
    # Compute diff: pool done but trek state non-terminal.
    diff: list[dict] = []
    for entry_id, entry in states.items():
        try:
            current_state = trek_mod.get_task_state(t, entry_id)
        except Exception:
            current_state = (entry or {}).get("state") or ""
        pool = pool_status.get(entry_id, "")
        if pool == "done" and current_state not in trek_mod.TERMINAL_TASK_STATES:
            diff.append({
                "entry_id": entry_id,
                "trek_state": current_state,
                "pool_status": pool,
                "would_change_to": "done",
            })
    applied: list[str] = []
    if body.apply and diff:
        now_iso = trek_mod.utcnow_iso()
        for item in diff:
            entry_id = item["entry_id"]
            existing = states.get(entry_id) or {}
            states[entry_id] = {
                **existing,
                "state": "done",
                "updated_at": now_iso,
                "last_activity_at": now_iso,
                "updated_by_session_id": "task-pool-mirror",
                "note": (
                    "task pool で done 化、 mirror 同期 (= ms-88 / e-2167 "
                    "reconcile)"
                ),
            }
            applied.append(entry_id)
        t["task_states"] = states
        t["updated_at"] = now_iso
        try:
            db.save_trek(trek_id, t)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"trek save failed: {type(e).__name__}: {e}",
            )
    return {
        "trek_id": trek_id,
        "applied": body.apply,
        "diff": diff,
        "applied_entry_ids": applied,
    }


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


# ---------------------------------------------------------------------------
# ms-86 / e-2248 — Trek scope aggregate endpoints for milestones / operations /
# tasks. The Trek detail UI needs to render MS / Op / Task entries from every
# project the Trek's scope reaches, without the client knowing which project
# each entry belongs to in advance. Mirrors the
# ``/api/treks/{trek_id}/documents`` pattern (= same /documents shape: iterate
# scope, walk each project's data, dedupe, attach source ``project_id``).
#
# Scope semantics (= identical to ``_list_related_treks`` match rule, mirror):
#   * ``{"project": pid}`` (= project-wide) → every MS / Op / Task in that
#     project belongs to the Trek
#   * ``{"project": pid, "milestone": ms_id}`` → just that MS (and all its
#     child tasks for the /tasks endpoint)
#   * ``{"project": pid, "operation": op_id}`` → just that Op (and all its
#     child tasks for the /tasks endpoint)
#   * ``{"project": pid, "task": entry_id}`` → just that task (contributes
#     to /tasks only, not /milestones or /operations)
#
# Why the duplicate walk vs. asking the client to iterate scope[] itself:
# (1) one round-trip per concept beats N round-trips per project, (2) the
# server already has the disclosure-policy check (= ms-63) wired into
# ``_load(pid, user)`` so cross-project visibility is enforced server-side,
# (3) ``state.project`` decoupling on the client (= e-2240 SPEC 設計方針 5)
# requires the lookup to come from a Trek-keyed endpoint, not a project-keyed
# one. See SPEC ``IgCFK8I34cBfPco3UE06`` for the full design rationale.
# ---------------------------------------------------------------------------


def _trek_scope_by_project(t: dict) -> dict[str, list[dict]]:
    """Group ``t['scope']`` entries by their ``project`` field.

    Returns ``{project_id: [scope_entry, ...]}``. Entries missing the
    ``project`` field are silently dropped — they violate
    ``normalize_scope_entry`` and shouldn't exist on disk, but old data
    could; we skip rather than 500.
    """
    by_project: dict[str, list[dict]] = {}
    for entry in t.get("scope") or []:
        pid = entry.get("project") or ""
        if not pid:
            continue
        by_project.setdefault(pid, []).append(entry)
    return by_project


def _scope_contributes(entries: list[dict], *, kind: str) -> tuple[bool, set[str]]:
    """Decide what a project's scope entries contribute for one entity kind.

    ``kind`` is ``"milestone"`` / ``"operation"`` / ``"task"``. Returns
    ``(include_all, narrow_ids)``:

    * ``include_all=True`` when any scope entry is project-wide (= no
      narrowing key) — the whole project's entities of this kind belong.
    * ``include_all=False, narrow_ids={...}`` when every matching entry
      narrows; ``narrow_ids`` is the union of the matching IDs.

    For ``kind="task"`` the contribution also widens via milestone /
    operation narrowing (= "all tasks under that MS / Op"). That widening
    is handled by the caller; this helper only reports direct task IDs.
    """
    if not entries:
        return False, set()
    narrow_ids: set[str] = set()
    for entry in entries:
        ms = entry.get("milestone") or ""
        op = entry.get("operation") or ""
        task = entry.get("task") or ""
        if not (ms or op or task):
            # Project-wide entry — whole project is in.
            return True, set()
        if kind == "milestone" and ms:
            narrow_ids.add(ms)
        elif kind == "operation" and op:
            narrow_ids.add(op)
        elif kind == "task" and task:
            narrow_ids.add(task)
    return False, narrow_ids


@app.get("/api/treks/{trek_id}/milestones")
def list_trek_milestones_endpoint(trek_id: str,
                                  user: dict = Depends(require_auth)):
    """Return milestones in this Trek's scope, walking each scope project.

    Mirrors the /documents pattern: iterate scope → project_ids, load each
    project, pick milestones in-scope (= project-wide entries include all,
    narrowed entries pick by milestone ID), dedupe across projects, attach
    source ``project_id`` on each entry for UI deep-linking.
    """
    t = _load_trek_for_read(trek_id, user)
    by_project = _trek_scope_by_project(t)
    out: list = []
    seen: set[tuple[str, str]] = set()
    for pid, entries in by_project.items():
        try:
            project = _load(pid, user)
        except Exception:
            # Stale scope entry pointing at a project the caller cannot
            # read; skip silently so a single bad ref doesn't break the
            # whole listing (= same regime as /documents).
            continue
        include_all, narrow_ids = _scope_contributes(entries, kind="milestone")
        if not include_all and not narrow_ids:
            # Project is in scope only through operation / task narrowing,
            # so the /milestones aggregate has nothing to contribute here.
            continue
        for ms in project.get("milestones", []) or []:
            ms_id = ms.get("id") or ""
            if not include_all and ms_id not in narrow_ids:
                continue
            key = (pid, ms_id)
            if key in seen:
                continue
            seen.add(key)
            ms_out = dict(ms)
            ms_out["project_id"] = pid
            out.append(ms_out)
    return out


@app.get("/api/treks/{trek_id}/operations")
def list_trek_operations_endpoint(trek_id: str,
                                  user: dict = Depends(require_auth)):
    """Return Operations in this Trek's scope. Mirrors /milestones."""
    t = _load_trek_for_read(trek_id, user)
    by_project = _trek_scope_by_project(t)
    out: list = []
    seen: set[tuple[str, str]] = set()
    for pid, entries in by_project.items():
        try:
            project = _load(pid, user)
        except Exception:
            continue
        include_all, narrow_ids = _scope_contributes(entries, kind="operation")
        if not include_all and not narrow_ids:
            continue
        for op in project.get("operations", []) or []:
            op_id = op.get("id") or ""
            if not include_all and op_id not in narrow_ids:
                continue
            key = (pid, op_id)
            if key in seen:
                continue
            seen.add(key)
            op_out = dict(op)
            op_out["project_id"] = pid
            out.append(op_out)
    return out


@app.get("/api/treks/{trek_id}/tasks")
def list_trek_tasks_endpoint(trek_id: str,
                             user: dict = Depends(require_auth)):
    """Return tasks in this Trek's scope, walking MS / Op containers.

    Task scope sources (= union across all scope entries):
      * project-wide entry → every task in the project
      * milestone entry → every task under that MS
      * operation entry → every task under that Op
      * task entry → that single task

    Each output task carries source ``project_id`` and the immediate
    container reference (``milestone_id`` or ``operation_id``) so the UI
    can render the parent path without re-resolving.
    """
    t = _load_trek_for_read(trek_id, user)
    by_project = _trek_scope_by_project(t)
    out: list = []
    seen: set[tuple[str, str]] = set()
    for pid, entries in by_project.items():
        try:
            project = _load(pid, user)
        except Exception:
            continue
        # Compute per-project inclusion sets for task collection.
        include_all_tasks, _ = _scope_contributes(entries, kind="task")
        # Project-wide scope (no narrowing) also implies all tasks.
        if not include_all_tasks:
            for entry in entries:
                if not (entry.get("milestone") or entry.get("operation")
                        or entry.get("task")):
                    include_all_tasks = True
                    break
        _, ms_narrow = _scope_contributes(entries, kind="milestone")
        _, op_narrow = _scope_contributes(entries, kind="operation")
        _, task_narrow = _scope_contributes(entries, kind="task")

        def _emit(task: dict, *, milestone_id: str = "",
                  operation_id: str = "") -> None:
            tid = task.get("id") or ""
            if not tid:
                return
            key = (pid, tid)
            if key in seen:
                return
            seen.add(key)
            row = dict(task)
            row["project_id"] = pid
            if milestone_id:
                row["milestone_id"] = milestone_id
            if operation_id:
                row["operation_id"] = operation_id
            out.append(row)

        for ms in project.get("milestones", []) or []:
            ms_id = ms.get("id") or ""
            ms_wanted = (
                include_all_tasks or (ms_id and ms_id in ms_narrow)
            )
            for entry in ms.get("entries", []) or []:
                if entry.get("type") != "task":
                    continue
                tid = entry.get("id") or ""
                if ms_wanted or (tid and tid in task_narrow):
                    _emit(entry, milestone_id=ms_id)
        for op in project.get("operations", []) or []:
            op_id = op.get("id") or ""
            op_wanted = (
                include_all_tasks or (op_id and op_id in op_narrow)
            )
            for entry in op.get("entries", []) or []:
                if entry.get("type") != "task":
                    continue
                tid = entry.get("id") or ""
                if op_wanted or (tid and tid in task_narrow):
                    _emit(entry, operation_id=op_id)
    return out


# ---------------------------------------------------------------------------
# ms-95 / e-2640 — Trek scope-entries endpoint (= MS detail body hydration
# for the Trek detail view). The Trek detail UI used to call
# ``GET /api/projects/{state.projectId}/milestones/{ms_id}/entries`` from
# ``fetchMilestoneEntries``, which embeds the *currently selected project*
# in the URL. For cross-project Treks the MS lives in a different project
# than the user's current cwd / selected project, so the call resolves to
# the wrong project, returns 404, and the UI renders the MS card as
# "milestone not loaded".
#
# This endpoint mirrors the ``/milestones`` / ``/tasks`` aggregate pattern
# but ships the full ``entries[]`` body for every in-scope MS (= where the
# scope entry has a ``milestone`` narrowing key OR the scope is
# project-wide). Entries are serialized via ``core.entries_to_json`` so the
# shape matches the legacy per-project endpoint exactly (= client side
# patch minimal, ``state.milestoneEntries[msId] = entries`` works
# unchanged).
#
# Authorization: ``_load_trek_for_read`` (= creator-or-member RBAC, mirrors
# every other ``/api/treks/{id}/*`` endpoint).
# ---------------------------------------------------------------------------

@app.get("/api/treks/{trek_id}/scope-entries")
def list_trek_scope_entries_endpoint(
    trek_id: str,
    user: dict = Depends(require_auth),
):
    """Return ``entries[]`` for every MS in this Trek's scope, cross-project.

    Response shape (= one list keyed off the Trek, dedup'd across projects)::

        {
          "trek_id": "tk-xxx",
          "milestones": [
            {
              "project_id": "lps-abcdef",
              "milestone_id": "ms-22",
              "milestone_title": "...",
              "entries": [ ...entries_to_json shape... ]
            },
            ...
          ]
        }

    The ``entries[]`` shape exactly mirrors
    ``GET /api/projects/{pid}/milestones/{ms_id}/entries`` so the Web UI
    can stuff the result straight into ``state.milestoneEntries[msId]``
    without any per-entry rewriting (= ms-95 / e-2640 AC3).

    Scope semantics (= same rule as ``/api/treks/{id}/milestones`` walker
    above — see ``_scope_contributes``):

      * project-wide entry (= no narrowing key) → every MS in the
        project contributes its entries
      * ``{"project": pid, "milestone": ms_id}`` → just that MS's entries

    Operation- or task-only scope entries do not contribute MS entries
    (= mirrors ``/milestones`` which returns ``[]`` in those cases).

    Stale scope entries pointing at projects the caller cannot read are
    silently skipped (= identical to ``/documents`` / ``/milestones``).
    """
    t = _load_trek_for_read(trek_id, user)
    by_project = _trek_scope_by_project(t)
    out_ms: list = []
    seen: set[tuple[str, str]] = set()
    for pid, entries in by_project.items():
        try:
            project = _load(pid, user)
        except Exception:
            # Stale scope entry pointing at a project the caller cannot
            # read; skip silently so a single bad ref doesn't break the
            # whole listing (= same regime as /documents / /milestones).
            continue
        include_all, narrow_ids = _scope_contributes(entries, kind="milestone")
        if not include_all and not narrow_ids:
            # Project only contributes via operation / task narrowing;
            # the MS aggregate has nothing to ship here.
            continue
        for ms in project.get("milestones", []) or []:
            ms_id = ms.get("id") or ""
            if not include_all and ms_id not in narrow_ids:
                continue
            key = (pid, ms_id)
            if key in seen:
                continue
            seen.add(key)
            raw_entries = ms.get("entries", []) or []
            out_ms.append({
                "project_id": pid,
                "milestone_id": ms_id,
                "milestone_title": ms.get("title", ""),
                "entries": core.entries_to_json(raw_entries),
            })
    return {
        "trek_id": t.get("trek_id") or trek_id,
        "milestones": out_ms,
    }


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
    env_tier = (body.envelope or {}).get("tier", "") or ""
    env_issuer = (body.envelope or {}).get("issuer", "") or ""
    gate_lookup = dm_gate_mod.build_shared_trek_lookup_from_lists(
        # Sender-side trek visibility is sufficient — Trek membership
        # query is symmetric (creator OR members) on either backend.
        # ms-97 / e-2659 Phase 3 (G4): the lookup is invoked fresh per
        # bus event — no list reuse — so a member removed between
        # events stops granting bypass on the next invocation.
        lambda uid: db.list_treks(actor_id=uid) if uid else [],
    )
    # ms-97 / e-2659 Phase 3 (AC18): pass session ids so phase A+ treks
    # are evaluated session-grain. Recipient sid is resolved from the
    # payload (= same shape used by ``_resolve_bus_event_user_ids``).
    recipient_sid_for_gate = ""
    if isinstance(body.payload, dict):
        recipient_sid_for_gate = str(
            body.payload.get("recipient_session_id") or ""
        )
    # ms-83 / e-1995: pass tier+issuer so the gate can recognise
    # T1-system server-mint envelopes as T1-equivalent (= bypass).
    should_gate, gate_reason, gate_trek_id = dm_gate_mod.should_gate_dm_action(
        sender_user_id=sender_uid,
        receiver_user_id=receiver_uid,
        actions_authorized=env_actions,
        shared_trek_lookup=gate_lookup,
        envelope_tier=env_tier,
        envelope_issuer=env_issuer,
        sender_session_id=body.sender_session_id or "",
        receiver_session_id=recipient_sid_for_gate,
    )
    audit_record["dm_gate"] = {
        "should_gate": should_gate,
        "reason": gate_reason,
        "sender_user_id": sender_uid,
        "receiver_user_id": receiver_uid,
        # ms-97 / e-2659 Phase 3 (AC19): bypass flag + matched trek_id.
        # bypass=True when the gate let an action-bearing cross-user
        # envelope through *because* of a shared trek membership. The
        # trek_id makes the audit log searchable by trek.
        "bypass": (
            (not should_gate)
            and gate_reason == dm_gate_mod.GATE_REASON_SHARED_TREK
        ),
        "trek_id": gate_trek_id,
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


class TrekHeartbeatRequest(BaseModel):
    """Body for POST /api/treks/{trek_id}/session-heartbeat (ms-83 / e-2001).

    The AI session inside the trek's leader (or any member) pings this
    endpoint after each tick completes, so the server's idle detector
    knows the session is alive. Stamps ``meta.last_session_response_at``
    on the trek doc.

    Identity check: caller must be a joined member of the trek.
    """
    session_id: str = ""


@app.post("/api/treks/{trek_id}/session-heartbeat")
def trek_session_heartbeat(
    trek_id: str,
    body: TrekHeartbeatRequest,
    user: dict = Depends(require_auth),
):
    """Stamp the trek's last_session_response_at (ms-83 / e-2001)."""
    t = _load_trek_for_read(trek_id, user)
    _require_trek_joined_member(t, user)
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    meta = t.setdefault("meta", {})
    meta["last_session_response_at"] = now_iso
    if body.session_id:
        meta["last_session_response_session_id"] = body.session_id
    t["updated_at"] = trek_mod.utcnow_iso()
    db.save_trek(trek_id, t)
    return {
        "trek_id": trek_id,
        "last_session_response_at": now_iso,
    }


class TrekSchedulerTickRequest(BaseModel):
    """Body for POST /api/system/trek-scheduler/tick (ms-83 / e-1997).

    Cloud Scheduler calls this endpoint at a fixed cadence (= every minute
    by default). The endpoint walks all active Treks, decides which are
    due based on each trek's ``meta.cadence_minutes`` and previous fire
    time, mints a T1-system envelope for each live member session, and
    posts one ``dm``-channel bus event per session into that session's
    home project bus (= ms-95 / e-2639 dm-transport migration, per
    ms-97 SPEC 中心原則 6 「Wake 経路は DM と完全同一」).

    Pre-e-2639 the tick wrote to dedicated ``trek-progress-check`` /
    ``trek-leader-digest`` channels in ``scope[0]['project']`` only;
    members whose home project sat in ``scope[1..N]`` (= cross-project
    Trek) were permanently deaf. The dm rail re-uses the wake path
    that already reliably injects context into AI sessions, so every
    member gets the same delivery guarantee with no new subscription
    or hook plumbing.

    Authentication: ``X-Beacon-Scheduler-Key`` header (same key as
    T1-system mint endpoint — they share a trust boundary).

    Body is empty by default; ``project_id`` / ``trek_ids`` overrides let
    integration tests scope the tick to a single trek without iterating
    every project in the backend.
    """
    # Optional scoping for tests / staged rollout. None = iterate all.
    project_ids: Optional[list[str]] = None
    trek_ids: Optional[list[str]] = None


def _build_executor_targets_user_grain(
    *,
    fanout_trek_doc: dict,
    live_sessions: dict[str, dict],
    leader_sid: str,
) -> list[dict]:
    """Legacy pre-A fanout target builder (= user_id grain).

    ms-97 / e-2659 Phase 3 — kept verbatim for backward compatibility
    with treks that have NOT been migrated to session_id keyed members[].
    Walks the live_sessions map (which was already filtered by
    ``member_user_ids`` upstream), excludes the leader session, and
    applies the per-session ``should_fire_executor_tick`` lazy-start
    gate. This is the byte-for-byte equivalent of the pre-Phase-3 inline
    loop body — extracted into a function purely so the new Phase A+
    path sits next to it for diff clarity.
    """
    targets: list[dict] = []
    for sid, info in live_sessions.items():
        if leader_sid and sid == leader_sid:
            # The stamped leader session is structurally excluded
            # from progress-check (= CORE doc trek-leader-stance /
            # e-2166: leader's role is review, not pickup).
            continue
        # ms-95 / e-2644 — fanout 評価は **snapshot** ベース
        # (= 同 tick 内で stall transition が走っても claim を喪失
        # しない、 dogfood findings § #19 構造的対策)。
        if not trek_scheduler_mod.should_fire_executor_tick(
            fanout_trek_doc, session_id=sid,
        ):
            continue
        targets.append({
            "session_id": sid,
            "home_project_id": info["home_project_id"],
        })
    return targets


def _build_executor_targets_session_grain(
    *,
    fanout_trek_doc: dict,
    trek_doc: dict,
    scope_project_ids: list[str],
    leader_sid: str,
    live_cutoff: str,
) -> list[dict]:
    """ms-97 / e-2659 Phase 3 (AC16) — phase A+ fanout target builder.

    Iterates ``trek_doc.members[]`` directly (= session_id keyed) and
    resolves each session's home project by scanning every scope
    project's session registry. First match wins (= a session_id is
    unique to one project bridge in practice). Sessions without a
    resolvable home project are skipped with a structured warning to
    stderr — the leader-digest payload's ``alarm`` field already
    surfaces this case upstream, so the warning is purely log-side.

    Why iterate scope_project_ids for home resolution: the scheduler
    has no session→project reverse index. With N scope projects the
    cost is O(M·N) per tick where M = member count; both numbers are
    small (single digits) in dogfood. If/when scale demands, swap to
    a session_id → project_id directory point lookup.

    Filters applied (in order):
      1. Leader session_id → excluded (CORE doc trek-leader-stance).
      2. No resolvable home project → skipped + stderr warning.
      3. Session not live (= last_active before live_cutoff) → skipped
         (= 10-min cutoff matches the user_grain path for parity).
      4. ``should_fire_executor_tick`` lazy-start gate → skipped if
         the executor has no signal worth waking on this tick.
    """
    members = trek_doc.get("members") or []
    trek_id_for_log = trek_doc.get("trek_id", "")
    # ms-97 / e-2660 — diagnostic header. Surfaces input shape so an
    # empty-targets outcome can be triaged from logs without needing
    # to recompute the inputs. Emitted at the start of every call.
    print(
        f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
        f"_build_executor_targets_session_grain entry: "
        f"len(members)={len(members)} "
        f"live_cutoff={live_cutoff} "
        f"scope_project_ids={scope_project_ids} "
        f"leader_sid={leader_sid}",
        file=sys.stderr,
    )
    targets: list[dict] = []
    for m in members:
        msid = m.get("session_id") or ""
        mrole = m.get("role") or ""
        if not msid:
            # Invitation-stage placeholder (= invited, not joined). No
            # session yet to wake.
            print(
                f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
                f"skipped: empty session_id (role={mrole})",
                file=sys.stderr,
            )
            continue
        if leader_sid and msid == leader_sid:
            print(
                f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
                f"sid={msid} skipped: leader_sid match",
                file=sys.stderr,
            )
            continue
        # Role-based leader skip (= belt + suspenders with the sid
        # match above; covers cases where the doc has role=leader but
        # leader_session_id is unstamped).
        if mrole == "leader":
            print(
                f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
                f"sid={msid} skipped: role=leader",
                file=sys.stderr,
            )
            continue
        # Resolve home project. First scope project whose session
        # registry contains msid wins.
        resolved_pid = ""
        last_active = ""
        for pid in scope_project_ids:
            try:
                project_sessions = db.list_sessions(pid)
            except Exception:
                project_sessions = []
            for s in project_sessions:
                if (s.get("session_id") or "") == msid:
                    resolved_pid = pid
                    last_active = s.get("last_active") or ""
                    break
            if resolved_pid:
                break
        if not resolved_pid:
            # Ghost member: session vanished from every scope project's
            # registry. Skip + log; the alarm hook upstream already
            # surfaces this via leader-digest payload.
            print(
                f"warn[ms-97 e-2659 AC16]: trek "
                f"{trek_id_for_log} member session "
                f"{msid} has no resolvable home project in scope "
                f"{scope_project_ids}",
                file=sys.stderr,
            )
            print(
                f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
                f"sid={msid} skipped: no home project resolved "
                f"(= ghost)",
                file=sys.stderr,
            )
            continue
        # Live cutoff filter — keeps parity with user_grain path. An
        # offline session that has not registered a recent heartbeat
        # would not receive the dm anyway.
        if last_active and last_active < live_cutoff:
            print(
                f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
                f"sid={msid} skipped: last_active={last_active} "
                f"< live_cutoff={live_cutoff}",
                file=sys.stderr,
            )
            continue
        # Lazy-start gate (= AC33).
        if not trek_scheduler_mod.should_fire_executor_tick(
            fanout_trek_doc, session_id=msid,
        ):
            print(
                f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
                f"sid={msid} skipped: "
                f"should_fire_executor_tick=False",
                file=sys.stderr,
            )
            continue
        print(
            f"diag[ms-97 e-2660 AC16] trek={trek_id_for_log} "
            f"sid={msid} kept: home_pid={resolved_pid}",
            file=sys.stderr,
        )
        targets.append({
            "session_id": msid,
            "home_project_id": resolved_pid,
        })
    return targets


@app.post("/api/system/trek-scheduler/tick")
def trek_scheduler_tick_endpoint(
    body: TrekSchedulerTickRequest,
    request: Request,
):
    """Run one Trek scheduler tick (ms-83 / e-1997).

    Returns a structured report of which treks were considered, which
    fired, and any per-trek errors. The report is what Cloud Scheduler
    logs hold onto for observability.

    Decision logic (= pure) lives in ``lib/trek_scheduler.py`` so unit
    tests pin the cadence math without standing up an HTTP server.
    """
    provided = request.headers.get("X-Beacon-Scheduler-Key", "")
    expected = envelope_mod.scheduler_internal_key()
    if not provided or provided != expected:
        raise HTTPException(
            status_code=403,
            detail="trek scheduler tick requires X-Beacon-Scheduler-Key",
        )

    import copy
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    # Fan out across active treks. Without project scoping we list every
    # trek in the backend (admin-style enumeration); the scheduler tick is
    # an internal service, not a user-driven query, so this is acceptable
    # and matches Operation scheduler's behaviour for the same reason.
    candidate_treks = db.list_treks(actor_id=None)
    if body.trek_ids:
        wanted = set(body.trek_ids)
        candidate_treks = [t for t in candidate_treks
                           if t.get("trek_id") in wanted]
    candidate_treks = [t for t in candidate_treks
                       if t.get("status") == "active"]
    # ms-97 / e-2612 (AC32) — explicit halt accounting. ``is_trek_due``
    # already filters halted treks out of the due set (= the cadence
    # decision returns False when halt is set, see lib.trek_scheduler),
    # but the response payload needs to surface the skip so observers
    # and tests can tell "skipped because halted" apart from "cadence
    # not elapsed". Collect halted IDs from the candidate set before
    # select_due_treks drops them silently.
    halted: list[dict] = []
    for t in candidate_treks:
        if trek_mod.is_halted(t):
            halted.append({
                "trek_id": t.get("trek_id", ""),
                "reason": "halted",
            })
    due_treks = trek_scheduler_mod.select_due_treks(
        candidate_treks, now=now,
    )
    # ms-83 / e-2001: snapshot idle decisions BEFORE the progress-check
    # pass stamps last_progress_check_at — otherwise every fire would
    # reset the idle clock to "now" and we'd never escalate.
    idle_trek_ids = {
        t.get("trek_id", "")
        for t in candidate_treks
        if trek_scheduler_mod.should_fire_idle_escalation(t, now=now)
    }

    fired: list[dict] = []
    errors: list[dict] = []
    quiesced: list[dict] = []
    for trek_doc in due_treks:
        trek_id = trek_doc.get("trek_id", "")
        # ms-95 / e-2644 — **fanout snapshot strategy** (= dogfood findings
        # `e70cUf8IS5uEIS1HIEXt` § #19)。 2026-06-28 dogfood で
        # 「executor が active claim を保持していたのに progress-check fanout
        # から漏れた」 病理を観察。 root cause: 同 tick 内で stall transition
        # が走ると task_states[*].updated_by_session_id が "" にリセットされ、
        # その後の fanout 評価 (= session_has_active_claim) で claim 主と
        # session_id が一致せず False を返す → executor_targets から除外。
        #
        # 構造的対策: tick endpoint 冒頭で task_states を snapshot 化し
        # (= deep copy)、 fanout 評価は **snapshot ベース** で実行する。
        # stall mutation は同 transaction 内で fanout 評価の **後** に
        # 走るのでこの tick の fanout には影響しない (= 次 tick 以降は
        # 新しい task_states を snapshot 化した上で評価)。
        #
        # snapshot trek_doc は live trek_doc の shallow copy + task_states
        # の deep copy を持つ shape。 fanout helpers が読むのは task_states
        # 以外には scope / members / leader_session_id 等の概ね tick 内で
        # 不変な field のみなので、 shallow copy で十分。
        task_states_snapshot = copy.deepcopy(
            trek_doc.get("task_states") or {}
        )
        fanout_trek_doc = dict(trek_doc)
        fanout_trek_doc["task_states"] = task_states_snapshot
        # ms-97 / e-2612 (AC32) — Defense-in-depth halt skip. The cadence
        # decision (``is_trek_due`` in lib.trek_scheduler) already filters
        # halted treks out of ``due_treks``, so this branch should never
        # actually execute. Keep it as a structural guarantee so a future
        # cadence-decision change can't silently re-introduce tick fires
        # on halted treks.
        if trek_mod.is_halted(trek_doc):
            continue
        # ms-97 / Phase 7-B / AC22 — auto-succession orchestrator.
        # halt をすり抜けてここまで来た上で、 phase A+ trek (= session_id
        # keyed members) かつ leader session が不応 (= N=3 連続 pulse-ack
        # miss AND last_active >= 30 min stale AND 条件) の時、 priority
        # order = invited_at 昇順で次 candidate session を選び、 1 hop
        # consent DM を emit する。 candidate 不在の時は user に escalation
        # (= meta.succession_escalated_at を 1 度だけ stamp、 leader-digest
        # 経由で観測可能)。 各 trek ごとに 1 tick 最大 1 candidate を
        # nominate する idempotent 設計 (= pending stamp で二重発射防止)。
        if trek_mod.is_session_id_keyed(trek_doc):
            try:
                leader_sid_for_succession = (
                    trek_doc.get("leader_session_id") or ""
                )
                leader_last_active = ""
                if leader_sid_for_succession:
                    succession_scope_ids = _resolve_trek_scope_project_ids(
                        trek_doc
                    )
                    for project_pid in succession_scope_ids:
                        try:
                            project_sessions = db.list_sessions(project_pid)
                        except Exception:
                            project_sessions = []
                        found_la = ""
                        for s in project_sessions:
                            if s.get("session_id") == leader_sid_for_succession:
                                found_la = s.get("last_active") or ""
                                break
                        if found_la:
                            leader_last_active = found_la
                            break
                unresponsive = trek_mod.detect_unresponsive_leader(
                    trek_doc, now,
                    leader_last_active=leader_last_active,
                )
                if unresponsive:
                    meta = trek_doc.setdefault("meta", {})
                    pending_already = (
                        meta.get(
                            trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY
                        )
                        or ""
                    )
                    if not pending_already:
                        candidate = trek_mod.pick_succession_candidate(
                            trek_doc
                        )
                        if candidate is None:
                            if not meta.get(
                                trek_mod.SUCCESSION_ESCALATED_AT_META_KEY
                            ):
                                meta[
                                    trek_mod.SUCCESSION_ESCALATED_AT_META_KEY
                                ] = trek_mod.utcnow_iso()
                                print(
                                    f"warn[ms-97 e-2684]: trek "
                                    f"{trek_id} succession escalated — "
                                    "no candidate available",
                                    file=sys.stderr,
                                )
                                trek_doc["updated_at"] = (
                                    trek_mod.utcnow_iso()
                                )
                                try:
                                    db.save_trek(trek_id, trek_doc)
                                except Exception:
                                    pass
                        else:
                            candidate_sid = candidate.get("session_id") or ""
                            if candidate_sid:
                                emit_leader_succession_consent_dm(
                                    trek_doc, candidate_sid,
                                    former_leader_session_id=(
                                        leader_sid_for_succession
                                    ),
                                )
                                meta[
                                    trek_mod.SUCCESSION_PENDING_CANDIDATE_META_KEY
                                ] = candidate_sid
                                meta[
                                    trek_mod.SUCCESSION_PENDING_EMITTED_AT_META_KEY
                                ] = trek_mod.utcnow_iso()
                                trek_doc["updated_at"] = (
                                    trek_mod.utcnow_iso()
                                )
                                try:
                                    db.save_trek(trek_id, trek_doc)
                                except Exception:
                                    pass
            except Exception as exc:
                # Succession orchestrator must never break the tick loop.
                # Log the error and continue with the regular fanout —
                # next tick re-evaluates from a clean slate.
                print(
                    f"warn[ms-97 e-2684]: trek {trek_id} succession "
                    f"orchestrator error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        # ms-75 / e-2048 — Trek task state machine integration. When every
        # task that an executor has stamped state for has reached terminal
        # (= done or waiting-review), the scheduler goes silent for this
        # trek. The leader review notification was already emitted by the
        # PATCH /api/treks/{id}/task-state endpoint at the moment of the
        # terminal stamp, so re-firing here would just be noise. A trek
        # with no stamped states is NOT terminal — the scheduler still
        # fires obligation DMs to wake an executor that has not yet
        # declared anything (= pre-state-machine compatible).
        if trek_scheduler_mod.is_trek_task_aggregate_terminal(trek_doc):
            quiesced.append({
                "trek_id": trek_id,
                "reason": "task_state_aggregate_terminal",
            })
            # Still stamp last_progress_check_at so the next cadence
            # window does not re-flag this trek as "never fired".
            meta = trek_doc.setdefault("meta", {})
            meta["last_progress_check_at"] = now.strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            trek_doc["updated_at"] = trek_mod.utcnow_iso()
            try:
                db.save_trek(trek_id, trek_doc)
            except Exception:
                pass
            continue
        # ms-95 / e-2639 — Trek scheduler tick migrated to **dm channel
        # per-member fanout** (= ms-97 SPEC AC16 / 中心原則 6 「Wake 経路
        # は DM と完全同一」)。 Prior to e-2639 the tick posted to
        # ``scope[0]['project']`` only on dedicated ``trek-progress-check``
        # / ``trek-leader-digest`` channels — members whose home project
        # sat in ``scope[1..N]`` (= cross-project Trek) were permanently
        # deaf because the bridge only subscribes to its own project's
        # bus. The 2026-06-28 dogfood (= tk-LPS cross-project Trek)
        # surfaced this as opened ✗ for every member except scope[0]
        # residents. SPEC 中心原則 6 prescribes the fix: route Trek tick
        # over the same dm rail that already wakes AI sessions reliably,
        # one event per recipient in their own home project bus.
        scope = trek_doc.get("scope") or []
        if not scope:
            errors.append({
                "trek_id": trek_id,
                "error": "empty_scope_no_target_project",
            })
            continue
        scope_project_ids = _resolve_trek_scope_project_ids(trek_doc)
        if not scope_project_ids:
            errors.append({
                "trek_id": trek_id,
                "error": "scope_entry_missing_project",
            })
            continue
        # Audit-surface project_id (= back-compat for the dashboard /
        # observability consumers that already key off the legacy
        # single-project field). The dm fanout itself addresses each
        # session in its own home project, so this field becomes a
        # purely informational "first scope project" marker rather than
        # the canonical bus location.
        target_project_id = scope_project_ids[0]
        # Build per-session message bodies once: scheduler has no local
        # project.json so the body is the minimal scope-aware fallback
        # (= scope refs only); the receiver-side AI enriches from its
        # local repo. Done before iterating sessions because the body
        # is identical for every recipient on this tick.
        # ms-95 / e-2644 — payload も snapshot から build (= scope refs
        # と task_state aggregate を読む経路、 stall flip 影響を受けない)。
        progress_payload = trek_scheduler_mod.build_progress_check_payload(
            fanout_trek_doc,
            project_data=None,
            now=now,
        )
        # Walk every scope project's session registry and stitch a
        # canonical (user_id, session_id, home_project_id, last_active)
        # tuple per live member session. Live cutoff matches the
        # pre-e-2639 progress-check filter (= 10 minutes).
        member_user_ids = {
            (m.get("user_id") or "")
            for m in (trek_doc.get("members") or [])
            if m.get("user_id")
        }
        live_cutoff = (now - datetime.timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        # Map of session_id → {"user_id", "home_project_id"}. A given
        # session_id can only exist in one project (= bridge registers
        # under its cwd project), so we de-dup by session_id with
        # "first project wins" — in practice each session appears in
        # exactly one project's registry.
        live_sessions: dict[str, dict] = {}
        for project_pid in scope_project_ids:
            try:
                project_sessions = db.list_sessions(project_pid)
            except Exception:
                project_sessions = []
            for s in project_sessions:
                uid = s.get("user_id") or ""
                sid = s.get("session_id") or ""
                last_active = s.get("last_active") or ""
                if not uid or not sid:
                    continue
                if uid not in member_user_ids:
                    continue
                if last_active < live_cutoff:
                    continue
                if sid in live_sessions:
                    continue
                live_sessions[sid] = {
                    "user_id": uid,
                    "home_project_id": project_pid,
                }
        # ms-97 / e-2658 — Migration safety 機構 #5: 不整合 alarming.
        # Phase A+ trek (= members[] が session_id keyed に書き換わった後)
        # に限り、 各 member.session_id が scope projects の session
        # registry に居るかを check し、 居なければ warning log を出して
        # leader-digest payload に ``alarm`` field を載せる。 Phase pre-A
        # trek (= 旧 user_id keyed legacy) は session_id field を持たない
        # ので skip する (= alarming 対象外、 構造的 no-op)。
        member_session_alarm: list[str] = []
        try:
            current_phase = trek_mod.get_migration_phase(trek_doc)
        except Exception:
            current_phase = "pre-A"
        if current_phase in ("A", "B", "C"):
            # Build the union of session_ids known across all scope
            # projects (= NOT filtered by live cutoff or user_id; the
            # alarm fires when a stamped member session has *vanished*
            # entirely, not when it's merely offline).
            all_known_sids: set[str] = set()
            for project_pid in scope_project_ids:
                try:
                    for s in db.list_sessions(project_pid):
                        sid = s.get("session_id") or ""
                        if sid:
                            all_known_sids.add(sid)
                except Exception:
                    # Best-effort: per-project listing failure must not
                    # break the tick. Missing project just contributes
                    # zero known sids, which over-reports alarms — that
                    # is the safer direction (= surface, don't hide).
                    continue
            for m in trek_doc.get("members") or []:
                msid = m.get("session_id") or ""
                if not msid:
                    continue
                if msid not in all_known_sids:
                    member_session_alarm.append(msid)
            if member_session_alarm:
                # Structured warning into stderr → captured by Cloud Run
                # logs and by local-mode dev server output. Includes
                # trek_id so the log line is greppable.
                print(
                    f"warn[ms-97 e-2658]: trek {trek_id} has member "
                    f"sessions absent from session registry: "
                    f"{member_session_alarm}",
                    file=sys.stderr,
                )
        # ms-97 / e-2637 — Welcome tick fanout (= bootstrap path for
        # fresh joiners that the join-time hook missed, e.g. sessions
        # that joined before this server code was deployed, or where
        # the join-time fire failed). Walks members[] (phase A+ only,
        # because pre-A members have no session_id field) and fires a
        # one-shot welcome tick to each session whose stamp is missing.
        # Stamps are recorded on ``meta.welcome_tick_fired_at`` so the
        # next tick does not re-fire.
        if trek_mod.is_session_id_keyed(trek_doc):
            _fanout_welcome_ticks_for_pending_members(
                trek_doc=trek_doc, trek_id=trek_id,
                scope_project_ids=scope_project_ids,
            )

        # ms-88 / e-2109 + ms-97 / e-2613 (AC33) — per-executor lazy
        # start. The leader role is structurally excluded from progress-
        # check (= CORE doc trek-leader-stance / e-2166: leader's role
        # is review, not pickup). For every other live session the
        # per-session ``should_fire_executor_tick`` gate decides whether
        # there is signal (= active claim OR unclaim todo float) worth
        # waking on this tick.
        leader_sid_for_filter = trek_doc.get("leader_session_id") or ""
        leader_user_id = ""
        for m in trek_doc.get("members") or []:
            if (m.get("role") or "") == "leader" and m.get("user_id"):
                leader_user_id = m.get("user_id") or ""
                break
        # ms-97 / e-2659 Phase 3 (AC16) — fanout target build is now
        # **phase-gated**. Phase A+ treks (= members[] is session_id
        # keyed) iterate members[] directly and resolve each session's
        # home project via the session registry. Pre-A treks keep the
        # legacy "walk live_sessions, filter by user_id membership"
        # path verbatim — backward compat for treks not yet migrated.
        #
        # Why this matters (= dogfood findings root cause): the old
        # path required (a) the executor's home project to be in
        # ``scope[]`` and (b) the session registry to surface a live
        # row. For LPS / PE cross-project Treks where the executor's
        # home project was *not* in scope (= scope listed only the
        # primary project), step (a) failed silently and the executor
        # never received a tick. Phase A+ iteration removes step (a)
        # by relying solely on members[] as the canonical recipient
        # list.
        if trek_mod.is_session_id_keyed(trek_doc):
            executor_targets = _build_executor_targets_session_grain(
                fanout_trek_doc=fanout_trek_doc,
                trek_doc=trek_doc,
                scope_project_ids=scope_project_ids,
                leader_sid=leader_sid_for_filter,
                live_cutoff=live_cutoff,
            )
        else:
            executor_targets = _build_executor_targets_user_grain(
                fanout_trek_doc=fanout_trek_doc,
                live_sessions=live_sessions,
                leader_sid=leader_sid_for_filter,
            )
        # Audit-surface ``recipients`` list (= back-compat for callers
        # that read ``fired[].recipients``). Empty string sentinel marks
        # the broadcast fallback.
        target_sids: list[str] = (
            [t["session_id"] for t in executor_targets]
            if executor_targets else [""]
        )
        # Per-member fanout: each session gets its own dm posted into
        # its own home project bus. T1-system envelope is minted per
        # target project so the envelope's project_id matches the bus
        # it lands in (= envelope verify chain stays consistent).
        event_ids: list[str] = []
        any_send_succeeded = False
        if executor_targets:
            for target in executor_targets:
                send_payload = dict(progress_payload)
                send_payload["recipient_session_id"] = target["session_id"]
                # Mark this DM as system-issued so receivers can filter
                # Trek tick events from human DMs without parsing the
                # envelope tier (= cheap discriminator on the payload
                # surface). The body itself already carries the
                # ``[Trek progress check]`` header for human-readable
                # audit.
                send_payload["sender_type"] = "trek-scheduler"
                send_payload["origin_channel"] = "trek-progress-check"
                try:
                    target_envelope = envelope_mod.issue_t1_system_envelope(
                        project_id=target["home_project_id"],
                        trek_id=trek_id,
                        actions_authorized=["trek.progress_check"],
                        data_class="free",
                        ttl_seconds=3600,
                    )
                except ValueError as exc:
                    errors.append({
                        "trek_id": trek_id,
                        "recipient_session_id": target["session_id"],
                        "error": f"envelope_mint_failed: {exc}",
                    })
                    continue
                bus_data = {
                    "channel": "dm",
                    "sender_session_id": "",
                    "payload": send_payload,
                    "envelope": target_envelope,
                    "delivery": "auto-execute",
                    "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                }
                try:
                    event_ids.append(
                        db.append_bus_event(
                            target["home_project_id"], bus_data,
                        )
                    )
                    any_send_succeeded = True
                except Exception as exc:
                    errors.append({
                        "trek_id": trek_id,
                        "recipient_session_id": target["session_id"],
                        "error": (
                            f"bus_append_failed: {type(exc).__name__}: "
                            f"{exc}"
                        ),
                    })
        else:
            # Broadcast fallback (= no live executor sessions resolved
            # OR every live session was filtered out by the lazy-start
            # gate). Mirrors the pre-e-2639 broadcast minimal-tick
            # contract: one event posted to the audit-surface project
            # bus so the trek still surfaces in *some* inbox and
            # downstream observers see the "fired at minimum once"
            # guarantee.
            #
            # ms-97 / e-2660 — recipient_session_id MUST be set to the
            # stamped leader_session_id so _bus_event_addressed_to can
            # deliver it. Prior to this fix the broadcast fallback
            # posted with channel="dm" and no recipient stamp; the DM
            # routing filter (server/app.py _bus_event_addressed_to)
            # structurally drops "DM + no recipient" events, so the
            # broadcast event was written to storage but never reached
            # any bridge. Pinning the recipient to the stamped leader
            # session preserves SPEC 中心原則 6 (= Wake 経路 DM 統一)
            # AND surfaces the broadcast-fallback as a leader audit
            # signal (= leader sees "fanout had no executor targets
            # this tick").
            stamped_leader_sid_for_broadcast = (
                trek_doc.get("leader_session_id") or ""
            )
            send_payload = dict(progress_payload)
            send_payload["sender_type"] = "trek-scheduler"
            send_payload["origin_channel"] = "trek-progress-check"
            if stamped_leader_sid_for_broadcast:
                send_payload["recipient_session_id"] = (
                    stamped_leader_sid_for_broadcast
                )
            try:
                target_envelope = envelope_mod.issue_t1_system_envelope(
                    project_id=target_project_id,
                    trek_id=trek_id,
                    actions_authorized=["trek.progress_check"],
                    data_class="free",
                    ttl_seconds=3600,
                )
            except ValueError as exc:
                errors.append({
                    "trek_id": trek_id,
                    "error": f"envelope_mint_failed: {exc}",
                })
                continue
            bus_data = {
                "channel": "dm",
                "sender_session_id": "",
                "payload": send_payload,
                "envelope": target_envelope,
                "delivery": "auto-execute",
                "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            }
            try:
                event_ids.append(
                    db.append_bus_event(target_project_id, bus_data)
                )
                any_send_succeeded = True
            except Exception as exc:
                errors.append({
                    "trek_id": trek_id,
                    "recipient_session_id": (
                        stamped_leader_sid_for_broadcast
                    ),
                    "error": (
                        f"bus_append_failed: {type(exc).__name__}: {exc}"
                    ),
                })
        if not any_send_succeeded:
            # Every send for this trek failed; skip the stamp so the next
            # tick retries instead of silently swallowing the failure.
            continue
        # ms-92 / e-2164 — leader-digest fanout (= one event per leader
        # live session carrying aggregated per-executor status). Same
        # cadence as the progress-check pass (= they fire together so
        # the leader sees "what is everyone doing right now" without
        # polling).
        #
        # ms-95 / e-2639 — leader-digest is now also a dm to the
        # leader's home project bus. SPEC 中心原則 6: 「Wake 経路は DM
        # と完全同一」 — the leader's session subscribes to the same dm
        # channel as everyone else, so a dm posted into their home
        # project wakes them reliably. ``leader_digest_recipients`` audit
        # field stays for back-compat with dashboard consumers (=
        # `fired[].leader_digest_recipients`).
        # ms-95 / e-2645 — leader-digest **narrow fanout**。 旧実装は
        # 「leader user の全 live session」 に digest を broadcast していたが、
        # 2026-06-28 dogfood で「同 user の executor session が leader-digest
        # を受信」 する病理を観察 (= same-user multi-session で user_id filter
        # が collapse、 dogfood findings § #16)。
        #
        # 新方針:
        #   * **primary path**: stamped ``leader_session_id`` が live なら
        #     **その 1 session のみ** に digest を送る (= 役割境界明示)。
        #   * **fallback path**: stamped が stale (= live でない) の時のみ、
        #     leader user の全 live session に fan out (= ms-92 e-2164
        #     multi-leader-session 互換、 reconnect / fork で sid が変わった
        #     場合に digest が宙に消えない経路)。
        #
        # この narrow は same-user dogfood で executor の noise を断つだけ
        # でなく、 cross-user Trek の正常動作 (= leader user の sessions
        # のみ) も同じ logic で表現できる (= leader_session_id 一致 →
        # 一つだけ、 stale → leader user の sessions に拡散)。
        #
        # ms-95 / e-2723 — fallback path の **second narrow**。 2026-06-28
        # dogfood 続報 (= dogfood findings § #16 致命的連鎖 #2): e-2645
        # primary path が落ちて fallback に到達した時、 旧 fallback は
        # 「leader user の全 live session」 に fan out していた。 これだと
        # **same-user multi-session** (= leader + executor が同 user の
        # 別 session、 dogfood の Mac / Win 並走 や 1 user 多 role 検証)
        # で executor session も leader-digest を受信してしまう。 ms-92
        # e-2164 SPEC Done when #1 「recipient = leader_session_id のみ」
        # の意図に反する。
        #
        # 新 fallback (= ms-97 AC6 land 後の session_id keyed members[]
        # を活用):
        #   * Phase A+ trek (= ``is_session_id_keyed`` True): members[]
        #     を walk し、 ``role=leader`` な member.session_id が live
        #     なものだけに narrow。 1 leader = 1 session が原則なので
        #     normal flow では 1 target、 multi-leader role を許容する
        #     trek (= 将来の co-leader) でも members[] に書かれた範囲
        #     だけが対象 (= executor は構造的に除外)。
        #   * Pre-A trek (= legacy user_id keyed): session_id field が
        #     members[] に存在しないので fallback は従来通り leader user
        #     の全 live session。 same-user collapse は pre-A trek でも
        #     起きうるが、 phase A migration で構造解消する方が筋が良い
        #     (= pre-A trek の数は migration 進行で減るのみ、 long-term
        #     には phase A+ が default)。
        leader_targets: list[dict] = []
        leader_target_pids: list[str] = []
        stamped_leader_sid = trek_doc.get("leader_session_id") or ""
        if (
            stamped_leader_sid
            and stamped_leader_sid in live_sessions
            and (
                # Sanity: stamped session should actually belong to the
                # leader user. If the doc is corrupted we fall through
                # to the fallback path rather than misroute.
                live_sessions[stamped_leader_sid].get("user_id")
                == leader_user_id
                or not leader_user_id
            )
        ):
            # Primary path: stamped leader session is live → narrow to it.
            info = live_sessions[stamped_leader_sid]
            leader_targets.append({
                "session_id": stamped_leader_sid,
                "home_project_id": info["home_project_id"],
            })
        elif trek_mod.is_session_id_keyed(trek_doc):
            # Fallback path (= phase A+): stamped leader is stale. Walk
            # members[] and pick live sessions with role=leader only.
            # This is the e-2723 narrow that closes the same-user
            # collapse: executor session (= same user_id but
            # role=executor) is structurally excluded because we never
            # look at it.
            for m in trek_doc.get("members") or []:
                if (m.get("role") or "") != "leader":
                    continue
                msid = m.get("session_id") or ""
                if not msid:
                    # Invitation-stage leader placeholder (= invited but
                    # not joined) — no session to wake.
                    continue
                if msid not in live_sessions:
                    # Stamped or named leader sid is offline; skip and
                    # let the "fully offline" tail fall through to the
                    # stamped-only stub below.
                    continue
                # Sanity user_id match (= belt + suspenders); if the
                # session registry disagrees with members[] we skip
                # rather than misroute.
                if (
                    leader_user_id
                    and live_sessions[msid].get("user_id") != leader_user_id
                ):
                    continue
                leader_targets.append({
                    "session_id": msid,
                    "home_project_id": live_sessions[msid]["home_project_id"],
                })
        else:
            # Fallback path (= pre-A legacy): members[] is user_id keyed
            # only — no session_id field to narrow on. Preserve the
            # legacy "all live sessions of the leader user" fan-out for
            # backward compat. Phase A migration (= ms-97 AC6) eventually
            # removes this branch by upgrading every trek's members[].
            for sid, info in live_sessions.items():
                if not leader_user_id:
                    break
                if info["user_id"] != leader_user_id:
                    continue
                leader_targets.append({
                    "session_id": sid,
                    "home_project_id": info["home_project_id"],
                })
        leader_live_sids: list[str] = [t["session_id"] for t in leader_targets]
        # Fallback (= leader fully offline): no live leader session resolves
        # AND the primary stamped sid was already considered above (=
        # neither live nor present in live_sessions). Fall back to the
        # stamped leader_session_id targeting the first scope project so
        # the observability surface (= meta.last_leader_digest_at stamping)
        # keeps working even when the leader is fully offline. Planning-
        # era treks (= leader_session_id empty AND no live leader) get
        # skipped — there is genuinely no recipient.
        if not leader_targets:
            stamped = trek_doc.get("leader_session_id") or ""
            if stamped:
                leader_targets.append({
                    "session_id": stamped,
                    "home_project_id": target_project_id,
                })
                leader_live_sids = [stamped]
        leader_digest_event_id = ""
        # ms-97 / e-2613 (AC33) — per-leader lazy start. Fire the digest
        # only when the leader genuinely has signal to consume (=
        # leader_review queue / todo float / completion imminent). When
        # the gate closes AND no progress-check fired either (= every
        # executor was also quiet), the broadcast fallback above already
        # injects one minimal dm event this tick so leader-digest can
        # stay silent without violating the "no complete silence" rule.
        # ms-95 / e-2644 — leader-digest gate も snapshot ベース。
        # ms-97 / Phase 7-A / AC21 — leader が user summary DM 送信後
        # (= ``meta.summary_sent_at`` stamped) かつ completion_ready が
        # 既に 1 回 fire 済 (= ``meta.completion_notified_at`` stamped)
        # の状態では leader-digest tick を停止する。 「完遂宣言が
        # 終わった trek に digest を打ち続けない」 ための停止条件。
        # 片方だけ stamped の状態では従来通り fire し続ける。
        leader_meta_snapshot = trek_doc.get("meta") or {}
        leader_halted_by_summary = bool(
            leader_meta_snapshot.get("summary_sent_at")
        ) and bool(
            leader_meta_snapshot.get("completion_notified_at")
        )
        # ms-97 / Phase 7-A / AC20 — completion_ready シグナル。
        # 全 task_states terminal + Op slot 不在 + 未通知 の時 1 回限り
        # leader-digest payload に ``completion_ready=True`` を載せ、
        # fanout 成功後に ``meta.completion_notified_at`` を stamp して
        # 二度目の fire を構造的に防ぐ。
        completion_ready_now = (
            trek_scheduler_mod.is_completion_ready(fanout_trek_doc)
        )
        leader_should_fire = (
            (not leader_halted_by_summary)
            and (
                trek_scheduler_mod.should_fire_leader_tick(fanout_trek_doc)
                or completion_ready_now
            )
        )
        completion_ready_fanned_out = False
        if leader_targets and leader_should_fire:
            base_digest_payload = trek_scheduler_mod.build_leader_digest_payload(
                fanout_trek_doc, now=now,
            )
            if completion_ready_now:
                base_digest_payload["completion_ready"] = True
            for target in leader_targets:
                lsid = target["session_id"]
                lpid = target["home_project_id"]
                try:
                    digest_envelope = envelope_mod.issue_t1_system_envelope(
                        project_id=lpid,
                        trek_id=trek_id,
                        actions_authorized=["trek.leader_digest"],
                        data_class="free",
                        ttl_seconds=3600,
                    )
                except ValueError as exc:
                    errors.append({
                        "trek_id": trek_id,
                        "recipient_session_id": lsid,
                        "error": f"leader_digest_envelope_mint_failed: {exc}",
                    })
                    continue
                digest_payload = dict(base_digest_payload)
                digest_payload["recipient_session_id"] = lsid
                digest_payload["sender_type"] = "trek-scheduler"
                digest_payload["origin_channel"] = "trek-leader-digest"
                # ms-97 / e-2658 — surface missing member sessions to
                # the leader so a vanished session can be triaged from
                # the digest UI without crawling logs. Phase pre-A trek
                # never sets this (= alarming gated by migration_phase
                # above), so legacy treks read the field as empty.
                if member_session_alarm:
                    digest_payload["alarm"] = {
                        "missing_member_sessions": list(
                            member_session_alarm
                        ),
                    }
                digest_bus_data = {
                    "channel": "dm",
                    "sender_session_id": "",
                    "payload": digest_payload,
                    "envelope": digest_envelope,
                    "delivery": "auto-execute",
                    "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                }
                try:
                    # Record the latest event_id so the stamp below
                    # fires on at least one successful append. We do
                    # not collect all event_ids here (= existing
                    # observability surface = stamp + count is
                    # sufficient; per-event audit is upstream of bus
                    # storage).
                    leader_digest_event_id = db.append_bus_event(
                        lpid, digest_bus_data,
                    )
                    if lpid not in leader_target_pids:
                        leader_target_pids.append(lpid)
                    if completion_ready_now:
                        completion_ready_fanned_out = True
                except Exception as exc:
                    errors.append({
                        "trek_id": trek_id,
                        "recipient_session_id": lsid,
                        "error": (
                            f"leader_digest_send_failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    })
        # Stamp last_progress_check_at so the next tick skips this trek
        # until its cadence elapses again.
        meta = trek_doc.setdefault("meta", {})
        meta["last_progress_check_at"] = now.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        # ms-92 / e-2164 — record the latest leader-digest fire so the
        # dashboard can show "last leader digest at X". Sits next to
        # last_progress_check_at since they are co-scheduled.
        if leader_digest_event_id:
            meta["last_leader_digest_at"] = now.strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        # ms-97 / Phase 7-A / AC20 — completion_ready idempotent stamp.
        # Fanout が 1 つ以上の leader session に成功した時のみ stamp する
        # (= 全 leader が宛先不在で失敗した場合は次 tick で retry)。 stamp
        # 後は ``is_completion_ready`` が False を返すので、 同 trek
        # ライフサイクル内で再 fire しない (= 二度目の通知病理を構造的に
        # 防ぐ)。
        if completion_ready_fanned_out:
            meta["completion_notified_at"] = trek_mod.utcnow_iso()
        trek_doc["updated_at"] = trek_mod.utcnow_iso()
        try:
            db.save_trek(trek_id, trek_doc)
        except Exception as exc:
            errors.append({
                "trek_id": trek_id,
                "error": f"trek_save_failed: {type(exc).__name__}: {exc}",
            })
            continue
        # ms-97 / Phase 7-C / AC26 — aggregate tick log row (= one per
        # tick, not per recipient). Captures fanout breadth for later
        # analysis without flooding the logs subcollection.
        _append_trek_log_safe(trek_id, {
            "kind": "tick",
            "session_id": "",
            "payload": {
                "project_id": target_project_id,
                "event_ids": list(event_ids),
                "recipients": list(target_sids),
                "leader_digest_event_id": leader_digest_event_id,
                "leader_digest_recipients": list(leader_live_sids),
                "completion_ready": bool(completion_ready_fanned_out),
            },
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        })
        fired.append({
            "trek_id": trek_id,
            "project_id": target_project_id,
            "event_ids": event_ids,
            "recipients": target_sids,
            "leader_digest_event_id": leader_digest_event_id,
            # ms-95 / e-2539: keep ``leader_session_id`` in the audit
            # surface as the **stamped** sid (= the trek_doc field value)
            # for backward compat with the existing dashboard /
            # observability consumers. The live fan-out targets are
            # captured in the new ``leader_digest_recipients`` field
            # below so callers can tell "where the digest actually went"
            # apart from "who the doc thinks the leader is".
            "leader_session_id": trek_doc.get("leader_session_id") or "",
            "leader_digest_recipients": list(leader_live_sids),
            # ms-95 / e-2639: per-member dm fanout records the home
            # project bus each recipient was reached at. Observers can
            # tell whether scope[0] / scope[1..N] members were each
            # delivered in their own home project (= the SPEC AC17
            # cross-project verification surface).
            "recipient_home_project_ids": [
                t["home_project_id"] for t in executor_targets
            ],
            "leader_digest_target_project_ids": leader_target_pids,
        })

    # ms-83 / e-2001: idle escalation pass. Use the pre-snapshot
    # idle_trek_ids so cadence-fire stamps in this same tick don't
    # silently un-idle the trek.
    escalations: list[dict] = []
    for trek_doc in candidate_treks:
        trek_id = trek_doc.get("trek_id", "")
        if trek_id not in idle_trek_ids:
            continue
        # ms-97 / e-2612 (AC32) — halt 中 は idle escalation も停止。
        # 「leader が pause した状態で自律 escalation が走る」 のは
        # halt の意図 (= 全 autonomous activity の一時停止) に反する。
        # Resume 後の次 tick で再度 idle 判定 → 必要なら escalate。
        if trek_mod.is_halted(trek_doc):
            continue
        # Re-read the trek so the cadence-pass save lands in our
        # working copy (= last_idle_escalation_at sits next to the
        # freshly-stamped last_progress_check_at).
        fresh = db.get_trek(trek_id)
        if fresh is not None:
            trek_doc = fresh
        scope = trek_doc.get("scope") or []
        if not scope:
            # Same fallback path as the progress-check loop; without a
            # target project we have nowhere to post.
            continue
        # ms-95 / e-2639 — use the scope-wide resolver and pick the
        # first canonical project as the idle escalation's notify
        # surface. Idle escalation goes to the ``notify`` channel
        # (= user-facing, notify-user-only), not the per-member dm
        # rail used by progress-check / leader-digest. Posting to
        # scope[0] keeps the legacy single-bus contract; the dm
        # transport migration covers the AI-wake rails only.
        scope_project_ids = _resolve_trek_scope_project_ids(trek_doc)
        if not scope_project_ids:
            continue
        target_project_id = scope_project_ids[0]
        payload = trek_scheduler_mod.build_idle_escalation_payload(
            trek_doc, now=now,
        )
        # Notify channel is the user-facing audit surface. Delivery is
        # notify-user-only (= surface in inbox, do not auto-execute).
        try:
            envelope_obj = envelope_mod.issue_t1_system_envelope(
                project_id=target_project_id,
                trek_id=trek_id,
                actions_authorized=["trek.idle_escalation"],
                data_class="free",
                ttl_seconds=3600,
            )
        except ValueError:
            envelope_obj = None
        bus_data = {
            "channel": "notify",
            "sender_session_id": "",
            "payload": payload,
            "envelope": envelope_obj,
            "delivery": "notify-user-only",
            "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        try:
            event_id = db.append_bus_event(target_project_id, bus_data)
        except Exception:
            continue
        meta = trek_doc.setdefault("meta", {})
        meta["last_idle_escalation_at"] = now.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        trek_doc["updated_at"] = trek_mod.utcnow_iso()
        try:
            db.save_trek(trek_id, trek_doc)
        except Exception:
            continue
        escalations.append({
            "trek_id": trek_id,
            "project_id": target_project_id,
            "event_id": event_id,
            "idle_minutes": payload.get("idle_minutes"),
        })

    # ms-75 / e-2067: auto-stall pass. Independent of cadence-fire because
    # the safety net must fire whenever a working task crosses its TTL —
    # regardless of whether the trek's progress-check is due this tick.
    # This catches executors that recognised the trek-progress-check event
    # but skipped state transition (= silent-ack pathology empirically
    # observed in the 2026-06-19 dogfood). The transition to waiting-review
    # is reversible: VALID_TASK_STATE_TRANSITIONS allows waiting-review →
    # working, so a false-positive auto-stall is recoverable by the leader.
    auto_stalled: list[dict] = []
    for trek_doc in candidate_treks:
        trek_id = trek_doc.get("trek_id", "")
        # Re-read to pick up in-tick mutations (= the cadence-fire pass
        # above may have bumped last_progress_check_at on this trek and
        # we want the freshest task_states view for stall detection).
        fresh = db.get_trek(trek_id)
        if fresh is not None:
            trek_doc = fresh
        # ms-97 / e-2612 (AC32) — halt 中 は auto-stall も停止。
        # detect_auto_stalled_tasks 内部にも halt guard はあるが、
        # 「server tick endpoint 側で halt なら全 autonomous activity を
        # 止める」 という contract を一箇所で読み取れるよう、ここでも
        # 明示的に skip する (= 多層 defence)。
        if trek_mod.is_halted(trek_doc):
            continue
        stalled = trek_scheduler_mod.detect_auto_stalled_tasks(
            trek_doc, now=now,
        )
        if not stalled:
            continue
        scope = trek_doc.get("scope") or []
        if not scope:
            continue
        target_pid = scope[0].get("project", "")
        if not target_pid:
            continue
        leader_sid = trek_doc.get("leader_session_id") or ""
        for s in stalled:
            task_id = s["task_id"]
            silence = s["silence_minutes"]
            ttl = s["ttl_minutes"]
            note = trek_scheduler_mod.build_auto_stall_note(silence)
            # ms-88 / e-2107: 罰則先 を `waiting-review` → `leader_review` に変更
            # (= 5 状態 state machine 厳密化、 「leader 判断要請」 と「user 判断
            # 要請」 の conflate 解消)。 set_task_state は legacy migration を
            # 経由するので old-schema 既存データとの interop は silent。
            try:
                trek_mod.set_task_state(
                    trek_doc,
                    task_id=task_id,
                    state="leader_review",
                    updated_by_session_id="",  # = server-initiated
                    note=note,
                )
            except ValueError:
                # Another tick already transitioned this task (= race
                # window); skip silently and continue with other stalls.
                continue
            try:
                db.save_trek(trek_id, trek_doc)
            except Exception:
                continue
            event_id = ""
            if leader_sid:
                try:
                    envelope = envelope_mod.issue_t1_system_envelope(
                        project_id=target_pid,
                        trek_id=trek_id,
                        actions_authorized=["trek.task_review"],
                        data_class="free",
                        ttl_seconds=3600,
                    )
                except Exception:
                    envelope = None
                review_payload = {
                    "kind": "trek-task-review",
                    "trek_id": trek_id,
                    "task_id": task_id,
                    "state": "leader_review",
                    "note": note,
                    "updated_by_session_id": "",
                    "recipient_session_id": leader_sid,
                    "auto_stalled": True,
                    "silence_minutes": silence,
                    "ttl_minutes": ttl,
                    "body": (
                        f"[Trek task auto-stalled] trek_id={trek_id} "
                        f"task_id={task_id} silence={silence} min "
                        f"(TTL={ttl})\n"
                        f"executor が working state のまま {silence} 分無活動。"
                        f" server-side TTL safety net (= e-2067 / ms-88 e-2107) "
                        f"が leader_review に降格しました。\n"
                        f"次の action: /beacon-trek-review {trek_id} "
                        f"{task_id} で done / user_review (forward) / "
                        f"working (re-work + 方針 DM) を選んでください。 "
                        f"false-positive なら leader_review → working に "
                        f"re-stamp で復旧可能。"
                    ),
                    "created_at": trek_mod.utcnow_iso(),
                }
                bus_data = {
                    "channel": "trek-task-review",
                    "sender_session_id": "",
                    "payload": review_payload,
                    "envelope": envelope,
                    "delivery": "auto-execute",
                    "created_at": trek_mod.utcnow_iso(),
                }
                try:
                    event_id = db.append_bus_event(target_pid, bus_data)
                except Exception:
                    event_id = ""
            auto_stalled.append({
                "trek_id": trek_id,
                "task_id": task_id,
                "silence_minutes": silence,
                "ttl_minutes": ttl,
                "event_id": event_id,
            })

    return {
        "now": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "candidates": len(candidate_treks),
        "due": len(due_treks),
        "fired": fired,
        "escalations": escalations,
        "auto_stalled": auto_stalled,
        "errors": errors,
        "quiesced": quiesced,
        # ms-97 / e-2612 (AC32) — treks skipped because halt is set.
        # Separate from ``quiesced`` (= terminal task-state aggregate)
        # so observers can tell "leader pulled the cord" apart from
        # "work is genuinely done".
        "halted": halted,
    }


class CheckTaskAddRequest(BaseModel):
    """Body for POST /api/projects/{id}/bus/envelope/check-task-add (ms-83 / e-2000).

    Pure verify endpoint: given an envelope and a target MS, return
    whether the AI may add a task autonomously (auto), should propose
    to the user (propose), or must be rejected outright (reject).

    The endpoint runs the regular 9-step envelope verify pipeline first.
    If verify passes, it then evaluates the action × tier matrix for
    ``task.add`` against the target MS. T1 always permits. T2 permits
    when the Operation scope enumerates the MS. T1-system permits when
    the Trek scope (= server-side trek doc) includes the MS. Anything
    else degrades to propose-to-ai.
    """
    envelope: dict
    target_ms: str
    payload: dict = {}


@app.post("/api/projects/{project_id}/bus/envelope/check-task-add")
def check_task_add_envelope(
    project_id: str,
    body: CheckTaskAddRequest,
    user: dict = Depends(require_auth),
):
    """Decide whether ``task.add`` against ``target_ms`` is auto / propose / reject.

    ms-83 / e-2000. Membership-gated read; the receiver project's
    members are the ones running their AI session through this gate.
    """
    _require_project_role(project_id, user)
    if not body.target_ms:
        raise HTTPException(
            status_code=400,
            detail="target_ms required",
        )

    # Step 1: 9-step verify. Failure → reject (= envelope is broken,
    # don't even propose).
    nonce_store = _get_envelope_nonce_store()
    parent_lookup = _get_envelope_parent_lookup()
    result = envelope_mod.verify(
        body.envelope,
        project_id=project_id,
        payload=body.payload or {},
        requested_action=envelope_mod.TASK_ADD_ACTION,
        nonce_store=nonce_store,
        parent_lookup=parent_lookup,
        sender_session_id="",
    )
    if not result.passed:
        return {
            "permit": "reject",
            "reason": result.rejection_reason or "envelope_verify_failed",
            "steps": result.steps,
        }

    # Step 2: tier-aware scope match.
    tier = body.envelope.get("tier", "")
    if tier == envelope_mod.TIER_T1:
        return {"permit": "auto", "reason": "t1_unrestricted"}
    if tier == envelope_mod.TIER_T2:
        if envelope_mod.check_task_add_scope_match(
            body.envelope, body.target_ms,
        ):
            return {"permit": "auto", "reason": "t2_scope_match"}
        return {
            "permit": "propose",
            "reason": "t2_scope_mismatch_propose_to_ai",
        }
    if tier == envelope_mod.TIER_T1_SYSTEM:
        # T1-system requires a Trek scope walk because the envelope only
        # carries trek:<id>, not the MS list directly.
        scope = body.envelope.get("scope", "") or ""
        if not scope.startswith("trek:"):
            return {
                "permit": "reject",
                "reason": "t1_system_scope_malformed",
            }
        trek_id = scope[len("trek:"):]
        trek_doc = db.get_trek(trek_id)
        if trek_doc is None:
            return {
                "permit": "reject",
                "reason": "t1_system_trek_not_found",
            }
        if trek_doc.get("status") != "active":
            return {
                "permit": "reject",
                "reason": "t1_system_trek_not_active",
            }
        if envelope_mod.trek_scope_includes_ms(trek_doc, body.target_ms):
            return {"permit": "auto", "reason": "t1_system_trek_scope_match"}
        return {
            "permit": "propose",
            "reason": "t1_system_trek_scope_mismatch_propose_to_ai",
        }
    # T3 / T5 / unknown → never auto.
    return {"permit": "propose", "reason": "tier_not_eligible_for_auto"}


def _get_envelope_nonce_store():
    """Lazy-resolve the envelope nonce store binding.

    For the test path we need to reach the same store the bus-event
    flow uses. The store has process scope so we keep one module-level
    singleton here.
    """
    global _envelope_nonce_store_singleton
    try:
        return _envelope_nonce_store_singleton
    except NameError:
        _envelope_nonce_store_singleton = envelope_mod.InMemoryNonceStore()
        return _envelope_nonce_store_singleton


def _get_envelope_parent_lookup():
    """Return a parent_lookup that consults the bus_event store.

    For task.add we never have in_reply_to chains (= scheduler-fired
    envelopes have no parent), so a constant-None lookup is sufficient.
    """
    return envelope_mod.FunctionParentLookup(lambda _pid, _eid: None)


class T1SystemEnvelopeRequest(BaseModel):
    """Body for POST /api/projects/{project_id}/bus/envelope/t1-system/issue.

    ms-83 / e-1995. Used by the server-side scheduler (= the periodic loop
    that fires "next, please" progress-check DMs into a Trek's claim
    session) to mint a T1-equivalent envelope for an active Trek scope.
    The caller must present the shared scheduler key in the
    ``X-Beacon-Scheduler-Key`` header so a user account cannot pose as
    the server.
    """
    trek_id: str
    actions_authorized: list[str] = []
    data_class: str = "free"
    ttl_seconds: int = 3600
    conversation_id: Optional[str] = None


@app.post("/api/projects/{project_id}/bus/envelope/t1-system/issue")
def issue_t1_system_bus_envelope(
    project_id: str,
    body: T1SystemEnvelopeRequest,
    request: Request,
):
    """Issue a T1-system bus envelope (ms-83 / e-1995).

    Authorization: the request MUST present a matching
    ``X-Beacon-Scheduler-Key`` header. This endpoint is the only path
    that mints ``tier=T1-system`` envelopes; receiver-side verify
    cross-checks that ``issuer=beacon-system`` and ``scope=trek:<id>``.

    Validation:
      * The trek must exist and be ``active``. ``planning`` / ``archived``
        Treks cannot have server-mint authority.
      * ``actions_authorized`` is validated by the envelope module
        (strict enumeration, no wildcards).
    """
    # Internal-only authorization. The shared secret is rotated out of
    # band in production; the dev fallback lets the test suite run.
    provided = request.headers.get("X-Beacon-Scheduler-Key", "")
    expected = envelope_mod.scheduler_internal_key()
    if not provided or provided != expected:
        raise HTTPException(
            status_code=403,
            detail="T1-system mint requires X-Beacon-Scheduler-Key",
        )
    # Trek validity gate. The mint is bounded to active Treks so a
    # planning Trek can't accidentally receive auto-execute DMs.
    trek_doc = db.get_trek(body.trek_id)
    if trek_doc is None:
        raise HTTPException(
            status_code=404,
            detail=f"Trek {body.trek_id!r} not found",
        )
    if trek_doc.get("status") != "active":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trek {body.trek_id!r} is not active "
                f"(status={trek_doc.get('status')!r})"
            ),
        )
    try:
        env = envelope_mod.issue_t1_system_envelope(
            project_id=project_id,
            trek_id=body.trek_id,
            actions_authorized=body.actions_authorized,
            data_class=body.data_class,
            ttl_seconds=body.ttl_seconds,
            conversation_id=body.conversation_id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"T1-system envelope issuance rejected: {e}",
        )
    return env


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
    for polling-style catch-up; ``channel`` for server-side routing filter.

    ms-93 / e-2275: DM-channel events have their ``payload`` redacted before
    return when the caller is neither sender nor recipient. Sidecar metadata
    (event_id, channel, sender_session_id, created_at, receipt timestamps,
    envelope view) stays visible so audit/diagnostics tooling keeps working.
    """
    _load(project_id, user)
    events = db.list_bus_events(
        project_id, since=since, channel=channel, limit=limit,
    )
    return _apply_dm_payload_visibility(project_id, events, _caller_uid(user))


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


# ---------------------------------------------------------------------------
# DM payload visibility boundary (ms-93 / e-2275)
#
# Routing (above) decides "who gets a push" — visibility (below) decides
# "what the response shows" for direct-read endpoints (GET /bus, GET
# /bus/unread, GET /bus/{event_id}). The two are orthogonal: a non-recipient
# project member can still legitimately call GET /bus to inspect bus activity
# (= sidecar status / cursor diagnostics), but the DM body itself is private
# to (sender, recipient).
#
# Without this gate, every project member who can list bus events sees the
# full ``payload`` field of every DM, including conversations they were not
# party to. The Web UI / `beacon bus audit` rely on sidecar metadata
# (event_id, channel, sender_session_id, created_at, delivered_at, opened_at,
# envelope verify result) which are NOT secrets — only the free-text DM
# body is.
#
# The redaction strategy:
#   * Non-DM channels: untouched. Broadcast events have no privacy contract.
#   * DM channels: resolve sender/recipient user_ids via the project session
#     registry (same lookup the post-side gate uses, ``_resolve_bus_event_user_ids``).
#     If the caller's user_id matches either party, return the event verbatim.
#     Otherwise replace ``payload`` with the redaction marker and keep every
#     other field (sidecar metadata stays visible).
#
# A 403 was considered and rejected: a caller who can read ``GET /bus`` MUST
# still get the list back for non-DM bus traffic and for sidecar bookkeeping
# on DM events they don't own. Dropping the request entirely would force
# the Web UI to special-case DM-only filtering before issuing the read,
# which is an unnecessary client-side coupling. Stripping the payload field
# is the minimum-surface change that satisfies AC1+AC2 simultaneously.
# ---------------------------------------------------------------------------

_PAYLOAD_REDACTED = {"redacted": True, "reason": "dm_payload_visibility"}


def _redact_dm_payload(event: dict) -> dict:
    """Return a shallow copy of ``event`` with ``payload`` stripped to the
    redaction marker. Sidecar fields (event_id, channel, sender_session_id,
    created_at, delivery, *_at receipts, envelope view) are preserved.
    """
    if not isinstance(event, dict):
        return event
    redacted = dict(event)
    redacted["payload"] = dict(_PAYLOAD_REDACTED)
    return redacted


def _caller_can_see_dm_payload(
    event: dict,
    caller_uid: str,
    sender_uid: str,
    receiver_uid: str,
) -> bool:
    """Decide whether ``caller_uid`` is sender or recipient of ``event``.

    Empty-string caller_uid (= dev mode / unauthenticated path) collapses to
    "can see everything" since the auth layer is what enforces identity in
    the first place. Empty-string sender/receiver (= unknown — e.g. the
    sender session was never registered) are treated conservatively: caller
    cannot match an empty party, so the payload stays redacted unless the
    caller is the other (known) party.
    """
    if not caller_uid:
        # Dev mode (no auth) or anonymous read — same trust model as
        # _require_project_role's auth_disabled bypass.
        return True
    if sender_uid and caller_uid == sender_uid:
        return True
    if receiver_uid and caller_uid == receiver_uid:
        return True
    return False


def _apply_dm_payload_visibility(
    project_id: str,
    events: list[dict],
    caller_uid: str,
) -> list[dict]:
    """Walk ``events`` and redact the ``payload`` of any DM-channel event
    the caller is neither sender nor recipient of.

    Performs the session→user_id resolution in a single ``list_sessions``
    pass to keep this on the same cost shape as the existing
    ``_resolve_bus_event_user_ids`` (one DB roundtrip regardless of event
    count).
    """
    if not events:
        return events
    if not _auth_enabled:
        # Dev mode: auth is disabled, so identity-based visibility gating is
        # meaningless. Skip the resolve+redact pass entirely so local dev
        # against ``BEACON_AUTH_ENABLED=0`` keeps seeing full DM bodies (the
        # same trust model _require_project_role uses to bypass the role
        # check in dev).
        return events
    has_dm = any((e.get("channel") or "") in _DM_CHANNELS for e in events)
    if not has_dm:
        return events
    # Build sid → uid lookup once.
    try:
        sessions = db.list_sessions(project_id)
    except Exception:
        # Backend unavailable: fail closed for DM payloads (drop the body
        # rather than risk leaking) but keep sidecar visible.
        return [
            _redact_dm_payload(e)
            if (e.get("channel") or "") in _DM_CHANNELS
            else e
            for e in events
        ]
    sid_to_uid = {
        str(s.get("session_id") or ""): str(s.get("user_id") or "")
        for s in sessions
        if s.get("session_id")
    }
    out: list[dict] = []
    for ev in events:
        channel = ev.get("channel") or ""
        if channel not in _DM_CHANNELS:
            out.append(ev)
            continue
        sender_sid = str(ev.get("sender_session_id") or "")
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        recipient_sid = str(payload.get("recipient_session_id") or "")
        sender_uid = sid_to_uid.get(sender_sid, "")
        receiver_uid = sid_to_uid.get(recipient_sid, "")
        if _caller_can_see_dm_payload(ev, caller_uid, sender_uid, receiver_uid):
            out.append(ev)
        else:
            out.append(_redact_dm_payload(ev))
    return out


def _caller_uid(user: dict | None) -> str:
    """Extract the caller's stable user_id from a require_auth claims dict.

    Empty string when ``user`` is None (internal callers) or carries no
    ``sub`` claim (= auth disabled in dev). Callers that pass this to
    ``_caller_can_see_dm_payload`` will then get the "see everything" branch,
    matching the existing dev-mode bypass semantics.
    """
    if not user:
        return ""
    return str(user.get("sub") or "")


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
    # Cost-reduction: this is one of the highest-volume polling endpoints
    # (bus.mjs hits it every 2-3 seconds per session). The handler body
    # never reads data["milestones"] — it only needs the meta doc for the
    # role check. Meta-only load turns 97 Firestore reads into 1.
    _load_meta_only(project_id, user)
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
    # ms-93 / e-2275: redact DM payloads the caller isn't a party to. The
    # recipient_id query param is the session asking for its inbox, but the
    # *caller* is the authenticated user behind that request. In dogfood,
    # callers typically pass their own recipient_id (= bridge polling for
    # itself), so this is a no-op for the common path; the guard catches
    # the cross-user case where a member queries another session's unread.
    return _apply_dm_payload_visibility(project_id, filtered, _caller_uid(user))


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
    # Cost-reduction: cursor advance follows every bus/unread poll (paired
    # write after read). Handler only needs the meta doc for auth — the
    # cursor state itself lives in bus_cursors, not data["milestones"].
    _load_meta_only(project_id, user)
    return db.advance_bus_cursor(project_id, recipient_id, body.last_seen_at)


@app.get("/api/projects/{project_id}/bus/cursors/{recipient_id}")
def get_bus_cursor(
    project_id: str,
    recipient_id: str,
    user: dict = Depends(require_auth),
):
    """Return the current cursor for ``recipient_id`` ({} if unset)."""
    # Cost-reduction: read-only cursor fetch, no milestones touched.
    _load_meta_only(project_id, user)
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
    sub-session. We do require project membership via _load_meta_only. The
    ``actor.email`` already on the session document is the audit trail for
    who actually owns it.
    """
    # Cost-reduction: intent stamping writes to the session doc only.
    # Membership check needs the meta doc; milestones are never read.
    _load_meta_only(project_id, user)
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
    # Cost-reduction: receipt ack writes to the bus event doc only.
    # Handler only needs auth via the meta doc; no milestones access.
    _load_meta_only(project_id, user)
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

    ms-93 / e-2275: when the event is on a DM channel and the caller is
    neither sender nor recipient, the ``payload`` field is replaced with a
    redaction marker — sidecar fields (delivery, *_at, channel, etc.) stay
    visible so the 3-stage display still renders.
    """
    _load(project_id, user)
    event = db.find_bus_event(project_id, event_id)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail=f"bus event {event_id!r} not found",
        )
    redacted = _apply_dm_payload_visibility(project_id, [event], _caller_uid(user))
    return redacted[0]


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
    # v2 hydration は Firestore 専用 (db.get_db())。dynamodb / mysql backend では
    # 全 project が v1 unified なので skip する (= store_router に get_db が無く
    # AttributeError になるのも防ぐ、ms-96 e-2379)。
    if os.environ.get("BEACON_STORE_BACKEND", "firestore").lower() != "firestore":
        return data
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


def _enrich_project_slim(data: dict) -> dict:
    """Slim variant for WS broadcast — drops tab-scoped heavy arrays.

    ms-84 / e-2326 (= initial fix) + follow-up: dropping entries[] alone left
    the Beacon project at 413 KB which is still above whatever WS frame size
    Cloud Run / GFE tolerates in practice (= 5 retries of wss:// confirmed
    1006 close at 173 KB and 413 KB, while 32 KB went through). Profiling the
    slim payload pointed at top-level arrays that the dashboard doesn't need:
    pushes 311 KiB / deployments 80 KiB / worktree_sessions 5 KiB. These are
    Releases / Worktree tab content. We drop them from the WS broadcast and
    the Web UI / Tauri fetch them via tab-specific REST endpoints on demand:

      GET /api/projects/{id}/pushes
      GET /api/projects/{id}/deployments
      GET /api/projects/{id}/worktree-sessions

    Milestones keep total_tasks / done_tasks so card summaries stay accurate;
    entries[] is still fetched per-MS on expand via
    GET /api/projects/{id}/milestones/{ms_id}/entries. Operations stays in
    slim because the dashboard renders operation cards inline.

    REST GET /api/projects/{id} (= default, full) is unchanged so CLI / Tauri
    IPC consumers continue to see the complete payload.
    """
    enriched = {**data}
    for tab_scoped in ("pushes", "deployments", "worktree_sessions"):
        enriched.pop(tab_scoped, None)
    milestones = []
    for ms in data.get("milestones", []):
        entries = ms.get("entries", [])
        total, done = core.count_task_status(entries)
        slim_ms = {k: v for k, v in ms.items() if k != "entries"}
        slim_ms["total_tasks"] = total
        slim_ms["done_tasks"] = done
        milestones.append(slim_ms)
    enriched["milestones"] = milestones
    return enriched


def _enrich_project_active_only(
    data: dict, *, drop_done_entries: bool = True
) -> dict:
    """Active-only enrichment — drop done milestones (and optionally done entries).

    Cost / payload reduction sibling of :func:`_enrich_project` and
    :func:`_enrich_project_slim`. A typical Beacon project accumulates a
    long tail of ``status="done"`` milestones (and each surviving milestone
    accumulates a long tail of ``status="done"`` entries). Most polling
    consumers (dashboard "what's happening now" views, status widgets,
    Trek executor progress checks) only care about work that is currently
    in flight. This helper filters both layers so callers that opt in can
    ship a much smaller payload without touching the storage layer.

    Contract:
      * Milestones with ``status == "done"`` are dropped entirely.
      * When ``drop_done_entries=True`` (default), remaining milestones
        have their ``entries`` list filtered to items whose ``status`` is
        not ``"done"``. When ``False``, all entries on surviving
        milestones are preserved.
      * All other fields on data / milestones / entries are pass-through.
      * Same computed fields as :func:`_enrich_project`
        (``total_tasks``, ``done_tasks``, ``entries_to_json``) are added.
        The counts are computed on the FULL entries list (before the
        done-drop) so consumers can still see "3 of 5 done" summaries
        even when the done entries are not returned inline.

    Deliberately NOT wired into any endpoint. Add on an opt-in basis in a
    follow-up so we can measure impact per caller instead of silently
    changing behavior of existing consumers.
    """
    enriched = {**data}
    milestones = []
    for ms in data.get("milestones", []):
        if ms.get("status") == "done":
            continue
        entries = ms.get("entries", []) or []
        total, done = core.count_task_status(entries)
        kept_entries = entries
        if drop_done_entries:
            kept_entries = [
                e for e in entries if e.get("status") != "done"
            ]
        milestones.append({
            **ms,
            "entries": core.entries_to_json(kept_entries),
            "total_tasks": total,
            "done_tasks": done,
        })
    enriched["milestones"] = milestones
    return enriched


async def _broadcast(project_id: str, data: dict):
    """Notify subscribed WS clients that the project changed (ms-84 / e-2326).

    Signal-only payload (~30 bytes). The previous attempts (full payload →
    slim w/ entries dropped → slim w/ tab-scoped arrays dropped) all hit a
    Cloud Run / GFE WS frame tolerance that's tighter than expected (=
    measured: 17.6 KiB works, 84 KiB consistently 1006-closes). Instead of
    chasing the threshold by stripping more fields, we make the WS a pure
    signal channel: clients fetch the actual state via REST (which has no
    frame limit). This is a permanent fix to the frame-size class of bugs.
    """
    clients = _ws_connections.get(project_id, set()).copy()
    if not clients:
        return
    msg = {"type": "project_changed"}
    for ws in clients:
        try:
            await ws.send_json(msg)
        except Exception:
            _ws_connections.get(project_id, set()).discard(ws)


def _apply_op_and_broadcast(project_id: str, op, *,
                            op_name: str = "project.update",
                            actor: str = "",
                            reason: str = "",
                            project_file: Optional[str] = None):
    """``operations.apply_operation`` + explicit WS broadcast (ms-43 / e-2128).

    Thin wrapper: run apply_operation normally; on success, fan out the new
    project state to subscribed WS clients via ``_broadcast_project_after_write``.
    Use this from HTTP write endpoints in place of ``operations.apply_operation``
    so every write deterministically reaches live clients (= dogfood病理
    の構造解、 listener 不在経路で dark にならない)。

    Errors from apply_operation propagate normally (= HTTP 4xx/5xx is fired by
    the caller). Broadcast itself is fail-safe (= broadcast 失敗で write を
    巻き戻さない、 _broadcast_project_after_write が内部で吸収)。
    """
    result = operations.apply_operation(
        project_id, op,
        op_name=op_name,
        actor=actor,
        reason=reason,
        project_file=project_file,
    )
    _broadcast_project_after_write(project_id)
    return result


def _broadcast_project_after_write(project_id: str) -> None:
    """Explicit fan-out of the latest project state after a write (ms-43).

    Background — dogfood (2026-06-19) で観測された病理: cloud mode で
    ``beacon milestone add`` を打っても WebUI に live 反映されない。 原因は
    PUT /api/projects + apply_operation 経路が broadcast を Firestore の
    ``on_snapshot`` listener に **完全に委ねている** 設計にあった。 listener は
    silent disconnect (= 長時間 idle 後の Firestore SDK 仕様)、 multi-instance
    Cloud Run の watcher 偏り、 identical-content write の SDK dedup 等で
    fire しなくなる経路を持つ。 fire しないと WebUI 側は **dark** のままに
    なる (= 「broadcast されない write」 が物理的に存在する状態)。

    構造解 (= ms-43 / e-2128 path): broadcast を listener に依存させない。
    write 経路の HTTP endpoint で必ず本 helper を呼んで explicit broadcast を
    打つ。

    ms-84 / e-2325 更新: listener (= ``_on_snapshot``) は disable (=
    ``_start_watcher`` が no-op に変更) 。 1 write が explicit + listener の
    2 経路で fire していたのが over-broadcast 病理の構造源で、 単一 instance
    posture (min=max=1) では fallback 不要、 explicit 1 本に集約する。 詳細
    は ``_start_watcher`` の docstring 参照。

    Behavior:
      * No-op when no WS clients are subscribed to ``project_id`` (= 速攻 return)
      * No-op when ``_event_loop`` is unset (= startup hook 未発火、 lambda
        lifespan=off 経路、 cold-start race)
      * fail-safe: load_project_consistent 失敗 / broadcast 失敗で write 経路
        を巻き戻さない、 caller 視点では fire-and-forget
      * thread-safe: ``asyncio.run_coroutine_threadsafe`` で worker thread から
        event loop に乗せる (= apply_operation は同期 path で呼ばれる)
    """
    if not _ws_connections.get(project_id):
        return
    if _event_loop is None:
        return
    try:
        data = operations.load_project_consistent(project_id)
    except Exception:
        # load 失敗で broadcast 諦め、 write の成功は影響させない (= UX 上の
        # 遅延は許容、 listener fallback が後から拾うかもしれない)。
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _broadcast(project_id, data), _event_loop
        )
    except Exception:
        # event loop が閉じてる等の race。 fire-and-forget なので silent skip。
        return


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
    """Firestore on_snapshot callback (runs in background thread).

    Retained but no longer wired up after ms-84 / e-2325. Kept so the v2
    milestone-hydration contract test (tests/test_ws_v2_snapshot_hydrates_milestones.py)
    can still exercise the function directly. If the listener is ever
    re-attached, the body still hydrates v2 milestones correctly before
    broadcasting.
    """
    for doc in doc_snapshot:
        data = doc.to_dict()
        if _event_loop and _ws_connections.get(project_id):
            hydrated = _hydrate_v2_milestones(project_id, data)
            asyncio.run_coroutine_threadsafe(
                _broadcast(project_id, hydrated), _event_loop
            )


def _start_watcher(project_id: str):
    """No-op stub (ms-84 / e-2325).

    Previously attached a Firestore ``on_snapshot`` listener as a cross-
    instance broadcast fallback. The listener fires once per project doc
    write *in addition to* the explicit broadcast from
    ``_broadcast_project_after_write`` — so every write produced two
    broadcasts. After e-2326 made WS payloads signal-only
    (``{"type":"project_changed"}``), the two broadcasts are byte-identical
    and the comment at ``_broadcast_project_after_write`` claiming
    "client-side JSON dedup absorbs the duplicate" is structurally false
    (identical payloads have no dedup signal). User-observed symptom:
    Web UI WS Messages tab shows a row every 2-3 seconds with no
    user-visible writes (e-2325 motivation).

    Cloud Run is currently pinned to single-instance
    (``--min-instances=1 --max-instances=1`` in
    ``.github/workflows/deploy-cloud-run.yml``), so the cross-instance
    fanout fallback this listener provided is structurally unneeded —
    every write hits the same instance that owns the WS connections.
    The explicit broadcast in ``_broadcast_project_after_write`` covers
    100% of real-time updates in this posture.

    If multi-instance is restored later, the right design is a proper
    pub/sub layer (Cloud Pub/Sub) — not ``on_snapshot``, which is fragile
    (silent disconnect, identical-content dedup, multi-watcher偏り —
    see line 5121 comment, same trade-off).

    The function name + signature is kept as a no-op so existing test
    fixtures that patch it as ``lambda pid: None`` still resolve.
    """
    return


def _stop_watcher(project_id: str):
    """No-op stub (ms-84 / e-2325). See ``_start_watcher`` for rationale."""
    return


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

    # ms-84 / e-2326 — signal-only WS. Past attempts to push project state
    # over WS (full / slim / aggressively-slim) all hit a Cloud Run / GFE WS
    # frame tolerance somewhere in the 20-50 KB range, well under what
    # Starlette's `max_size` default (1 MiB) would suggest. Rather than
    # chasing that opaque limit, we send a tiny "ready" notification and
    # let the client pull the actual state via REST (which has no frame
    # limit and already returns the slim variant via ?slim=true). Subsequent
    # change events on this socket follow the same shape — type=project_changed
    # with no body — so the client's update path is "refetch on signal".
    await websocket.send_json({
        "type": "ws_ready",
        "project_id": project_id,
    })

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
            # local_dev: Web UI がローカル開発ログインフォームを出すかの判定に使う
            "local_dev": _local_dev_enabled,
        }
    # Firebase / Cloud Run 既存経路 (= 後方互換)
    return {
        "provider": "firebase",
        "client_id": os.environ.get("BEACON_OAUTH_CLIENT_ID", ""),
        # local_dev: ローカル時のみ true。本番 Cloud Run では env 未設定 = false。
        "local_dev": _local_dev_enabled,
    }


class DevLoginRequest(BaseModel):
    email: str
    name: str = ""


@app.post("/api/auth/dev-login")
def dev_login(body: DevLoginRequest):
    """ローカル専用・IdP 不要のログイン。任意の email に対して bcli トークンを発行する。

    BEACON_LOCAL_DEV=1 のときだけ有効 (= ハードゲート)。本番 (Cloud Run) は
    この env を設定しないので、ここは常に 404 を返し到達不能。Google / Cognito を
    立てずに、ローカルサーバを複数人が別アカウントで使い分けられるようにするための
    入口。発行したトークンは provider 非依存の HMAC なので require_auth が
    そのまま検証し、sub / email 単位でアカウントが分かれる。
    """
    if not _local_dev_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    # sub は email から決定論的に導出 (= 同じ email は同じアカウント)。
    sub = f"dev:{email}"
    token, expiry = _make_cli_token(sub, email)
    # member picker 等に出るよう先にユーザー登録しておく。
    db.get_or_create_user(sub, email)
    name = (body.name or "").strip()
    if name:
        db.update_user(sub, {"display_name": name})
    return {"status": "ok", "id_token": token, "email": email, "token_expiry": expiry}


@app.post("/api/auth/exchange-cli-token")
def exchange_cli_token(user: dict = Depends(require_auth)):
    """ms-43 / e-2298 — Web UI session を 30 日有効化する exchange endpoint。

    Web UI が初回ログイン直後 (= Firebase id_token を受け取った瞬間) に呼ぶ。
    既存 require_auth で id_token を検証 → _make_cli_token(sub, email) で
    bcli.* 形式の HMAC token (= 30 日 TTL、 CLI と同じ機構) を発行して返す。
    Web UI は以降 bcli token を Authorization header / WS の ?token= で
    使い、 Firebase id_token の固定 1 時間 TTL に縛られず session を持続する。

    Firebase / Cognito どちらでも動く (= require_auth が provider 非依存で
    成功すれば bcli token を発行する設計)。 dev-login の bcli token 発行
    ロジックを公開 endpoint として小さく切り出した形 (= 同 lib 関数 reuse)。
    """
    sub = user.get("sub", "")
    email = user.get("email", "")
    if not sub:
        raise HTTPException(status_code=400, detail="missing sub claim")
    token, expiry = _make_cli_token(sub, email)
    return {"status": "ok", "id_token": token, "email": email, "token_expiry": expiry}


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
