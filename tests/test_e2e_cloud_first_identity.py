"""End-to-end scenario tests for cloud-first identity (ms-62 / e-1506).

These tests exercise the full server-first code path in-process: server
endpoint logic (mocked via TestClient), CLI machine cache I/O, lib/session.py
heartbeat orchestration, and the dm_discover memo.

They simulate the SPEC acceptance scenarios in a single Python process:
multi-bclaude on the same cwd, same-terminal restart continuity, cross-
machine discovery, and graceful failure under server outage. They do NOT
replace real cross-machine E2E (= requires 2 physical machines + a live
beacon-api-prod), but they pin the in-process contract so a regression in
any layer of the stack surfaces immediately.
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "server"))

import session


class _ServerSim:
    """A tiny in-memory stand-in for /api/me/{machine,heartbeat}.

    Models the identity tuple lookup semantics required by the SPEC:
      (project_id, machine_id, parent_pid) → sid (= deterministic mapping)
      (user_id, fingerprint) → machine_id (= deterministic mapping)
    """

    def __init__(self):
        self.machine_seq = 0
        self.session_seq = 0
        self.machines: dict[tuple[str, str], str] = {}
        self.sessions: dict[tuple[str, str, int], str] = {}
        # Track heartbeat count per sid so we can assert continuity.
        self.heartbeats: dict[str, int] = {}

    def me_upsert_machine(self, fingerprint, *, hostname="", agent=""):
        # NB: we collapse user_id to a single tenant in tests; the real
        # server keys on user_id but the in-process flow only ever sees
        # one user.
        key = ("user-1", fingerprint)
        if key in self.machines:
            return {
                "machine_id": self.machines[key],
                "minted": False,
                "fingerprint": fingerprint,
            }
        self.machine_seq += 1
        mid = f"mc-{self.machine_seq:08d}"
        self.machines[key] = mid
        return {
            "machine_id": mid,
            "minted": True,
            "fingerprint": fingerprint,
        }

    def me_heartbeat(self, project_id, machine_id, parent_pid, **kw):
        key = (project_id, machine_id, parent_pid)
        if key in self.sessions:
            sid = self.sessions[key]
            self.heartbeats[sid] = self.heartbeats.get(sid, 0) + 1
            return {
                "session_id": sid,
                "minted": False,
                "last_heartbeat_at": "2026-06-12T00:00:00Z",
                "created_at": "2026-06-12T00:00:00Z",
            }
        self.session_seq += 1
        sid = f"sv-{machine_id[3:]}-{parent_pid}-{self.session_seq:04d}"
        self.sessions[key] = sid
        self.heartbeats[sid] = 1
        return {
            "session_id": sid,
            "minted": True,
            "last_heartbeat_at": "2026-06-12T00:00:00Z",
            "created_at": "2026-06-12T00:00:00Z",
        }


@pytest.fixture
def sim(tmp_path, monkeypatch):
    """Set up a project, home dir, and a server simulator. Patch commands."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".beacon").mkdir()
    (proj / ".beacon" / "cloud.json").write_text(
        json.dumps({"project_id": "proj-e2e", "api_url": "https://test"})
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(session.ENV_USE_CLOUD_FIRST, "1")
    monkeypatch.delenv(session.ENV_FORCE_MINT, raising=False)
    monkeypatch.delenv(session.ENV_SESSION_ID, raising=False)

    server = _ServerSim()
    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = lambda: (server, {})
    monkeypatch.setitem(sys.modules, "commands", fake_mod)

    # Make sure the test sees a clean machine.json each run.
    machine_path = home / ".beacon" / "machine.json"
    if machine_path.exists():
        machine_path.unlink()
    return server


def test_e2e_distinct_terminals_get_distinct_sids(sim, monkeypatch):
    """Multi-bclaude in same cwd: T1 + T2 terminals → different sids."""
    # Terminal 1
    monkeypatch.setenv(session.ENV_PARENT_PID, "111")
    r1 = session.get_or_mint_session()
    assert r1["source"] == "server_minted"
    sid_t1 = r1["session_id"]

    # Terminal 2 (different terminal pid → different sid).
    monkeypatch.setenv(session.ENV_PARENT_PID, "222")
    r2 = session.get_or_mint_session()
    assert r2["source"] == "server_minted"
    sid_t2 = r2["session_id"]

    assert sid_t1 != sid_t2
    # Both bound to the SAME machine_id (= same fingerprint + cache reuse).
    assert r1["machine_id"] == r2["machine_id"]


def test_e2e_same_terminal_restart_keeps_sid(sim, monkeypatch):
    """Same parent_pid → identical sid even across restarts (= continuity)."""
    # ms-98 e-2769 added a 300 s session mint cache that short-circuits the
    # second call to get_or_mint_session() without touching the server. That
    # preserves the sid identity contract (the cached sid IS the same one),
    # but it means the server only sees one heartbeat in this in-process
    # test. Disable the cache so this pin still tests the underlying
    # "same-tuple → same-sid" server contract rather than the cache layer.
    monkeypatch.setenv("BEACON_SESSION_CLOUD_MINT_TTL_SECONDS", "0")
    monkeypatch.setenv(session.ENV_PARENT_PID, "333")
    r1 = session.get_or_mint_session()
    sid_first = r1["session_id"]

    # Simulate restart: read_session() picks up the cached sid; in the
    # server-first path the server returns the same sid for the same
    # tuple, so the "restart" path lands at the same id.
    r2 = session.get_or_mint_session()
    sid_second = r2["session_id"]
    assert sid_first == sid_second
    # Server saw two heartbeats on the same sid.
    assert sim.heartbeats[sid_first] >= 2


def test_e2e_cross_machine_distinct_sids(sim, monkeypatch):
    """Different fingerprint → different machine_id → different sid."""
    # Machine A
    import agent as _agent_mod
    monkeypatch.setattr(_agent_mod, "get_actor", lambda: {"machine": "mac-host"})
    monkeypatch.setenv(session.ENV_PARENT_PID, "444")
    r_mac = session.get_or_mint_session()
    sid_mac = r_mac["session_id"]
    mid_mac = r_mac["machine_id"]

    # Simulate machine B (= different hostname → new fingerprint → new
    # machine_id → new sid even though parent_pid is the same).
    # Clear local cache + change fingerprint.
    (Path.home() / ".beacon" / "machine.json").unlink()
    monkeypatch.setattr(_agent_mod, "get_actor", lambda: {"machine": "win-host"})
    r_win = session.get_or_mint_session()
    sid_win = r_win["session_id"]
    mid_win = r_win["machine_id"]

    assert mid_mac != mid_win
    assert sid_mac != sid_win


def test_e2e_server_outage_falls_back_to_legacy(sim, monkeypatch):
    """Cloud-first env set + server outage → legacy mint still produces a sid."""
    # Replace fake _get_api_client with a broken one (after sim setup).
    def _broken():
        raise ConnectionError("network down")

    fake_mod = types.ModuleType("commands")
    fake_mod._get_api_client = _broken
    monkeypatch.setitem(sys.modules, "commands", fake_mod)
    monkeypatch.setenv(session.ENV_PARENT_PID, "555")

    result = session.get_or_mint_session()
    # Legacy chain ran (mint_fresh or freshness reuse).
    assert result["session_id"]
    # Source NOT server_minted because that path raised.
    assert result["source"] != "server_minted"


def test_e2e_machine_cache_persists_across_calls(sim, monkeypatch):
    """First server call mints machine_id; subsequent calls reuse cache."""
    import agent as _agent_mod
    monkeypatch.setattr(_agent_mod, "get_actor", lambda: {"machine": "host-x"})
    monkeypatch.setenv(session.ENV_PARENT_PID, "666")

    session.get_or_mint_session()
    # Machine cache file exists.
    cache_path = Path.home() / ".beacon" / "machine.json"
    assert cache_path.exists()
    cached = json.loads(cache_path.read_text())
    assert cached["machine_id"].startswith("mc-")
    machine_id_first = cached["machine_id"]

    # Second call should hit cache, not call me_upsert_machine.
    initial_machine_count = len(sim.machines)
    session.get_or_mint_session()
    assert len(sim.machines) == initial_machine_count
    # Cache still points at same machine_id.
    cached2 = json.loads(cache_path.read_text())
    assert cached2["machine_id"] == machine_id_first
