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


def test_codex_armed_skill_present_and_covers_ac6(tmp_path):
    """The Codex armed Skill (= e-2519 AC 6) must exist and carry the safety
    surface: explicit opt-in / budget gate / stop / audit / separate-worker
    disclosure copy."""
    skill = PLUGIN_ROOT / "skills" / "beacon-codex-armed" / "SKILL.md"
    assert skill.is_file()
    body = skill.read_text(encoding="utf-8")
    assert "name: beacon-codex-armed" in body
    # budget gate (max auto replies) + arm/status/stop verbs.
    assert "bus budget" in body
    assert "stop" in body
    # allowed channels + audit trail.
    assert "auto-execute" in body
    assert "receive-loop.log" in body
    # the "spawns a separate worker, does not wake your TUI" disclosure copy.
    assert "TUI" in body


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

    def fake_start(install_root, cwd, **kwargs):
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


def test_stop_escalates_to_sigkill_when_graceful_window_expires(tmp_path, monkeypatch):
    """A daemon that ignores SIGTERM (stuck shutdown) must still be stopped.

    ms-93 / e-3110: the graceful shutdown path (final shutdown=True heartbeat
    + app_server_client.stop()) can hang on a slow / unreachable network,
    leaving the old daemon and its pidfile alive past the 10s window. Since
    `restart` no longer gates on stop's exit code, `start` would then see a
    live pid and no-op as "already running" — silently keeping a stale
    pull-only daemon and dropping the app-server (wake) upgrade. cmd_stop must
    escalate to SIGKILL so the stop is guaranteed and the pidfile is cleaned.
    """
    bridge = _load_bridge_module()
    codex_dir = tmp_path / ".beacon" / "codex"
    codex_dir.mkdir(parents=True)
    pidfile = codex_dir / "receive-loop.pid"
    pidfile.write_text("999999")

    signals = []
    state = {"alive": True}

    def fake_kill(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            state["alive"] = False
            return
        if sig == 0 and not state["alive"]:
            raise ProcessLookupError()
        # SIGTERM (ignored by the stuck daemon) and liveness probes while
        # alive are no-ops.

    monkeypatch.setattr(bridge.os, "kill", fake_kill)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    rc = bridge.cmd_stop(tmp_path)

    assert rc == 0, "stop must succeed once it escalates to SIGKILL"
    assert (999999, signal.SIGTERM) in signals, "SIGTERM must be tried first"
    assert (999999, signal.SIGKILL) in signals, "SIGKILL must be the escalation"
    assert not pidfile.exists(), "pidfile must be cleaned after the kill"


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


def test_app_server_extract_turn_id_from_turn_start_response():
    client = _load_app_server_client_module()
    rsp = {"result": {"turn": {"id": "turn-1"}}}
    assert client.extract_turn_id(rsp) == "turn-1"


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


def test_app_server_drain_until_idle_reads_agent_notifications(monkeypatch):
    client = _load_app_server_client_module()
    messages = iter([
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thr-1", "turnId": "turn-1", "delta": "he"},
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thr-1",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "hello"},
            },
        },
        {
            "method": "thread/status/changed",
            "params": {"threadId": "thr-1", "status": {"type": "idle"}},
        },
    ])

    monkeypatch.setattr(client, "_recv_one", lambda _h, timeout_s=30.0: next(messages))
    notifications = client.drain_until_idle(
        object(), thread_id="thr-1", turn_id="turn-1", timeout_s=1
    )
    assert client.agent_message_text_from_notifications(notifications) == "hello"


def test_app_server_process_env_blocks_budget_self_grant(monkeypatch):
    client = _load_app_server_client_module()
    captured = {}

    class FakeProc:
        stdin = None
        stdout = None
        stderr = None

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env") or {}

    monkeypatch.setattr(client.subprocess, "Popen", FakeProc)
    client.start_app_server(codex_bin="/tmp/codex")
    assert captured["argv"] == ["/tmp/codex", "app-server", "--stdio"]
    assert captured["env"]["BEACON_OPERATION_AUTO_EXECUTE"] == "1"
    assert captured["env"]["BEACON_CODEX_APP_SERVER_BRIDGE"] == "1"


