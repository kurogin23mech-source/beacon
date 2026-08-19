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
import threading
import time
import uuid
from typing import List, Optional

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
import org as org_mod  # ms-113 / e-3731: Organization (組織) テナンシー primitives
import principal as principal_mod  # ms-113 / e-3732: 主体モデル + 実効スコープ合成
import disclosure as disclosure_mod  # ms-111 / e-3872: cross-project Account 開示の read 配線
import master_projection  # ms-111 / e-3621: 投影 Account/Contact identity を master 経由で解決
import master_adapter  # ms-111 / e-3621: backend 配線済み Beacon-default master adapter
import master_binding  # ms-111 / e-3621 chunk2b: project の master 束縛宣言 (org_id 軸) を ingest で読む
import work_model  # ms-109 e-3643: 職種非依存の Target 正準ラベル tolerant reader
import sales_entities  # ms-108 e-5194: enrich siblings resolve the sales funnel identically
import dm_gate as dm_gate_mod  # ms-70 / e-1713: cross-user DM action authorization judge
import dm_consent as dm_consent_mod  # ms-110 / e-3443: sender-side cross-user consent backstop
import decision_event as decision_event_mod  # ms-90 / e-3246: decision-event 記録
import envelope as envelope_mod
import invitations as invitations_mod  # ms-78 e-1803/e-1804: token-based invites
import phantom_done_evidence as phantom_done_mod  # ms-95 / e-2726: task done evidence gate
import store_router as db  # e-1544: BEACON_STORE_BACKEND で firestore / dynamodb を切替
import operations
import redis_client  # ms-96 / e-2381: rate limit 用の揮発カウンタ (fail-open)
import trek as trek_mod  # ms-69 / e-1656: trek schema + pure mutators
import trek_scheduler as trek_scheduler_mod  # ms-83 / e-1997: progress-check cadence logic
import tick_scheduler  # ms-107 e-3434/e-3461: target-agnostic periodic-tick cadence
import deadline  # ms-139 e-4953: L2 締切エンジン (overdue 規則 + reminder dedup)
import occupation  # ms-142 e-5010: 職種非依存の Target/WorkItem 抽象イテレータ
import tick_health as tick_health_mod  # e-1391 / ms-66: tick-liveness evaluation

# e-1391 (ms-66) — last successful periodic tick, recorded by the
# trek-scheduler tick endpoint and read back by /api/system/tick-health so an
# external watchdog can catch a silently-dead tick driver. In-memory (per
# process): a restart resets it and the next tick re-baselines within ≤1 min.
_last_tick_at: str = ""
_last_tick_report: dict = {}
# e-1391 follow-up (review H1) — process start time, so tick-health can tell a
# just-booted server (never ticked yet = fine) from one up long enough that a
# missing/dead tick driver is overdue (= alert). In-memory like _last_tick_at.
_server_start_at: "datetime.datetime | None" = None

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
# Rate limiting (ms-96 / e-2381)
# ---------------------------------------------------------------------------
# app 側 Redis 固定窓レート制限。認証済なら user_id、無ければ client IP をキーに、
# window_seconds ごとの窓内で上限を超えたら 429 を返す。Redis 不通・未設定時は
# redis_client が None を返し fail-open (= 制限せず通す、可用性優先)。
#
# 数値ポリシー (window / 上限) は env で調整可能。より細かい上限ポリシーは運用側の
# 設定 (env / 外部供給) で与え、本体は「キー単位で上限を課す汎用機構」に閉じる。
_RATE_LIMIT_ENABLED = os.environ.get("BEACON_RATE_LIMIT", "1") != "0"
_RATE_LIMIT_WINDOW = int(os.environ.get("BEACON_RATE_LIMIT_WINDOW_S", "60"))
# 認証済 user は多セッション (bus poll 5s × N session) を許容する必要があるため
# IP より高め。既定は「制限が乱用者にだけ効き、正常運用では 429 にならない」水準。
_RATE_LIMIT_USER = int(os.environ.get("BEACON_RATE_LIMIT_USER_PER_WINDOW", "1200"))
_RATE_LIMIT_IP = int(os.environ.get("BEACON_RATE_LIMIT_IP_PER_WINDOW", "600"))

# 除外パス: liveness / WS handshake / ヘルスは対象外。bus poll 等の高頻度
# エンドポイントは除外せず、認証済 user の高め上限 (_RATE_LIMIT_USER) で吸収する。
_RATE_LIMIT_EXEMPT_PREFIXES = ("/api/version", "/health", "/ws/")


def _rate_limit_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _RATE_LIMIT_EXEMPT_PREFIXES)


