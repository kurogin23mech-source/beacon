"""Production-startup guard for sender-consent enforcement (ms-158 / e-6208).

Twin of test_scheduler_key_startup.py for the cross-user DM 误送信 boundary.
The sender-consent backstop (``BEACON_SENDER_CONSENT_ENABLED``) is read only at
the send choke point; unlike the scheduler key it had no startup check, so a
production deploy that forgot the env var ran fail-open on consent *silently*
(this actually happened in prod until 2026-09-06 — the deploy templates said
``=1`` but ``/etc/beacon/app.env`` had drifted to not having it).

The fix (``server/app.py:_verify_sender_consent_configured``) is a startup
handler that:

  * raises RuntimeError when ``_auth_enabled=True`` AND consent is not enabled
    AND no explicit opt-out (production posture + silent-off → fail fast, deploy
    health-check fails),
  * boots with a loud WARNING when the opt-out
    ``BEACON_SENDER_CONSENT_ALLOW_DISABLED=1`` is set (acknowledged phased
    rollout),
  * boots silently when consent is enabled (any posture),
  * boots silently when ``_auth_enabled=False`` (dev / test posture).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import app as app_module  # noqa: E402


def _run_startup_guard() -> None:
    """Invoke the sender-consent startup guard coroutine synchronously."""
    asyncio.get_event_loop().run_until_complete(
        app_module._verify_sender_consent_configured()
    )


@pytest.fixture
def _restore_auth_flag():
    original = app_module._auth_enabled
    yield
    app_module._auth_enabled = original


@pytest.fixture(autouse=True)
def _clean_consent_env(monkeypatch):
    """Each test starts from a known-clean consent env."""
    monkeypatch.delenv("BEACON_SENDER_CONSENT_ENABLED", raising=False)
    monkeypatch.delenv("BEACON_SENDER_CONSENT_ALLOW_DISABLED", raising=False)
    yield


# --- Refuse-to-start matrix -------------------------------------------------

def test_startup_refuses_when_auth_enabled_and_consent_disabled(
    monkeypatch, _restore_auth_flag
):
    # production posture, consent flag absent, no explicit opt-out → fail fast
    app_module._auth_enabled = True
    with pytest.raises(RuntimeError, match="BEACON_SENDER_CONSENT_ENABLED"):
        _run_startup_guard()


def test_startup_refuses_when_consent_flag_not_exactly_one(
    monkeypatch, _restore_auth_flag
):
    # a truthy-looking but non-"1" value must NOT count as enabled
    monkeypatch.setenv("BEACON_SENDER_CONSENT_ENABLED", "true")
    app_module._auth_enabled = True
    with pytest.raises(RuntimeError, match="BEACON_SENDER_CONSENT_ENABLED"):
        _run_startup_guard()


def test_startup_allows_production_with_consent_enabled(
    monkeypatch, _restore_auth_flag
):
    monkeypatch.setenv("BEACON_SENDER_CONSENT_ENABLED", "1")
    app_module._auth_enabled = True
    _run_startup_guard()  # must not raise


def test_startup_allows_production_with_explicit_optout(
    monkeypatch, _restore_auth_flag, caplog
):
    # acknowledged phased-rollout OFF: boots, but must warn loudly
    monkeypatch.setenv("BEACON_SENDER_CONSENT_ALLOW_DISABLED", "1")
    app_module._auth_enabled = True
    import logging

    with caplog.at_level(logging.WARNING):
        _run_startup_guard()  # must not raise
    assert any(
        "sender-consent enforcement is DISABLED" in r.message
        for r in caplog.records
    ), "expected a loud WARNING when consent is disabled via explicit opt-out"


def test_startup_allows_dev_mode_with_consent_disabled(
    monkeypatch, _restore_auth_flag
):
    # local dev / unit tests (auth off): consent OFF is fine, no-op
    app_module._auth_enabled = False
    _run_startup_guard()  # must not raise


def test_optout_ignored_when_consent_actually_enabled(
    monkeypatch, _restore_auth_flag, caplog
):
    # if consent is ON, the opt-out is irrelevant and no warning is emitted
    monkeypatch.setenv("BEACON_SENDER_CONSENT_ENABLED", "1")
    monkeypatch.setenv("BEACON_SENDER_CONSENT_ALLOW_DISABLED", "1")
    app_module._auth_enabled = True
    import logging

    with caplog.at_level(logging.WARNING):
        _run_startup_guard()  # must not raise
    assert not any(
        "sender-consent enforcement is DISABLED" in r.message
        for r in caplog.records
    ), "no disabled-warning should fire when consent is enabled"