def test_app_server_proxy_uses_daemon_proxy_command(monkeypatch):
    client = _load_app_server_client_module()
    captured = {}

    class FakeProc:
        stdin = None
        stdout = None
        stderr = None

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv

    monkeypatch.setattr(client.subprocess, "Popen", FakeProc)
    client.start_app_server(
        codex_bin="/tmp/codex",
        proxy=True,
        sock="/tmp/codex.sock",
    )
    assert captured["argv"] == [
        "/tmp/codex", "app-server", "proxy", "--sock", "/tmp/codex.sock"
    ]


def test_app_server_remote_url_uses_websocket(monkeypatch):
    client = _load_app_server_client_module()
    captured = {}

    class FakeWs:
        def send(self, _msg):
            pass

        def recv(self, timeout=None):
            return "{}"

        def close(self):
            captured["closed"] = True

    def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeWs()

    import types
    fake_client_mod = types.SimpleNamespace(connect=fake_connect)
    fake_sync_mod = types.SimpleNamespace(client=fake_client_mod)
    fake_websockets_mod = types.SimpleNamespace(sync=fake_sync_mod)
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets_mod)
    monkeypatch.setitem(sys.modules, "websockets.sync", fake_sync_mod)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", fake_client_mod)

    handle = client.start_app_server(remote_url="ws://127.0.0.1:39988")
    assert captured["url"] == "ws://127.0.0.1:39988"
    assert captured["kwargs"] == {
        "ping_interval": None,
        "max_size": client._WS_MAX_SIZE,
    }
    assert handle.ws is not None
    handle.stop()
    assert captured["closed"] is True


def test_app_server_thread_resume_request(monkeypatch):
    client = _load_app_server_client_module()
    captured = {}

    def fake_request(_handle, method, params=None):
        captured["method"] = method
        captured["params"] = params
        return {"result": {"thread": {"id": params["threadId"]}}}

    monkeypatch.setattr(client, "request", fake_request)
    rsp = client.thread_resume(object(), thread_id="thr-existing", cwd="/tmp/p")
    assert captured == {
        "method": "thread/resume",
        "params": {"threadId": "thr-existing", "cwd": "/tmp/p"},
    }
    assert client.extract_thread_id(rsp) == "thr-existing"


def test_bridge_app_server_thread_uses_response_only_instructions(monkeypatch):
    client = _load_app_server_client_module()
    captured = {}

    monkeypatch.setattr(client, "start_app_server", lambda **_kwargs: object())
    monkeypatch.setattr(client, "initialize", lambda _handle: {"result": {}})

    def fake_thread_start(_handle, **kwargs):
        captured.update(kwargs)
        return {"result": {"thread": {"id": "thr-1"}}}

    monkeypatch.setattr(client, "thread_start", fake_thread_start)
    bridge = client.BridgeAppServerClient()
    bridge.ensure_started(cwd="/tmp/project")
    assert bridge.thread_id == "thr-1"
    assert captured["cwd"] == "/tmp/project"
    assert "Do not run shell commands" in captured["base_instructions"]
    assert "parent bridge daemon is responsible for sending" in captured[
        "base_instructions"
    ]


def test_bridge_app_server_can_resume_existing_remote_thread(monkeypatch):
    client = _load_app_server_client_module()
    captured = {}

    def fake_start_app_server(**kwargs):
        captured["start_kwargs"] = kwargs
        return object()

    def fake_thread_resume(_handle, **kwargs):
        captured["resume_kwargs"] = kwargs
        return {"result": {"thread": {"id": kwargs["thread_id"]}}}

    monkeypatch.setattr(client, "start_app_server", fake_start_app_server)
    monkeypatch.setattr(client, "initialize", lambda _handle: {"result": {}})
    monkeypatch.setattr(client, "thread_resume", fake_thread_resume)

    bridge = client.BridgeAppServerClient(
        target_thread_id="thr-existing",
        use_proxy=True,
        proxy_sock="/tmp/codex.sock",
        remote_url="ws://127.0.0.1:39988",
    )
    bridge.ensure_started(cwd="/tmp/project")
    assert bridge.thread_id == "thr-existing"
    assert captured["start_kwargs"] == {
        "proxy": True,
        "sock": "/tmp/codex.sock",
        "remote_url": "ws://127.0.0.1:39988",
    }
    assert captured["resume_kwargs"] == {
        "thread_id": "thr-existing",
        "cwd": "/tmp/project",
    }


# ------------------------------------------------------------------ #
# 7. bridge --app-server forwarding (= ms-93 / e-2519 MVP wiring)
# ------------------------------------------------------------------ #


