"""Tests for ms-98 / e-2777 — client-side circuit breaker in api_client.

The breaker's job is to stop the CLI from piling requests onto a
degraded server. On 2026-07-02 the client kept retrying against Cloud
Run instances that were already returning 429; the retries showed up in
the storm's request-rate signal and made the incident worse. This
breaker turns "just landed a 429" into "will fail-fast for a minute"
after a small burst, so the client backs off automatically.

These tests pin:
  * threshold ⇒ open — the third 429 within the window opens the circuit
  * open ⇒ raise — subsequent calls raise before touching the network
  * cooldown ⇒ closed — after the cooldown elapses, calls resume
  * disable env kills the breaker entirely
  * a stale 429 outside the window doesn't count toward the threshold
"""

import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import api_client  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_circuit(monkeypatch):
    """Force a clean state before every test."""
    api_client._circuit_reset_for_tests()
    # Neutralise any environment override from the calling shell.
    monkeypatch.delenv(api_client._CIRCUIT_LOCK_ENV, raising=False)
    monkeypatch.delenv(api_client._CIRCUIT_WINDOW_ENV, raising=False)
    monkeypatch.delenv(api_client._CIRCUIT_THRESHOLD_ENV, raising=False)
    monkeypatch.delenv(api_client._CIRCUIT_COOLDOWN_ENV, raising=False)
    yield
    api_client._circuit_reset_for_tests()


def _fake_urlopen_that_always_429(*args, **kwargs):
    """Simulate a server that always returns 429."""
    raise urllib.error.HTTPError(
        url="http://test/", code=429, msg="Rate exceeded", hdrs=None,
        fp=_FakeReadable(b'{"detail": "too fast"}'),
    )


class _FakeReadable:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def close(self):
        pass


def _client() -> api_client.ApiClient:
    return api_client.ApiClient("http://beacon-api.test", token="")


def test_third_429_opens_the_circuit(monkeypatch):
    """Threshold=3 (default) ⇒ after three 429s the breaker is open."""
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_that_always_429)
    c = _client()

    for _ in range(3):
        with pytest.raises(RuntimeError, match="API error 429"):
            c.get("/api/heartbeat")

    # The next call must trip the breaker, not the 429 handler.
    with pytest.raises(RuntimeError, match="circuit open"):
        c.get("/api/heartbeat")


def test_open_circuit_raises_without_network_call(monkeypatch):
    """After the breaker opens, ``_request`` never hits ``urlopen``."""
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_that_always_429)
    c = _client()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="API error 429"):
            c.get("/api/heartbeat")

    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return _fake_urlopen_that_always_429(*a, **k)

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="circuit open"):
        c.get("/api/heartbeat")
    assert calls["n"] == 0, "circuit-open call must NOT reach urlopen"


def test_cooldown_elapses_and_call_resumes(monkeypatch):
    """After the cooldown, requests reach the network again."""
    # Very short cooldown so the test runs fast.
    monkeypatch.setenv(api_client._CIRCUIT_COOLDOWN_ENV, "0.15")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_that_always_429)
    c = _client()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="API error 429"):
            c.get("/api/heartbeat")
    # Confirm the circuit is open right now.
    with pytest.raises(RuntimeError, match="circuit open"):
        c.get("/api/heartbeat")

    # Wait past the cooldown; next call should hit the network again
    # (which still 429s in this test, but the point is that the request
    # is *attempted*).
    time.sleep(0.20)
    with pytest.raises(RuntimeError, match="API error 429"):
        c.get("/api/heartbeat")


def test_disable_env_makes_the_breaker_inert(monkeypatch):
    """``BEACON_API_CIRCUIT_BREAKER_DISABLE=1`` ⇒ never trips."""
    monkeypatch.setenv(api_client._CIRCUIT_LOCK_ENV, "1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_that_always_429)
    c = _client()

    # Even after many 429s, every call still hits the 429 path (not the
    # breaker path).
    for _ in range(10):
        with pytest.raises(RuntimeError, match="API error 429"):
            c.get("/api/heartbeat")


def test_stale_429s_outside_window_do_not_count(monkeypatch):
    """A 429 older than the window is pruned before the threshold check."""
    # Big enough window to catch the first hit but tiny enough that a
    # short sleep expires it.
    monkeypatch.setenv(api_client._CIRCUIT_WINDOW_ENV, "0.1")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_that_always_429)
    c = _client()

    # Fire one 429, let it age out.
    with pytest.raises(RuntimeError, match="API error 429"):
        c.get("/api/heartbeat")
    time.sleep(0.15)

    # Two fresh 429s follow. Because the first one has been pruned, the
    # total inside the window is 2, still below threshold=3, so no open.
    with pytest.raises(RuntimeError, match="API error 429"):
        c.get("/api/heartbeat")
    with pytest.raises(RuntimeError, match="API error 429"):
        c.get("/api/heartbeat")

    # The next call must therefore be a 429 (breaker still closed).
    with pytest.raises(RuntimeError, match="API error 429"):
        c.get("/api/heartbeat")
