"""Tests for Cognito JWT verification path in server/app.py (e-1545).

Strategy:
- Generate a local RSA keypair.
- Patch ``jwt.PyJWKClient.get_signing_key_from_jwt`` to return the local
  public key so we don't have to spin up an HTTP mock for the JWKS endpoint.
- Mint tokens with the local private key for both ID-token and access-token
  use cases.
- Drive ``_verify_id_token`` / ``_verify_cognito_token`` through the full
  dispatch (provider env var) so the integration with the bearer-token
  pathway is also covered.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from typing import Iterator

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))


USER_POOL_ID = "ap-northeast-1_testpool"
CLIENT_ID = "test-client-id"
REGION = "ap-northeast-1"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"


@pytest.fixture
def keypair():
    """Generate a fresh RSA keypair for signing test tokens."""
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key()
    return private, public


@pytest.fixture
def cognito_app(monkeypatch, keypair):
    """Reload server.app with Cognito env vars + patch JWKS key resolution."""
    pytest.importorskip("jwt")
    pytest.importorskip("fastapi")

    monkeypatch.setenv("BEACON_AUTH_PROVIDER", "cognito")
    monkeypatch.setenv("BEACON_COGNITO_USER_POOL_ID", USER_POOL_ID)
    monkeypatch.setenv("BEACON_COGNITO_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AWS_REGION", REGION)

    # Reload app so module-level ``_AUTH_PROVIDER`` picks up the env var.
    if "app" in sys.modules:
        del sys.modules["app"]
    app_module = importlib.import_module("app")

    # Reset the JWKS client cache so the patched resolver applies cleanly.
    app_module._cognito_jwks_client = None

    private, public = keypair

    class _StubKey:
        def __init__(self, key):
            self.key = key

    def _stub_get_signing_key_from_jwt(self, token):  # noqa: ARG001
        return _StubKey(public)

    import jwt
    monkeypatch.setattr(
        jwt.PyJWKClient, "get_signing_key_from_jwt", _stub_get_signing_key_from_jwt
    )

    yield app_module, private

    # Cleanup so other tests don't inherit Cognito env / module state
    app_module._cognito_jwks_client = None
    if "app" in sys.modules:
        del sys.modules["app"]


def _mint(private_key, *, token_use: str = "id",
          aud: str = CLIENT_ID,
          client_id_claim: str = CLIENT_ID,
          email: str = "alice@example.com",
          sub: str = "user-sub-1",
          iss: str = ISSUER,
          exp_offset: int = 3600) -> str:
    """Mint an RS256-signed JWT matching the Cognito claim shape."""
    import jwt

    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": iss,
        "token_use": token_use,
        "iat": now,
        "exp": now + exp_offset,
    }
    if token_use == "id":
        payload["aud"] = aud
        payload["email"] = email
    elif token_use == "access":
        payload["client_id"] = client_id_claim
    return jwt.encode(payload, private_key, algorithm="RS256")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_cognito_id_token_accepted(cognito_app):
    app_module, private = cognito_app
    token = _mint(private)
    claims = app_module._verify_id_token(token)
    assert claims["sub"] == "user-sub-1"
    assert claims["email"] == "alice@example.com"
    assert claims["token_use"] == "id"


def test_cognito_access_token_accepted(cognito_app):
    app_module, private = cognito_app
    token = _mint(private, token_use="access")
    claims = app_module._verify_id_token(token)
    assert claims["sub"] == "user-sub-1"
    assert claims["token_use"] == "access"
    # access token has no email claim by default; auth path must still surface
    # an empty string so downstream get_or_create_user can call safely.
    assert claims["email"] == ""


def test_cli_token_still_accepted_under_cognito_provider(cognito_app, monkeypatch):
    """CLI HMAC token is provider-agnostic — works on cognito too."""
    app_module, _private = cognito_app
    monkeypatch.setenv("BEACON_CLI_TOKEN_SECRET", "test-secret")
    token, _exp = app_module._make_cli_token("cli-user", "cli@example.com")
    claims = app_module._verify_id_token(token)
    assert claims["sub"] == "cli-user"
    assert claims["email"] == "cli@example.com"


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------

def test_cognito_rejects_wrong_audience(cognito_app):
    app_module, private = cognito_app
    bad = _mint(private, aud="some-other-client")
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token(bad)
    # FastAPI HTTPException carries the 401 we want
    assert getattr(ei.value, "status_code", None) == 401


def test_cognito_rejects_wrong_issuer(cognito_app):
    app_module, private = cognito_app
    bad = _mint(private, iss="https://malicious.example.com")
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token(bad)
    assert getattr(ei.value, "status_code", None) == 401


def test_cognito_rejects_expired_token(cognito_app):
    app_module, private = cognito_app
    bad = _mint(private, exp_offset=-60)  # expired 60s ago
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token(bad)
    assert getattr(ei.value, "status_code", None) == 401


def test_cognito_rejects_access_token_with_mismatched_client_id(cognito_app):
    app_module, private = cognito_app
    bad = _mint(private, token_use="access", client_id_claim="not-ours")
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token(bad)
    assert getattr(ei.value, "status_code", None) == 401


def test_cognito_rejects_unknown_token_use(cognito_app):
    app_module, private = cognito_app
    bad = _mint(private, token_use="refresh")
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token(bad)
    assert getattr(ei.value, "status_code", None) == 401


def test_cognito_rejects_garbage_token(cognito_app):
    app_module, _private = cognito_app
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token("not.a.jwt")
    assert getattr(ei.value, "status_code", None) == 401


# ---------------------------------------------------------------------------
# Misconfiguration paths
# ---------------------------------------------------------------------------

def test_cognito_provider_without_user_pool_id_returns_500(monkeypatch, keypair):
    """If operator sets BEACON_AUTH_PROVIDER=cognito but forgets the user pool
    env var, the request should fail with 500 rather than silently fall
    through to a permissive path."""
    pytest.importorskip("jwt")
    monkeypatch.setenv("BEACON_AUTH_PROVIDER", "cognito")
    monkeypatch.delenv("BEACON_COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("BEACON_COGNITO_CLIENT_ID", raising=False)

    if "app" in sys.modules:
        del sys.modules["app"]
    app_module = importlib.import_module("app")
    app_module._cognito_jwks_client = None

    # We never actually verify a token in this path because the missing env
    # short-circuits at the JWKS bootstrap.
    with pytest.raises(Exception) as ei:
        app_module._verify_id_token("anything.but.not.cli.prefix")
    assert getattr(ei.value, "status_code", None) == 500

    if "app" in sys.modules:
        del sys.modules["app"]


# ---------------------------------------------------------------------------
# Default provider regression — must not break Firebase path
# ---------------------------------------------------------------------------

def test_default_provider_is_firebase_and_uses_google_path(monkeypatch):
    """When BEACON_AUTH_PROVIDER is unset the dispatcher must keep going
    through Google ID token verification, never through Cognito."""
    monkeypatch.delenv("BEACON_AUTH_PROVIDER", raising=False)
    if "app" in sys.modules:
        del sys.modules["app"]
    app_module = importlib.import_module("app")
    assert app_module._AUTH_PROVIDER == "firebase"
    if "app" in sys.modules:
        del sys.modules["app"]
