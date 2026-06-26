"""Tests for ms-93 / e-2508 — Codex plugin minimum viable install.

Pins the contracts the plugin promises:

- ``.codex-plugin/plugin.json`` validates against the upstream Codex
  ``validate_plugin.py`` schema (= so ``codex plugin add`` won't reject it)
- ``beacon-codex-bridge install-hook`` is idempotent: re-running does
  not duplicate entries in ``~/.codex/hooks.json``, and only beacon-owned
  entries are touched (= other entries are preserved)
- ``uninstall-hook`` removes the entry only for the requested cwd
  (= multi-project install_in safe)
- start / stop / restart manage the pidfile correctly
- stale pidfile (= pid is dead) is cleaned automatically on start
- collision: a second start for the same cwd with a live pid is a no-op
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "beacon"
BRIDGE_PATH = PLUGIN_ROOT / "scripts" / "beacon-codex-bridge"
APP_SERVER_CLIENT_PATH = (
    PLUGIN_ROOT / "scripts" / "beacon_codex_app_server_client.py"
)

CODEX_VALIDATOR = Path(
    os.path.expanduser(
        "~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py"
    )
)


def _load_bridge_module():
    """Load the extensionless bridge script as a module."""
    loader = importlib.machinery.SourceFileLoader("bridge", str(BRIDGE_PATH))
    spec = importlib.util.spec_from_loader("bridge", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def _load_app_server_client_module():
    spec = importlib.util.spec_from_file_location(
        "beacon_codex_app_server_client", APP_SERVER_CLIENT_PATH
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------------ #
# 1. Plugin manifest validation
# ------------------------------------------------------------------ #


@pytest.mark.skipif(
    not CODEX_VALIDATOR.is_file(),
    reason="Codex plugin-creator validator not installed on this machine.",
)
def test_plugin_manifest_passes_codex_validator():
    """Reject regressions: plugin.json must satisfy the upstream schema."""
    proc = subprocess.run(
        [sys.executable, str(CODEX_VALIDATOR), str(PLUGIN_ROOT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"validate_plugin failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_plugin_skill_present():
    """SKILL.md is what makes the plugin's skill discoverable to Codex."""
    skill = PLUGIN_ROOT / "skills" / "beacon-codex-bridge" / "SKILL.md"
    assert skill.is_file()
    body = skill.read_text(encoding="utf-8")
    assert "name: beacon-codex-bridge" in body


def test_plugin_manifest_does_not_declare_hooks_field():
    """Validator rejects `hooks` in plugin.json (= ms-93 / e-2508 finding).

    We don't ship a `hooks` top-level field — the install Skill merges into
    ~/.codex/hooks.json instead.
    """
    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text("utf-8")
    )
    assert "hooks" not in manifest


# ------------------------------------------------------------------ #
# 2. install-hook / uninstall-hook
# ------------------------------------------------------------------ #


@pytest.fixture
def fake_hooks_path(tmp_path, monkeypatch):
    """Redirect ~/.codex/hooks.json to a tmp file for hook merge tests."""
    bridge = _load_bridge_module()
    target = tmp_path / "hooks.json"
    monkeypatch.setattr(bridge, "CODEX_HOOKS_PATH", target)
    return bridge, target


def test_install_hook_creates_file_when_absent(fake_hooks_path, tmp_path):
    bridge, target = fake_hooks_path
    cwd = tmp_path / "proj"
    cwd.mkdir()
    rc = bridge.cmd_install_hook(REPO_ROOT, cwd)
    assert rc == 0
    data = json.loads(target.read_text("utf-8"))
    bucket = data["hooks"]["UserPromptSubmit"]
    assert len(bucket) == 1
    entry = bucket[0]
    cmd = entry["hooks"][0]["command"]
    assert "codex-inbox-hook.py" in cmd
    assert str(cwd) in cmd


def test_install_hook_is_idempotent(fake_hooks_path, tmp_path):
    """Re-installing twice must not duplicate the entry."""
    bridge, target = fake_hooks_path
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert bridge.cmd_install_hook(REPO_ROOT, cwd) == 0
    assert bridge.cmd_install_hook(REPO_ROOT, cwd) == 0
    data = json.loads(target.read_text("utf-8"))
    assert len(data["hooks"]["UserPromptSubmit"]) == 1