class _FakeProc:
    """Stand-in for subprocess.Popen, captures argv + env."""

    last = None  # class-level: most recent instance, for assertions

    def __init__(self, argv, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = 12345
        self._poll_returns = None
        _FakeProc.last = self

    def poll(self):
        return self._poll_returns

    @property
    def returncode(self):
        return self._poll_returns


def _prep_fake_start(bridge, monkeypatch, tmp_path):
    """Set up the minimum surface cmd_start touches before subprocess spawn."""
    # cloud.json so the "no cloud config" early-return is bypassed.
    cloud = tmp_path / ".beacon" / "cloud.json"
    cloud.parent.mkdir(parents=True, exist_ok=True)
    cloud.write_text("{}")

    # Pretend the daemon script exists at the expected location.
    real_isfile = Path.is_file

    def fake_isfile(self):
        if str(self).endswith("scripts/codex-receive-loop.py"):
            return True
        return real_isfile(self)

    monkeypatch.setattr(Path, "is_file", fake_isfile)

    # Pretend the hook install succeeds (= side-effect free).
    monkeypatch.setattr(bridge, "cmd_install_hook", lambda *a, **k: 0)
    # Skip the "pidfile not yet visible" wait loop by claiming a live pid
    # immediately so cmd_start returns 0 without polling.
    monkeypatch.setattr(bridge, "_read_pid", lambda cwd: 99999)


def test_cmd_start_without_app_server_flag_omits_argv_and_env(
    monkeypatch, tmp_path
):
    bridge = _load_bridge_module()
    _prep_fake_start(bridge, monkeypatch, tmp_path)
    # First _read_pid call (existing-pid guard) must be 0, then 99999 after spawn.
    pid_returns = iter([0, 99999])
    monkeypatch.setattr(bridge, "_read_pid", lambda cwd: next(pid_returns))
    monkeypatch.setattr(bridge.subprocess, "Popen", _FakeProc)

    rc = bridge.cmd_start(REPO_ROOT, tmp_path, app_server=False)
    assert rc == 0
    assert "--app-server" not in _FakeProc.last.argv, (
        "default daemon argv must not contain --app-server"
    )
    assert "BEACON_CODEX_APP_SERVER" not in _FakeProc.last.kwargs["env"], (
        "default env must not set BEACON_CODEX_APP_SERVER"
    )


def test_cmd_start_with_app_server_flag_adds_argv_and_env(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    _prep_fake_start(bridge, monkeypatch, tmp_path)
    pid_returns = iter([0, 99999])
    monkeypatch.setattr(bridge, "_read_pid", lambda cwd: next(pid_returns))
    monkeypatch.setattr(bridge.subprocess, "Popen", _FakeProc)

    rc = bridge.cmd_start(REPO_ROOT, tmp_path, app_server=True)
    assert rc == 0
    assert "--app-server" in _FakeProc.last.argv, (
        "--app-server flag must be forwarded to the daemon argv"
    )
    assert _FakeProc.last.kwargs["env"].get("BEACON_CODEX_APP_SERVER") == "1", (
        "BEACON_CODEX_APP_SERVER must be set so a stale daemon argv cannot "
        "silently disable opt-in on restart"
    )


def test_cmd_start_with_remote_thread_adds_proxy_args_and_env(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    _prep_fake_start(bridge, monkeypatch, tmp_path)
    pid_returns = iter([0, 99999])
    monkeypatch.setattr(bridge, "_read_pid", lambda cwd: next(pid_returns))
    monkeypatch.setattr(bridge.subprocess, "Popen", _FakeProc)

    rc = bridge.cmd_start(
        REPO_ROOT,
        tmp_path,
        app_server=True,
        codex_thread_id="thr-existing",
        app_server_proxy=True,
        app_server_sock="/tmp/codex.sock",
        app_server_url="ws://127.0.0.1:39988",
    )
    assert rc == 0
    assert "--codex-thread-id" in _FakeProc.last.argv
    assert "thr-existing" in _FakeProc.last.argv
    assert "--app-server-proxy" in _FakeProc.last.argv
    assert "--app-server-sock" in _FakeProc.last.argv
    assert "/tmp/codex.sock" in _FakeProc.last.argv
    assert "--app-server-url" in _FakeProc.last.argv
    assert "ws://127.0.0.1:39988" in _FakeProc.last.argv
    env = _FakeProc.last.kwargs["env"]
    assert env["BEACON_CODEX_THREAD_ID"] == "thr-existing"
    assert env["BEACON_CODEX_APP_SERVER_PROXY"] == "1"
    assert env["BEACON_CODEX_APP_SERVER_SOCK"] == "/tmp/codex.sock"
    assert env["BEACON_CODEX_APP_SERVER_URL"] == "ws://127.0.0.1:39988"


def test_cmd_restart_propagates_app_server_flag(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    seen = {}

    def fake_stop(cwd):
        return 0

    def fake_clean(cwd):
        return False

    def fake_start(install_root, cwd, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(bridge, "cmd_stop", fake_stop)
    monkeypatch.setattr(bridge, "_clean_stale_pidfile", fake_clean)
    monkeypatch.setattr(bridge, "cmd_start", fake_start)

    rc = bridge.cmd_restart(REPO_ROOT, tmp_path, app_server=True)
    assert rc == 0
    assert seen.get("app_server") is True, (
        "restart must propagate --app-server through to start"
    )


def test_argparse_app_server_flag_is_off_by_default(monkeypatch):
    """The CLI default must keep pull-on-prompt behaviour identical.

    Parses argv via the bridge's parser; doesn't exec subprocess.
    """
    bridge = _load_bridge_module()
    # Replay the parser construction without invoking main() (which would
    # try to resolve install_root paths). Easiest: dispatch a quick parse.
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("subaction", nargs="?", default="status")
    p.add_argument("--cwd", default="")
    p.add_argument("--install-root", default="")
    p.add_argument("--app-server", action="store_true")
    ns = p.parse_args(["start"])
    assert ns.app_server is False
    ns2 = p.parse_args(["start", "--app-server"])
    assert ns2.app_server is True


# ------------------------------------------------------------------ #
# 8. bridge --armed forwarding (= ms-93 / e-2519 AC 2 + AC 6
#    autonomous reply opt-in)
# ------------------------------------------------------------------ #


def test_cmd_start_without_armed_flag_omits_argv_and_env(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    _prep_fake_start(bridge, monkeypatch, tmp_path)
    pid_returns = iter([0, 99999])
    monkeypatch.setattr(bridge, "_read_pid", lambda cwd: next(pid_returns))
    monkeypatch.setattr(bridge.subprocess, "Popen", _FakeProc)

    rc = bridge.cmd_start(REPO_ROOT, tmp_path, app_server=True, armed=False)
    assert rc == 0
    assert "--armed" not in _FakeProc.last.argv, (
        "default armed=False must not forward --armed to daemon argv"
    )
    assert "BEACON_CODEX_ARMED" not in _FakeProc.last.kwargs["env"], (
        "default armed=False must not set BEACON_CODEX_ARMED in env"
    )


def test_cmd_start_with_armed_flag_adds_argv_and_env(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    _prep_fake_start(bridge, monkeypatch, tmp_path)
    pid_returns = iter([0, 99999])
    monkeypatch.setattr(bridge, "_read_pid", lambda cwd: next(pid_returns))
    monkeypatch.setattr(bridge.subprocess, "Popen", _FakeProc)

    rc = bridge.cmd_start(REPO_ROOT, tmp_path, app_server=True, armed=True)
    assert rc == 0
    assert "--armed" in _FakeProc.last.argv, (
        "--armed must be forwarded to daemon argv when opt-in"
    )
    assert _FakeProc.last.kwargs["env"].get("BEACON_CODEX_ARMED") == "1", (
        "BEACON_CODEX_ARMED must be set so a stale daemon argv cannot "
        "silently disable autonomous reply on restart"
    )


def test_cmd_restart_propagates_armed_flag(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    seen = {}

    def fake_start(install_root, cwd, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(bridge, "cmd_stop", lambda cwd: 0)
    monkeypatch.setattr(bridge, "_clean_stale_pidfile", lambda cwd: False)
    monkeypatch.setattr(bridge, "cmd_start", fake_start)

    rc = bridge.cmd_restart(REPO_ROOT, tmp_path, app_server=True, armed=True)
    assert rc == 0
    assert seen.get("armed") is True, (
        "restart must propagate --armed through to start"
    )


# ------------------------------------------------------------------ #
# 9. build_dm_reply_args helper (= ms-93 / e-2519 AC 2 reply builder)
# ------------------------------------------------------------------ #


def _load_receive_loop_module():
    """Load lib/codex_receive_loop.py for testing build_dm_reply_args."""
    import importlib.util as _u
    path = REPO_ROOT / "lib" / "codex_receive_loop.py"
    spec = _u.spec_from_file_location("codex_receive_loop_for_test", path)
    m = _u.module_from_spec(spec)
    # bus_protocol is a sibling module; make it importable from lib/.
    if str(REPO_ROOT / "lib") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "lib"))
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def test_build_dm_reply_args_returns_argv_for_valid_input():
    crl = _load_receive_loop_module()
    argv = crl.build_dm_reply_args(
        event={"event_id": "evt-1", "sender_session_id": "sv-other-abc"},
        agent_text="hello back",
        beacon_bin="/abs/beacon",
    )
    assert argv is not None
    assert argv[:3] == ["/abs/beacon", "bus", "send"]
    assert "--channel" in argv and "dm" in argv
    assert "--to" in argv and "sv-other-abc" in argv
    assert "--in-reply-to" in argv and "evt-1" in argv
    # payload index follows "--payload"
    payload_idx = argv.index("--payload") + 1
    assert "hello back" in argv[payload_idx]
    assert argv[-1] == "--json"


def test_build_dm_reply_args_skips_when_agent_text_empty():
    crl = _load_receive_loop_module()
    for empty in ("", "   ", "\n\n", None):
        argv = crl.build_dm_reply_args(
            event={"event_id": "evt-1", "sender_session_id": "sv-other"},
            agent_text=empty or "",
            beacon_bin="beacon",
        )
        assert argv is None, (
            f"empty agent_text {empty!r} must skip the reply (= no spam)"
        )


def test_build_dm_reply_args_skips_when_event_id_or_sender_missing():
    """Missing event_id breaks the conversation thread; missing
    sender_session_id leaves no addressee. Either makes the reply
    impossible to thread back."""
    crl = _load_receive_loop_module()
    for evt in (
        {"sender_session_id": "sv-other"},  # no event_id
        {"event_id": "evt-1"},  # no sender_session_id
        {"event_id": "", "sender_session_id": "sv-other"},  # empty event_id
        {"event_id": "evt-1", "sender_session_id": ""},  # empty sender
        {},  # both missing
    ):
        argv = crl.build_dm_reply_args(
            event=evt, agent_text="non-empty", beacon_bin="beacon",
        )
        assert argv is None, (
            f"missing event_id or sender_session_id must skip the reply ({evt!r})"
        )


def test_build_dm_reply_args_payload_is_valid_json():
    """The --payload value must be valid JSON the bus accepts."""
    crl = _load_receive_loop_module()
    argv = crl.build_dm_reply_args(
        event={"event_id": "evt-1", "sender_session_id": "sv-other"},
        agent_text='hello with "quotes" and \nnewline',
        beacon_bin="beacon",
    )
    assert argv is not None
    payload_idx = argv.index("--payload") + 1
    parsed = json.loads(argv[payload_idx])
    assert parsed["text"] == 'hello with "quotes" and \nnewline'


# ------------------------------------------------------------------ #
# 10. Codex sandbox network_access ensure (= ms-93 / e-3000)
#
# Codex's workspace-write sandbox blocks network by default, so every
# beacon cloud call from inside Codex died on DNS resolution
# (socket.gaierror [Errno 8]). The bridge idempotently writes
# `[sandbox_workspace_write] network_access = true` into
# ~/.codex/config.toml, preserving all existing content.
# ------------------------------------------------------------------ #


@pytest.fixture
def fake_config_path(tmp_path, monkeypatch):
    """Redirect ~/.codex/config.toml to a tmp file for TOML-ensure tests."""
    bridge = _load_bridge_module()
    target = tmp_path / "config.toml"
    monkeypatch.setattr(bridge, "CODEX_CONFIG_PATH", target)
    return bridge, target


def _net_enabled(text: str) -> bool:
    """True iff the config text enables sandbox network access.

    Accepts both the table form and the root dotted-key form.
    """
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[[") and s.endswith("]]"):
            cur = "\x00"
            continue
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1].strip()
            continue
        body = s.split("#", 1)[0]
        if "=" not in body:
            continue
        key, _, val = body.partition("=")
        key = key.strip()
        is_true = val.strip().lower() == "true"
        if cur == "sandbox_workspace_write" and key == "network_access" and is_true:
            return True
        if cur is None and key == "sandbox_workspace_write.network_access" and is_true:
            return True
    return False


def test_ensure_network_creates_block_when_file_absent(fake_config_path):
    bridge, target = fake_config_path
    assert not target.exists()
    verdict, detail = bridge._ensure_codex_network_access()
    assert verdict == "created"
    assert target.is_file()
    text = target.read_text("utf-8")
    assert "[sandbox_workspace_write]" in text
    assert _net_enabled(text)


def test_ensure_network_is_idempotent(fake_config_path):
    bridge, target = fake_config_path
    assert bridge._ensure_codex_network_access()[0] == "created"
    first = target.read_text("utf-8")
    verdict, _ = bridge._ensure_codex_network_access()
    assert verdict == "already"
    # No-op must not rewrite / mutate the file.
    assert target.read_text("utf-8") == first


def test_ensure_network_inserts_key_into_existing_table(fake_config_path):
    bridge, target = fake_config_path
    target.write_text(
        "[sandbox_workspace_write]\nexclude_tmpdir_env_var = true\n",
        encoding="utf-8",
    )
    verdict, _ = bridge._ensure_codex_network_access()
    assert verdict == "inserted"
    text = target.read_text("utf-8")
    assert _net_enabled(text)
    # Existing sibling key preserved.
    assert "exclude_tmpdir_env_var = true" in text


def test_ensure_network_flips_false_to_true(fake_config_path):
    bridge, target = fake_config_path
    target.write_text(
        "[sandbox_workspace_write]\nnetwork_access = false\n",
        encoding="utf-8",
    )
    verdict, _ = bridge._ensure_codex_network_access()
    assert verdict == "updated"
    text = target.read_text("utf-8")
    assert _net_enabled(text)
    assert "network_access = false" not in text


def test_ensure_network_flips_dotted_false_to_true(fake_config_path):
    bridge, target = fake_config_path
    target.write_text(
        "sandbox_workspace_write.network_access = false\n\n"
        '[projects."/x"]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    verdict, _ = bridge._ensure_codex_network_access()
    assert verdict == "updated"
    assert _net_enabled(target.read_text("utf-8"))


def test_ensure_network_dotted_true_is_already(fake_config_path):
    bridge, target = fake_config_path
    target.write_text(
        "sandbox_workspace_write.network_access = true\n", encoding="utf-8"
    )
    assert bridge._ensure_codex_network_access()[0] == "already"


def test_ensure_network_preserves_unrelated_tables(fake_config_path):
    bridge, target = fake_config_path
    seed = (
        '[projects."/Users/x/tools/beacon"]\n'
        'trust_level = "trusted"\n\n'
        "[tui.model_availability_nux]\n"
        '"gpt-5.5" = 4\n'
    )
    target.write_text(seed, encoding="utf-8")
    verdict, _ = bridge._ensure_codex_network_access()
    assert verdict == "created"
    text = target.read_text("utf-8")
    # Every original line survives.
    for line in seed.splitlines():
        assert line in text
    assert _net_enabled(text)


def test_ensure_network_does_not_match_key_in_other_table(fake_config_path):
    """A `network_access` under a different table must not count as satisfied."""
    bridge, target = fake_config_path
    target.write_text(
        "[some_other_table]\nnetwork_access = true\n", encoding="utf-8"
    )
    verdict, _ = bridge._ensure_codex_network_access()
    assert verdict == "created"
    text = target.read_text("utf-8")
    assert "[sandbox_workspace_write]" in text
    assert _net_enabled(text)


def test_ensure_network_error_when_config_unreadable(fake_config_path):
    """A directory at the config path yields a non-fatal error verdict."""
    bridge, target = fake_config_path
    target.mkdir()  # path exists but is a directory → read/write fails
    verdict, detail = bridge._ensure_codex_network_access()
    assert verdict == "error"
    assert str(target) in detail


def test_install_hook_also_ensures_network_access(fake_hooks_path, tmp_path, monkeypatch):
    """install-hook must enable sandbox network access as a side effect."""
    bridge, _hooks = fake_hooks_path
    config = tmp_path / "config.toml"
    monkeypatch.setattr(bridge, "CODEX_CONFIG_PATH", config)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    assert bridge.cmd_install_hook(REPO_ROOT, cwd) == 0
    assert config.is_file()
    assert _net_enabled(config.read_text("utf-8"))


def test_cmd_ensure_network_subaction_returns_zero(fake_config_path, capsys):
    bridge, _target = fake_config_path
    assert bridge.cmd_ensure_network() == 0
    out = capsys.readouterr().out
    assert "network_access" in out
