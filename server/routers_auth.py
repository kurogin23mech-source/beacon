"""Authentication ("/api/auth/*") router — login / CLI-pairing / token exchange.

ms-127 e-4869 (B フェーズ, e-4869 の最終スライス): the auth endpoints extracted
from the server/app.py god-module, following the auth-router型 established by
routers_me.py / routers_orgs.py / routers_admin.py
(make_router(require_auth, *, ...injected helpers)).

Pure move: every route body is verbatim from app.py's auth handlers — same
paths, same shapes, no behavior change.

The CLI-pairing pending-code table ``_cli_pending`` is **auth-local in-memory
state** (only cli-start / cli-approve / cli-poll touch it), so it moves here as a
module global. cli-start writes it, cli-approve marks approved, cli-poll reads +
issues the token — all against this one process-local dict.

Injected dependencies (owned by app.py, passed in to avoid an import cycle):

- ``require_auth``           — the host app's identity dependency (exchange-cli-
                               token and cli-approve require it).
- ``make_cli_token``         — ``app._make_cli_token``. Mints the long-lived HMAC
                               ``bcli.*`` token. Kept in app.py because it pairs
                               with ``_verify_cli_token`` (which ``require_auth``'s
                               verify path uses) — the HMAC scheme stays
                               single-sourced.
- ``get_local_dev_enabled``  — a zero-arg callable returning app.py's current
                               ``_local_dev_enabled``. Passed as a callable (not a
                               bool) because tests flip the shared flag at
                               runtime; a frozen snapshot would go stale (same
                               reasoning as routers_orgs' is_auth_enabled).
- ``get_auth_provider``      — a zero-arg callable returning app.py's current
                               ``_AUTH_PROVIDER`` string (``firebase`` / ``cognito``),
                               which tests monkeypatch.

``db`` mirrors app.py's binding — ``store_router as db`` (e-1544 backend routing).
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import store_router as db  # e-1544: same backend-routing binding app.py uses

# In-memory pending CLI-pairing codes: code -> {sub, email, id_token, expires}.
# Auth-local process state (moved verbatim from app.py); shared across the three
# cli-auth routes built by make_router below.
_cli_pending: dict[str, dict] = {}


class DevLoginRequest(BaseModel):
    email: str
    name: str = ""


class CliApproveRequest(BaseModel):
    code: str


def make_router(
    require_auth: Callable,
    *,
    make_cli_token: Callable[[str, str], tuple],
    get_local_dev_enabled: Callable[[], bool],
    get_auth_provider: Callable[[], str],
) -> APIRouter:
    """Build the /api/auth/* router with the host app's auth + token helpers.

    Called once from app.py and mounted via::

        app.include_router(make_router(
            require_auth,
            make_cli_token=_make_cli_token,
            get_local_dev_enabled=lambda: _local_dev_enabled,
            get_auth_provider=lambda: _AUTH_PROVIDER,
        ))

    Keyword-only + ``Callable``-typed injected helpers, with a construction-time
    callability check (same rationale as the sibling routers): a mis-wire fails at
    mount rather than at request time.
    """
    for _name, _dep in (
        ("require_auth", require_auth),
        ("make_cli_token", make_cli_token),
        ("get_local_dev_enabled", get_local_dev_enabled),
        ("get_auth_provider", get_auth_provider),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_auth.make_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    @router.get("/api/auth/config")
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
        provider = get_auth_provider()
        if provider == "cognito":
            return {
                "provider": "cognito",
                "client_id": os.environ.get("BEACON_COGNITO_CLIENT_ID", ""),
                "cognito_domain": os.environ.get("BEACON_COGNITO_HOSTED_UI_DOMAIN", ""),
                "region": os.environ.get("AWS_REGION", "ap-northeast-1"),
                # local_dev: Web UI がローカル開発ログインフォームを出すかの判定に使う
                "local_dev": get_local_dev_enabled(),
            }
        # Firebase / Cloud Run 既存経路 (= 後方互換)
        # BEACON_OAUTH_CLIENT_ID は公開値 (= Web の Google Identity Services が使う
        # client_id。ページ HTML に埋まって配信されるので secret ではない) だが、
        # 固有値をソースにハードコードすると (a) このデプロイが常に beacon-ai.dev の
        # OAuth アプリを指す前提が焼き込まれ、(b) env 欠落を黙って埋めて設定ミスを
        # 隠す silent fallback になる。そこで値はソースに持たせず env を唯一の真値源に
        # する (= ms-96 e-3196)。固有の実値は runbook (docs/DEPLOY_VPS.md) と本番の
        # /etc/beacon/app.env だけが持ち、repo の deploy/app.env.example は placeholder
        # を置くテンプレート (= 固有値を焼き込まない、ms-105 e-3313)。env 欠落は隠さず、
        # 本番では /health が 503 を返して deploy を赤くする (= e-3197、上記 health()
        # 参照) ので、実値を repo に置かなくても無音欠落しない。dev はログインフォーム
        # 経路なので空でも 200 のまま。
        return {
            "provider": "firebase",
            "client_id": os.environ.get("BEACON_OAUTH_CLIENT_ID", ""),
            # local_dev: ローカル時のみ true。本番 Cloud Run では env 未設定 = false。
            "local_dev": get_local_dev_enabled(),
        }

    @router.post("/api/auth/dev-login")
    def dev_login(body: DevLoginRequest):
        """ローカル専用・IdP 不要のログイン。任意の email に対して bcli トークンを発行する。

        BEACON_LOCAL_DEV=1 のときだけ有効 (= ハードゲート)。本番 (Cloud Run) は
        この env を設定しないので、ここは常に 404 を返し到達不能。Google / Cognito を
        立てずに、ローカルサーバを複数人が別アカウントで使い分けられるようにするための
        入口。発行したトークンは provider 非依存の HMAC なので require_auth が
        そのまま検証し、sub / email 単位でアカウントが分かれる。
        """
        if not get_local_dev_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        email = (body.email or "").strip().lower()
        if not email:
            raise HTTPException(status_code=400, detail="email required")
        # sub は email から決定論的に導出 (= 同じ email は同じアカウント)。
        sub = f"dev:{email}"
        token, expiry = make_cli_token(sub, email)
        # member picker 等に出るよう先にユーザー登録しておく。
        db.get_or_create_user(sub, email)
        name = (body.name or "").strip()
        if name:
            db.update_user(sub, {"display_name": name})
        return {"status": "ok", "id_token": token, "email": email, "token_expiry": expiry}

    @router.post("/api/auth/exchange-cli-token")
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
        token, expiry = make_cli_token(sub, email)
        return {"status": "ok", "id_token": token, "email": email, "token_expiry": expiry}

    # ---- CLI Auth (Web UI-mediated flow) ----

    @router.post("/api/auth/cli-start")
    def cli_auth_start():
        """CLI calls this to get a pairing code. No auth required."""
        code = secrets.token_urlsafe(6)[:8].upper()  # Short human-readable code
        _cli_pending[code] = {"expires": time.time() + 300}
        # Cleanup expired
        now = time.time()
        for k in [k for k, v in _cli_pending.items() if v["expires"] < now]:
            del _cli_pending[k]
        return {"code": code, "expires_in": 300, "url": f"https://beacon-ai.dev/cli-auth?code={code}"}

    @router.post("/api/auth/cli-approve")
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

    @router.get("/api/auth/cli-poll")
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
        cli_token, token_expiry = make_cli_token(sub, email)
        result = {
            "status": "approved",
            "email": email,
            "id_token": cli_token,
            "token_expiry": token_expiry,
        }
        del _cli_pending[code]
        return result

    return router
