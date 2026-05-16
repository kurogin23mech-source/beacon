"""Beacon Auth - Google OAuth authentication for cloud storage."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
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
        with open(cloud_json, "r") as f:
            return json.load(f).get("api_url", "")
    return "https://beacon-ai.dev"


def _load_firebase_config() -> dict:
    """Load Firebase client config from local file."""
    if not FIREBASE_CONFIG_PATH.exists():
        print(f"Error: Firebase config not found at {FIREBASE_CONFIG_PATH}")
        print("Use 'beacon auth login --web' for web-mediated login (no config file needed).")
        sys.exit(1)
    with open(FIREBASE_CONFIG_PATH, "r") as f:
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
    with open(CREDENTIALS_PATH, "w") as f:
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
                # Save the id_token
                BEACON_HOME.mkdir(parents=True, exist_ok=True)
                creds_data = {
                    "token": result.get("id_token", ""),
                    "email": result.get("email", ""),
                    "web_auth": True,
                }
                with open(CREDENTIALS_PATH, "w") as f:
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


def load_credentials():
    """Load and refresh cached credentials. Returns credentials or None."""
    if not CREDENTIALS_PATH.exists():
        return None

    with open(CREDENTIALS_PATH, "r") as f:
        creds_data = json.load(f)

    # Web auth mode: return raw dict with id_token
    if creds_data.get("web_auth"):
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
            with open(CREDENTIALS_PATH, "w") as f:
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
