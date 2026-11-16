"""Authentication ("/api/auth/*") router — login / CLI-pairing / token exchange.

ms-127 e-4869 (B フェーズ, e-4869 の最終スライス): the auth endpoints extracted
from the server/app.py god-module, following the auth-router型 established by
routers_me.py / routers_orgs.py / routers_admin.py
(make_router(require_auth, *, ...injected helpers)).

Originally a pure move from app.py (same paths, same shapes). Since then the
CLI-pairing routes (cli-start / cli-approve / cli-poll) have changed behaviour
(ms-187 e-7488): the code table lives in a shared store; cli-approve requires a
``sub`` (400 otherwise) and refuses a code another account already approved
(409); cli-poll answers 409 for a code that was already used; the approver's
browser token is no longer stored. The other routes are still verbatim moves.

The CLI-pairing code table lives in the injected ``pairing_table``
(``cli_pairing.PairingTable``), backed by a store **every server instance sees**
(ms-187 e-7488). It used to be a per-process dict here, which broke login on the
multi-instance Cloud Run deploy: cli-start / cli-approve / cli-poll land on
different instances, and an instance that never issued the code answered 404.
See ``server/cli_pairing.py`` for the full story and the safety properties.

Injected dependencies (owned by app.py, passed in to avoid an import cycle):

- ``pairing_table``          — ``cli_pairing.PairingTable`` over the shared store
                               (``StorePersistence(store_router)`` in production).
                               Type-checked at construction: a plain dict or a
                               missing table fails at mount, not at login time.

- ``require_auth``           — the host app's identity dependency (exchange-cli-
                               token and cli-approve require it).
- ``make_cli_token``         — ``app._make_cli_token``. Mints the long-lived HMAC
                               ``bcli.*`` token. Contract:
                               ``make_cli_token(sub, email, *, auth_time=None)
                               -> (token, expiry)``; exchange-cli-token passes
                               ``auth_time`` to carry the original sign-in time
                               forward (ms-187 90-day cap), so a substitute
                               must accept that keyword. Kept in app.py because it pairs
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
import time
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import store_router as db  # e-1544: same backend-routing binding app.py uses
from cli_pairing import PairingError, PairingTable

# ms-187 e-7490: the CLI renews its token through exchange-cli-token while it is in
# use. Cap how long one sign-in can be stretched that way, so renewal does not turn
# a 30-day token into an indefinitely valid one (independent AX review finding).
# Past this age the endpoint refuses and the person signs in again.
CLI_SESSION_MAX_AGE = 86400 * 90
# CLI tokens minted before auth_time existed carry only ``exp``; they were issued
# with a 30-day lifetime, so their sign-in time is at most exp - 30 days.
_LEGACY_CLI_TOKEN_LIFETIME = 86400 * 30


def _signed_in_at(user: dict) -> int:
    """When the caller actually signed in (``auth_time``), for the renewal cap.

    Firebase / Cognito ID tokens and CLI tokens minted since ms-187 carry
    ``auth_time``. Older CLI tokens only carry ``exp``; fall back to the latest
    possible issue time (exp - 30 days).
    """
    try:
        auth_time = int(user.get("auth_time") or 0)
    except (TypeError, ValueError):
        auth_time = 0
    if auth_time:
        return auth_time
    try:
        exp = int(user.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    return exp - _LEGACY_CLI_TOKEN_LIFETIME if exp else int(time.time())


class DevLoginRequest(BaseModel):
    email: str
    name: str = ""


class CliApproveRequest(BaseModel):
    code: str


def make_router(
    require_auth: Callable,
    *,
    make_cli_token: Callable[..., tuple[str, int]],
    get_local_dev_enabled: Callable[[], bool],
    get_auth_provider: Callable[[], str],
    pairing_table: PairingTable,
) -> APIRouter:
    """Build the /api/auth/* router with the host app's auth + token helpers.

    Called once from app.py and mounted via::

        app.include_router(make_router(
            require_auth,
            make_cli_token=_make_cli_token,
            get_local_dev_enabled=lambda: _local_dev_enabled,
            get_auth_provider=lambda: _AUTH_PROVIDER,
            pairing_table=PairingTable(StorePersistence(db)),
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

    if not isinstance(pairing_table, PairingTable):
        raise TypeError(
            "routers_auth.make_router: pairing_table must be a cli_pairing.PairingTable "
            f"(got {type(pairing_table).__name__}) — the CLI pairing codes must live in "
            "a store every server instance can see (ms-187 e-7488)."
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
        # ms-187 e-7490: the CLI also calls this to renew its own (bcli) token while
        # in use. Carry the original sign-in time forward and refuse once the chain
        # is older than CLI_SESSION_MAX_AGE, so renewal has a ceiling.
        signed_in_at = _signed_in_at(user)
        if int(time.time()) - signed_in_at > CLI_SESSION_MAX_AGE:
            raise HTTPException(
                status_code=401,
                detail="Sign-in is too old to renew; run `beacon auth login` again",
            )
        token, expiry = make_cli_token(sub, email, auth_time=signed_in_at)
        return {"status": "ok", "id_token": token, "email": email, "token_expiry": expiry}

    # ---- CLI Auth (Web UI-mediated flow) ----
    # 合言葉の表は pairing_table (= 全サーバー台で共有する保存先、ms-187 e-7488) が持つ。
    # start / approve / poll が別々の台に届いても同じ記録を見る。

    @router.post("/api/auth/cli-start")
    def cli_auth_start():
        """CLI calls this to get a pairing code. No auth required."""
        try:
            code = pairing_table.start()
        except PairingError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        return {
            "code": code,
            "expires_in": pairing_table.ttl_seconds,
            "url": f"https://beacon-ai.dev/cli-auth?code={code}",
        }

    @router.post("/api/auth/cli-approve")
    def cli_auth_approve(body: CliApproveRequest, user: dict = Depends(require_auth)):
        """Web UI calls this (authenticated) to approve a CLI pairing code."""
        sub = user.get("sub", "")
        if not sub:
            raise HTTPException(status_code=400, detail="missing sub claim")
        try:
            return pairing_table.approve(body.code, sub=sub, email=user.get("email", ""))
        except PairingError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)

    @router.get("/api/auth/cli-poll")
    def cli_auth_poll_get(code: str = ""):
        """CLI polls this to check if the code has been approved. No auth required."""
        try:
            result = pairing_table.poll(code)
        except PairingError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        if result["status"] != "approved":
            return {"status": "pending"}
        # Approved (and already marked consumed) — issue a long-lived CLI token.
        cli_token, token_expiry = make_cli_token(result["sub"], result["email"])
        return {
            "status": "approved",
            "email": result["email"],
            "id_token": cli_token,
            "token_expiry": token_expiry,
        }

    return router
