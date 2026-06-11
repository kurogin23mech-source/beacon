"""Tests for the cloud-first session mint path (ms-62 / e-1510).

The new ``get_or_mint_session_via_server`` function is gated by the
``BEACON_USE_CLOUD_FIRST_SESSION=1`` env var. When the env is set, it
takes precedence over the legacy mint chain; on any failure the legacy
chain still runs so a missing endpoint / network glitch can't break the
CLI.
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import session


@pytest.fixture
def project_root(tmp_path, monkeypatch):
    """Set up a fake project with .beacon/cloud.json and HOME pointing tmp."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / ".beacon").mkdir()
    cloud = {"project_id": "proj-cloud-123", "api_url": "https://test.example"}
    (proj / ".beacon" / "cloud.json").write_text(json.dumps(cloud))
    monkeypatch.chdir(proj)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return proj


def _patch_commands(monkeypatch, fake_client):
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = lambda: (fake_client, {})
    monkeypatch.setitem(sys.modules, "commands", fake_mod)


class _FakeClient:
    def __init__(self, *, sid="sv-test-1", machine_id="mc-test"):
        self.sid = sid
        self.machine_id = machine_id
        self.heartbeat_calls = []
        self.machine_calls = []
        self.machine_minted = True

    def me_upsert_machine(self, fingerprint, *, hostname="", agent=""):
        self.machine_calls.append({
            "fingerprint": fingerprint,
            "hostname": hostname,
            "agent": agent,
        })
        return {
            "machine_id": self.machine_id,
            "minted": self.machine_minted,
            "fingerprint": fingerprint,
        }

    def me_heartbeat(self, project_id, machine_id, parent_pid, **kw):
        self.heartbeat_calls.append({
            "project_id": project_id,
            "machine_id": machine_id,
            "parent_pid": parent_pid,
            **kw,
        })
        return {
            "session_id": self.sid,
            "minted": True,
            "last_heartbeat_at": "2026-06-12T00:00:00Z",
            "created_at": "2026-06-12T00:00:00Z",
        }


def test_resolve_parent_pid_prefers_env(monkeypatch):
    monkeypatch.setenv(session.ENV_PARENT_PID, "9999")
    assert session._resolve_parent_pid() == 9999


def test_resolve_parent_pid_falls_back_to_getppid(monkeypatch):
    monkeypatch.delenv(session.ENV_PARENT_PID, raising=False)
    val = session._resolve_parent_pid()
    assert val > 0


def test_resolve_parent_pid_rejects_garbage_env(monkeypatch):
    monkeypatch.setenv(session.ENV_PARENT_PID, "not-a-number")
    val = session._resolve_parent_pid()
    assert val > 0  # falls back to getppid()


def test_machine_cache_roundtrip(project_root):
    session._write_machine_cache("mc-abc", "host-x")
    data = session._read_machine_cache()
    assert data["machine_id"] == "mc-abc"
    assert data["fingerprint"] == "host-x"
    assert data["cached_at"]


def test_get_or_mint_session_via_server_first_call(project_root, monkeypatch):
    client = _FakeClient(sid="sv-fresh", machine_id="mc-fresh")
    _patch_commands(monkeypatch, client)
    monkeypatch.setenv(session.ENV_PARENT_PID, "12345")

    result = session.get_or_mint_session_via_server()

    assert result["session_id"] == "sv-fresh"
    assert result["machine_id"] == "mc-fresh"
    assert result["parent_pid"] == 12345
    assert result["source"] == "server_minted"
    # session.json materialised so legacy readers see same sid
    persisted = session.read_session()
    assert persisted["session_id"] == "sv-fresh"
    # machine_id cached
    cache = session._read_machine_cache()
    assert cache["machine_id"] == "mc-fresh"


def test_get_or_mint_session_via_server_reuses_machine_cache(project_root, monkeypatch):
    """Second call with same fingerprint shouldn't call me_upsert_machine."""
    session._write_machine_cache("mc-cached", "host-x")

    class _FpClient(_FakeClient):
        pass

    # Stub fingerprint resolution: monkeypatch the actor function.
    import agent as _agent_mod
    monkeypatch.setattr(
        _agent_mod, "get_actor", lambda: {"machine": "host-x", "agent": "claude"},
    )
    client = _FpClient(sid="sv-via-cache", machine_id="mc-WRONG")
    _patch_commands(monkeypatch, client)
    monkeypatch.setenv(session.ENV_PARENT_PID, "999")

    session.get_or_mint_session_via_server()
    assert client.machine_calls == []
    assert client.heartbeat_calls[0]["machine_id"] == "mc-cached"


def test_get_or_mint_session_fallthrough_on_server_failure(project_root, monkeypatch):
    """BEACON_USE_CLOUD_FIRST_SESSION=1 + server failure → legacy chain runs."""
    monkeypatch.setenv(session.ENV_USE_CLOUD_FIRST, "1")
    monkeypatch.delenv(session.ENV_FORCE_MINT, raising=False)
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)

    def _broken_get_api_client():
        raise RuntimeError("API error 404: Not Found")

    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _broken_get_api_client
    monkeypatch.setitem(sys.modules, "commands", fake_mod)

    result = session.get_or_mint_session()
    # Legacy path produced a sid (= newly minted via _mint_session_id).
    assert result["session_id"]
    assert result["source"] in ("minted", "minted_fresh", "claude_code_env")


def test_get_or_mint_session_uses_server_when_enabled(project_root, monkeypatch):
    monkeypatch.setenv(session.ENV_USE_CLOUD_FIRST, "1")
    monkeypatch.setenv(session.ENV_PARENT_PID, "777")
    monkeypatch.delenv(session.ENV_FORCE_MINT, raising=False)
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)
    client = _FakeClient(sid="sv-server")
    _patch_commands(monkeypatch, client)

    result = session.get_or_mint_session()
    assert result["session_id"] == "sv-server"
    assert result["source"] == "server_minted"


def test_get_or_mint_session_skips_server_when_env_unset(project_root, monkeypatch):
    monkeypatch.delenv(session.ENV_USE_CLOUD_FIRST, raising=False)
    monkeypatch.delenv(session.ENV_FORCE_MINT, raising=False)
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)

    def _should_not_run():
        raise AssertionError("server path should not run when env unset")

    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _should_not_run
    monkeypatch.setitem(sys.modules, "commands", fake_mod)

    result = session.get_or_mint_session()
    assert result["session_id"]
    assert result["source"] in ("minted", "minted_fresh", "claude_code_env")
