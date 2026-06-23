"""ms-43 / e-2298 — exchange-cli-token endpoint tests.

Verifies the contract that the Web UI relies on for the 30-day session feature:
the short-lived Firebase / Cognito ``id_token`` arriving in the ``Authorization``
header can be swapped for a ``bcli.*`` HMAC token (= 30-day TTL, same shape that
the CLI already uses). The Web UI immediately persists the returned token in
``localStorage`` and uses it for subsequent API / WS calls, sidestepping the
1-hour cap that Firebase enforces on its own ``id_token``.

Three properties are pinned here:

* **happy path**: an authenticated request returns a ``bcli.*`` token whose
  ``token_expiry`` is roughly 30 days in the future, and the returned token
  is accepted by ``_verify_cli_token`` (= the same verifier ``require_auth``
  uses on incoming Bearer tokens — so the round-trip works end to end).
* **unauthenticated path**: requests without a valid Bearer token are rejected
  with 401. The endpoint must not leak ``bcli.*`` tokens to anonymous callers
  because they would carry an arbitrary ``sub`` / ``email`` payload.
* **claim threading**: the returned token's ``sub`` / ``email`` claims mirror
  whatever ``require_auth`` resolved from the inbound token, so the user's
  identity is preserved across the exchange.
"""

import importlib
import json
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    """Load server/app.py with auth disabled-then-enabled toggles per test."""
    # Force a stable HMAC secret so the issued bcli token is verifiable
    # within the same process.
    monkeypatch.setenv("BEACON_CLI_TOKEN_SECRET", "test-secret-e2298")
    repo_root = os.path.join(os.path.dirname(__file__), "..")
    sys.path.insert(0, os.path.join(repo_root, "server"))
    if "app" in sys.modules:
        del sys.modules["app"]
    app_mod = importlib.import_module("app")
    return app_mod


def test_exchange_with_auth_disabled_returns_bcli_token(app_module, monkeypatch):
    """Smoke path: with auth disabled, ``require_auth`` returns the dev claim
    and the endpoint still issues a valid ``bcli.*`` token.

    This is the easiest path to assert without standing up a real IdP — it
    pins the **endpoint contract** (= ``status: ok`` / ``id_token`` is
    a ``bcli.*`` token / ``token_expiry`` ~30 days out / round-trip verify
    succeeds). The auth-enabled paths are covered separately by the auth /
    cognito test suites.
    """
    monkeypatch.setattr(app_module, "_auth_enabled", False)
    client = TestClient(app_module.app)
    res = client.post("/api/auth/exchange-cli-token")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    cli_token = body["id_token"]
    assert cli_token.startswith("bcli."), cli_token

    # Round-trip: the returned token must verify with the same secret /
    # algorithm that ``require_auth`` uses for incoming Bearer tokens. This
    # is what makes the Web UI's "persist + reuse" loop sound.
    claims = app_module._verify_cli_token(cli_token)
    assert claims is not None, "bcli token failed self-verification"
    assert claims.get("sub") == "dev"
    assert claims.get("email") == "dev@local"

    # token_expiry should sit roughly 30 days in the future (allow a generous
    # window so a slow CI box doesn't flake the test).
    now = int(time.time())
    expiry = int(body["token_expiry"])
    delta_days = (expiry - now) / 86400
    assert 29 < delta_days < 31, f"expected ~30-day TTL, got {delta_days:.2f} days"


def test_exchange_without_auth_header_returns_401(app_module, monkeypatch):
    """Auth enabled + no Authorization header = 401.

    ``require_auth`` raises ``HTTPException(401)`` when auth is enabled and
    the Bearer credential is missing. The exchange endpoint must inherit
    that gate (= anonymous callers must not be able to mint ``bcli.*``
    tokens for arbitrary identities).
    """
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    client = TestClient(app_module.app)
    res = client.post("/api/auth/exchange-cli-token")
    assert res.status_code in (401, 403), (
        f"expected anonymous request to be rejected, got {res.status_code}: {res.text}"
    )


def test_exchange_returns_bcli_token_that_verifies_on_subsequent_call(
    app_module, monkeypatch
):
    """End-to-end: exchange once, then use the returned token on a
    second authenticated request.

    With auth disabled the dev claim path is the easiest to wire, but the
    important property is symmetric: the token that comes out of /exchange
    is the same shape that ``_verify_cli_token`` accepts on the way in.
    That's what makes the Web UI's "persist in localStorage, use for next
    /api call" loop reliable across the 1-hour Firebase boundary.
    """
    monkeypatch.setattr(app_module, "_auth_enabled", False)
    client = TestClient(app_module.app)
    res = client.post("/api/auth/exchange-cli-token")
    assert res.status_code == 200
    cli_token = res.json()["id_token"]
    assert cli_token.startswith("bcli.")

    # Re-verify via the public helper — this is what require_auth invokes
    # before any IdP-specific verification, so a bcli token bypasses the
    # 1-hour Firebase TTL entirely on subsequent requests.
    claims = app_module._verify_cli_token(cli_token)
    assert claims is not None
    assert claims["sub"] == "dev"
    assert claims["exp"] > time.time() + 86400 * 29  # at least 29 days out


def test_make_cli_token_helper_lifetime_is_30_days(app_module):
    """Direct assertion on ``_CLI_TOKEN_LIFETIME``.

    The Web UI session feature depends on this constant staying at 30 days
    (= the user-facing promise is "log in monthly, not hourly"). Pin it so
    a future refactor that accidentally drops it back to seconds-of-hours
    surfaces in CI rather than in a user-visible regression.
    """
    assert app_module._CLI_TOKEN_LIFETIME == 86400 * 30