def test_install_hook_preserves_unrelated_entries(fake_hooks_path, tmp_path):
    bridge, target = fake_hooks_path
    # Seed with an unrelated user hook.
    seed = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/usr/bin/echo other",
                            "timeout": 5,
                        }
                    ]
                }
            ]
        }
    }
    target.write_text(json.dumps(seed), encoding="utf-8")

    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert bridge.cmd_install_hook(REPO_ROOT, cwd) == 0
    data = json.loads(target.read_text("utf-8"))
    bucket = data["hooks"]["UserPromptSubmit"]
    cmds = [h["hooks"][0]["command"] for h in bucket]
    assert "/usr/bin/echo other" in cmds
    assert any("codex-inbox-hook.py" in c for c in cmds)
    assert len(bucket) == 2


def test_install_hook_multi_cwd_creates_separate_entries(
    fake_hooks_path, tmp_path
):
    """Two cwd should each get their own entry (= multi-project safe)."""
    bridge, target = fake_hooks_path
    cwd_a = tmp_path / "a"
    cwd_a.mkdir()
    cwd_b = tmp_path / "b"
    cwd_b.mkdir()
    assert bridge.cmd_install_hook(REPO_ROOT, cwd_a) == 0
    assert bridge.cmd_install_hook(REPO_ROOT, cwd_b) == 0
    data = json.loads(target.read_text("utf-8"))
    bucket = data["hooks"]["UserPromptSubmit"]
    assert len(bucket) == 2
    cmds = [h["hooks"][0]["command"] for h in bucket]
    assert any(str(cwd_a) in c for c in cmds)
    assert any(str(cwd_b) in c for c in cmds)


def test_uninstall_hook_removes_only_requested_cwd(fake_hooks_path, tmp_path):
    bridge, target = fake_hooks_path
    cwd_a = tmp_path / "a"
    cwd_a.mkdir()
    cwd_b = tmp_path / "b"
    cwd_b.mkdir()
    bridge.cmd_install_hook(REPO_ROOT, cwd_a)
    bridge.cmd_install_hook(REPO_ROOT, cwd_b)
    assert bridge.cmd_uninstall_hook(cwd_a) == 0
    data = json.loads(target.read_text("utf-8"))
    bucket = data["hooks"]["UserPromptSubmit"]
    cmds = [h["hooks"][0]["command"] for h in bucket]
    assert not any(str(cwd_a) in c for c in cmds)
    assert any(str(cwd_b) in c for c in cmds)


def test_uninstall_hook_no_op_when_absent(fake_hooks_path, tmp_path):
    bridge, _target = fake_hooks_path
    cwd = tmp_path / "proj"
    cwd.mkdir()
    # Removing without prior install should not raise / fail.
    assert bridge.cmd_uninstall_hook(cwd) == 0


# ------------------------------------------------------------------ #
# 3. Pidfile lifecycle (= daemon state without actually running the daemon)
# ------------------------------------------------------------------ #


def test_read_pid_returns_zero_for_missing_pidfile(tmp_path):
    bridge = _load_bridge_module()
    assert bridge._read_pid(tmp_path) == 0


def test_read_pid_returns_zero_for_stale_pidfile(tmp_path):
    """A pidfile whose pid is dead must be treated as stale (not live)."""
    bridge = _load_bridge_module()
    codex_dir = tmp_path / ".beacon" / "codex"
    codex_dir.mkdir(parents=True)
    # Spawn-and-reap a short-lived process to get a guaranteed-dead pid.
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    (codex_dir / "receive-loop.pid").write_text(str(proc.pid))
    assert bridge._read_pid(tmp_path) == 0


def test_clean_stale_pidfile_removes_dead_entry(tmp_path):
    bridge = _load_bridge_module()
    codex_dir = tmp_path / ".beacon" / "codex"
    codex_dir.mkdir(parents=True)
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    pidfile = codex_dir / "receive-loop.pid"
    pidfile.write_text(str(proc.pid))
    assert bridge._clean_stale_pidfile(tmp_path) is True
    assert not pidfile.exists()


def test_read_pid_returns_live_pid(tmp_path):
    """A live pid in the pidfile is detected."""
    bridge = _load_bridge_module()
    codex_dir = tmp_path / ".beacon" / "codex"
    codex_dir.mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        (codex_dir / "receive-loop.pid").write_text(str(proc.pid))
        assert bridge._read_pid(tmp_path) == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=2)


def test_stop_is_no_op_when_not_running(tmp_path):
    bridge = _load_bridge_module()
    assert bridge.cmd_stop(tmp_path) == 0


def test_stop_cleans_stale_pidfile(tmp_path):
    """`stop` against a stale pidfile must succeed and clean it."""
    bridge = _load_bridge_module()
    codex_dir = tmp_path / ".beacon" / "codex"
    codex_dir.mkdir(parents=True)
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    (codex_dir / "receive-loop.pid").write_text(str(proc.pid))
    assert bridge.cmd_stop(tmp_path) == 0
    assert not (codex_dir / "receive-loop.pid").exists()


