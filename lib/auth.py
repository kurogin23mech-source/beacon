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

# Credentials are stored per-user, not per-project
BEACON_HOME = Path.home() / ".beacon"
CREDENTIALS_PATH = BEACON_HOME / "credentials.json"
FIREBASE_CONFIG_PATH = Path(__file__).parent / "firebase_config.json"

SCOPES = [
    "https://www.googleapis.com/auth/datastore",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


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
    """Get API URL from cloud.json if available."""
    cloud_json = Path(".beacon/cloud.json")
    if cloud_json.exists():
        with open(cloud_json, "r", encoding="utf-8") as f:
            return json.load(f).get("api_url") or "https://beacon-ai.dev"
    return "https://beacon-ai.dev"


def _load_firebase_config() -> dict:
    """Load Firebase client config from local file."""
    if not FIREBASE_CONFIG_PATH.exists():
        print(f"Error: Firebase config not found at {FIREBASE_CONFIG_PATH}")
        print("Use 'beacon auth login --web' for web-mediated login (no config file needed).")
        sys.exit(1)
    with open(FIREBASE_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def login():
    """Run Google OAuth flow and save credentials."""
    # Check if --web flag is passed (or no firebase_config.json)
    use_web = "--web" in sys.argv or not FIREBASE_CONFIG_PATH.exists()

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

    # Save credentials
    BEACON_HOME.mkdir(parents=True, exist_ok=True)
    creds_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or SCOPES),
    }
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump(creds_data, f, indent=2)

    email = _get_user_email(credentials)
    print(f"Logged in as: {email}")
    print(f"Credentials saved to: {CREDENTIALS_PATH}")


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
                BEACON_HOME.mkdir(parents=True, exist_ok=True)
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
                with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
                    json.dump(creds_data, f, indent=2)
                print(f"Logged in as: {result.get('email', '?')}")
                print(f"Credentials saved to: {CREDENTIALS_PATH}")
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


def logout():
    """Remove cached credentials."""
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
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
        with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2)
    except OSError:
        pass  # best-effort; return the updated dict even if write fails

    return updated


def _get_firebase_api_key_from_token(token: str) -> str | None:
    """Extract the Firebase project's web API key stored in the token 'aud'.

    Firebase id_tokens have 'aud' = the project ID, not the API key directly.
    We fall back to reading firebase_config.json if present.
    """
    if FIREBASE_CONFIG_PATH.exists():
        try:
            with open(FIREBASE_CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
            # Accept both top-level and nested {"web": {...}} formats
            web = config.get("web", config)
            return web.get("apiKey") or web.get("api_key")
        except Exception:
            pass
    return None


def load_credentials():
    """Load and refresh cached credentials. Returns credentials or None."""
    if not CREDENTIALS_PATH.exists():
        return None

    with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
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
            with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
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
