"""Production-startup guard for the scheduler tick key (ms-83 / e-4115).

Twin of test_envelope_secret_startup.py for the *other* internal secret.
``server/envelope.py`` falls back to a hard-coded placeholder scheduler key
when ``BEACON_SCHEDULER_INTERNAL_KEY`` is unset. That placeholder is visible
in the public repo, so a production deploy that forgets the env var would let
anyone drive ``POST /api/system/trek-scheduler/tick`` (fire Trek / Operation
autonomy) and the T1-system envelope mint path.

The fix (``server/app.py:_verify_scheduler_key_configured``) is a startup
handler that:

  * raises RuntimeError when ``_auth_enabled=True`` AND
    ``envelope.is_using_dev_scheduler_key()=True`` (production posture +
    missing key → fail fast, deploy health-check fails),
  * logs INFO and continues when ``_auth_enabled=False`` (dev / test posture),
  * passes silently when a real key is configured (any posture).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import app as app_module  # noqa: E402
import envelope as envelope_mod  # noqa: E402


def _run_startup_guard() -> None:
    """Invoke the scheduler-key startup guard coroutine synchronously."""
    asyncio.get_event_loop().run_until_complete(
        app_module._verify_scheduler_key_configured()
    )


@pytest.fixture
def _restore_auth_flag():
    original = app_module._auth_enabled
    yield
    app_module._auth_enabled = original


# --- Refuse-to-start matrix -------------------------------------------------

def test_startup_refuses_when_auth_enabled_and_key_unset(
    monkeypatch, _restore_auth_flag
):
    monkeypatch.delenv("BEACON_SCHEDULER_INTERNAL_KEY", raising=False)
    app_module._auth_enabled = True
    assert envelope_mod.is_using_dev_scheduler_key() is True
    with pytest.raises(RuntimeError, match="BEACON_SCHEDULER_INTERNAL_KEY"):
        _run_startup_guard()


def test_startup_refuses_when_auth_enabled_and_key_is_dev_placeholder(
    monkeypatch, _restore_auth_flag
):
    monkeypatch.setenv(
        "BEACON_SCHEDULER_INTERNAL_KEY",
        envelope_mod._DEV_FALLBACK_SCHEDULER_KEY,
    )
    app_module._auth_enabled = True
    assert envelope_mod.is_using_dev_scheduler_key() is True
    with pytest.raises(RuntimeError, match="BEACON_SCHEDULER_INTERNAL_KEY"):
        _run_startup_guard()


def test_startup_allows_dev_mode_with_fallback_key(
    monkeypatch, _restore_auth_flag
):
    monkeypatch.delenv("BEACON_SCHEDULER_INTERNAL_KEY", raising=False)
    app_module._auth_enabled = False
    assert envelope_mod.is_using_dev_scheduler_key() is True
    _run_startup_guard()  # must not raise


def test_startup_allows_production_with_real_key(
    monkeypatch, _restore_auth_flag
):
    monkeypatch.setenv(
        "BEACON_SCHEDULER_INTERNAL_KEY", "a-real-random-32-byte-key-xxxxxxxxxx"
    )
    app_module._auth_enabled = True
    assert envelope_mod.is_using_dev_scheduler_key() is False
    _run_startup_guard()  # must not raise


# --- is_using_dev_scheduler_key() helper unit tests ------------------------

def test_helper_true_when_unset(monkeypatch):
    monkeypatch.delenv("BEACON_SCHEDULER_INTERNAL_KEY", raising=False)
    assert envelope_mod.is_using_dev_scheduler_key() is True


def test_helper_true_when_empty(monkeypatch):
    monkeypatch.setenv("BEACON_SCHEDULER_INTERNAL_KEY", "")
    assert envelope_mod.is_using_dev_scheduler_key() is True


def test_helper_true_when_matches_placeholder(monkeypatch):
    monkeypatch.setenv(
        "BEACON_SCHEDULER_INTERNAL_KEY",
        envelope_mod._DEV_FALLBACK_SCHEDULER_KEY,
    )
    assert envelope_mod.is_using_dev_scheduler_key() is True


def test_helper_false_for_real_key(monkeypatch):
    monkeypatch.setenv("BEACON_SCHEDULER_INTERNAL_KEY", "real-key-xyz")
    assert envelope_mod.is_using_dev_scheduler_key() is False


def test_helper_true_for_unfilled_template_placeholder(monkeypatch):
    """H2 (AX review): the app.env.example placeholder must trip the guard so
    copying the template verbatim fails loud at boot (as the template promises),
    instead of booting prod with a public-repo string as the key."""
    monkeypatch.setenv(
        "BEACON_SCHEDULER_INTERNAL_KEY",
        "<generate-with-openssl-rand-hex-32-do-not-commit>",
    )
    assert envelope_mod.is_using_dev_scheduler_key() is True


def test_helper_matches_shipped_app_env_example_placeholder(monkeypatch):
    """Pin against the ACTUAL placeholder shipped in deploy/app.env.example, so
    if the template value changes the guard is re-checked to still reject it."""
    import re
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "deploy" / "app.env.example"
    text = example.read_text(encoding="utf-8")
    m = re.search(r"^BEACON_SCHEDULER_INTERNAL_KEY=(.+)$", text, re.MULTILINE)
    assert m, "app.env.example must declare BEACON_SCHEDULER_INTERNAL_KEY"
    monkeypatch.setenv("BEACON_SCHEDULER_INTERNAL_KEY", m.group(1).strip())
    assert envelope_mod.is_using_dev_scheduler_key() is True


def test_startup_refuses_when_auth_enabled_and_key_is_template_placeholder(
    monkeypatch, _restore_auth_flag
):
    monkeypatch.setenv(
        "BEACON_SCHEDULER_INTERNAL_KEY",
        "<generate-with-openssl-rand-hex-32-do-not-commit>",
    )
    app_module._auth_enabled = True
    with pytest.raises(RuntimeError, match="BEACON_SCHEDULER_INTERNAL_KEY"):
        _run_startup_guard()


def test_envelope_and_scheduler_secrets_are_independent(monkeypatch):
    """Leaking/rotating one internal secret must not affect the other."""
    monkeypatch.setenv("BEACON_ENVELOPE_SECRET", "real-envelope-secret")
    monkeypatch.delenv("BEACON_SCHEDULER_INTERNAL_KEY", raising=False)
    assert envelope_mod.is_using_dev_fallback() is False
    assert envelope_mod.is_using_dev_scheduler_key() is True
