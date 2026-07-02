"""Tests for ms-98 / e-2769 — cloud-first session mint cache.

Under ``BEACON_USE_CLOUD_FIRST_SESSION=1`` every ``beacon <cmd>`` used to
call POST ``/api/me/heartbeat`` because ``get_or_mint_session_via_server``
had no local cache. On 2026-07-02 this was one of the two main hot paths
feeding the 429 storm. The cache pins the previous server-minted sid in
``.beacon/session.json`` and reuses it whenever the identity tuple is
stable and the timestamp is fresh.

These tests pin the observable contract so a future refactor cannot
silently reopen the hot path.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import session  # noqa: E402


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    """Fresh cwd + writable .beacon/ dir + minimal cloud.json."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / ".beacon").mkdir()
    (project_dir / ".beacon" / "cloud.json").write_text(
        json.dumps({"project_id": "test-project-xyz"}), encoding="utf-8",
    )
    monkeypatch.chdir(project_dir)
    # Also isolate the machine-cache path from the user's real ~/.beacon.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    yield project_dir


def _iso(delta_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


def _write_session_file(project_dir: Path, **overrides):
    payload = {
        "session_id": "sv-cached-1234",
        "actor": {"machine": "test-mac", "agent": "test-agent"},
        "created_at": _iso(-3600),
        "last_active": _iso(-60),  # 1 minute ago — fresh under default 300s TTL
        "harness": "test",
        "source": "server_minted",
        "machine_id": "mc-abc-def",
        "parent_pid": 42424,
    }
    payload.update(overrides)
    (project_dir / ".beacon" / "session.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    return payload


def _stub_pid(monkeypatch, pid=42424):
    monkeypatch.setattr(session, "_resolve_parent_pid", lambda: pid)


def _stub_machine_cache(monkeypatch, machine_id="mc-abc-def", fingerprint="test-mac"):
    monkeypatch.setattr(
        session, "_read_machine_cache",
        lambda: {"machine_id": machine_id, "fingerprint": fingerprint},
    )


def _fail_if_heartbeat_called(monkeypatch):
    """Assert the me_heartbeat network call is never issued."""
    def _boom_client_factory():
        class _Boom:
            def me_upsert_machine(self, *a, **k):
                pytest.fail("me_upsert_machine should NOT be called on cache hit")

            def me_heartbeat(self, *a, **k):
                pytest.fail("me_heartbeat should NOT be called on cache hit")
        return _Boom(), {}

    # Patch _get_api_client via the lazy-imported module.
    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", _boom_client_factory)


def test_cache_hit_returns_cached_sid_without_heartbeat(
    isolated_project, monkeypatch,
):
    """Fresh matching cache ⇒ no network call, cached sid returned."""
    _write_session_file(isolated_project)
    _stub_pid(monkeypatch, 42424)
    _stub_machine_cache(monkeypatch)
    monkeypatch.setattr(
        session._agent, "get_actor",
        lambda: {"machine": "test-mac", "agent": "test-agent"},
    )
    _fail_if_heartbeat_called(monkeypatch)

    result = session.get_or_mint_session_via_server()

    assert result["session_id"] == "sv-cached-1234"
    assert result["minted"] is False
    assert result["source"] == "server_minted"


def test_cache_miss_when_pid_differs_calls_heartbeat(
    isolated_project, monkeypatch,
):
    """Parent pid drift ⇒ tuple changed ⇒ heartbeat runs and mints fresh."""
    _write_session_file(isolated_project, parent_pid=99999)  # cached under a different pid
    _stub_pid(monkeypatch, 42424)  # current call has a different pid
    _stub_machine_cache(monkeypatch)
    monkeypatch.setattr(
        session._agent, "get_actor",
        lambda: {"machine": "test-mac", "agent": "test-agent"},
    )

    calls = {"heartbeat": 0}

    def _client_factory():
        class _Client:
            def me_upsert_machine(self, *a, **k):
                return {"machine_id": "mc-abc-def"}

            def me_heartbeat(self, *a, **k):
                calls["heartbeat"] += 1
                return {
                    "session_id": "sv-newly-minted",
                    "minted": True,
                    "created_at": _iso(0),
                }
        return _Client(), {}

    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", _client_factory)

    result = session.get_or_mint_session_via_server()

    assert calls["heartbeat"] == 1
    assert result["session_id"] == "sv-newly-minted"
    assert result["minted"] is True


def test_cache_miss_when_stale_last_active_calls_heartbeat(
    isolated_project, monkeypatch,
):
    """Old last_active (past TTL window) ⇒ heartbeat runs."""
    _write_session_file(isolated_project, last_active=_iso(-999999))
    _stub_pid(monkeypatch, 42424)
    _stub_machine_cache(monkeypatch)
    monkeypatch.setattr(
        session._agent, "get_actor",
        lambda: {"machine": "test-mac", "agent": "test-agent"},
    )

    calls = {"heartbeat": 0}

    def _client_factory():
        class _Client:
            def me_upsert_machine(self, *a, **k):
                return {"machine_id": "mc-abc-def"}

            def me_heartbeat(self, *a, **k):
                calls["heartbeat"] += 1
                return {"session_id": "sv-refresh", "minted": False}
        return _Client(), {}

    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", _client_factory)

    result = session.get_or_mint_session_via_server()

    assert calls["heartbeat"] == 1
    assert result["session_id"] == "sv-refresh"


def test_ttl_zero_disables_cache(isolated_project, monkeypatch):
    """``BEACON_SESSION_CLOUD_MINT_TTL_SECONDS=0`` ⇒ heartbeat runs even when fresh."""
    monkeypatch.setenv("BEACON_SESSION_CLOUD_MINT_TTL_SECONDS", "0")
    _write_session_file(isolated_project)
    _stub_pid(monkeypatch, 42424)
    _stub_machine_cache(monkeypatch)
    monkeypatch.setattr(
        session._agent, "get_actor",
        lambda: {"machine": "test-mac", "agent": "test-agent"},
    )

    calls = {"heartbeat": 0}

    def _client_factory():
        class _Client:
            def me_upsert_machine(self, *a, **k):
                return {"machine_id": "mc-abc-def"}

            def me_heartbeat(self, *a, **k):
                calls["heartbeat"] += 1
                return {"session_id": "sv-forced-refresh", "minted": False}
        return _Client(), {}

    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", _client_factory)

    session.get_or_mint_session_via_server()

    assert calls["heartbeat"] == 1


def test_cache_refuses_non_server_minted_source(
    isolated_project, monkeypatch,
):
    """Refuse the cache when the on-disk sid was locally minted.

    Rationale: if the previous mint came from the legacy client-side path
    (source=='minted'), the server has no record of it under the tuple
    lookup. Returning it here would set the caller up to write to a
    ghost session record.
    """
    _write_session_file(isolated_project, source="minted")
    _stub_pid(monkeypatch, 42424)
    _stub_machine_cache(monkeypatch)
    monkeypatch.setattr(
        session._agent, "get_actor",
        lambda: {"machine": "test-mac", "agent": "test-agent"},
    )

    calls = {"heartbeat": 0}

    def _client_factory():
        class _Client:
            def me_upsert_machine(self, *a, **k):
                return {"machine_id": "mc-abc-def"}

            def me_heartbeat(self, *a, **k):
                calls["heartbeat"] += 1
                return {"session_id": "sv-server-authoritative", "minted": False}
        return _Client(), {}

    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", _client_factory)

    result = session.get_or_mint_session_via_server()

    assert calls["heartbeat"] == 1
    assert result["session_id"] == "sv-server-authoritative"


def test_twenty_sequential_calls_hit_heartbeat_once(
    isolated_project, monkeypatch,
):
    """The headline AC: 20 back-to-back calls make **one** heartbeat call.

    Pins the primary claim of e-2769 — that the cache converts N CLI
    invocations to 1 network round trip when the tuple is stable.
    """
    # Start with no session file; the first call mints, the next 19 hit cache.
    _stub_pid(monkeypatch, 42424)
    _stub_machine_cache(monkeypatch)
    monkeypatch.setattr(
        session._agent, "get_actor",
        lambda: {"machine": "test-mac", "agent": "test-agent"},
    )

    calls = {"heartbeat": 0}

    def _client_factory():
        class _Client:
            def me_upsert_machine(self, *a, **k):
                return {"machine_id": "mc-abc-def"}

            def me_heartbeat(self, *a, **k):
                calls["heartbeat"] += 1
                return {
                    "session_id": "sv-first-mint",
                    "minted": True,
                    "created_at": _iso(0),
                }
        return _Client(), {}

    import commands as _commands
    monkeypatch.setattr(_commands, "_get_api_client", _client_factory)

    sids = [
        session.get_or_mint_session_via_server()["session_id"] for _ in range(20)
    ]

    assert calls["heartbeat"] == 1
    assert set(sids) == {"sv-first-mint"}
