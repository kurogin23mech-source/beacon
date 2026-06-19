"""Beacon Auth - Google OAuth authentication for cloud storage."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
import webbrowser
from pathlib import Path

# Path-bundled firebase_config.json — used as fallback when the active
# profile does not have its own copy. Kept as a constant so test fixtures
# can compute its location without importing the module twice.
_BUNDLED_FIREBASE_CONFIG_PATH = Path(__file__).parent / "firebase_config.json"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


# ---------------------------------------------------------------------------
# Path helpers (ms-64 / e-1457: profile-aware resolution)
# ---------------------------------------------------------------------------
#
# These replace the legacy module-level constants. Resolution happens at each
# call so that switching profile (= ``--profile`` CLI arg or BEACON_PROFILE
# env var or cwd ``.beacon/cloud.json.profile`` field) is honored inside one
# process invocation without re-importing the module.

def _active_profile():
    """Resolve the active Profile for this call.

    Local import avoids any chance of a circular dependency when
    ``profile.py`` someday needs to look at auth state (it doesn't today).
    """
    import profile as _profile
    return _profile.resolve_active_profile()


def _credentials_path() -> Path:
    """Path to credentials.json under the active profile directory."""
    return _active_profile().credentials_path


def _firebase_config_path() -> Path | None:
    """Resolve firebase_config.json: prefer profile-local, fall back to bundled.

    Returns the profile-local copy if the active profile has one. Otherwise
    falls back to the file bundled inside the installed package
    (``lib/firebase_config.json``). Returns ``None`` if neither exists — the
    caller is expected to surface a clean error or route the user to the
    web-mediated login flow that does not need a config file.
    """
    profile_fc = _active_profile().firebase_config_path
    if profile_fc is not None and profile_fc.exists():
        return profile_fc
    if _BUNDLED_FIREBASE_CONFIG_PATH.exists():
        return _BUNDLED_FIREBASE_CONFIG_PATH
    return None


def _ensure_deps():
    """Check that auth dependencies are installed."""
    try:
        import google.auth  # noqa: F401
        import google.oauth2.credentials  # noqa: F401
    except ImportError:
        print("Error: Auth dependencies not installed.")
        print("Run: pip install google-auth google-auth-oauthlib google-cloud-firestore")
        sys.exit(1)


def _get_api_url() -> str:
    """Get API URL via the active profile resolution chain.

    Precedence (= lib/profile.py のドキュメントに従う):
      1. BEACON_API_URL env var
      2. cwd .beacon/cloud.json.api_url
      3. ~/.beacon/profiles/<active>/profile.json.api_url
      4. DEFAULT_API_URL (= https://beacon-ai.dev)

    旧版は (2) と (4) しか見ておらず、(1)(3) を完全に無視していた。
    その結果 BEACON_PROFILE=aws-ga で profile を指定しても auth login が
    https://beacon-ai.dev に向かう regression があった (= e-1627 検証で観測)。
    """
    return _active_profile().api_url


def _load_firebase_config() -> dict:
    """Load Firebase client config from the active profile (or bundled fallback)."""
    fc_path = _firebase_config_path()
    if fc_path is None:
        print("Error: Firebase config not found in the active profile or bundled with the package.")
        print("Use 'beacon auth login --web' for web-mediated login (no config file needed).")
        sys.exit(1)
    with open(fc_path, "r", encoding="utf-8") as f:
        return json.load(f)


def login():
    """Run identity-provider login flow and save credentials.

    Dispatches based on the active profile's server-side ``/api/auth/config``:
      - ``provider == "cognito"`` → Cognito Hosted UI redirect flow
        (= AWS GA-incubation 経路, e-1545)
      - else → Firebase / Google OAuth (= 既存 Cloud Run 経路)

    Profile resolution (= どのサーバを見るか) は active profile に従う。
    """
    # IdP 不要のローカル開発ログイン (= ローカルクラウド / docker-compose 経路)。
    # サーバが BEACON_LOCAL_DEV=1 で立てた /api/auth/dev-login にメールを投げて
    # bcli トークンを受け取る。provider discovery も firebase config も不要なので
    # 最優先で分岐する (ms-12 e-2041)。dispatch.py / bin/beacon が --dev を
    # BEACON_AUTH_DEV=1 に翻訳して渡す。
    if os.environ.get("BEACON_AUTH_DEV") == "1":
        login_dev()
        return

    # First discover what auth provider the server uses
    api_url = _get_api_url()
    try:
        with urllib.request.urlopen(f"{api_url}/api/auth/config", timeout=10) as r:
            server_auth = json.loads(r.read())
    except Exception:
        server_auth = {}

    if server_auth.get("provider") == "cognito":
        login_cognito(server_auth)
        return

    # Check if --web flag is passed (or no firebase_config.json resolvable)
    use_web = "--web" in sys.argv or _firebase_config_path() is None

    if use_web:
        login_web()
        return

    _ensure_deps()
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    from google_auth_oauthlib.flow import InstalledAppFlow

    config = _load_firebase_config()
    flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES)

    print("Opening browser for Google sign-in...")
    credentials = flow.run_local_server(port=0)

    # Save credentials into the active profile dir
    cred_path = _credentials_path()
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    creds_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or SCOPES),
    }
    with open(cred_path, "w", encoding="utf-8") as f:
        json.dump(creds_data, f, indent=2)

    email = _get_user_email(credentials)
    print(f"Logged in as: {email}")
    print(f"Credentials saved to: {cred_path}")


def login_web():
    """Web UI-mediated login flow. No firebase_config.json needed."""
    api_url = _get_api_url()

    # Step 1: Request a pairing code from the server
    print("Requesting pairing code...")
    try:
        req = urllib.request.Request(f"{api_url}/api/auth/cli-start", method="POST",
                                     data=b"", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"Error: Could not reach server at {api_url}: {e}")
        sys.exit(1)

    code = data["code"]
    url = data["url"]

    # Step 2: Open browser for user to approve
    print(f"\nYour pairing code: {code}")
    print(f"Opening: {url}")
    print("Sign in with Google and enter the code to authorize this CLI.\n")
    webbrowser.open(url)

    # Step 3: Poll for approval
    print("Waiting for approval", end="", flush=True)
    deadline = time.time() + 300
    while time.time() < deadline:
        time.sleep(3)
        print(".", end="", flush=True)
        try:
            poll_url = f"{api_url}/api/auth/cli-poll?code={code}"
            with urllib.request.urlopen(poll_url, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("status") == "approved":
                print(" approved!")
                # Save id_token (and refresh_token if provided by the server)
                cred_path = _credentials_path()
                cred_path.parent.mkdir(parents=True, exist_ok=True)
                id_token = result.get("id_token", "")
                creds_data: dict = {
                    "token": id_token,
                    "email": result.get("email", ""),
                    "web_auth": True,
                }
                # Prefer server-provided expiry (long-lived CLI tokens);
                # fall back to decoding the JWT for legacy Google ID tokens.
                if result.get("token_expiry"):
                    creds_data["token_expiry"] = result["token_expiry"]
                    creds_data["token_type"] = "beacon_cli"
                elif id_token:
                    creds_data["token_expiry"] = _decode_jwt_expiry(id_token)
                if result.get("refresh_token"):
                    creds_data["refresh_token"] = result["refresh_token"]
                with open(cred_path, "w", encoding="utf-8") as f:
                    json.dump(creds_data, f, indent=2)
                print(f"Logged in as: {result.get('email', '?')}")
                print(f"Credentials saved to: {cred_path}")
                return
        except urllib.error.HTTPError as e:
            if e.code == 410:
                print("\nCode expired. Run 'beacon auth login' again.")
                sys.exit(1)
            if e.code == 404:
                print("\nInvalid code.")
                sys.exit(1)
        except Exception:
            pass

    print("\nTimeout. Run 'beacon auth login' again.")
    sys.exit(1)


def _git_user_email() -> str:
    """Best-effort `git config user.email` lookup (= dev login の email 既定値)."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def login_dev() -> None:
    """IdP 不要のローカル開発ログイン (= server /api/auth/dev-login, ms-12 e-2041)。

    ローカルクラウド (docker-compose) は Google / Cognito を立てないので、本番の
    Web UI 承認フロー (login_web) が使えない。代わりにサーバの dev-login
    エンドポイントへ任意のメールアドレスを投げて bcli トークンを受け取り、
    active profile の credentials.json に保存する。本番サーバ (= dev-login 未有効)
    に対しては 404 が返るので、その旨を案内して中断する。

    メールの解決順:
      1. BEACON_DEV_EMAIL env (= dispatch.py が --email を翻訳)
      2. git config user.email
    表示名は BEACON_DEV_NAME env (= --name、任意)。

    本番用の認証情報を壊さないために、--profile / cloud.json.profile で
    ローカル専用プロファイルに切ってから実行するのが安全 (= credentials.json は
    profile 単位の共有ファイル)。
    """
    api_url = _get_api_url()
    email = os.environ.get("BEACON_DEV_EMAIL", "").strip() or _git_user_email()
    if not email:
        print("Error: dev login にはメールアドレスが必要です。")
        print("  `beacon auth login --dev --email you@example.com` のように渡すか、")
        print("  `git config user.email` を設定してください。")
        sys.exit(1)
    name = os.environ.get("BEACON_DEV_NAME", "").strip()

    payload: dict = {"email": email}
    if name:
        payload["name"] = name
    try:
        req = urllib.request.Request(
            f"{api_url}/api/auth/dev-login",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"Error: {api_url} は dev login 非対応です。")
            print("  サーバが BEACON_LOCAL_DEV=1 で起動しているか確認してください")
            print("  (本番サーバは dev login を常に 404 にします)。")
        else:
            body = ""
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            print(f"Error: dev login に失敗しました (HTTP {e.code}): {body}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {api_url} に接続できません: {e}")
        sys.exit(1)

    id_token = result.get("id_token", "")
    if not id_token:
        print(f"Error: サーバ応答にトークンが含まれていません: {result}")
        sys.exit(1)

    cred_path = _credentials_path()
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    creds_data: dict = {
        "token": id_token,
        "email": result.get("email", email),
        "web_auth": True,
        "token_type": "beacon_cli",
    }
    if result.get("token_expiry"):
        creds_data["token_expiry"] = result["token_expiry"]
    with open(cred_path, "w", encoding="utf-8") as f:
        json.dump(creds_data, f, indent=2)

    print(f"Logged in (dev) as: {creds_data['email']}")
    print(f"Server: {api_url}")
    print(f"Credentials saved to: {cred_path}")


def login_cognito(server_config: dict) -> None:
    """Cognito Hosted UI redirect flow (= e-1545 / AWS GA-incubation 経路).

    手順:
      1. 設定値を server_config から取得 (client_id / cognito_domain / region)
      2. ローカル HTTP listener を空きポートで立てる
      3. ブラウザを Cognito Hosted UI に開く (redirect_uri = ローカル listener)
      4. ?code= callback を受け取る
      5. POST /oauth2/token で id_token + refresh_token に交換
      6. active profile の credentials.json に保存

    Cognito SPA Client は ``generate_secret=false`` で立ててあるので
    client_secret は不要 (= 公開 client、PKCE は dev 段階では未使用、本番化前に追加)。
    """
    import http.server
    import socket
    import threading
    import urllib.parse

    client_id = server_config.get("client_id", "")
    cognito_domain = server_config.get("cognito_domain", "")
    if not client_id or not cognito_domain:
        print(
            "Error: Server returned cognito provider but missing "
            "client_id / cognito_domain. Check Lambda env vars "
            "(BEACON_COGNITO_CLIENT_ID / BEACON_COGNITO_HOSTED_UI_DOMAIN)."
        )
        sys.exit(1)

    # 1. Cognito SPA Client の callback_urls に登録されている固定ポートから
    #    空いてるものを選ぶ。Cognito は redirect_uri の完全一致を要求するため
    #    ランダムポートだと "redirect_uri does not match" でブロックされる
    #    (= beacon-cloud terraform module.cognito.callback_urls 参照)。
    candidate_ports = [5173, 8080]
    port = None
    for p in candidate_ports:
        try:
            test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test.bind(("127.0.0.1", p))
            test.close()
            port = p
            break
        except OSError:
            continue
    if port is None:
        print(
            f"Error: All callback ports {candidate_ports} are in use. "
            "Stop processes listening on these ports and retry "
            "(or add another port to Cognito's callback_urls)."
        )
        sys.exit(1)
    redirect_uri = f"http://localhost:{port}/"

    # 2. callback 受信用 listener
    received = {"code": None, "error": None}

    class _CognitoCallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server interface
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                received["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<!doctype html><html><head><title>Beacon login</title></head>"
                    b"<body style='font-family:sans-serif;padding:2em;text-align:center'>"
                    b"<h1>\xe2\x9c\x93 Login successful</h1>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )
            elif "error" in params:
                err = params.get("error_description", params.get("error", ["unknown"]))[0]
                received["error"] = err
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(err.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # noqa: N802 — http.server interface
            pass  # suppress access log

    server = http.server.HTTPServer(("127.0.0.1", port), _CognitoCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # 3. ブラウザを開く
    login_url = (
        f"https://{cognito_domain}/login?"
        f"client_id={urllib.parse.quote(client_id)}"
        f"&response_type=code"
        f"&scope={urllib.parse.quote('openid email profile')}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
    )
    print("Opening browser for Cognito sign-in...")
    print(f"If the browser does not open, visit:\n  {login_url}\n")
    webbrowser.open(login_url)
    print(f"Waiting for callback on {redirect_uri} (5 min timeout)...")

    # 4. callback を待つ
    timeout_sec = 300
    start = time.time()
    try:
        while received["code"] is None and received["error"] is None:
            if time.time() - start > timeout_sec:
                print("Error: Login timed out (5 minutes).")
                sys.exit(1)
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()

    if received["error"]:
        print(f"Error: Cognito returned an error: {received['error']}")
        sys.exit(1)

    code = received["code"]

    # 5. token 交換
    token_url = f"https://{cognito_domain}/oauth2/token"
    token_body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    req = urllib.request.Request(
        token_url,
        data=token_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"Error: token exchange failed (HTTP {e.code}): {err_body}")
        sys.exit(1)

    id_token = tokens.get("id_token", "")
    if not id_token:
        print(f"Error: token response missing id_token: {tokens}")
        sys.exit(1)

    # 6. credentials に保存
    issued_at = int(time.time())
    expires_in = int(tokens.get("expires_in", 86400))
    creds_data = {
        "provider": "cognito",
        "token": id_token,                          # downstream は creds["token"] を Bearer に使う
        "id_token": id_token,
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": "cognito_id_token",
        "expires_in": expires_in,
        "issued_at": issued_at,
        "token_expiry": issued_at + expires_in,
        "web_auth": True,                           # load_credentials の web_auth 経路で扱われる
        "email": _decode_jwt_email(id_token),
        "cognito_domain": cognito_domain,
        "client_id": client_id,
    }
    cred_path = _credentials_path()
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cred_path, "w", encoding="utf-8") as f:
        json.dump(creds_data, f, indent=2)
    try:
        cred_path.chmod(0o600)
    except OSError:
        pass
    print(f"Logged in as: {creds_data['email'] or '<unknown email>'}")
    print(f"Credentials saved to: {cred_path}")


def _decode_jwt_email(token: str) -> str:
    """JWT payload から email claim を抜き出す。失敗時は空文字。"""
    import base64
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email", "")
    except Exception:
        return ""


def logout():
    """Remove cached credentials from the active profile."""
    cred_path = _credentials_path()
    if cred_path.exists():
        cred_path.unlink()
        print("Logged out. Credentials removed.")
    else:
        print("Not logged in.")


def status():
    """Show current login status."""
    creds = load_credentials()
    if creds is None:
        print("Not logged in. Run: beacon auth login")
        return

    # Web auth stores email directly
    if isinstance(creds, dict) and creds.get("web_auth"):
        print(f"Logged in as: {creds.get('email', '?')} (web auth)")
        return

    email = _get_user_email(creds)
    if email:
        print(f"Logged in as: {email}")
    else:
        print("Logged in (could not retrieve email).")


def _decode_jwt_expiry(token: str) -> int:
    """Return the 'exp' Unix timestamp from a JWT without verifying signature.

    Returns 0 on any parse error (treat as already expired).
    """
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return 0
        # Add padding so base64 does not raise
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


def _refresh_web_auth_token(creds_data: dict) -> dict | None:
    """Try to refresh the web-auth id_token using a saved refresh_token.

    Uses the Firebase securetoken REST endpoint (no firebase_config.json needed
    as the API key is embedded in the token itself via the 'aud' claim).

    Returns an updated creds_data dict on success, or None on failure.
    """
    refresh_token = creds_data.get("refresh_token")
    if not refresh_token:
        return None

    # Derive the Firebase API key from the token audience ('aud' claim)
    api_key = _get_firebase_api_key_from_token(creds_data.get("token", ""))
    if not api_key:
        return None

    url = f"https://securetoken.googleapis.com/v1/token?key={api_key}"
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
    except Exception:
        return None

    new_id_token = result.get("id_token", "")
    if not new_id_token:
        return None

    updated = dict(creds_data)
    updated["token"] = new_id_token
    updated["token_expiry"] = _decode_jwt_expiry(new_id_token)
    if result.get("refresh_token"):
        updated["refresh_token"] = result["refresh_token"]

    try:
        cred_path = _credentials_path()
        with open(cred_path, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2)
    except OSError:
        pass  # best-effort; return the updated dict even if write fails

    return updated


def _get_firebase_api_key_from_token(token: str) -> str | None:
    """Extract the Firebase project's web API key stored in the token 'aud'.

    Firebase id_tokens have 'aud' = the project ID, not the API key directly.
    We fall back to reading firebase_config.json if present (profile-local
    first, then bundled).
    """
    fc_path = _firebase_config_path()
    if fc_path is not None:
        try:
            with open(fc_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            # Accept both top-level and nested {"web": {...}} formats
            web = config.get("web", config)
            return web.get("apiKey") or web.get("api_key")
        except Exception:
            pass
    return None


def load_credentials():
    """Load and refresh cached credentials. Returns credentials or None."""
    cred_path = _credentials_path()
    if not cred_path.exists():
        return None

    with open(cred_path, "r", encoding="utf-8") as f:
        creds_data = json.load(f)

    # Web auth mode: check expiry
    if creds_data.get("web_auth"):
        expiry = creds_data.get("token_expiry") or _decode_jwt_expiry(
            creds_data.get("token", "")
        )
        now = int(time.time())
        if expiry and now >= expiry - 60:
            # Long-lived CLI token: no refresh possible, just re-login
            if creds_data.get("token_type") == "beacon_cli":
                print(
                    "Error: セッションが期限切れです。`beacon auth login` を実行してください。\n"
                    "       (Session expired. Run: beacon auth login)"
                )
                return None
            # Cognito id_token: 現状は refresh 経路未実装、再ログインを促す
            # (= refresh_token は creds に保存済なので、follow-up で
            # /oauth2/token grant_type=refresh_token に交換する経路を足せる)
            if creds_data.get("token_type") == "cognito_id_token":
                print(
                    "Error: Cognito セッションが期限切れです (24h)。"
                    "`beacon auth login` で再ログインしてください。\n"
                    "       (Cognito session expired. Run: beacon auth login)"
                )
                return None
            # Legacy Google ID token: try silent refresh via refresh_token
            refreshed = _refresh_web_auth_token(creds_data)
            if refreshed:
                return refreshed
            print(
                "Error: トークンが期限切れです。`beacon auth login` を実行してください。\n"
                "       (Token expired. Run: beacon auth login)"
            )
            return None
        return creds_data

    # OAuth mode: use google credentials
    _ensure_deps()
    import google.oauth2.credentials
    import google.auth.transport.requests

    credentials = google.oauth2.credentials.Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri=creds_data.get("token_uri"),
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=creds_data.get("scopes"),
    )

    if credentials.refresh_token:
        try:
            credentials.refresh(google.auth.transport.requests.Request())
            creds_data["token"] = credentials.token
            if credentials.id_token:
                creds_data["id_token"] = credentials.id_token
            with open(cred_path, "w", encoding="utf-8") as f:
                json.dump(creds_data, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not refresh token: {e}")
            print("Run: beacon auth login")
            return None

    return credentials


def _get_user_email(credentials) -> str | None:
    """Get user email from credentials using userinfo endpoint."""
    try:
        import google.auth.transport.requests

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(google.auth.transport.requests.Request())

        session = google.auth.transport.requests.AuthorizedSession(credentials)
        resp = session.get("https://www.googleapis.com/oauth2/v3/userinfo")
        if resp.status_code == 200:
            return resp.json().get("email")
    except Exception:
        pass
    return None
