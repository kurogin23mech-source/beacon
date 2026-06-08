"""Production-startup guard for the envelope signing secret (ms-54 / e-1291).

Background: ``server/envelope.py`` falls back to a hard-coded placeholder
secret when ``BEACON_ENVELOPE_SECRET`` is unset. That placeholder is visible
in the public repo, so a production deploy that forgets the env var would
let anyone forge T1 envelopes (= authorize high-risk actions on the bus).

The fix (in ``server/app.py``) is a startup handler that:

  * raises RuntimeError when ``_auth_enabled=True`` AND
    ``envelope.is_using_dev_fallback()=True`` (production-ish posture +
    missing secret → fail fast, deploy health-check fails)
  * logs an INFO and continues when ``_auth_enabled=False`` (dev / test
    posture — placeholder is fine)
  * passes silently when ``BEACON_ENVELOPE_SECRET`` is set to a real value
    (regardless of auth posture)

These tests exercise the 3 matrix cases against the live startup coroutine.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import app as app_module  # noqa: E402
import envelope as envelope_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_startup_guard() -> None:
    """Invoke the envelope-secret startup guard synchronously.

    The guard is registered as an ``@app.on_event("startup")`` async handler.
    For unit-testing the guard in isolation we call the coroutine directly,
    rather than spinning up the whole TestClient (which would also fire the
    other startup handlers and have side effects on the global event loop).
    """
    asyncio.get_event_loop().run_until_complete(
        app_module._verify_envelope_secret_configured()
    )


@pytest.fixture
def _restore_auth_flag():
    """Snapshot and restore ``_auth_enabled`` across each test."""
    original = app_module._auth_enabled
    yield
    app_module._auth_enabled = original


# ---------------------------------------------------------------------------
# Refuse-to-start matrix
# ---------------------------------------------------------------------------

def test_startup_refuses_when_auth_enabled_and_secret_unset(
    monkeypatch, _restore_auth_flag
):
    """Production posture (auth on) + missing secret → boot must abort."""
    monkeypatch.delenv("BEACON_ENVELOPE_SECRET", raising=False)
    app_module._auth_enabled = True

    assert envelope_mod.is_using_dev_fallback() is True
    with pytest.raises(RuntimeError, match="BEACON_ENVELOPE_SECRET"):
        _run_startup_guard()


def test_startup_refuses_when_auth_enabled_and_secret_is_dev_placeholder(
    monkeypatch, _restore_auth_flag
):
    """Explicitly setting the env var to the public placeholder is still a
    misconfiguration — the guard must catch it (defense against copy-paste
    of the dev fallback into Cloud Run env)."""
    monkeypatch.setenv(
        "BEACON_ENVELOPE_SECRET", envelope_mod._DEV_FALLBACK_SECRET
    )
    app_module._auth_enabled = True

    assert envelope_mod.is_using_dev_fallback() is True
    with pytest.raises(RuntimeError, match="BEACON_ENVELOPE_SECRET"):
        _run_startup_guard()


def test_startup_allows_dev_mode_with_fallback_secret(
    monkeypatch, _restore_auth_flag
):
    """``BEACON_API_AUTH=0`` posture (local dev / tests) → fallback OK."""
    monkeypatch.delenv("BEACON_ENVELOPE_SECRET", raising=False)
    app_module._auth_enabled = False

    assert envelope_mod.is_using_dev_fallback() is True
    # Must not raise.
    _run_startup_guard()


def test_startup_allows_production_with_real_secret(
    monkeypatch, _restore_auth_flag
):
    """Production posture + real secret configured → boot succeeds."""
    monkeypatch.setenv(
        "BEACON_ENVELOPE_SECRET", "a-real-random-32-byte-secret-xxxxxxxxxxxx"
    )
    app_module._auth_enabled = True

    assert envelope_mod.is_using_dev_fallback() is False
    # Must not raise.
    _run_startup_guard()


def test_startup_allows_dev_mode_with_real_secret(
    monkeypatch, _restore_auth_flag
):
    """``BEACON_API_AUTH=0`` + real secret set → also fine (no-op path)."""
    monkeypatch.setenv("BEACON_ENVELOPE_SECRET", "some-dev-secret-also-fine")
    app_module._auth_enabled = False

    assert envelope_mod.is_using_dev_fallback() is False
    _run_startup_guard()


# ---------------------------------------------------------------------------
# is_using_dev_fallback() helper unit tests
# ---------------------------------------------------------------------------

def test_is_using_dev_fallback_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("BEACON_ENVELOPE_SECRET", raising=False)
    assert envelope_mod.is_using_dev_fallback() is True


def test_is_using_dev_fallback_when_env_var_empty(monkeypatch):
    """Empty string is treated as unset (same code path returns the fallback)."""
    monkeypatch.setenv("BEACON_ENVELOPE_SECRET", "")
    assert envelope_mod.is_using_dev_fallback() is True


def test_is_using_dev_fallback_when_env_var_matches_placeholder(monkeypatch):
    """Catching the copy-paste-the-placeholder failure mode."""
    monkeypatch.setenv(
        "BEACON_ENVELOPE_SECRET", envelope_mod._DEV_FALLBACK_SECRET
    )
    assert envelope_mod.is_using_dev_fallback() is True


def test_is_using_dev_fallback_false_for_real_secret(monkeypatch):
    monkeypatch.setenv("BEACON_ENVELOPE_SECRET", "real-secret-xyz")
    assert envelope_mod.is_using_dev_fallback() is False