def _rate_limit_identity(request: Request) -> tuple[str, str]:
    """レート制限キーを解決。認証済なら (user_id, "user")、無ければ (ip, "ip")。

    middleware は endpoint auth より前に走るので、Authorization ヘッダを best-effort
    で検証して user_id を得る。検証失敗 / auth 無効時は IP に fall back。
    """
    if _auth_enabled:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                claims = _verify_id_token(auth[len("Bearer "):])
                sub = str(claims.get("sub") or "")
                if sub:
                    return sub, "user"
            except Exception:
                # 無効トークンは IP scope に倒す (= 401 は endpoint auth が返す)。
                pass
    xff = request.headers.get("x-forwarded-for", "")
    ip = (xff.split(",")[0].strip() if xff
          else (request.client.host if request.client else "unknown"))
    return ip, "ip"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window rate limit keyed by user_id (authed) or client IP.

    Redis 不通時は fail-open (= 通す)。除外パスは無条件で通す。
    """

    async def dispatch(self, request: Request, call_next):
        if not _RATE_LIMIT_ENABLED or _rate_limit_exempt(request.url.path):
            return await call_next(request)
        ident, scope = _rate_limit_identity(request)
        limit = _RATE_LIMIT_USER if scope == "user" else _RATE_LIMIT_IP
        count = redis_client.incr_fixed_window(f"{scope}:{ident}", _RATE_LIMIT_WINDOW)
        # count is None ⇒ Redis unavailable ⇒ fail-open.
        if count is not None and count > limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too Many Requests"},
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
            )
        return await call_next(request)


# Added after AuditLogMiddleware so it wraps it (= runs first on each request),
# rejecting over-limit callers before audit/auth/handler work happens.
app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# Request-wide timeout (ms-145 / e-5317)
# ---------------------------------------------------------------------------
# 本番インシデント (2026-08): 認証時の外部依存ハングで 1 本のリクエストが約 3.6h
# 滞留し、単一ワーカーの本番プロセスが飢えて 502 で突然死した。個別の外部呼び出しに
# タイムアウトを入れる (e-5316 の証明書取得など) のが一次防御だが、「どこか 1 箇所で
# も timeout を入れ忘れたら数時間ハングしうる」状態は残る。ここでは HTTP リクエスト
# 全体に上限時間を課し、"数時間かかるリクエスト" を機構として物理的に不可能にする
# (= 多層防御の外殻)。上限超過は 504 Gateway Timeout で即座に閉じる。
#
# 限界の明示 (誠実に): asyncio.wait_for はコルーチンをキャンセルするので、await
# 点を持つ非同期経路は確実に打ち切れる。ただしワーカースレッド上で回る完全同期の
# ブロッキング呼び出し (timeout 無しの同期 I/O 等) は、504 を返してもスレッド自体は
# 解放されないことがある。だからこそ個別呼び出し側の timeout (e-5316) が一次防御で、
# 本 middleware はその取りこぼしを塞ぐ二次防御という位置づけ。
#
# WebSocket (= /ws/ の idle-wake 接続) は BaseHTTPMiddleware が http scope 以外を
# 素通しするため、この timeout の対象外 (長時間接続を壊さない)。
_REQUEST_TIMEOUT = float(os.environ.get("BEACON_REQUEST_TIMEOUT_S", "30"))


class RequestTimeoutMiddleware(BaseHTTPMiddleware):
    """Cap every HTTP request at a wall-clock ceiling, returning 504 on overrun.

    ``BEACON_REQUEST_TIMEOUT_S <= 0`` で無効化できる (= opt-out)。
    """

    async def dispatch(self, request: Request, call_next):
        if _REQUEST_TIMEOUT <= 0:
            return await call_next(request)
        try:
            return await asyncio.wait_for(
                call_next(request), timeout=_REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            _server_logger.warning(
                "Request timed out after %.1fs: method=%s path=%s",
                _REQUEST_TIMEOUT,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=504,
                content={
                    "detail": f"Request timed out after {_REQUEST_TIMEOUT:.1f}s"
                },
            )


# Added last so it is the outermost middleware: it wraps rate-limit, audit,
# CORS, and the handler, guaranteeing the whole request is bounded in time.
app.add_middleware(RequestTimeoutMiddleware)


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


# ms-145 / e-5316: Google 公開鍵 (証明書) 取得のタイムアウト & プロセス内キャッシュ。
#
# 本番インシデント (2026-08): id_token.verify_oauth2_token() は認証リクエストの
# たびに https://www.googleapis.com/oauth2/v1/certs を取りに行く。google-auth の
# 証明書 fetch に渡るタイムアウトはバージョン依存で、古い版では実質「無限待ち」に
# なりうる。googleapis 側の TLS 不調でこの fetch がハングすると、1 本のリクエストが
# 数時間滞留し (実測 elapsed_ms ≒ 3.6h)、単一ワーカーの本番プロセスが飢えて 502 で
# 突然死した。再起動で復旧するが根治にならない。
#
# 構造で閉じる (お願いでなく物理的に不可能にする):
#   1) 証明書取得に必ず有界タイムアウトを課す (ライブラリ既定に頼らず自前で明示)。
#   2) 取得済み証明書を TTL 付きでプロセス内キャッシュし、hot path から
#      ネットワーク往復そのものを消す (Google の署名鍵は日次ローテなので 1h TTL は安全)。
#   3) fetch が失敗しても有効なキャッシュがあれば stale-serve でしのぐ (可用性優先)。
#
# 秒単位の env var は既存の BEACON_RATE_LIMIT_WINDOW_S / BEACON_REQUEST_TIMEOUT_S と
# 揃えて _S サフィックスで単位を明示する。
_GOOGLE_CERTS_TIMEOUT_S = float(os.environ.get("BEACON_GOOGLE_CERTS_TIMEOUT_S", "5"))
_GOOGLE_CERTS_TTL_S = float(os.environ.get("BEACON_GOOGLE_CERTS_TTL_S", "3600"))
# google-auth の内部定数 (_GOOGLE_OAUTH2_CERTS_URL / _GOOGLE_ISSUERS) はアンダースコア
# 接頭辞の非公開 API で、バージョン更新で名前や値が変わると証明書取得・issuer 検証が
# silent に壊れる。ライブラリ内部に継ぎ目を貫通させず、自前の定数として 1 箇所に固定する。
_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
_GOOGLE_VALID_ISSUERS = frozenset(
    {"accounts.google.com", "https://accounts.google.com"}
)
_google_certs_cache: dict = {"certs": None, "fetched_at": 0.0}
_google_certs_lock = threading.Lock()


def _google_certs_cache_valid(force: bool, now: float) -> bool:
    """キャッシュ済み証明書がそのまま使えるか (TTL 内 かつ force でない)。

    double-checked locking のロック外/ロック内の 2 箇所から呼ぶ単一の真実源。
    """
    return (
        not force
        and _google_certs_cache["certs"] is not None
        and (now - _google_certs_cache["fetched_at"]) < _GOOGLE_CERTS_TTL_S
    )


def _fetch_google_certs(force: bool = False) -> dict:
    """Google の OAuth2 証明書を有界タイムアウト + TTL キャッシュで取得する。

    ``force=True`` は TTL を無視して強制再取得する (鍵ローテ直後に kid 不一致で
    検証失敗した時の 1 回リトライ用)。fetch 失敗時は有効なキャッシュがあれば
    stale-serve し、無ければ 503 を上げる (ハングでなく即座に失敗させる)。
    """
    from google.auth.transport import requests as google_requests

    cache = _google_certs_cache
    if _google_certs_cache_valid(force, time.monotonic()):
        return cache["certs"]

    with _google_certs_lock:
        # ロック取得後に再チェック (別スレッドが fetch 済みなら往復を省く)。
        if _google_certs_cache_valid(force, time.monotonic()):
            return cache["certs"]

        request = google_requests.Request()
        try:
            response = request(
                _GOOGLE_CERTS_URL,
                method="GET",
                timeout=_GOOGLE_CERTS_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 - どんな transport 例外もハングにしない
            if cache["certs"] is not None:
                return cache["certs"]
            raise HTTPException(
                status_code=503, detail=f"Could not fetch Google certs: {e}"
            )
        if response.status != 200:
            if cache["certs"] is not None:
                return cache["certs"]
            raise HTTPException(
                status_code=503,
                detail=f"Could not fetch Google certs: HTTP {response.status}",
            )
        certs = json.loads(response.data.decode("utf-8"))
        cache["certs"] = certs
        cache["fetched_at"] = time.monotonic()
        return certs


def _verify_google_id_token(token: str) -> dict:
    """Google ID token をキャッシュ済み証明書で検証する。

    ``id_token.verify_oauth2_token`` の挙動 (証明書 fetch → jwt.decode → issuer
    チェック) を再現しつつ、証明書取得だけを ``_fetch_google_certs`` に置き換えて
    タイムアウト & キャッシュを効かせる。検証失敗は 401、証明書取得失敗は 503。
    """
    from google.auth import jwt as google_jwt

    certs = _fetch_google_certs()
    try:
        claims = google_jwt.decode(token, certs=certs, audience=None)
    except ValueError:
        # kid 不一致 = 鍵ローテ直後の可能性。証明書を強制更新して 1 回だけ再試行。
        certs = _fetch_google_certs(force=True)
        try:
            claims = google_jwt.decode(token, certs=certs, audience=None)
        except ValueError as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
    if claims.get("iss") not in _GOOGLE_VALID_ISSUERS:
        raise HTTPException(status_code=401, detail="Invalid token: wrong issuer")
    return claims


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
    # Fall back to Google ID token verification (= Cloud Run 既存経路)。
    # ms-145 / e-5316: 証明書取得を有界タイムアウト + キャッシュ経路に置き換え、
    # googleapis のハングで本番プロセスが飢える経路を構造的に断つ。
    return _verify_google_id_token(token)


# ms-113 / e-3731: personal org (= 個人組織) の lazy ensure。
#
# require_auth は毎リクエスト走る hot path なので、org doc の get を毎回叩くと
# 直近のメモリ枯渇インシデント (= 高頻度経路の余計な store 読み) を再現しかねない。
# そこで「この process 内でこの user の personal org を既に ensure 済か」を
# in-process set でキャッシュし、store を叩くのは user あたり instance あたり
# 最初の 1 回だけに抑える (= 冪等 & 有界な追加負荷)。決定的 org id ゆえ、複数
# instance が同時に ensure しても同じ doc を指すので競合しても安全 (last-write
# が同じ内容)。
_ENSURED_PERSONAL_ORGS: set[str] = set()


def _ensure_personal_org(user_id: str, email: str = "") -> None:
    """user の personal org doc が無ければ作る (lazy retrofit, fail-safe)。

    org ストアが未配線の backend や一過性エラーでも auth を止めない (= best
    effort)。実際の認可は依然 project owner / members で判定するため、personal
    org doc の生成失敗はこの時点のアクセスを壊さない。
    """
    if not user_id or user_id in _ENSURED_PERSONAL_ORGS:
        return
    try:
        org_id = org_mod.personal_org_id(user_id)
        if db.get_org(org_id) is None:
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            db.save_org(org_id, org_mod.build_personal_org(user_id, email, now=now))
        # 成功時のみキャッシュ (= 失敗は次リクエストで再試行させる)。
        _ENSURED_PERSONAL_ORGS.add(user_id)
    except Exception:
        # best-effort: org backfill の失敗で auth を落とさない。
        pass


# ---------------------------------------------------------------------------
# Principal (主体) の解決と伝播 (ms-113 / e-3732, e-3733).
#
# principal は request の claims から **store I/O 無し** で組み立て、request.state
# に載せて伝播させる (= require_auth の hot path を汚さない)。開示判定に必要な
# 「実効スコープ (= 触れてよい project 集合)」は participation の解決を伴うので、
# それが要る endpoint だけが _effective_scope_for を lazy に呼ぶ。
# ---------------------------------------------------------------------------

def _build_request_principal(claims: dict, *, focus: str | None = None,
                             agent_kind: str = principal_mod.AGENT_CLIENT) -> dict:
    """claims から client principal を組み立てる (cheap, no store I/O)。"""
    uid = claims.get("sub", "") if claims else ""
    org_id = org_mod.personal_org_id(uid) if uid else ""
    return principal_mod.make_principal(
        uid, org_id, agent_kind=agent_kind, focus=focus)


def _user_participation(user_id: str) -> set[str]:
    """user が参加している project 集合を返す (= 開示境界の真値)。

    owner か members に居る project。``db.list_projects`` が既にこの可視性で絞る。
    query 時に評価する (= 現在の membership で判定 ⇒ 剥奪即時 / grandfather しない、
    e-3733)。fail-safe: 解決失敗は空集合 (= 何も開示しない安全側)。
    """
    if not user_id:
        return set()
    try:
        projs = db.list_projects(user_id) or []
    except Exception:
        return set()
    ids: set[str] = set()
    for p in projs:
        pid = p.get("project_id") or p.get("id")
        if pid:
            ids.add(pid)
    return ids


def _effective_scope_for(principal: dict) -> set[str]:
    """principal の実効 project 集合を、いま解決した participation を用いて返す。

    client principal 専用の facade (= endpoint が「この user に何が見えるか」を得る
    入口)。backend principal の min 合成は work-unit / originating を持つ呼び出し
    側が ``principal_mod.backend_effective_scope`` を直接使う。
    """
    participation = _user_participation((principal or {}).get("user_id", ""))
    return principal_mod.client_effective_scope(principal or {}, participation)


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """FastAPI dependency that enforces Bearer token auth and auto-registers users."""
    if not _auth_enabled:
        request.state.audit_user_id = "dev"
        request.state.audit_email = "dev@local"
        dev_claims = {"sub": "dev", "email": "dev@local"}
        # ms-113 / e-3732: propagate a principal even in dev mode (cheap, no I/O).
        request.state.principal = _build_request_principal(dev_claims)
        return dev_claims
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authorization header required")
    claims = _verify_id_token(credentials.credentials)
    # Auto-register user on first login
    user_id = claims.get("sub", "")
    email = claims.get("email", "")
    if user_id:
        db.get_or_create_user(user_id, email)
        # ms-113 / e-3731: ensure the user's personal org exists (cached, at
        # most one store touch per user per instance — see _ensure_personal_org).
        _ensure_personal_org(user_id, email)
    # Store for audit middleware
    request.state.audit_user_id = user_id
    request.state.audit_email = email
    # ms-113 / e-3732: build + propagate the client principal on request state.
    # Cheap (no store I/O); endpoints resolve effective scope lazily via
    # _effective_scope_for when they actually need disclosure filtering.
    request.state.principal = _build_request_principal(claims)
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
    # ms-113 / e-3731: lazy org retrofit. project が owner を持ち org_id 未設定
    # なら、owner の personal org (= 決定的 id) を stamp する。純粋な文字列導出
    # のみで store I/O は無い (O(1)) ため、全 write が通るこのチョークポイントで
    # 毎回呼んでも安全 (= hot-path のメモリ churn を作らない)。既存挙動は不変:
    # org_id が付いても認可は依然 owner / members で判定する (合成は e-3733)。
    org_mod.stamp_project_org(data)
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
    # ms-90 / e-3246: decision-event の背景 (= 直面した問題) と判断理由。
    # DM 発信を「問題駆動の相談」として decision_events ストリームに記録する
    # ためのメタデータ。本文 (payload) とは別に運ぶ (= 受信者には見せない)。
    # context は主役だが未指定でも送信は通す (= hard block しない)。
    context: str = ""
    rationale: str = ""
    # ms-110 / e-4001: DM 重複配信を構造で防ぐ冪等キー。
    # ``client_event_id`` はクライアントが 1 回の論理送信につき 1 つ生成する安定した
    # 識別子。送信結果が曖昧 (= ハング / timeout / 接続断で ack を受け取れず、サーバが
    # 処理したか不明) なとき、クライアントは同じ ``client_event_id`` で再送する。
    # サーバは初回送信でこのキーをイベントに刻み、``is_retry`` 付きの再送では同じキー
    # を持つ既存イベントを探して「重複を作らず既存を返す」(idempotent replay)。
    # 初回送信 (is_retry=False) には走査コストを一切かけない (= クライアントが「今は
    # 再送だ」と知っている唯一の主体なので、その申告に従う)。空なら従来どおり毎回新規。
    client_event_id: str = ""
    is_retry: bool = False


# EnvelopeIssueRequest now lives at MODULE scope in server/routers_projects.py
# (canonical home, used by make_bus_gate_router). Moved in ms-127 e-4871 PR3a.
# BusCursorAdvance and BusEventReceiptAck moved to routers_projects.py
# (canonical home, used by make_bus_delivery_router). Moved in ms-127 e-4871 PR3b.


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

# ms-127 e-4868 (B フェーズ scaffold): GET /api/version は
# server/routers_version.py の make_router() へ切り出し、下部の
# app.include_router(_make_version_router()) で mount する。app.py god-module
# 分割の型づくり (factory + include_router)。挙動不変 (純移動)。


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


def _session_is_live(s: dict, cutoff_iso: str) -> bool:
    """Whether a session counts as live for ``--live`` directory queries.

    e-3214 / e-3220: a gracefully shut-down session stamps ``shutdown=true``
    together with a fresh ``last_active`` (so ``healthy_only`` kills it
    immediately, e-2305). A recency-only check therefore keeps advertising a
    just-stopped daemon as live. BOTH directory endpoints — ``/api/me/sessions``
    (cross-project, the DEFAULT ``bus directory`` path) and
    ``/api/projects/{pid}/sessions`` — must agree, so the rule lives in ONE
    place. (2026-07-10 miss: e-3214 fixed only the per-project filter, leaving
    ``bus directory --live`` still leaking shutdown rows via /api/me/sessions.)
    """
    if bool(s.get("shutdown", False)):
        return False
    la = s.get("last_active", "")
    return bool(la) and la >= cutoff_iso


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


# ms-84 / e-2326 follow-up — tab-scoped REST endpoints. The slim WS broadcast
# drops these arrays so the frame fits inside Cloud Run's WS tolerance; the
# Web UI / Tauri fetch them lazily when the user switches to the matching
# tab (= Releases / Worktree). Each returns the raw array under a top-level
# key so the client can drop it straight into state.project.{name}.


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Entries (tasks / commits / notes)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Operation envelopes (ms-60 / e-1339)
#
# T2 envelope flow: SPEC doc declares approved_actions in YAML frontmatter →
# beacon operation approve mints a server-signed envelope from that list →
# the envelope record lives in projects/{id}/operation_envelopes/.
# Re-approve auto-revokes any prior active envelope for the same op_id.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Log (commit recording)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Related treks (ms-69 / e-1663) — reverse lookup from a project work item
# (milestone / operation / task) to the treks that include it in scope.
#
# Used by the e-1664 Related Treks widget on the project detail page.
# Archived treks are included by default so the widget can render historic
# associations ("we worked on this together in trek X, archived 2 weeks ago").
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Members (invite / remove)
# ---------------------------------------------------------------------------


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


# ms-127 e-4869: /api/admin/* は server/routers_admin.py へ切り出し済み
# (factory + include_router)。_require_admin と _apply_op_and_broadcast は
# app.py の他 endpoint も使う共有 helper なので app.py 所有のまま注入する。


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


# ms-109 e-3699 (fable B-2): opportunity/account narrow a sales Trek scope, the
# same way milestone/operation/task narrow a dev one. Both models carry the full
# occupation-agnostic vocabulary; the endpoints copy whichever keys are present
# via trek_mod.NARROWING_KEYS rather than naming them one by one.


# ms-99 / e-2830 — Trek slot schema v2 body models. Slot-specific verbs
# (add / amend / claim) carry different payloads: add is a scope entry +
# optional children opt-in; amend edits child list; claim stamps a
# session id (or clears it via ``session_id=""``).


# ms-92 / e-2141 — cross-project task add via Trek scope.
# Body for POST /api/treks/{trek_id}/task-add. The server walks
# trek.check_trek_task_add_allowed and either writes the task to the
# target project's milestone (stamping meta.trek_id for audit) or
# returns 403 with the scope-guard reason code so the CLI can show
# the right remediation hint.


# ms-88 / e-2089 — fresh session take-over (= dead leader_session_id 引き継ぎ)


# ms-88 / e-2106 — pulse-ack body (= /beacon-trek-pulse Skill self-report).
# ms-92 / e-2165 — structured payload fields added so the leader-digest
# (e-2164) can mechanically aggregate "stuck=N idle=M" counts without
# parsing natural-language notes. All structured fields are optional and
# backward-compat: pre-e-2165 callers (= bridge versions / scripts that
# only set picked_choice + note) keep working.


# ms-88 / e-2138 — kickoff completion body (= /beacon-trek-pulse Step 0 が呼ぶ)


# ms-97 / Phase 7-B / e-2684 — leader succession consent body (= candidate
# accept / decline 1 hop response). caller_session_id は X-Beacon-Session
# header から取るので body には載せない。


# ms-95 / e-2308 — extend TTL on a single task (= leader hints "I delegated
# this to a subagent that can't stamp activity itself"). See
# lib/trek.extend_task_ttl docstring for semantics.


# ms-97 / Phase 7-C / AC24, e-2603 — blanket scope approval body.


# ---------------------------------------------------------------------------
# Organizations (ms-118 / e-4231) — top-level tenancy entity.
#
# ms-113 が org データモデル / store (db.get_org / save_org / list_orgs_for_user)
# と participation-only の開示エンジンを入れた。ここはその store に client から
# 到達する REST の口 (= trek の /api/treks と同型)。owner 判定・membership は
# lib/org.py が持つので再実装しない。org 招待 / project 付け替え / owner ガード /
# 最小 UI は後続タスク (e-4232〜e-4237)。
# ---------------------------------------------------------------------------

# ms-127 e-4869: 上記 /api/orgs/* の REST 口は server/routers_orgs.py へ切り出し済み
# (factory + include_router)。_load_org_for_member だけは projects の rehome endpoint
# (/api/projects/{id}/rehome) も使う共有 helper なので app.py 所有のまま残し、org
# router には factory 注入する。


def _load_org_for_member(org_id: str, user: dict) -> dict:
    """org を読み、caller が member であることを保証する (非 member は 404 で秘匿)。

    get_org_endpoint と同じ開示規則を org 変更系 endpoint で再利用する。
    """
    doc = db.get_org(org_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="org not found")
    if _auth_enabled and not org_mod.is_org_member(doc, user.get("sub", "")):
        raise HTTPException(status_code=404, detail="org not found")
    return doc


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


# ---------------------------------------------------------------------------
# ms-99 / e-2830 — Trek slot schema v2 endpoints (SPEC 方針 6)
# ---------------------------------------------------------------------------
# Four endpoints mirror the four CLI verbs. All go through the same
# ``pending_scope_ops`` queue so AC 15 ("all slot ops via staging")
# holds server-side too — the actual mutation on ``scope[]`` still
# lands via ``scope-approve``.
#
# Shape validation (AC 14) surfaces as HTTP 400 (bad request) for
# malformed input distinct from HTTP 404 (slot not found) and HTTP 409
# (duplicate scope entry). The audit log carries the new
# ``action="slot_<verb>_pending"`` / ``"slot_<verb>_approved"`` markers
# so Cloud Logging can filter by verb.


# ---------------------------------------------------------------------------
# ms-97 / Phase 7-C / AC24, e-2603 — blanket scope approval endpoints.
# Leader-only. The category string is validated by trek_mod helpers.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ms-97 / Phase 7-C / AC26 + AC27, e-2603 — structured logs read API.
# Write paths live next to the relevant endpoints (= tick, pulse-ack,
# task-state) and call ``db.append_trek_log`` directly.
# ---------------------------------------------------------------------------


# ms-128 方針4 (e-4365) — add_blocker の error kind → HTTP status。cycle /
# not_blockable は状態・グラフの衝突 (409、状態が変われば張れる)、self_block /
# missing_id は入力不正 (400)。文字列 sniffing でなく機械可読 kind で分ける
# (AX レビュー 2026-07-29)。


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


# ---------------------------------------------------------------------------
# Retro
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session registry (ms-57 / e-1063)
# ---------------------------------------------------------------------------


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


def _stamp_session_liveness(session: dict, project_id: str, now_dt) -> None:
    """Stamp poll_health / bridge / ws_live / live onto a session row in place
    (ms-101 / e-3010).

    従来 directory の「この session は今 DM を受け取れるか」の signal は
    ``poll_health.healthy`` だった。これは ``last_poll_at`` (= 最後にポーリング
    した時刻) から導く遅れる指標で、接続直後や切断直後を捉えられず「live 0 人
    なのに DM は届く」ズレを生んだ (ms-96 = バックエンドの VPS 移行 の直後に観測)。

    ms-101 は「今つながっている WebSocket 接続の集合」を liveness の真値源に
    する。ここでは接続台帳 (redis_client.ws_session_live) の signal を ``ws_live``
    として stamp し、poll heartbeat と OR で束ねた ``live`` を出す (= 段階移行。
    どちらか一方でも生きていれば live と見なす):

      ws_live : True  — 接続台帳に期限内の WS 接続がある
                False — 接続台帳に無い
                None  — Redis 不通で判定不能 (= poll 判定に委ねる、fail-open)
      live    : (ws_live is True) or (poll_health.healthy is True)

    ``live`` を union にするのは移行期の保険。session_id を WS に付けない旧版
    bridge (= e-3009 以前) は接続台帳に載らず ws_live=False になるが、polling が
    健全なら live に残す。「切断で即 not-live」(= ws_live=False が poll_healthy を
    上書きする完全 cutover) は poll 取得を止める e-3013 で行う。ここでは union に
    留め、既存挙動を壊さない (Redis 不通時は live == poll_healthy と一致)。
    """
    session["poll_health"] = _compute_poll_health(session, now_dt)
    session["bridge"] = bool(session.get("last_poll_at"))
    sid = session.get("session_id") or ""
    ws_live = redis_client.ws_session_live(project_id, sid) if sid else None
    session["ws_live"] = ws_live
    poll_healthy = session["poll_health"].get("healthy") is True
    session["live"] = (ws_live is True) or poll_healthy


# ---------------------------------------------------------------------------
# Session log (ms-57 / e-1037)
# ---------------------------------------------------------------------------


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


def _find_bus_event_by_client_id(
    project_id: str, client_event_id: str, channel: str = "",
) -> dict | None:
    """Find a previously-recorded bus event carrying ``client_event_id``.

    ms-110 / e-4001 idempotency lookup. The first send stamps
    ``client_event_id`` into the event ``data`` (persisted verbatim by
    ``append_bus_event``), so a retry can recover the original event instead
    of creating a duplicate. Scans only the recent window (event_ids are
    timestamp-prefixed, and a retry follows an ambiguous failure by seconds/
    minutes, so the match is near the top) and only on the retry path, so the
    common first-send path pays nothing. Returns the event dict (with
    ``event_id``) or None if no match / backend unavailable.
    """
    if not client_event_id:
        return None
    try:
        recent = db.list_bus_events(project_id, channel=channel, limit=100)
    except Exception:
        # Backend hiccup: treat as "not found". The caller then proceeds to
        # create the event — favouring delivery over dedup on a rare scan
        # failure is the safe bias (a lost message is worse than a rare dup).
        return None
    for ev in recent:
        if str(ev.get("client_event_id") or "") == client_event_id:
            return ev
    return None


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

    # ms-110 / e-4001: idempotent-retry short-circuit. Placed AFTER verify (so a
    # replay of a request that would itself be rejected is not treated as a
    # successful prior send) and BEFORE the gate / append / sidecar / fanout (so
    # a genuine retry does none of those a second time). Only the client-flagged
    # retry path scans; the first send skips this entirely. If the original
    # event is found, return it verbatim with an ``idempotent_replay`` marker —
    # no duplicate event, no duplicate side-effects. If NOT found (the original
    # never landed, e.g. it failed before append), fall through and create it,
    # so an ambiguous failure that truly dropped the message is not silently
    # lost. This is the structural half of "safe to resend on ambiguous result".
    # ms-110 / e-4001 (review e-4001 AX): a retry that carries no key is a
    # contradiction — the client claims "this is a resend" but gives the server
    # nothing to dedup on, so it would silently create a NEW event (the very
    # double-delivery this feature prevents). Reject loudly with a recovery
    # path instead of falling through to a silent create.
    if body.is_retry and not body.client_event_id:
        db.append_bus_audit(project_id, audit_record)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "is_retry_requires_client_event_id",
                "reason": "is_retry=true was sent without client_event_id; "
                          "resend with the same client_event_id used on the "
                          "first attempt so the server can dedup.",
            },
        )

    if body.client_event_id and body.is_retry:
        _dup = _find_bus_event_by_client_id(
            project_id, body.client_event_id, channel=body.channel,
        )
        if _dup is not None:
            # ms-110 / e-4001 (review e-4001 AX): guard against key reuse with a
            # *different* payload. Returning the stored event verbatim would
            # silently drop the new content (a silent no-op). A reused key must
            # mean "the same logical send"; a different payload means the caller
            # reused a key by mistake — fail loudly so they mint a fresh key.
            if (_dup.get("payload") or {}) != (body.payload or {}):
                db.append_bus_audit(project_id, audit_record)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "client_event_id_payload_mismatch",
                        "reason": "client_event_id was already used with a "
                                  "different payload; generate a new "
                                  "client_event_id per distinct logical send.",
                        "event_id": _dup.get("event_id"),
                    },
                )
            audit_record["idempotent_replay"] = {
                "client_event_id": body.client_event_id,
                "event_id": _dup.get("event_id"),
            }
            db.append_bus_audit(project_id, audit_record)
            return {**_dup, "idempotent_replay": True}

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
    # ms-110 / e-3886: the SENDER is the authenticated caller, which is
    # project-independent. Resolving the sender from the POST-target project's
    # session registry (``_resolve_bus_event_user_ids``, above) leaks the
    # *project* axis into what must be a *user_id* decision: a cross-project
    # sender's session is not in the target project's registry, so
    # ``sender_uid`` comes back "" and the same-user carve-out (both this
    # ms-70 gate and the sender-consent backstop below) cannot fire. That is
    # the single root cause behind e-3492 / e-3531 / e-3566 / e-3567 / e-3880 —
    # five same-user cross-project false-holds/false-403s, each previously
    # patched locally. Fix the structure once: use ``user["sub"]`` (the JWT
    # subject) as the authoritative sender for BOTH gates. Fall back to the
    # registry-resolved value only in auth-disabled dev/test, where ``sub`` is
    # the shared "dev" placeholder and the registry is the real discriminator.
    _auth_sub = str((user or {}).get("sub") or "")
    authoritative_sender_uid = (
        _auth_sub if (_auth_enabled and _auth_sub) else sender_uid
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
        sender_user_id=authoritative_sender_uid,
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
        "sender_user_id": authoritative_sender_uid,
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

    # ms-110 / e-3443: sender-side cross-user consent backstop.
    # ms-70 (above) protects the *receiver* — it holds an action DM for the
    # receiver's human. This is the symmetric *sender*-side guard: the server
    # refuses to accept a *proven* cross-user new-send whose recipient no human
    # confirmed. It is the one choke point every client path (CLI / MCP reply /
    # headless / cron) must pass. same-user / reply / Trek / Operation / non-dm
    # are carved out so armed auto-reply, Trek协奏, and Operation autonomy do
    # not regress (SPEC FZcvJ5ivhLu0UkEtw7Ew §1/§2/AC5).
    #
    # e-3492 (P1 fix): this gate is behind a kill-switch and re-enabled only
    # after the claim-issuing client is distributed. Two failures made the
    # first cut break the same-user cross-project handoff flow in prod:
    #   1. sender identity was resolved from the POST-target project's session
    #      registry, but a cross-project sender's session is not there, so
    #      sender_uid came back "" and the same-user carve-out never fired →
    #      false cross-user → 403. Fixed: the sender is the *authenticated
    #      caller* (user["sub"]), which is project-independent.
    #   2. the distributed CLI (0.58.0) cannot issue the recipient_confirmed
    #      claim the server requires, so every legit cross-user send would 403
    #      with a misleading "use /beacon-dm-send" hint. Fixed: BEACON_SENDER_
    #      CONSENT_ENABLED gates enforcement (default OFF); flip it on only once
    #      a claim-capable client is rolled out.
    consent_enforced = os.environ.get("BEACON_SENDER_CONSENT_ENABLED") == "1"
    # Sender = the authoritative (project-independent) identity resolved once
    # above (e-3886). The ms-70 gate and this backstop now share exactly one
    # sender axis, so they can never diverge on "who is sending".
    consent_sender_uid = authoritative_sender_uid
    consent_allow = True
    consent_required = False
    consent_reason = "gate_disabled"
    if consent_enforced:
        consent_is_reply = bool((body.envelope or {}).get("in_reply_to")) or (
            env_tier == envelope_mod.TIER_T3
        )
        consent_operation_env = env_tier in (
            envelope_mod.TIER_T1_SYSTEM, envelope_mod.TIER_T2,
        )
        # Cheap classification first (no backend calls). Carve-outs (same-user /
        # unresolved / reply / Trek-channel / Operation / non-dm) return "not
        # required" and short-circuit — no claim check, no Trek lookup.
        consent_required, consent_reason = dm_consent_mod.classify_send_consent(
            sender_user_id=consent_sender_uid,
            recipient_user_id=receiver_uid,
            channel=body.channel,
            is_reply=consent_is_reply,
            operation_envelope=consent_operation_env,
            shared_trek=False,
        )
        consent_allow = not consent_required
        if consent_required:
            # Claim check is backend-free — a confirmed send (the common path
            # via /beacon-dm-send) is allowed without paying for a Trek lookup.
            consent_claim = (body.envelope or {}).get(dm_consent_mod.CONSENT_CLAIM_KEY)
            _decision = dm_consent_mod.evaluate_send(
                sender_user_id=consent_sender_uid,
                recipient_user_id=receiver_uid,
                recipient_session_id=recipient_sid_for_gate,
                channel=body.channel,
                is_reply=consent_is_reply,
                operation_envelope=consent_operation_env,
                shared_trek=False,
                consent_claim=consent_claim,
            )
            consent_allow = _decision["allow"]
            consent_reason = _decision["reason"]
            if not consent_allow and consent_sender_uid and receiver_uid:
                # Last resort before rejecting: a shared-Trek pair is
                # pre-approved (§1). Only now pay for the Trek lookup (fresh per
                # event). Reuse the session-grain lookup ms-70 built above.
                _c_matched, _c_trek_id = gate_lookup(
                    consent_sender_uid, receiver_uid,
                    body.sender_session_id or "", recipient_sid_for_gate,
                )
                if _c_matched:
                    consent_allow = True
                    consent_reason = dm_consent_mod.CONSENT_SKIP_SHARED_TREK
    audit_record["sender_consent"] = {
        "enforced": consent_enforced,
        "allow": consent_allow,
        "consent_required": consent_required,
        "reason": consent_reason,
        "sender_user_id": consent_sender_uid,
        "receiver_user_id": receiver_uid,
        "had_envelope": body.envelope is not None,
    }
    if consent_enforced and not consent_allow:
        # Proven cross-user new-send without a valid human recipient
        # confirmation. Reject at the choke point. Audit before raising.
        db.append_bus_audit(project_id, audit_record)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "sender_consent_required",
                "reason": consent_reason,
                "hint": (
                    "cross-user DM requires a human-confirmed recipient. "
                    "Send via /beacon-dm-send (which confirms the recipient "
                    "and issues the recipient_confirmed claim) instead of "
                    "posting the bus primitive directly. --no-envelope is not "
                    "permitted for cross-user DMs."
                ),
            },
        )

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
    # ms-110 / e-4001: stamp the idempotency key onto the event so a later
    # retry (``_find_bus_event_by_client_id``) can recover this exact event and
    # dedup instead of creating a duplicate. Benign metadata — receivers read
    # ``payload``, not this field.
    if body.client_event_id:
        data["client_event_id"] = body.client_event_id
    # ms-97 C3 (= review finding H3): stamp a pending-approval marker on the
    # event itself when the ms-70 gate held it for receiver consent. The
    # approval *record* lives in the sidecar (below), but the sidecar is not
    # joined on the receiver's hot inbox read, so the inbox hook had no way to
    # tell a pending action DM from a normal one — the "[DM-PENDING-APPROVAL]"
    # banner only surfaced on the CLI/session-start paths, not the main
    # bridge+hook delivery. Carrying the marker on the event lets the hook
    # render the banner inline so a human sees "this needs approve/deny" on
    # the very next turn.
    if should_gate:
        data["pending_approval"] = True

    event_id = db.append_bus_event(project_id, data)
    audit_record["event_id"] = event_id
    db.append_bus_audit(project_id, audit_record)

    # ms-90 / e-3246: DM 発信を decision-event ストリームに記録する (主役経路)。
    # 「問題に直面して相談を始めた瞬間」を context (= 背景) と共に残し、将来
    # ローカル LLM で PM 専用 AI を訓練する材料にする。DM 本文は複製せず
    # related.event_id で参照する。書き込み失敗は send を壊してはならない
    # (= 記録は付随的、bus event は既に永続化済) ので握り潰してログするだけ。
    try:
        _dec = decision_event_mod.maybe_dm_send_record(
            channel=body.channel,
            payload=body.payload,
            sender_session_id=body.sender_session_id,
            sender_user_id=sender_uid or "",
            context=body.context or "",
            rationale=body.rationale or "",
            event_id=event_id,
            agent=env_issuer or None,
        )
        if _dec is not None:
            db.append_decision_event(project_id, _dec)
    except Exception as _dec_exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning(
            "append_decision_event failed for event_id=%s: %s",
            event_id, _dec_exc,
        )

    # ms-111 e-3622 chunk2a: master-sync event の consumer (outbound write-through)。
    # 投影 rename が発行した master-sync event を受け、master の canonical name を更新する。
    # authz anchor (leader guard): payload の org_id は信用せず (送信者が詐称できる)、
    # 認証済み送信者 (JWT sub) が master の実 org の member か org membership で照合してから
    # apply する。unauthorized / 未知 master / 空 name は drop し、audit として log に残す
    # (silent にしない = mis-route/攻撃兆候の観測)。書き込み失敗は post を壊してはならない
    # (bus event は既に永続化済) ので握り潰してログするだけ。
    try:
        _ms_payload = body.payload or {}
        if (_ms_payload.get("kind") == "master_sync"
                and _ms_payload.get("entity") == "account"):
            _ms_sender = user.get("sub", "")
            _ms_sender_orgs = {
                (o.get("org_id") or o.get("id"))
                for o in (db.list_orgs_for_user(_ms_sender) or [])
            }
            _ms_sender_orgs.discard(None)
            _ms_sender_orgs.discard("")
            _ms_rec, _ms_reason = master_projection.apply_master_name_sync(
                master_adapter.get_master_adapter(),
                master_account_id=str(_ms_payload.get("master_account_id") or ""),
                new_name=str(_ms_payload.get("new_name") or ""),
                sender_org_ids=_ms_sender_orgs,
                now=data.get("created_at") or "",
            )
            if _ms_reason:
                logging.getLogger(__name__).info(
                    "master_sync drop event_id=%s reason=%s sender=%s",
                    event_id, _ms_reason, _ms_sender,
                )
            elif _ms_rec is not None:
                # ms-111 e-3622 chunk2b: master 更新成功 → 同 org の該当 projection doc へ
                # inbound fan-out (bounded)。
                _fanout_master_name_to_projections(
                    master_account_id=str(_ms_payload.get("master_account_id") or ""),
                    new_name=str(_ms_payload.get("new_name") or ""),
                    master_org=str((_ms_rec or {}).get("org_id") or ""),
                )
    except Exception as _ms_exc:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning(
            "master_sync apply failed for event_id=%s: %s", event_id, _ms_exc,
        )

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
    # ms-101 / e-3011 — 新着 bus event の wake hint を全プロセスの受信者へ届ける。
    # 旧 e-997 は「同一プロセスに接続がある場合のみ」push しており (= 下の
    # _ws_connections ローカル判定)、受信者の bridge が別プロセス (uvicorn 別
    # ワーカー) につながっていると素通りして届かなかった。_fanout_bus_event は
    # Redis pub/sub で全プロセスへ中継し、接続を持つプロセスがローカル配信する
    # (Redis 不通時は同プロセスのローカル配信に fallback)。
    await _fanout_bus_event(project_id, event)
    return event


def _fanout_master_name_to_projections(*, master_account_id: str, new_name: str,
                                       master_org: str) -> int:
    """master name 変更を、同 org でその master を参照する projection doc へ write-back する
    (ms-111 e-3622 chunk2b、master-authority consumer の一部)。

    leader refine で write amplification を bound する:
      (1) 参照 filter: 該当 account を投影している project だけ write (sync が変化時のみ True)。
      (2) idempotent: 同名 re-apply は no-op (sync が False を返し save しない)。
      (3) 各 write は save_project で追跡 (data-immutability)、変化した project のみ。
      (4) org-scoped + 受信側 org 整合: project の org が master の org と一致する時のみ。
    offline session は次 load 時に既に fresh。戻り値 = 実際に write した project 数。
    server が他 project doc を書くのは master-authority (master-wins / org-scoped) の正当な
    操作なので authz 問題は無い (leader 判断)。
    """
    if not master_org or not master_account_id:
        return 0
    written = 0
    for _summ in (db.list_projects() or []):
        _pid = _summ.get("project_id") or _summ.get("id")
        if not _pid:
            continue
        _pdata = db.get_project(_pid)
        if not _pdata or org_mod.project_org_id(_pdata) != master_org:
            continue   # (4) 同 org のみ
        if master_projection.sync_master_name_into_projection(
                _pdata, master_account_id, new_name):
            db.save_project(_pid, _pdata)   # (1)(2)(3): 参照あり & 変化時のみ tracked write
            written += 1
    return written


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
        # ms-97 / e-2815 (2026-07-03) — lazy-start gate 撤廃。 以前は
        # ``should_fire_executor_tick`` で 「Trek 内 claim を持たない
        # executor は fire しない」 と絞っていたが、 実運用で 「Trek scope
        # は MS-level bind、 実装は project pool 側 (Trek 未 bind) で走る」
        # ケースが主流のため、 大多数の executor が silent に skip され
        # 「Trek scheduler → executor」 経路が事実上死ぬ dogfood 事故に
        # なった。 Trek 哲学 (= server tick = PM、 executor に周期的に
        # progress-check を投げる) を invariant として復元するため、
        # members[] 内の全 non-leader session を無条件に対象化する。
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
        # ms-97 / e-2815 (2026-07-03) — lazy-start gate 撤廃 (旧 AC33)。
        # 「Trek scope 内 task-level bind が無いと should_fire_executor_tick
        # が False を返し、 fresh executor が silent skip される」 dogfood
        # 事故 (LPS session が phase 1 実装 done 後も digest 不可視、
        # scheduler → executor 経路が完全停止) を受けて、 member[] 内の
        # 全 non-leader session を無条件に progress-check 対象にする。
        # 詳細: ms-97 e-2815 SPEC + user_grain 側 (line ~6912) 同期修正。
        print(
            f"diag[ms-97 e-2815 AC] trek={trek_id_for_log} "
            f"sid={msid} kept: home_pid={resolved_pid}",
            file=sys.stderr,
        )
        targets.append({
            "session_id": msid,
            "home_project_id": resolved_pid,
        })
    return targets


def _count_milestone_commits(project_doc: dict, ms_id: str) -> int:
    """Number of commit-type entries under milestone ``ms_id`` (ms-128 e-4367).

    The commit count is one half of the halt "progress stall" signal (= zero
    commit increment between ticks). Returns 0 when the milestone / project is
    missing so the sweep degrades to fingerprint-only detection rather than
    throwing into the tick loop.
    """
    for ms in (project_doc or {}).get("milestones") or []:
        if (ms.get("id") or ms.get("entry_id")) == ms_id:
            return sum(
                1 for e in (ms.get("entries") or [])
                if (e or {}).get("type") == "commit"
            )
    return 0


def _sweep_trek_target_halts(trek_doc: dict, *, now):
    """Run the ms-128 方針6/e-4309 halt sweep for one trek on the server tick.

    Builds ``commit_count_for`` from the trek's scope (target → project) and the
    live project pool, then delegates the pure halt evaluation + forced
    working→leader_review transitions to ``trek_mod.sweep_working_target_halts``.
    Returns the list of forced transitions (``[{target_id, halt_reason}]``).
    """
    scope = trek_doc.get("scope") or []
    target_project: dict = {}
    for s in scope:
        pid = (s or {}).get("project") or ""
        if not pid:
            continue
        for key in ("target_id", "milestone", "operation"):
            tid = (s or {}).get(key)
            if tid:
                target_project.setdefault(tid, pid)
    _proj_cache: dict = {}

    def commit_count_for(target_id: str) -> int:
        pid = target_project.get(target_id)
        if not pid:
            return 0
        pd = _proj_cache.get(pid)
        if pd is None:
            try:
                pd = db.get_project(pid) or {}
            except Exception:
                pd = {}
            _proj_cache[pid] = pd
        return _count_milestone_commits(pd, target_id)

    return trek_mod.sweep_working_target_halts(
        trek_doc, commit_count_for=commit_count_for, now=now,
    )


def _post_trek_notify_escalation(trek_id, trek_doc, *, payload, action,
                                 meta_key, now, errors):
    """Reserve the refire cooldown, then post a notify-user-only escalation.

    Shared by the idle + leader-review-stall escalation passes so the "post a
    trek escalation" contract (scope resolution → cooldown stamp → envelope →
    notify bus event) lives in one place instead of being copied per escalation
    type (maintainability review 2026-07-29).

    Ordering (AX review 2026-07-29): the cooldown is stamped + saved **before**
    the event is emitted. If the save fails we abort without emitting, so a save
    failure can never leave us posting the same notice every tick with no
    cooldown record (the old emit-then-stamp order flooded on save failure). A
    persistent condition simply re-fires on the next cooldown window. All
    failures (no scope / save / notify) are recorded in ``errors`` — never
    silently swallowed. Returns ``{project_id, event_id}`` or None.
    """
    scope_project_ids = _resolve_trek_scope_project_ids(trek_doc)
    if not scope_project_ids:
        errors.append({"trek_id": trek_id, "error": "no_scope_project",
                       "escalation": action})
        return None
    target_project_id = scope_project_ids[0]
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Reserve the cooldown first (stamp + save), so a save failure aborts before
    # any notification goes out.
    trek_doc.setdefault("meta", {})[meta_key] = now_iso
    trek_doc["updated_at"] = trek_mod.utcnow_iso()
    try:
        db.save_trek(trek_id, trek_doc)
    except Exception:
        errors.append({"trek_id": trek_id, "error": "escalation_save_failed",
                       "escalation": action})
        return None
    try:
        envelope_obj = envelope_mod.issue_t1_system_envelope(
            project_id=target_project_id,
            trek_id=trek_id,
            actions_authorized=[action],
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
        "created_at": now_iso,
    }
    try:
        event_id = db.append_bus_event(target_project_id, bus_data)
    except Exception:
        errors.append({"trek_id": trek_id, "error": "escalation_notify_failed",
                       "escalation": action})
        return None
    return {"project_id": target_project_id, "event_id": event_id}


# ms-128 / e-4386 補完 (受入条件12) — scheduler tick の注入可能な時計。
# クロスインスタンス相互ブロックの e2e ハーネス (AC12) が tick 駆動を deterministic
# に進めるための seam。時刻をここに注入すると、tick の cadence 判定 (is_trek_due) /
# progress-check stamp (last_progress_check_at) / halt 検知 / block reconcile が
# 単一の注入時計で動く (下流はすべて ``now`` を受け取るため)。
# module-global = **in-process からのみ設定可能**で HTTP 表面を持たない
# (= 本番の wall-clock を wire 越しに詐称できない)。default None = 実 UTC。
_INJECTED_SCHEDULER_NOW = None


def _scheduler_now():
    """Return the scheduler tick's clock — injected (AC12 harness) or real UTC."""
    import datetime as _dt
    if _INJECTED_SCHEDULER_NOW is not None:
        return _INJECTED_SCHEDULER_NOW
    return _dt.datetime.now(_dt.timezone.utc)