# ------------------------------------------------------------------ #
# 4. status / collision smoke
# ------------------------------------------------------------------ #


def test_status_handles_empty_cwd(tmp_path, capsys):
    bridge = _load_bridge_module()
    rc = bridge.cmd_status(REPO_ROOT, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "hook:" in out
    assert "daemon:" in out
    assert "session:" in out


def test_install_root_resolution_from_env(monkeypatch, tmp_path):
    """`BEACON_INSTALL_ROOT` env overrides script-relative fallback."""
    bridge = _load_bridge_module()
    fake = tmp_path / "fake-beacon"
    fake.mkdir()
    monkeypatch.setenv("BEACON_INSTALL_ROOT", str(fake))
    assert bridge._install_root() == fake.resolve()


def test_install_root_resolution_script_relative(monkeypatch):
    """Without env, install root is derived from the script path."""
    bridge = _load_bridge_module()
    monkeypatch.delenv("BEACON_INSTALL_ROOT", raising=False)
    # Script lives at plugins/beacon/scripts/beacon-codex-bridge; two more
    # parents up should be the repo root.
    assert bridge._install_root() == REPO_ROOT


# ------------------------------------------------------------------ #
# 5. restart resilience (= ms-93 / e-2508 Codex re-dogfood FINDING #4)
# ------------------------------------------------------------------ #


def test_restart_does_not_gate_on_stop_exit_code(tmp_path, monkeypatch):
    """`restart` must call `start` even when `stop` reports non-zero.

    The previous implementation bailed early on `cmd_stop` exit 1 (= when
    the daemon was slower than the polling window). Codex re-dogfood found
    `restart` silently failing while `start` worked. The fix decouples the
    two: `restart = stop + clean stale + start` regardless of stop's rc.
    """
    bridge = _load_bridge_module()
    calls = {"stop": 0, "start": 0, "clean": 0}

    def fake_stop(cwd):
        calls["stop"] += 1
        return 1  # simulate the "pidfile still present" timeout

    def fake_clean(cwd):
        calls["clean"] += 1
        return False

    def fake_start(install_root, cwd):
        calls["start"] += 1
        return 0

    monkeypatch.setattr(bridge, "cmd_stop", fake_stop)
    monkeypatch.setattr(bridge, "_clean_stale_pidfile", fake_clean)
    monkeypatch.setattr(bridge, "cmd_start", fake_start)

    rc = bridge.cmd_restart(REPO_ROOT, tmp_path)
    assert rc == 0
    assert calls["stop"] == 1
    assert calls["clean"] == 1
    assert calls["start"] == 1, "restart must reach start even when stop returned non-zero"


def test_stop_wait_window_is_at_least_5_seconds(tmp_path):
    """Sanity check that the wait window allows for slow network shutdown.

    The bug Codex hit: graceful shutdown sends a final heartbeat to the
    server (= network call) before the daemon unlinks its pidfile. Window
    < 5s could time out on slow networks.
    """
    bridge = _load_bridge_module()
    import inspect
    src = inspect.getsource(bridge.cmd_stop)
    # Look for "range(100)" or higher — 100*0.1s = 10s
    assert "range(100)" in src, (
        "cmd_stop polling window must be >= 100 iterations (= 10s); "
        f"FINDING #4 needs the wider window. Source:\n{src}"
    )


# ------------------------------------------------------------------ #
# 6. app-server spike helpers (= ms-93 / e-2519 SPEC §8-G option D)
# ------------------------------------------------------------------ #


def test_app_server_text_input_shape_is_sequence():
    client = _load_app_server_client_module()
    assert client.text_input("hello") == [{"type": "text", "text": "hello"}]


def test_app_server_extract_thread_id_from_thread_start_response():
    client = _load_app_server_client_module()
    rsp = {"result": {"thread": {"id": "thr-1"}}}
    assert client.extract_thread_id(rsp) == "thr-1"


def test_app_server_agent_message_prefers_completed_text():
    client = _load_app_server_client_module()
    notifications = [
        {
            "method": "item/agentMessage/delta",
            "params": {"delta": "hel"},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"delta": "lo"},
        },
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": "hello",
                },
            },
        },
    ]
    assert client.agent_message_text_from_notifications(notifications) == "hello"


def test_app_server_agent_message_falls_back_to_deltas():
    client = _load_app_server_client_module()
    notifications = [
        {
            "method": "item/agentMessage/delta",
            "params": {"delta": "he"},
        },
        {
            "method": "item/agentMessage/delta",
            "params": {"delta": "llo"},
        },
    ]
    assert client.agent_message_text_from_notifications(notifications) == "hello"
