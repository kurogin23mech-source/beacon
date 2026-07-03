"""Tests for the cloud-first mint recursion guard (ms-95 / e-2870).

Bug reported 2026-07-04 via cross-project DM: ``BEACON_USE_CLOUD_FIRST_SESSION=1``
made every ``beacon`` CLI invocation hang for 60 s and die on the wall-clock
watchdog. Root cause was a cycle introduced by ms-97 / e-2694:

  1. ``session.get_or_mint_session`` sees the env var and calls
  2. ``get_or_mint_session_via_server``, which calls
  3. ``api_client.me_heartbeat`` → ``api_client._request``
  4. ``_request`` stamps the ``X-Beacon-Session`` header by calling
     ``session.resolve_active_session_id`` (introduced by e-2694).
  5. Empty ``.beacon/session.json`` → resolver falls through to
     ``get_or_mint_session`` → back to (1) forever.

The fix is a ``ContextVar`` guard set for the duration of
``get_or_mint_session_via_server``. The resolver checks the flag and
returns an empty string when it fires, breaking the cycle without
touching ``api_client.py`` — the existing empty-string fallback path
there already handles a blank sid gracefully.

These tests pin:
- Guard flips True during the mint call and back to False on exit
  (both success and exception paths).
- ``resolve_active_session_id`` returns "" when the guard is True.
- The full ``get_or_mint_session_via_server`` completes without
  reentering itself when the reentrant resolver call fires from
  inside the fake HTTP client.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session  # noqa: E402


# ---------------------------------------------------------------------------
# ContextVar guard behaviour (pure unit — no HTTP mock needed)
# ---------------------------------------------------------------------------


def test_guard_default_is_false():
    # Fresh interpreter default. If a prior test forgot to reset, this
    # would surface here rather than as a mysterious skipped resolver.
    assert session._in_cloud_first_mint.get() is False


def test_resolve_active_session_id_returns_empty_when_guard_set():
    """The core recursion break: while the guard is True the resolver
    yields "" so ``api_client._request`` sends an empty
    ``X-Beacon-Session`` header instead of triggering another mint.
    """
    token = session._in_cloud_first_mint.set(True)
    try:
        assert session.resolve_active_session_id() == ""
    finally:
        session._in_cloud_first_mint.reset(token)
    # And back to normal after reset (= no lingering state).
    assert session._in_cloud_first_mint.get() is False


def test_resolve_active_session_id_still_delegates_when_guard_off(monkeypatch):
    """Sanity: without the guard the resolver still walks the usual
    bridge-claim / get_session_id chain. Uses a monkey-patched bridge
    reader so the test does not depend on a real .beacon/ layout.
    """
    monkeypatch.setattr(
        session, "read_bridge_session", lambda: {"session_id": "sv-bridge-42"},
    )
    assert session.resolve_active_session_id() == "sv-bridge-42"


# ---------------------------------------------------------------------------
# End-to-end: no infinite recursion during the mint
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """Fake project with cloud.json + HOME pointing at tmp (mirrors the
    existing ``tests/test_session_cloud_first.py`` fixture)."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / ".beacon").mkdir()
    cloud = {"project_id": "proj-cloud-1", "api_url": "https://test.example"}
    (proj / ".beacon" / "cloud.json").write_text(json.dumps(cloud))
    monkeypatch.chdir(proj)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return proj


class _ReentrantResolverClient:
    """Fake API client whose ``me_heartbeat`` calls the resolver
    reentrantly — the exact pattern ``api_client._request`` uses when it
    stamps the ``X-Beacon-Session`` header. If the guard is missing the
    resolver would loop back into ``get_or_mint_session_via_server`` and
    the test would hang / RecursionError.
    """

    def __init__(self, *, sid="sv-test-1", machine_id="mc-test"):
        self.sid = sid
        self.machine_id = machine_id
        self.heartbeat_calls = 0
        self.resolver_return_during_call: str | None = None

    def me_upsert_machine(self, fingerprint, *, hostname="", agent=""):
        return {"machine_id": self.machine_id, "minted": True}

    def me_heartbeat(self, project_id, machine_id, parent_pid, *,
                     cwd="", agent=None):
        self.heartbeat_calls += 1
        # This is the reentrant call ``api_client._request`` makes.
        # With the guard the resolver returns "" and we do not recurse.
        self.resolver_return_during_call = session.resolve_active_session_id()
        return {"session_id": self.sid, "minted": True, "created_at": ""}


def _patch_commands(monkeypatch, fake_client):
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = lambda: (fake_client, {})
    monkeypatch.setitem(sys.modules, "commands", fake_mod)


def _patch_parent_pid(monkeypatch, pid=12345):
    monkeypatch.setattr(session, "_resolve_parent_pid", lambda: pid)


def test_mint_via_server_completes_without_recursion(
    project_root, monkeypatch,
):
    """The whole ``get_or_mint_session_via_server`` call completes,
    ``me_heartbeat`` is invoked exactly once, and the reentrant resolver
    call it makes returns "" — i.e. the guard broke the cycle.
    """
    client = _ReentrantResolverClient()
    _patch_commands(monkeypatch, client)
    _patch_parent_pid(monkeypatch)

    result = session.get_or_mint_session_via_server()

    assert result["session_id"] == "sv-test-1"
    assert client.heartbeat_calls == 1
    assert client.resolver_return_during_call == ""
    # Guard cleaned up after the call.
    assert session._in_cloud_first_mint.get() is False


def test_guard_resets_on_exception_path(project_root, monkeypatch):
    """A failure inside the mint (network error, malformed response, etc.)
    must NOT leave the guard stuck at True — otherwise the whole process
    would silently produce blank ``X-Beacon-Session`` headers forever.
    """

    class _RaisingClient:
        def me_upsert_machine(self, *_a, **_kw):
            raise RuntimeError("simulated network error")

        def me_heartbeat(self, *_a, **_kw):  # pragma: no cover — unreached
            raise AssertionError("should not be called")

    _patch_commands(monkeypatch, _RaisingClient())
    _patch_parent_pid(monkeypatch)

    with pytest.raises(RuntimeError, match="simulated network error"):
        session.get_or_mint_session_via_server()

    assert session._in_cloud_first_mint.get() is False


def test_get_or_mint_session_falls_back_to_legacy_on_server_error(
    project_root, monkeypatch,
):
    """The outer ``get_or_mint_session`` catches BaseException from the
    server path and falls through to the legacy chain (existing e-1510
    contract). The guard reset from Test 2 makes the legacy resolver
    behave normally — this test pins that the fallback still works
    end-to-end.
    """

    class _RaisingClient:
        def me_upsert_machine(self, *_a, **_kw):
            raise RuntimeError("boom")

        def me_heartbeat(self, *_a, **_kw):  # pragma: no cover
            raise AssertionError("should not reach heartbeat")

    _patch_commands(monkeypatch, _RaisingClient())
    _patch_parent_pid(monkeypatch)
    monkeypatch.setenv(session.ENV_USE_CLOUD_FIRST, "1")
    # Force the "no reusable state" leg of the legacy chain so we get a
    # freshly minted id rather than an adopted env id.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("BEACON_SESSION_ID", raising=False)

    payload = session.get_or_mint_session()
    # Any well-formed session id is fine — the point is the CLI did not
    # hang and produced a payload. The legacy path returns a "minted"
    # marker on fresh mint.
    assert payload["session_id"]
    assert session._in_cloud_first_mint.get() is False