# ms-127 e-4870: the trek routes (/api/treks/*) moved to server/routers_treks.py
# (factory + include_router, mounted below). The trek-scheduler tick endpoint
# stays here (it also drives the Operation scheduler; see e-4870b). It still
# calls these self-contained trek helpers, now re-imported from the router module
# (routers_treks does NOT import app, so this is one-way, not circular).
from routers_treks import (  # noqa: E402
    _append_trek_log_safe,
    _canonicalise_trek_scope_projects_in_place,
    _fanout_welcome_ticks_for_pending_members,
    _resolve_leader_home_project_id,
    _resolve_trek_scope_project_ids,
    emit_leader_succession_consent_dm,
)

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
    now = _scheduler_now()
    # ms-66 fix: bind now_iso UNCONDITIONALLY here. It is read below by
    # _fire_due_scheduled(now_iso) (operation firing), but its only other binding
    # is inside the trek-quiesce conditional branch — so on every tick where that
    # branch does not run (the common case), now_iso was unbound at the call site
    # → UnboundLocalError, swallowed by the try/except below → the server tick's
    # Operation-firing path silently died on every tick since ms-107 (804dfa16).
    # Trek fanout was unaffected (it calls trek_mod.utcnow_iso() directly).
    # ms-128 AC12 — derive now_iso from the (possibly injected) clock so the
    # Operation-firing path and the Trek fanout share one time source under the
    # harness. In production (no injection) this equals utcnow_iso() (both real).
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
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
        # ms-99 / e-2833 — canonicalise scope BEFORE the terminal check so
        # ``materialize_slots`` can resolve pool for slug-stored scope.
        # (Already done above; the call is idempotent so a second call here
        # would also be safe. We rely on the above canonicalisation.)
        if trek_scheduler_mod.is_trek_task_aggregate_terminal(
            trek_doc, get_project=db.get_project,
        ):
            # ms-99 / e-2834 — quiesce observability trio.
            #
            # (1) Cloud Run stderr emits a diag[ms-99] line so operators
            #     can grep "who quiesced when" without cross-referencing
            #     the trek doc. Written unconditionally per tick.
            # (2) The trek doc's meta stamps ``quiesced_at`` (first-hit
            #     timestamp, never overwritten) and ``quiesce_reason``
            #     (may re-stamp as the reason space grows).
            # (3) The leader session receives one DM per quiesce lifecycle
            #     (= dedup via ``meta.quiesce_notified_at``). The DM
            #     carries the reason + trek id so the leader-side Skill
            #     can render a "your Trek has quiesced" notice without
            #     polling.
            #
            # This closes the CORE doc 5nfTSmCDVUzD4SLzIhI5 "observable
            # ack" gap: pre-e-2834 quiesce was semantically significant
            # but structurally silent — dogfood consumers were left to
            # infer it from the absence of DMs, which the silent-quiesce
            # dogfood (2026-07-03) proved was unreliable.
            quiesce_reason = "task_state_aggregate_terminal"
            meta = trek_doc.setdefault("meta", {})
            print(
                f"diag[ms-99 e-2834] trek={trek_id} quiesced "
                f"reason={quiesce_reason} "
                f"already_stamped={bool(meta.get('quiesced_at'))} "
                f"already_notified={bool(meta.get('quiesce_notified_at'))}",
                file=sys.stderr,
            )
            now_iso = trek_mod.utcnow_iso()
            if not meta.get("quiesced_at"):
                meta["quiesced_at"] = now_iso
            meta["quiesce_reason"] = quiesce_reason
            # Emit the per-quiesce leader DM iff we have not yet done so
            # for this quiesce lifecycle. The stamp is cleared when the
            # trek transitions back to non-terminal (= a task moves out
            # of terminal state) via the same PATCH endpoint that stamped
            # the terminal state; see ``clear_quiesce_marks_on_resume``.
            # ms-97 P3 (= review finding H1 / C2 decision 2026-07-06):
            # completion_ready (ms-97 AC20/21) was structurally unreachable —
            # ``is_completion_ready`` requires aggregate-terminal, but every
            # aggregate-terminal trek is handled here and ``continue``s below,
            # so the completion_ready block further down never saw it. Evaluate
            # it here (same trek_doc the terminal check above used) so the
            # quiesce DM doubles as the AC20 completion signal and — critically
            # — ``meta.completion_notified_at`` gets stamped, which is the
            # missing half of the AC21 stop condition (``summary_sent_at AND
            # completion_notified_at`` → leader-digest tick halts). The op-slot
            # exclusion + one-shot ``completion_notified_at`` gate both live
            # inside ``is_completion_ready``, so no extra guarding is needed.
            completion_ready_now = trek_scheduler_mod.is_completion_ready(
                trek_doc, get_project=db.get_project,
            )
            if not meta.get("quiesce_notified_at"):
                leader_sid = trek_doc.get("leader_session_id") or ""
                quiesce_scope_pids = _resolve_trek_scope_project_ids(trek_doc)
                if leader_sid and quiesce_scope_pids:
                    # ms-97 P4 — route to the leader's home project, not
                    # scope[0], so a cross-project leader actually receives
                    # the quiesce notice (before this, quiesce_notified_at
                    # was stamped on a send that never arrived).
                    quiesce_target_pid = _resolve_leader_home_project_id(
                        trek_doc,
                    )
                    try:
                        quiesce_envelope = (
                            envelope_mod.issue_t1_system_envelope(
                                project_id=quiesce_target_pid,
                                trek_id=trek_id,
                                actions_authorized=["trek.quiesce_notify"],
                                data_class="free",
                                ttl_seconds=3600,
                            )
                        )
                        quiesce_payload = {
                            "kind": "trek-quiesced",
                            "trek_id": trek_id,
                            "recipient_session_id": leader_sid,
                            "sender_type": "trek-scheduler",
                            "origin_channel": "trek-leader-digest",
                            "reason": quiesce_reason,
                            "quiesced_at": meta["quiesced_at"],
                            # ms-97 AC20 — mark the quiesce DM as the one-shot
                            # completion signal when the trek is genuinely
                            # completion-ready (= terminal, no Op slot, not yet
                            # notified). Leader-side can render "completed"
                            # instead of a plain quiesce notice.
                            **({"completion_ready": True}
                               if completion_ready_now else {}),
                            "body": (
                                f"[Trek quiesced] trek_id={trek_id}\n"
                                "Trek scope 内の全 slot が terminal state "
                                "(= done / user_review) に到達しました "
                                "(= AI 自律実行完了)。 leader review "
                                "(= /beacon-trek-review) で archive 判断、 "
                                "もしくは scope 追加 task の投入を "
                                "決めてください。\n"
                                f"reason: {quiesce_reason}"
                            ),
                        }
                        db.append_bus_event(quiesce_target_pid, {
                            "channel": "dm",
                            "sender_session_id": "",
                            "payload": quiesce_payload,
                            "envelope": quiesce_envelope,
                            "delivery": "auto-execute",
                            "created_at": now_iso,
                        })
                        # Only stamp the notified marker on a successful
                        # append so a transient failure retries next tick.
                        meta["quiesce_notified_at"] = now_iso
                        # ms-97 P3 / AC20 — stamp the one-shot completion
                        # marker on the same successful append. Gated by
                        # ``completion_ready_now`` (= is_completion_ready,
                        # which already excludes Op-slot treks and returns
                        # False once this is stamped) so it fires once per
                        # trek lifetime and unblocks the AC21 stop condition.
                        if completion_ready_now and not meta.get(
                            "completion_notified_at"
                        ):
                            meta["completion_notified_at"] = now_iso
                    except Exception as exc:  # noqa: BLE001
                        # Best-effort: log the failure but continue with
                        # the quiesce flow. Absence of quiesce_notified_at
                        # means next tick will retry emission.
                        print(
                            f"diag[ms-99 e-2834] trek={trek_id} "
                            f"quiesce_dm_failed: "
                            f"{type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
            quiesced.append({
                "trek_id": trek_id,
                "reason": quiesce_reason,
            })
            # Still stamp last_progress_check_at so the next cadence
            # window does not re-flag this trek as "never fired".
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
        # ms-99 / e-2833 — canonicalise trek_doc.scope in place so
        # ``materialize_slots`` (= called via the tick decision predicates
        # below) looks the project pool up under the full id rather than
        # the raw slug the CLI may have stored. Without this rewrite,
        # ``get_project`` sees the slug, returns None, and every MS slot
        # collapses to ``("todo", "unstamped")`` — the exact false-quiesce
        # / false-fire pathway that broke tk-29a11d2f dogfood.
        _canonicalise_trek_scope_projects_in_place(trek_doc)
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
        # ms-97 / Phase 7-A / AC21 + ms-128 / e-4284 — 完遂済 trek の停止条件。
        # 判定は trek_scheduler の単一定義に集約 (= signal / heartbeat どちらの
        # 発火経路にも等しく効かせ、停止条件を 2 ファイルに分散させない)。
        leader_halted_by_summary = trek_scheduler_mod.is_leader_halted_by_summary(
            trek_doc
        )
        # ms-97 / Phase 7-A / AC20 — completion_ready シグナル。
        # 全 task_states terminal + Op slot 不在 + 未通知 の時 1 回限り
        # leader-digest payload に ``completion_ready=True`` を載せ、
        # fanout 成功後に ``meta.completion_notified_at`` を stamp して
        # 二度目の fire を構造的に防ぐ。
        completion_ready_now = (
            trek_scheduler_mod.is_completion_ready(
                fanout_trek_doc, get_project=db.get_project,
            )
        )
        # ms-97 / e-2613 — signal gate (leader が消費すべき signal がある時)。
        leader_signal_fire = (
            trek_scheduler_mod.should_fire_leader_tick(
                fanout_trek_doc, get_project=db.get_project,
            )
            or completion_ready_now
        )
        # ms-128 / e-4284 — leader-digest heartbeat。signal gate が閉じていても
        # 遅い cadence で発火し、silent stall (= 全 executor が working のまま沈黙)
        # を leader に必ず surface する。
        leader_heartbeat_due = trek_scheduler_mod.is_leader_digest_heartbeat_due(
            fanout_trek_doc, now=now,
        )
        # ms-128 / e-4284 — OR 合成 + 発火理由の閉じた列挙を純関数で決定
        # (= endpoint のインライン式でなく unit test 可能な 1 か所に集約)。
        leader_should_fire, leader_fire_reasons = (
            trek_scheduler_mod.compose_leader_fire_decision(
                signal_fire=leader_signal_fire,
                heartbeat_due=leader_heartbeat_due,
                halted_by_summary=leader_halted_by_summary,
            )
        )
        completion_ready_fanned_out = False
        if leader_targets and leader_should_fire:
            base_digest_payload = trek_scheduler_mod.build_leader_digest_payload(
                fanout_trek_doc, now=now,
            )
            if completion_ready_now:
                base_digest_payload["completion_ready"] = True
            # ms-128 / e-4284 — 発火理由を常在の閉じた列挙 (["signal"] /
            # ["heartbeat"] / 両方) で payload に載せる。leader 側は "heartbeat"
            # のみ (= signal 無し) を「新 signal でなく stall 検知の定期 pulse」と
            # 区別できる。optional bool の有無で理由を運ばない (AX レビュー #536)。
            base_digest_payload["fire_reason"] = leader_fire_reasons
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
        # ms-128 / e-4284 — ⚠ この stamp は is_leader_digest_heartbeat_due の
        # cooldown 時計を兼ねる load-bearing フィールドになった: heartbeat は
        # ``now - last_leader_digest_at >= cadence × 3`` で次回発火を決める。
        # dashboard 都合でこの stamp を条件付き化 / 移動 / 削除すると heartbeat の
        # 抑制が消え、leader digest が毎 tick 発火する firehose になる。
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
        if not (trek_doc.get("scope") or []):
            # Same fallback path as the progress-check loop; without a
            # target project we have nowhere to post.
            continue
        # Idle escalation goes to the ``notify`` channel (= user-facing,
        # notify-user-only). The shared helper reserves the cooldown (stamp+save)
        # before emitting and records any failure in ``errors``.
        payload = trek_scheduler_mod.build_idle_escalation_payload(
            trek_doc, now=now,
        )
        posted = _post_trek_notify_escalation(
            trek_id, trek_doc, payload=payload,
            action="trek.idle_escalation",
            meta_key="last_idle_escalation_at", now=now, errors=errors,
        )
        if posted is None:
            continue
        escalations.append({
            "trek_id": trek_id,
            "project_id": posted["project_id"],
            "event_id": posted["event_id"],
            "idle_minutes": payload.get("idle_minutes"),
        })

    # ms-128 方針8 (e-4368) — leader-review-stall escalation pass. Independent of
    # the idle pass above: idle anchors on executor pulses, so a leader asleep
    # while executors are active is NOT idle and would be missed. This pass
    # detects a leader not draining the leader_review queue (oldest item stale
    # past its current leader's grace window) and escalates to the human user
    # (notify-user-only), with a refire cooldown. The leader is the root of the
    # watch-tree, so escalation must reach the human Trek owner, not another AI.
    leader_review_stall_escalations: list[dict] = []
    for trek_doc in candidate_treks:
        trek_id = trek_doc.get("trek_id", "")
        if trek_mod.is_halted(trek_doc):
            continue
        fresh = db.get_trek(trek_id)
        if fresh is not None:
            trek_doc = fresh
        stall_info = trek_scheduler_mod.pending_leader_review_stall_escalation(
            trek_doc, now=now,
        )
        if not stall_info:
            continue
        payload = trek_scheduler_mod.build_leader_review_stall_payload(
            trek_doc, stall_info=stall_info, now=now,
        )
        posted = _post_trek_notify_escalation(
            trek_id, trek_doc, payload=payload,
            action="trek.leader_review_stall_escalation",
            meta_key="last_leader_review_stall_escalation_at",
            now=now, errors=errors,
        )
        if posted is None:
            continue
        leader_review_stall_escalations.append({
            "trek_id": trek_id,
            "project_id": posted["project_id"],
            "event_id": posted["event_id"],
            "waited_minutes": payload.get("waited_minutes"),
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
        # ms-128 方針6/e-4309 — per-target halt sweep (response fingerprint +
        # commit increment + pulse timeout, 4h). Runs alongside the legacy
        # TTL auto-stall below and forces stalled / dead working Targets to
        # leader_review with a halt_reason tag (so the leader's verdict set can
        # branch 完成レビュー vs halt 救済). A no-op-repeating session trips this
        # via its unchanging fingerprint = the "無応答 → 介入" contract.
        try:
            forced_halts = _sweep_trek_target_halts(trek_doc, now=now)
        except Exception:
            forced_halts = []
        if forced_halts:
            try:
                db.save_trek(trek_id, trek_doc)
            except Exception:
                pass
        # ms-128 方針4/e-4365 — block reconcile. AND auto-unblock (全 blocker が
        # leader_review 到達で block→todo) と rollback (blocker 差し戻しで未着手の
        # 依存元を再 block、作業中は warning) を毎 tick 回す。edge は台帳に永続する
        # ので、これが block 状態を依存グラフに突き合わせる唯一の choke-point。
        try:
            block_result = trek_mod.reconcile_blocks(trek_doc, now=now)
        except Exception:
            block_result = None
        if block_result and (block_result.get("unblocked")
                             or block_result.get("reblocked")):
            try:
                db.save_trek(trek_id, trek_doc)
            except Exception:
                pass
        stalled = trek_scheduler_mod.detect_auto_stalled_tasks(
            trek_doc, now=now,
        )
        if not stalled:
            continue
        scope = trek_doc.get("scope") or []
        if not scope:
            continue
        # ms-97 P4 — route the auto-stall leader_review notice to the
        # leader's home project instead of scope[0] (cross-project fix).
        target_pid = _resolve_leader_home_project_id(trek_doc)
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

    # ms-107 e-3434 chunk 3b ("trek tick 相乗り") — on this same Cloud-Scheduler
    # tick, also fire due *scheduled* things (target-agnostic periodic-tick
    # primitive, lib/tick_scheduler). Operation is the first source; Account
    # 定期連絡 / short-lived watch tasks plug into the same seam later. Fully
    # isolated + best-effort: any failure is captured, never touches the Trek
    # fanout above.
    try:
        scheduled_fires = _fire_due_scheduled(now_iso)
    except Exception as _op_exc:  # pragma: no cover - defensive isolation
        # ms-66 fix: this bare except previously masked an UnboundLocalError for
        # ~every tick (see now_iso note above) so the dead Operation-firing path
        # was invisible in logs. Log to stderr so this class of silent failure is
        # visible in VPS journalctl; still isolated (never breaks the Trek fanout).
        _server_logger.error(
            "scheduled-fire tick error (operation firing skipped this tick): "
            "%s: %s", type(_op_exc).__name__, _op_exc)
        scheduled_fires = [{"error": f"{type(_op_exc).__name__}: {_op_exc}"}]

    # e-1391 (ms-66) — record that a tick just completed so an *external*
    # watchdog (scripts/tick-health-monitor.py) can notice when the driver
    # dies. The 2026-07 Cloud Run → VPS migration silently dropped the tick
    # driver and it was only caught by accident; last_tick_at + the
    # /api/system/tick-health endpoint make a dead tick loud from outside the
    # box. In-memory is sufficient: on process restart it resets and the next
    # tick (≤1 min) re-baselines it.
    global _last_tick_at, _last_tick_report
    _last_tick_at = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _last_tick_report = {
        "candidates": len(candidate_treks),
        "due": len(due_treks),
        "fired": len(fired) if isinstance(fired, list) else fired,
        "scheduled_fires": len(scheduled_fires)
        if isinstance(scheduled_fires, list) else 0,
    }

    return {
        "now": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "candidates": len(candidate_treks),
        "due": len(due_treks),
        "fired": fired,
        "escalations": escalations,
        "leader_review_stall_escalations": leader_review_stall_escalations,
        "auto_stalled": auto_stalled,
        "errors": errors,
        "quiesced": quiesced,
        # ms-97 / e-2612 (AC32) — treks skipped because halt is set.
        # Separate from ``quiesced`` (= terminal task-state aggregate)
        # so observers can tell "leader pulled the cord" apart from
        # "work is genuinely done".
        "halted": halted,
        # ms-107 e-3434 chunk 3b — scheduled things fired on this tick.
        "scheduled_fires": scheduled_fires,
    }


@app.get("/api/system/tick-health")
def tick_health_endpoint():
    """Report when the server last ran a periodic tick (e-1391 / ms-66).

    Read-only and unauthenticated (like ``/api/version``): it exposes only
    operational liveness, no user data, and must be cheaply pollable by an
    *external* watchdog (``scripts/tick-health-monitor.py`` on GitHub Actions
    cron). The migration incident that motivated this — the tick driver
    silently dropped in the Cloud Run → VPS move — is exactly a failure a
    down/misconfigured box cannot self-report, so the observer has to live
    outside the box and read this from over the network.

    ``last_tick_at`` is in-memory per process (see the module global), so a
    just-restarted server reports ``status=never`` until its first tick lands
    (≤1 min). The pure classification lives in ``lib/tick_health.py``.
    """
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    # e-1391 follow-up (review H1): pass uptime so evaluate_tick_health can
    # promote a persistent ``never`` (server up but driver never ticked = the
    # migration failure) to ``stale``, instead of staying silent forever.
    uptime_seconds = None
    if _server_start_at is not None:
        uptime_seconds = (now - _server_start_at).total_seconds()
    health = tick_health_mod.evaluate_tick_health(
        _last_tick_at, now, uptime_seconds=uptime_seconds,
    )
    return {
        **health,
        "last_tick_report": _last_tick_report,
    }


def _find_operation_spec_doc(project: dict, op_id: str) -> str:
    """Best-effort: the doc_id of the SPEC bound to an operation (scope=spec,
    operation=op_id), so the execute Skill can fetch its procedure. '' if none."""
    for doc in (project.get("documents", []) or []):
        if not isinstance(doc, dict):
            continue
        if doc.get("operation") == op_id and doc.get("scope") == "spec":
            return doc.get("doc_id", "") or doc.get("id", "") or ""
    return ""


def _operation_schedule_descriptor(op: dict) -> dict:
    """Adapter: map an Operation to the entity-agnostic schedule descriptor
    ``lib/tick_scheduler`` understands. An Operation is enabled for server-tick
    firing when it is open AND opted in via ``meta.server_tick``. Keeping this
    mapping here (not in tick_scheduler) is what decouples the tick from
    Operation — Account 定期連絡 / short-lived watch tasks add their own adapter
    without touching the scheduler."""
    meta = op.get("meta") or {}
    return {
        "enabled": op.get("status") == "open" and tick_scheduler.truthy(meta.get("server_tick")),
        "cadence_minutes": meta.get("cadence_minutes"),
        "last_fired_at": meta.get("last_fired_at"),
    }


def _fire_operation(pid: str, project: dict, op: dict, now_iso: str) -> dict:
    """Fire one due Operation by **reusing its pre-approved envelope** (minted by
    the human at ``beacon operation approve``, stored in ``operation_envelopes/``)
    — the server never mints here, it re-attaches the standing authorization.
    Requires an active envelope (else delivery degrades to notify-user-only and
    never auto-executes → skip). Returns a report dict; ``fired`` True means the
    caller should stamp ``meta.last_fired_at``."""
    op_id = op.get("id", "")
    envelope = None
    try:
        envs = db.list_operation_envelopes(pid, op_id=op_id, status="active")
        if envs:
            envelope = (envs[0] or {}).get("envelope")
    except Exception:
        envelope = None
    if not envelope:
        return {"project_id": pid, "op_id": op_id, "fired": False,
                "skipped": "no active envelope (approve first)"}
    meta = op.get("meta") or {}
    recipient = meta.get("claimer_session_id") or meta.get("open_by") or ""
    # ms-106 e-3504 — per-Operation fire target. An Operation is the periodic-
    # execution primitive; which Skill it wakes on fire is the Operation's own
    # property (``meta.execute_skill``), not hard-wired to the dev log-review
    # Skill. Defaults to ``beacon-operation-execute`` so existing dev Operations
    # are unaffected; the sales reply-watch Operation sets it to
    # ``beacon-sales-reply-watch`` (detection-only). This is what lets the same
    # tick primitive drive dev monitoring and sales watches from one seam.
    execute_skill = (meta.get("execute_skill") or "beacon-operation-execute").strip() \
        or "beacon-operation-execute"
    payload = {
        "op_id": op_id,
        "log_source": op.get("log_source", op_id),
        "spec_doc_id": _find_operation_spec_doc(project, op_id),
        "trigger_name": f"operation_check_{op_id}",
        "execute_skill": execute_skill,
        "message": f"{op_id} の定期チェック (server tick)。"
                   f"/{execute_skill} で実行してください。",
        "created_at": now_iso,
    }
    if recipient:
        payload["recipient_session_id"] = recipient
    bus_data = {
        "channel": "operation-trigger",
        "sender_session_id": "",
        "payload": payload,
        "envelope": envelope,
        "delivery": "auto-execute",
        "created_at": now_iso,
    }
    try:
        db.append_bus_event(pid, bus_data)
    except Exception as exc:
        return {"project_id": pid, "op_id": op_id, "fired": False,
                "error": f"append_bus_event: {type(exc).__name__}"}
    return {"project_id": pid, "op_id": op_id, "fired": True,
            "recipient": recipient or "(broadcast)"}


def _deadline_reminder_candidates(project: dict):
    """Yield ``(item, kind, label, recipient_session)`` for every work item that
    can carry a deadline — Targets (a milestone's ``target_date``) and their work
    items (dev task / sales activity ``deadline``) — ms-139 e-4953.

    A thin adapter over the SHARED occupation-agnostic enumeration
    ``occupation.iter_deadline_candidates`` (ms-142 e-5010): both this server
    reminder and the session-start display (``beacon deadline due``) walk that one
    enumeration, so neither names ``project['milestones']`` / ``entries`` /
    ``opportunities`` / ``activities`` and a new occupation's deadlines light up
    at both sites with no edit. An opportunity carries no Target-level deadline
    field today, so ``deadline.deadline_of`` returns '' and ``_fire_due_deadlines``
    skips it (behavior parity with the milestone-only Target yield this replaced).

    ``recipient`` is the session that *claims* the item's Target; '' when
    unclaimed (no live owner to DM). ``kind`` is the occupation-agnostic label
    (milestone / task / activity) the reminder message stamps."""
    for cand in occupation.iter_deadline_candidates(project):
        yield cand["item"], cand["kind"], cand["label"], cand["recipient"]


def _fire_due_deadlines(pid: str, project: dict, now_iso: str, report: list) -> bool:
    """締切超過の work item を検知し、claim 者のセッションへ 1 回だけ DM で知らせる
    — ms-139 e-4953。tick に相乗りする 2 つ目の source。

    判定規則は L2 締切エンジン (``deadline``): 今日 > 締切 かつ status が terminal
    (done/cancelled) でない → overdue。二重配信は ``deadline.pending_reminders`` /
    ``mark_reminded`` の dedup (最後にリマインドした締切値) で防ぐ — 締切を延ばして
    再び過ぎたら値が変わるので再通知される。reminder は「action を認可する DM」では
    なく **通知** なので envelope 不要 (delivery=propose-to-ai で受信 AI の文脈に載せ、
    idle-wake で起こす)。claim 者セッションが無い項目は DM 先が無いので skip。

    ``report`` に per-fire を積み、dedup 印を刻んだら True を返す (呼び出し側が
    ``save_project`` する)。"""
    today = now_iso[:10]
    changed = False
    for item, kind, label, recipient in _deadline_reminder_candidates(project):
        st = deadline.work_item_temporal_status(item, today)
        if st not in (deadline.TRANSITION_DUE, deadline.TRANSITION_OVERDUE):
            continue
        if item.get(deadline.REMINDED_FOR_KEY) == deadline.deadline_of(item):
            continue  # この締切値では既にリマインド済み
        dl = deadline.deadline_of(item)
        if not recipient:
            report.append({"project_id": pid, "kind": kind, "label": label,
                           "deadline": dl, "fired": False,
                           "skipped": "no claimer session"})
            continue
        # ms-139 思想レビュー finding#3: DUE(本日締切・未超過)でも発火するが、文面は
        # temporal に応じて分ける。overdue を DUE 当日に断言すると受信 AI の判断入力が汚れる。
        overdue = st == deadline.TRANSITION_OVERDUE
        head = "⏰ 締切超過" if overdue else "⏰ 本日締切"
        state = "を過ぎています" if overdue else "は本日が締切です"
        payload = {
            "kind": "deadline-reminder",
            "work_kind": kind,
            "label": label,
            "deadline": dl,
            "temporal": st,
            "recipient_session_id": recipient,
            "message": (f"{head}: {label} の締切 {dl} {state} ({kind})。"
                        f"済んだら完了、延ばすなら期日変更、やめたら取消で盤面から"
                        f"外してください。"),
            "created_at": now_iso,
        }
        bus_data = {
            "channel": "dm",
            "sender_session_id": "",
            "payload": payload,
            "delivery": "propose-to-ai",
            "created_at": now_iso,
        }
        try:
            db.append_bus_event(pid, bus_data)
        except Exception as exc:  # pragma: no cover
            report.append({"project_id": pid, "kind": kind, "label": label,
                           "fired": False,
                           "error": f"append_bus_event: {type(exc).__name__}"})
            continue
        deadline.mark_reminded(item)  # 二重配信防止の dedup 印
        item["deadline_reminded_at"] = now_iso
        changed = True
        report.append({"project_id": pid, "kind": kind, "label": label,
                       "deadline": dl, "fired": True, "recipient": recipient})
    return changed


def _fire_due_scheduled(now_iso: str) -> list:
    """ms-107 e-3434 chunk 3b — fire due *scheduled* things on the shared Trek
    scheduler tick ("相乗り"), using the target-agnostic ``lib/tick_scheduler``
    primitive.

    Source registry: each source yields ``(item, descriptor, fire)`` where the
    descriptor is entity-free ({enabled, cadence_minutes, last_fired_at}) and
    ``fire`` performs the source-specific activation. Today the only source is
    **Operations** (dev/sales persistent target); Account 定期連絡 and
    short-lived communication watch tasks plug in the same way later — the tick
    stays unaware of what it is firing.

    Best-effort throughout; a project's failure is captured and skipped. The
    ``meta.last_fired_at`` stamp advances the cadence gate (the dedup that stops
    double-firing). Returns a per-fire report for the tick response.

    Scale note: iterates all projects (``list_all_projects`` + ``get_project``)
    each tick — fine at dogfood scale; a project-level index of "has a
    server-tick schedulable" is the follow-up when project count grows."""
    report: list = []
    try:
        projects = db.list_all_projects()
    except Exception as exc:  # pragma: no cover
        return [{"error": f"list_all_projects: {type(exc).__name__}: {exc}"}]
    for proj_meta in (projects or []):
        pid = proj_meta.get("project_id") or proj_meta.get("id") or ""
        if not pid:
            continue
        try:
            project = db.get_project(pid)
        except Exception:
            continue
        if not project:
            continue
        changed = False
        # --- source: Operations (persistent targets opted into server-tick) ---
        for op in (project.get("operations", []) or []):
            if not tick_scheduler.is_due(_operation_schedule_descriptor(op), now_iso):
                continue
            result = _fire_operation(pid, project, op, now_iso)
            report.append(result)
            if result.get("fired"):
                op.setdefault("meta", {})["last_fired_at"] = now_iso
                changed = True
        # --- source: deadline reminders (ms-139 e-4953) — 締切超過の work item を
        #     claim 者セッションへ 1 回 DM。Operations と同じ tick に相乗りする。 ---
        if _fire_due_deadlines(pid, project, now_iso, report):
            changed = True
        # --- future sources (Account 定期連絡 / short-lived watch tasks) plug
        #     in here with their own descriptor adapter + fire callback ---
        if changed:
            try:
                db.save_project(pid, project)
            except Exception as exc:  # pragma: no cover
                report.append({"project_id": pid,
                               "error": f"save_project: {type(exc).__name__}"})
    return report


# /api/projects/{project_id}/bus/envelope|audit + /dm/pending|approval/* moved to
# routers_projects.make_bus_gate_router (ms-127 e-4871 PR3a). Delivery bus routes
# (send/receive/cursors/ack) still here → PR3b.
#
# The gate router is mounted HERE (not at the end of app.py) on purpose: its
# GET /bus/audit must register *before* the delivery route GET /bus/{event_id}
# below, or Starlette's registration-order matching lets /bus/{event_id} shadow
# /bus/audit (event_id="audit") and every audit read 404s. This preserves the
# pre-extraction order where /bus/audit was defined above /bus/{event_id}.
from routers_projects import make_bus_gate_router as _make_bus_gate_router

app.include_router(
    _make_bus_gate_router(
        require_auth,
        _load=lambda *a, **k: _load(*a, **k),
        _load_meta_only=lambda *a, **k: _load_meta_only(*a, **k),
        _require_project_role=lambda *a, **k: _require_project_role(*a, **k),
    )
)


# GET /api/projects/{project_id}/bus (list_bus_events) moved to
# routers_projects.make_bus_delivery_router (ms-127 e-4871 PR3b).


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


def _bus_event_addressed_to(event: dict, recipient_id: str,
                            recipient_user_id: str = "") -> bool:
    """Return True iff ``event`` should be delivered to ``recipient_id``.

    See the module-level rationale on _DM_CHANNELS above. This helper is
    the single source of truth for DM routing; both the /unread endpoint
    and any future WS fan-out filter must funnel through it.

    ms-54 / e-2934 (2026-07-06): user-scoped 宛先も判定する。
    ``recipient_user_id`` は caller (= 実行者 user) の user_id で、event の
    ``payload.recipient_user_id`` と比較する。session_id churn (= bclaude
    再起動で sid が変わる) 経由で消える情報 DM を、 user 単位アドレスなら
    次回起動時に読める形で救済する経路。session-scoped と user-scoped は
    payload の field で明示的に区別され、混在は無い (= sender が --to か
    --to-user のどちらかを明示、両者は排他)。優先順位は
    session-scoped 優先 (= session_id field が set なら user field は無視)、
    どちらも無ければ従来通り DM channel は drop / それ以外は broadcast。
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
    intended_sid = str(payload.get("recipient_session_id") or "")
    if intended_sid:
        return intended_sid == recipient_id
    # ms-54 e-2934: user-scoped delivery. payload.recipient_user_id が set
    # かつ caller の user_id と一致すれば delivery。recipient_user_id 引数が
    # 空 (= 呼び出し側が user 未解決 or session-only mode) なら user-scoped
    # は評価せず drop 相当 (= 「DM 宛先不明」 として fall-through)。
    intended_uid = str(payload.get("recipient_user_id") or "")
    if intended_uid:
        return bool(recipient_user_id) and intended_uid == recipient_user_id
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


# ---------------------------------------------------------------------------
# DM receipt attribution (ms-93 / Phase 3)
#
# ``opened_by`` / ``delivered_by`` / ``sender_session_id`` are raw ephemeral
# session_ids (route tokens). A human reading ``beacon bus status`` cannot tell
# WHO opened a DM from ``by sv-77e81553-…4649aa42`` — and a green ``opened`` ✓
# stamped by a mis-addressed session looks identical to a correct delivery
# ("緑の opened が誤配を隠す"). Phase 3 resolves those sids to a stable identity
# (email / machine / agent_kind / cwd) on the read side so mis-delivery becomes
# visible.
#
# Authorization (Phase 3 (b)): attribution is a disclosure, so it rides the
# SAME participant gate as the DM payload. Only a caller who is sender or
# recipient of the DM gets ``*_identity`` fields; a third-party member who can
# list bus sidecar metadata sees the receipt timestamps but NOT who opened it.
# This is enforced structurally by attaching attribution only inside the
# ``can_see`` branch of :func:`_apply_dm_payload_visibility` (never on a
# redacted event).
# ---------------------------------------------------------------------------

_ATTRIBUTION_FIELDS = (
    ("sender_session_id", "sender_identity"),
    ("delivered_by", "delivered_by_identity"),
    ("opened_by", "opened_by_identity"),
)


def _session_identity(session_row: dict) -> dict:
    """Extract a stable, human-readable identity from a directory session row.

    Prefers the spoof-resistant ``actor`` block (server stamps ``actor.email``
    from the authenticated JWT on every upsert) and falls back to the
    self-reported ``agent`` block for agent_kind / machine. Returns only the
    non-empty fields so the client can render a compact label.
    """
    actor = session_row.get("actor") if isinstance(session_row.get("actor"), dict) else {}
    agent = session_row.get("agent") if isinstance(session_row.get("agent"), dict) else {}
    identity: dict = {}
    email = str(actor.get("email") or "").strip()
    if email:
        identity["email"] = email
    machine = str(actor.get("machine") or agent.get("machine_id") or "").strip()
    if machine:
        identity["machine"] = machine
    agent_kind = str(actor.get("agent_kind") or agent.get("kind") or "").strip()
    if agent_kind:
        identity["agent_kind"] = agent_kind
    cwd = str(session_row.get("cwd") or "").strip()
    if cwd:
        identity["cwd"] = cwd
    return identity


def _attach_dm_attribution(event: dict, sid_to_identity: dict) -> dict:
    """Return a shallow copy of ``event`` with ``*_identity`` fields resolved
    for the receipt sids present. Also resolves ``payload.recipient_session_id``
    to ``recipient_identity`` (kept as a top-level field so a redaction-free
    payload copy is unnecessary). Unknown sids (GC'd / legacy rows) are skipped
    so the client falls back to the raw sid.
    """
    if not isinstance(event, dict):
        return event
    out = dict(event)
    for src_field, id_field in _ATTRIBUTION_FIELDS:
        sid = str(event.get(src_field) or "")
        identity = sid_to_identity.get(sid) if sid else None
        if identity:
            out[id_field] = identity
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    recipient_sid = str(payload.get("recipient_session_id") or "")
    recipient_identity = sid_to_identity.get(recipient_sid) if recipient_sid else None
    if recipient_identity:
        out["recipient_identity"] = recipient_identity
    return out


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
    # Phase 3: sid → identity for receipt attribution. Built from the same
    # sessions pass (no extra DB roundtrip). Attached only to events the caller
    # is allowed to see (see the can_see branch below).
    sid_to_identity = {
        str(s.get("session_id") or ""): _session_identity(s)
        for s in sessions
        if s.get("session_id")
    }

    # ms-95 defense-in-depth: for sessions that mint before ms-95's
    # user_id-stamp landed (= legacy rows written by older bridge builds),
    # fall back to actor.email → project.members[].user_id lookup. The
    # server stamps actor.email from the authenticated JWT on every upsert,
    # so it is spoof-resistant even for legacy rows. Without this fallback,
    # a legacy session cannot be resolved to a user_id, and DM payloads
    # addressed to that session get redacted even from the intended
    # recipient (= root cause of the 2026-07-06 Iruka observation).
    empty_sids = [
        (sid, str((s.get("actor") or {}).get("email") or ""))
        for s in sessions
        for sid in (str(s.get("session_id") or ""),)
        if sid and not sid_to_uid.get(sid) and (s.get("actor") or {}).get("email")
    ]
    if empty_sids:
        try:
            project_doc = db.get_project(project_id) or {}
        except Exception:
            project_doc = {}
        members = project_doc.get("members") or []
        email_to_uid: dict[str, str] = {}
        for m in members:
            if not isinstance(m, dict):
                continue
            m_email = str(m.get("email") or "").strip().lower()
            m_uid = str(m.get("user_id") or "")
            if m_email and m_uid:
                email_to_uid[m_email] = m_uid
        owner_email = str(project_doc.get("owner_email") or "").strip().lower()
        owner_uid = str(project_doc.get("owner") or "")
        if owner_email and owner_uid:
            email_to_uid.setdefault(owner_email, owner_uid)
        for sid, actor_email in empty_sids:
            fallback = email_to_uid.get(actor_email.strip().lower())
            if fallback:
                sid_to_uid[sid] = fallback

    out: list[dict] = []
    for ev in events:
        channel = ev.get("channel") or ""
        if channel not in _DM_CHANNELS:
            out.append(ev)
            continue
        sender_sid = str(ev.get("sender_session_id") or "")
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        recipient_sid = str(payload.get("recipient_session_id") or "")
        # ms-54 / e-2934: user-scoped DM は recipient_session_id が空で
        # recipient_user_id が set。 caller の user_id と直接比較する経路も
        # 必要 (= session→user 解決を経由しない、payload が持つ uid をそのまま
        # 権威とする)。
        recipient_uid_from_payload = str(payload.get("recipient_user_id") or "")
        sender_uid = sid_to_uid.get(sender_sid, "")
        receiver_uid = sid_to_uid.get(recipient_sid, "")
        # 通常 (session-scoped) の payload visibility 判定。
        can_see = _caller_can_see_dm_payload(
            ev, caller_uid, sender_uid, receiver_uid
        )
        # user-scoped の場合、 receiver_uid が payload の recipient_user_id と
        # 一致すれば caller は intended recipient として payload 可視。
        if (not can_see and recipient_uid_from_payload
                and caller_uid == recipient_uid_from_payload):
            can_see = True
        if can_see:
            # Phase 3 (b): attribution rides the participant gate — only a
            # sender/recipient sees who opened/delivered the DM.
            out.append(_attach_dm_attribution(ev, sid_to_identity))
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


# GET /bus/unread, POST+GET /bus/cursors/{recipient_id}, POST /bus/{event_id}/ack,
# GET /bus/{event_id} (list_unread_bus_events, advance_bus_cursor, get_bus_cursor,
# ack_bus_event_receipt, get_bus_event) moved to
# routers_projects.make_bus_delivery_router (ms-127 e-4871 PR3b).
# _RECEIPT_STAGES constant also moved there.

# ---------------------------------------------------------------------------
# Session intent (ms-54 / e-1369 Layer 4)
#
# Intent is the AI's narrative self-report ("I'm working on X right now").
# The bridge cannot stamp it because the bridge does not know what the AI is
# trying to do — only the AI does. We expose a tiny dedicated endpoint so a
# session can write its own intent without touching the heartbeat upsert
# (keeps the WHERE/WHAT/REACH bridge writes pristinely machine-observable).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket (real-time project updates via Firestore on_snapshot)
# ---------------------------------------------------------------------------

_ws_connections: dict[str, set[WebSocket]] = {}
_watchers: dict[str, object] = {}
_event_loop: asyncio.AbstractEventLoop | None = None

# ms-101 / e-3009 — WS receive の idle timeout (秒)。client ping 間隔 (bridge は
# 30s) より長く張り、その間 1 通も来なければ silent-dead (= 黙って切れた接続) と
# みなして台帳から外し close する。module 定数にしてテストが小さい値へ差し替え
# られるようにしてある。
_WS_IDLE_TIMEOUT_SECONDS = 90

# e-3834 — per-(project, session) の同時 WebSocket 接続数の上限。1 つのセッションの
# bridge が再接続を暴走させ、接続を 14,000 本超まで積み上げて server を OOM→全
# ユーザー 502 に落とした障害の構造防御。1 クライアントの異常再接続が他ユーザーの
# HTTP を巻き添えにしないための負荷隔離でもある。正常な bridge は 1 接続 (再接続の
# 重なりで一時的に 2)。8 は十分な余裕を持たせつつ OOM 域から遠い上限。session_id を
# 申告しない Web UI 接続はこの台帳に載らない (= 上限の対象外、複数タブを許容)。
_WS_MAX_CONNS_PER_SESSION = 8
_ws_session_conns: dict[tuple[str, str], set[str]] = {}


@app.on_event("startup")
async def _capture_event_loop():
    global _event_loop, _server_start_at
    _event_loop = asyncio.get_event_loop()
    # e-1391 follow-up (review H1): stamp boot time for tick-health uptime.
    import datetime as _dt
    _server_start_at = _dt.datetime.now(_dt.timezone.utc)


@app.on_event("startup")
async def _ensure_mysql_schema():
    """Create any missing MySQL tables at boot (ms-109 e-3591 incident,
    2026-07-19).

    The VPS deploy flow is ``git pull → pip install → systemctl restart`` — it
    does NOT run ``create_mysql_tables``. So a code change that adds a new
    entity/table (as the Target-decomposition split added opportunities /
    accounts / activities / communications / nurturings) deploys code that
    queries tables which do not exist yet, and every request 500s with
    ``pymysql 1146 (Table ... doesn't exist)`` until someone manually creates
    them. Ensuring the schema at boot means a schema addition can never again
    outrun its DDL. ``CREATE TABLE IF NOT EXISTS`` makes this a no-op once the
    tables exist. Non-fatal: a transient DB blip at boot must not crash-loop the
    service; the next healthy restart creates them and the error is logged.
    """
    if os.environ.get("BEACON_STORE_BACKEND", "firestore").lower() != "mysql":
        return
    try:
        import mysql_client  # noqa: PLC0415
        created = mysql_client.create_mysql_tables()
        _server_logger.info(
            "[startup] mysql schema ensured (%s table(s) created)", created)
    except Exception as e:  # noqa: BLE001 — never crash boot on a DB blip
        _server_logger.error("[startup] create_mysql_tables failed: %s", e)


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


@app.on_event("startup")
async def _verify_scheduler_key_configured():
    """Refuse to start in production with the dev scheduler-tick key (e-4115).

    Twin guard to ``_verify_envelope_secret_configured`` for the *other*
    internal secret. ``BEACON_SCHEDULER_INTERNAL_KEY`` gates
    ``POST /api/system/trek-scheduler/tick`` (which drives Trek / Operation
    autonomy) and the T1-system envelope mint path. Its dev fallback
    (``server/envelope.py:_DEV_FALLBACK_SCHEDULER_KEY``) is visible in the
    public repo, so booting production with the key unset would let anyone who
    reads the source drive the tick / mint endpoints. Fail fast at boot (which
    reddens the pull-deploy health check) rather than silently at first
    unauthorized tick.

    Same posture gate as the envelope-secret guard: only enforced when
    ``_auth_enabled`` (production-ish, ``BEACON_API_AUTH`` != "0"). Local dev /
    unit tests (``BEACON_API_AUTH=0``) may use the fallback; we only log INFO.
    """
    if _auth_enabled and envelope_mod.is_using_dev_scheduler_key():
        msg = (
            "BEACON_SCHEDULER_INTERNAL_KEY not configured for production "
            "(BEACON_API_AUTH=1 but the scheduler tick / mint endpoints would "
            "accept the dev fallback key visible in the public repo). Set "
            "BEACON_SCHEDULER_INTERNAL_KEY to a random 32+ byte value in "
            "/etc/beacon/app.env before deploying — e.g. "
            "`BEACON_SCHEDULER_INTERNAL_KEY=$(openssl rand -hex 32)` — and "
            "restart beacon-api. See docs/DEPLOY_VPS.md『定期ティック』."
        )
        _server_logger.error(msg)
        raise RuntimeError(msg)
    if envelope_mod.is_using_dev_scheduler_key():
        _server_logger.info(
            "scheduler tick key using dev fallback "
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
    # ms-108 e-5194: keep the enrich siblings symmetric — a sales project's board
    # needs the resolved funnel here too (no-op for non-sales / dev).
    enriched.update(sales_entities.payload_funnels(data))
    return enriched


async def _broadcast(project_id: str, data: dict | None = None):
    """Notify subscribed WS clients that the project changed (ms-84 / e-2326).

    Signal-only payload (~30 bytes). The previous attempts (full payload →
    slim w/ entries dropped → slim w/ tab-scoped arrays dropped) all hit a
    Cloud Run / GFE WS frame tolerance that's tighter than expected (=
    measured: 17.6 KiB works, 84 KiB consistently 1006-closes). Instead of
    chasing the threshold by stripping more fields, we make the WS a pure
    signal channel: clients fetch the actual state via REST (which has no
    frame limit). This is a permanent fix to the frame-size class of bugs.

    ms-98 (e-3837): ``data`` is IGNORED — the payload is signal-only. The
    parameter is retained (optional) only so the legacy ``_on_snapshot``
    caller (kept for a contract test) can still pass hydrated data without a
    signature break. Callers should pass nothing; do NOT ``load_project_*``
    just to feed this argument (that was the memory-churn dead-load removed
    in the 2026-07-21 hang fix).
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
      * fail-safe: broadcast 失敗で write 経路を巻き戻さない、 caller 視点では
        fire-and-forget
      * thread-safe: ``asyncio.run_coroutine_threadsafe`` で worker thread から
        event loop に乗せる (= apply_operation は同期 path で呼ばれる)

    ms-98 (e-3837): ``_broadcast`` の payload は ms-84 の signal-only 化以降
    ``{"type":"project_changed"}`` (~30 バイト) だけで、 渡した ``data`` を
    完全に無視する。 以前はここで ``load_project_consistent`` を呼んで全
    milestones/entries を再構成した dict を渡していたが、 それは 100% 捨てられる
    dead-load だった (= 全書き込みで巨大 dict を生成・破棄 → 2026-07-21 本番
    ハングのメモリ churn 副因)。 signal のみで broadcast し、 load 呼び出しは
    削除する。 client は WS signal を受けて REST で最新状態を取り直す。
    """
    if not _ws_connections.get(project_id):
        return
    if _event_loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _broadcast(project_id), _event_loop
        )
    except Exception:
        # event loop が閉じてる等の race。 fire-and-forget なので silent skip。
        return


async def _deliver_bus_signal_local(project_id: str, event_id):
    """このプロセスにローカル接続している WS クライアントへ bus event の wake hint
    を送る (ms-101 / e-3011 で _broadcast_bus_event から切り出し)。

    signal-only: frame は wake hint (event_id) のみ。DM 本文 / 送信者 / envelope は
    載せない (ms-97 P1 = review finding H1)。全 WS 購読者 (= bridge / Web UI) が
    宛先に関係なく frame を受け取るため、本文を載せると宛先でない receiver にまで
    漏れる。受信側は event_id を合図に REST inbox を引き直し、そこで per-recipient
    の ``_bus_event_addressed_to`` filter + DM payload redaction (ms-93 / e-2275) が
    効く。ここに追加してよいのは非機微な routing metadata のみ。
    """
    clients = _ws_connections.get(project_id, set()).copy()
    if not clients:
        return
    msg = {"type": "bus_event", "event_id": event_id}
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

    ms-101 / e-3011 以降は同プロセス内のローカル配信の薄い wrapper。cross-process
    配信は ``_fanout_bus_event`` (Redis pub/sub) が担う。
    """
    await _deliver_bus_signal_local(project_id, event.get("event_id"))


async def _fanout_bus_event(project_id: str, event: dict):
    """新着 bus event の wake hint を全プロセスの受信者へ届ける (ms-101 / e-3011)。

    従来 (e-997 / e-2380) は post_bus_event を処理したプロセスの WS 接続にしか
    push できなかった。受信者の bridge が別プロセス (= uvicorn の別ワーカー) に
    つながっていると届かず、その受信者は次の poll (= 定期問い合わせ) まで DM に
    気づけなかった。

    2 経路で届ける:
      (1) 同一プロセスの受信者へは **必ず直接** 配信する (_deliver_bus_signal_local)。
          Redis や subscriber スレッドの生死に依存させない。以前は Redis UP 時に
          publish のみで直接配信しなかったため、subscriber スレッドが listen の
          一時エラーで死ぬと、同プロセスの受信者が push を取りこぼし 120s backstop
          に無言で劣化していた (ms-101 review finding B の是正)。
      (2) 別プロセス (uvicorn 別ワーカー) の受信者へは Redis pub/sub で中継する。
          各プロセスの subscriber (_run_ws_push_subscriber) が受け取りローカル配信。

    signal-only (event_id のみ) なので、発行元プロセスの subscriber が pub/sub 経由で
    同じ hint を再配信して自プロセス受信者に二重に届いても無害 (bridge は wakePoll
    するだけで pollOnce が cursor で dedup する)。Redis 不通なら (2) が no-op になる
    だけで (1) は常に効く (= 単一プロセス構成 / Redis 障害でもローカル配信は保証)。
    """
    event_id = event.get("event_id")
    await _deliver_bus_signal_local(project_id, event_id)
    redis_client.publish_ws_push(project_id, event_id)


def _dispatch_ws_push_message(message) -> bool:
    """pub/sub で受けた 1 メッセージを解釈し、該当 project にローカル接続がある
    ときだけローカル配信コルーチンを event loop に乗せる (ms-101 / e-3011)。

    subscriber スレッドの blocking listen ループから切り出したので、per-message の
    ルーティング判断を loop と独立に検証できる。Returns True if a delivery was
    scheduled (= テスト用)。thread→loop の橋渡しは run_coroutine_threadsafe。
    """
    if message.get("type") != "message":
        return False
    try:
        data = json.loads(message.get("data") or "{}")
    except Exception:
        return False
    pid = data.get("project_id")
    event_id = data.get("event_id")
    if not pid:
        return False
    # このプロセスに該当 project の接続が無ければ配ることは無い (速攻 skip)。
    if _event_loop is not None and _ws_connections.get(pid):
        try:
            asyncio.run_coroutine_threadsafe(
                _deliver_bus_signal_local(pid, event_id), _event_loop
            )
            return True
        except Exception:
            # event loop が閉じている等の race。fire-and-forget で skip。
            return False
    return False


def _run_ws_push_subscriber():
    """Background thread: Redis の push チャンネルを購読し、受け取った wake hint を
    このプロセスのローカル WS 接続へ配信する (ms-101 / e-3011)。

    redis-py の pubsub は同期 API (listen() が blocking) なので専用スレッドで
    回し、message 受信時に ``asyncio.run_coroutine_threadsafe`` で event loop 上の
    ローカル配信コルーチンに乗せる (= 旧 Firestore watcher と同じ thread→loop 橋渡し)。

    resilient loop: listen 中の transient なエラーや Redis 一時不通でも thread を
    終わらせず、cooldown 後に再購読を試みる。以前は一度の listen エラー / boot 時の
    一瞬の Redis 不通で subscriber が永久に止まり、cross-process push がプロセス寿命
    まで復活しなかった (ms-101 review finding B/C の是正)。Redis 復旧後に自然回復する。
    local 配信は _fanout_bus_event が直接行うので、ここが一時的に止まっても影響は
    cross-process push のみ (= 別ワーカー接続の受信者が backstop poll に degrade する
    だけで DM は取りこぼさない)。

    redis-py 自体が未インストールの環境 (= CI / 従来 local) では Redis が永久に
    現れないので retry せず即終了する。
    """
    if redis_client._redis is None:
        _server_logger.info(
            "ws push subscriber disabled (redis-py not installed) — "
            "poll backstop covers delivery"
        )
        return
    _WS_SUB_RETRY_S = 5
    while True:
        pubsub = redis_client.ws_push_subscription()
        if pubsub is None:
            # Redis 不通 (redis_client 側の retry cooldown 中)。少し待って再試行。
            time.sleep(_WS_SUB_RETRY_S)
            continue
        _server_logger.info("ws push subscriber connected")
        try:
            for message in pubsub.listen():
                _dispatch_ws_push_message(message)
        except Exception as exc:
            _server_logger.warning(
                "ws push subscriber loop errored (%s) — re-subscribing after %ss",
                exc, _WS_SUB_RETRY_S,
            )
            try:
                pubsub.close()
            except Exception:
                pass
            time.sleep(_WS_SUB_RETRY_S)


_ws_subscriber_started = False


@app.on_event("startup")
async def _start_ws_push_subscriber():
    """起動時に ws push subscriber スレッドを立てる (ms-101 / e-3011)。

    daemon スレッドなので process 終了を妨げない。Redis 不通なら subscriber は
    poll backstop が配信を担保する (fail-open)。

    冪等: startup hook が複数回走っても (= 同一プロセスで ASGI lifespan が再入する
    テスト等) subscriber スレッドは 1 本だけにする。ガードが無いと startup ごとに
    スレッドが増え、1 event が同じ WS 受信者へ複数回配信される / スレッド leak になる
    (ms-101 review finding の是正)。
    """
    global _ws_subscriber_started
    if _ws_subscriber_started:
        return
    _ws_subscriber_started = True

    import threading

    threading.Thread(
        target=_run_ws_push_subscriber,
        name="ws-push-subscriber",
        daemon=True,
    ).start()


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
      4429  TOO MANY CONNECTIONS — this (project, session) already holds the
            (reason="too_many_connections")
                                  per-session connection cap
                                  (_WS_MAX_CONNS_PER_SESSION). Client MUST
                                  back off hard before retrying — a runaway
                                  reconnect once opened >14k sockets and
                                  OOM-ed the server (e-3834).

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

    # 接続の識別子。純粋な値計算 (I/O なし)。finally が discard / ws_unregister で
    # 参照するため try の前で確定させる。accept より前に確定させるのは、下の接続数
    # 上限チェックを accept 前に行い、超過分は accept せず即 close するため。
    conn_id = uuid.uuid4().hex
    session_id = websocket.query_params.get("session_id") or ""
    _registry_enabled = bool(session_id)

    # e-3834 — per-(project, session) 同時接続上限の enforce。reserve-then-check:
    # 予約 (set への add) と判定を await を挟まない同期区間で行うことで、暴走
    # クライアントの burst 接続が同時に上限チェックをすり抜けて超過するのを防ぐ。
    # 超過なら予約を戻し、accept せず 4429 (too_many_connections) で閉じる。正常な
    # クライアントはこの close code を受けて backoff する。session_id 申告なし (=
    # Web UI) は対象外。ここで登録した conn_id は finally で必ず外す。
    if _registry_enabled:
        _sess_key = (project_id, session_id)
        _sess_conns = _ws_session_conns.setdefault(_sess_key, set())
        _sess_conns.add(conn_id)
        if len(_sess_conns) > _WS_MAX_CONNS_PER_SESSION:
            _sess_conns.discard(conn_id)
            if not _sess_conns:
                _ws_session_conns.pop(_sess_key, None)
            await websocket.close(code=4429, reason="too_many_connections")
            return

    # accept とその後の登録 (_ws_connections への追加 / ws_register / ws_ready
    # 送信 / watcher 起動) を try の中で行い、途中で例外が出ても finally が必ず走って
    # in-process set と Redis 台帳の両方を後片付けする。登録を try の外でやると、
    # 例えば send_json 直後に client が TCP を切って例外が飛んだとき ws_unregister
    # が漏れ、死んだ session が最大 TTL(60s) 間 live=True と誤判定される
    # (ms-101 review finding の是正)。
    # e-3834 — accept も try の内側に入れる。上で予約した接続数上限のスロット
    # (_ws_session_conns) は、accept が例外を投げても finally で必ず解放される必要が
    # ある。accept を try の外に置くと、ストーム中に client がハンドシェイク途中で
    # 切って accept が投げたとき予約が台帳に漏れ、その session が幻の予約で恒久的に
    # cap 超過扱いになり正常な再接続まで永久に弾かれる (self-review で発見)。
    try:
        await websocket.accept()
        if project_id not in _ws_connections:
            _ws_connections[project_id] = set()
        _ws_connections[project_id].add(websocket)

        # ms-101 / e-3009 — 接続ベースの liveness (= 生存判定) 台帳への登録。
        # bridge (= 各セッションの常駐受信プロセス) は WS URL に ``session_id`` を
        # 付けて接続する。その接続を Redis の接続台帳 (redis_client.ws_register) に
        # 登録し、「今つながっている session」を複数プロセス間で共有された真値源に
        # する。Web UI ダッシュボードは session_id を付けずに接続するので、liveness
        # 台帳には載らない (= directory の live 一覧はあくまで bridge session が対象)。
        #
        # session_id は client 申告値 (token claims ではない)。project へのアクセスは
        # 直前の _require_project_role で既に認可済みなので、悪用しても「自分が入れる
        # project 内で任意の session_id を live に見せる」までで、他 project には波及
        # しない。token claims への束縛は将来課題 (= 過剰設計を避け現状は申告値を許容)。
        if _registry_enabled:
            redis_client.ws_register(project_id, session_id, conn_id)

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

        # ms-101 / e-3009 — ping/pong keepalive + silent 切断 (= 黙って切れた接続) の
        # 能動回収。bridge は 30s ごとに "ping" を送る (channel/bus.mjs)。受信のたびに
        # 台帳 TTL を更新 (ws_refresh) することで、生きている間だけ live が保たれる。
        #
        # WS は NAT / proxy のアイドル切断で TCP レベルの close が飛ばず黙って死ぬ
        # ことがある (SPEC #5)。receive を無期限に待つと dead socket を掴んだまま
        # ハングし、台帳の TTL 失効に頼るしかない。そこで client ping 間隔 (30s) より
        # 長い idle timeout (90s) を張り、その間 1 通も来なければ silent-dead とみなして
        # 台帳から外し close する (= 台帳 TTL 失効 <=60s を待たず能動的に回収する)。
        while True:
            try:
                msg = await asyncio.wait_for(
                    websocket.receive_text(), timeout=_WS_IDLE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                # client ping が途絶えた = silent-dead。close して後片付けへ。
                try:
                    await websocket.close(code=1001)  # going away
                except Exception:
                    pass
                break
            if msg == "ping":
                if _registry_enabled:
                    # keepalive: 生存確認できたので台帳 TTL を更新する。
                    redis_client.ws_refresh(project_id, session_id, conn_id)
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        _ws_connections.get(project_id, set()).discard(websocket)
        _stop_watcher(project_id)
        if _registry_enabled:
            redis_client.ws_unregister(project_id, session_id, conn_id)
            # e-3834 — 接続数上限台帳からも外す。空になった key は dict から除去して
            # session 台帳が無限に増えないようにする。
            _sess_key = (project_id, session_id)
            _sess_set = _ws_session_conns.get(_sess_key)
            if _sess_set is not None:
                _sess_set.discard(conn_id)
                if not _sess_set:
                    _ws_session_conns.pop(_sess_key, None)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

# 本番 readiness の宣言的チェック (= ms-105 e-3312)。
# 「本番 (= BEACON_ENV=prod) で満たされていないと Web UI / 認証が壊れる設定」を
# 1 箇所に宣言する。/health はこのリストを回すだけで判定するので、必須設定が
# 増えても health() 本体に症状ごとの手書き if を足さずに済む (= e-3197 で client_id
# 欠落だけを手書き if にしていたのを、宣言的な一般形に整理したもの)。
# 各 check は:
#   - env_var:  必須の環境変数名
#   - applies(env, provider): その環境で必須になる条件 (= 満たすとき check 対象)
#   - detail:   欠落時に /health が 503 で返す説明 (運用者向けに直し方まで書く)
_PROD_READINESS_CHECKS = [
    {
        "env_var": "BEACON_OAUTH_CLIENT_ID",
        "applies": lambda env, provider: env == "prod" and provider == "firebase",
        "detail": (
            "BEACON_OAUTH_CLIENT_ID is unset in a firebase-provider production "
            "deploy — the Web UI login button would be dead. Set it in "
            "/etc/beacon/app.env (see deploy/app.env.example for the required env "
            "and docs/DEPLOY_VPS.md for the value) and redeploy."
        ),
    },
]


def evaluate_prod_readiness(env: str, provider: str, environ) -> list:
    """現在の環境に対する readiness 失敗の説明一覧を返す (空 = 健全)。

    ``_PROD_READINESS_CHECKS`` のうち ``applies(env, provider)`` が真で、かつ
    ``env_var`` が未設定 / 空白のものが、その ``detail`` を 1 件ずつ積む。
    ms-105 e-3312: health() の症状ごとの手書き if を宣言的リストに置き換えた
    もので、新しい本番必須設定を足すのは上のリストへの 1 行追記で済む
    (= endpoint 本体を編集しない)。純関数なので単体テストしやすい。
    """
    failures = []
    for check in _PROD_READINESS_CHECKS:
        if not check["applies"](env, provider):
            continue
        if not str(environ.get(check["env_var"], "")).strip():
            failures.append(check["detail"])
    return failures


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
    # readiness: 本番 (prod) で満たされるべき必須設定を宣言的に評価する
    # (= ms-105 e-3312、旧 e-3197 の client_id 手書き if を一般化)。欠落があれば
    # 503 を返し、vps-pull-deploy.sh の health check (curl -fsS、非 2xx で失敗) が
    # deploy を赤くするので、silent 障害 (例: ログインボタンが黙って消える) が
    # 「気付けない本番障害」ではなく「赤くなった deploy」として顕在化する。
    # dev / test は applies が偽なので env 未設定でも 200 のまま。
    _env = os.environ.get("BEACON_ENV", "dev")
    _provider = os.environ.get("BEACON_AUTH_PROVIDER", "firebase").lower()
    _failures = evaluate_prod_readiness(_env, _provider, os.environ)
    if _failures:
        raise HTTPException(
            status_code=503,
            detail="degraded: " + "; ".join(_failures),
        )
    resp = {
        "status": "ok",
        "env": _env,
        "version": _beacon_version,
    }
    # ms-96 / e-3052: 総 DB 接続数を観測フィールドとして加える。status 判定には
    # 一切影響させない (= /health の 200/503 契約と deploy health check を壊さない)。
    # mysql backend の時だけ、接続レジストリの値を fail-safe に載せる (取得失敗は
    # 黙って省略 = 観測フィールドが health 本体を落とさない)。
    try:
        if os.environ.get("BEACON_STORE_BACKEND", "").lower() == "mysql":
            import mysql_client as _mysql_client
            resp["db"] = _mysql_client.connection_stats()
    except Exception:
        pass
    return resp

# ms-127 e-4869: /api/auth/* は server/routers_auth.py へ切り出し済み
# (factory + include_router)。token 発行 _make_cli_token は verify 対の
# _verify_cli_token と同居させるため app.py 所有のまま注入。config flag
# (_local_dev_enabled / _AUTH_PROVIDER) は test が runtime 書き換えするため
# 値でなく callable で渡す。_cli_pending は auth-local state として router 側へ移設。


# ---------------------------------------------------------------------------
# ms-127 e-4868 (B フェーズ scaffold): resource routers split out of this
# god-module and mounted via include_router. First extraction = /api/version
# (self-contained, no auth). Follow-ups add auth-requiring routers using the
# require_auth injection pattern (see trailnode.make_router below).
# ---------------------------------------------------------------------------

from routers_version import make_router as _make_version_router

app.include_router(_make_version_router())

# ms-127 e-4869: /api/me/* self-service router (first auth-requiring extraction).
# Shared session-liveness helpers stay owned by app.py (the not-yet-extracted
# /api/projects/{pid}/sessions consumes them too) and are injected, mirroring
# the require_auth injection pattern.
from routers_me import make_router as _make_me_router

app.include_router(
    _make_me_router(
        require_auth,
        stamp_session_liveness=_stamp_session_liveness,
        session_is_live=_session_is_live,
    )
)

# ms-127 e-4869: /api/orgs/* (Beacon team orgs, distinct from /api/trailnode/orgs).
# is_auth_enabled is injected as a callable (not a bool) so the membership guards
# read app.py's current _auth_enabled per-request; _load_org_for_member stays
# owned by app.py because /api/projects/{id}/rehome also uses it.
from routers_orgs import make_router as _make_orgs_router

app.include_router(
    _make_orgs_router(
        require_auth,
        is_auth_enabled=lambda: _auth_enabled,
        load_org_for_member=_load_org_for_member,
    )
)

# ms-127 e-4869: /api/admin/* (instance admin). _require_admin (also used by a
# non-admin endpoint) and _apply_op_and_broadcast (used by ~25 endpoints) stay
# owned by app.py and are injected.
from routers_admin import make_router as _make_admin_router

app.include_router(
    _make_admin_router(
        require_auth,
        require_admin=_require_admin,
        apply_op_and_broadcast=_apply_op_and_broadcast,
    )
)

# ms-127 e-4869 (final slice): /api/auth/* (login / CLI-pairing / token exchange).
# _make_cli_token stays in app.py (pairs with _verify_cli_token used by the
# verify path); the config flags are injected as callables so tests that flip
# app._local_dev_enabled / app._AUTH_PROVIDER at runtime are reflected.
from routers_auth import make_router as _make_auth_router

app.include_router(
    _make_auth_router(
        require_auth,
        make_cli_token=_make_cli_token,
        get_local_dev_enabled=lambda: _local_dev_enabled,
        get_auth_provider=lambda: _AUTH_PROVIDER,
    )
)

# ms-127 e-4870: /api/treks/* (collaborative trek work-rooms). Trek-only guards
# + route handlers moved to routers_treks.py; app.py owns _load / _require_admin /
# _resolve_author / _apply_op_and_broadcast (shared) and the _auth_enabled flag,
# all injected. The scheduler-tick endpoint stays in app.py (see above).
from routers_treks import make_router as _make_treks_router

app.include_router(
    _make_treks_router(
        require_auth,
        _load=_load,
        _require_admin=_require_admin,
        _resolve_author=_resolve_author,
        _apply_op_and_broadcast=_apply_op_and_broadcast,
        is_auth_enabled=lambda: _auth_enabled,
    )
)

# ms-127 e-4871 (PR1/3): /api/projects/* core slice (CRUD / milestones / entries /
# operations / documents / claims / changelog / log-summary) moved to
# server/routers_projects.py. The project guard family (_load / _require_project_role
# / _require_write / _require_owner / _load_meta_only) + write/broadcast helpers stay
# owned by app.py and are injected (shared across the remaining project slices, so
# moving them would force circular imports). PR2 (members/sessions/…) + PR3 (bus/dm)
# still live in app.py.
from routers_projects import make_router as _make_projects_router

app.include_router(
    _make_projects_router(
        require_auth,
        _load=_load,
        _load_meta_only=_load_meta_only,
        _require_project_role=_require_project_role,
        _require_write=_require_write,
        _require_owner=_require_owner,
        _apply_op_and_broadcast=_apply_op_and_broadcast,
        _resolve_author=_resolve_author,
        _save=_save,
        _broadcast_project_after_write=_broadcast_project_after_write,
        _broadcast_document_change=_broadcast_document_change,
        require_envelope_for_action=require_envelope_for_action,
        is_auth_enabled=lambda: _auth_enabled,
    )
)

# ms-127 e-4871 (PR2/3): /api/projects/* membership + collaboration/session slice
# + /api/invitations/{token} moved to routers_projects.make_collab_router. Guards
# + session-liveness helpers stay in app.py and are injected (_compute_poll_health
# stays too, used by the staying _stamp_session_liveness). PR3 (bus/dm) still here.
from routers_projects import make_collab_router as _make_collab_router

# The helper callables are injected as late-binding thunks (lambda *a, **k:
# _load(*a, **k)) rather than direct references, so a moved handler resolves
# app.py's *current* helper at call time. This preserves the pre-move
# module-global semantics exactly: before extraction these routes called the
# module-global _load / _stamp_session_liveness / etc., so tests that
# monkeypatch app._load (e.g. test_invitation_api's _mock_load, or
# test_session_heartbeat) still take effect. Same rationale as the
# is_auth_enabled getter. In production nothing rebinds these, so behaviour is
# identical. require_auth stays a direct reference (FastAPI introspects it as a
# Depends() and must see the real dependency callable).
app.include_router(
    _make_collab_router(
        require_auth,
        _load=lambda *a, **k: _load(*a, **k),
        _load_meta_only=lambda *a, **k: _load_meta_only(*a, **k),
        _require_project_role=lambda *a, **k: _require_project_role(*a, **k),
        _require_write=lambda *a, **k: _require_write(*a, **k),
        _save=lambda *a, **k: _save(*a, **k),
        _apply_op_and_broadcast=lambda *a, **k: _apply_op_and_broadcast(*a, **k),
        _load_org_for_member=lambda *a, **k: _load_org_for_member(*a, **k),
        _session_is_live=lambda *a, **k: _session_is_live(*a, **k),
        _stamp_session_liveness=lambda *a, **k: _stamp_session_liveness(*a, **k),
        is_auth_enabled=lambda: _auth_enabled,
    )
)

# ms-127 e-4871 (PR3b): bus delivery routes (receive/cursors/ack) moved to
# routers_projects.make_bus_delivery_router. End-of-file mount is safe because
# /bus/audit lives in the EARLIER-mounted make_bus_gate_router and therefore
# wins over this router's /bus/{event_id} wildcard.
from routers_projects import make_bus_delivery_router as _make_bus_delivery_router

app.include_router(
    _make_bus_delivery_router(
        require_auth,
        _load=lambda *a, **k: _load(*a, **k),
        _load_meta_only=lambda *a, **k: _load_meta_only(*a, **k),
        _apply_dm_payload_visibility=lambda *a, **k: _apply_dm_payload_visibility(*a, **k),
        _caller_uid=lambda *a, **k: _caller_uid(*a, **k),
        _bus_event_addressed_to=lambda *a, **k: _bus_event_addressed_to(*a, **k),
    )
)

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
