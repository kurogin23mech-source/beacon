"""Beacon Auth - Google OAuth authentication for cloud storage."""

from __future__ import annotations

import json
import os
import sys
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
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        print("Error: Auth dependencies not installed.")
        print("Run: pip install google-auth google-auth-oauthlib google-cloud-firestore")
        sys.exit(1)


def _load_firebase_config() -> dict:
    """Load Firebase client config."""
    if not FIREBASE_CONFIG_PATH.exists():
        print(f"Error: Firebase config not found at {FIREBASE_CONFIG_PATH}")
        print("This is a beacon installation issue.")
        sys.exit(1)
    with open(FIREBASE_CONFIG_PATH, "r") as f:
        return json.load(f)


def login():
    """Run Google OAuth flow and save credentials."""
    _ensure_deps()
    # Allow Google to return fewer scopes than requested (e.g. Firestore API not yet enabled)
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

    # Get user email
    email = _get_user_email(credentials)
    print(f"Logged in as: {email}")
    print(f"Credentials saved to: {CREDENTIALS_PATH}")


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

    email = _get_user_email(creds)
    if email:
        print(f"Logged in as: {email}")
    else:
        print("Logged in (could not retrieve email).")


def load_credentials():
    """Load and refresh cached credentials. Returns None if not logged in."""
    if not CREDENTIALS_PATH.exists():
        return None

    _ensure_deps()
    import google.oauth2.credentials
    import google.auth.transport.requests

    with open(CREDENTIALS_PATH, "r") as f:
        creds_data = json.load(f)

    credentials = google.oauth2.credentials.Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri=creds_data.get("token_uri"),
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=creds_data.get("scopes"),
    )

    # Always refresh to ensure we have a valid id_token for API calls
    if credentials.refresh_token:
        try:
            credentials.refresh(google.auth.transport.requests.Request())
            # Update saved token + id_token
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
