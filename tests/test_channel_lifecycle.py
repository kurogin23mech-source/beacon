"""Tests for `beacon channel {uninstall,opt-out,opt-in,status,install}` (ms-54 e-1266).

These pin the four-state lifecycle of the DM channel:

  (a) never installed
  (b) installed and active
  (c) installed but paused (--keep-files default for uninstall)
  (d) opted out (env / project / global flag)

The opt-out gate is the load-bearing piece: install MUST refuse if any of
the three opt-out sources is set, and the install commands MUST be the
only write path that can re-enable the channel (we never re-write
.mcp.json on the user's behalf when opt-out is active).

These tests bypass the bash wrapper (`bin/beacon`) and drive the python
side directly with env vars + monkeypatch'd cwd. That matches how
test_bus_cli.py exercises bus_send / bus_listen and keeps the test
surface stable across shells.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# Important: this import has side effects (module-level state). We import
# once and reset relevant globals per-test via the project_dir fixture.
import commands  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_dir(monkeypatch):
    """A throwaway project root with .beacon/project.json seeded.

    Mirrors the reset_store pattern from tests/test_api.py: we do not
    rely on global state from prior tests, and we restore the user-home
    location too so a wayward `--global` opt-out write doesn't pollute
    the developer's real ~/.beacon/config.json (ms-54 e-1266).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Project file
        beacon_dir = tmp_path / ".beacon"
        beacon_dir.mkdir()
        (beacon_dir / "project.json").write_text(
            json.dumps({
                "name": "channel-lifecycle-test",
                "objective": "exercise channel lifecycle",
                "milestones": [],
            }),
            encoding="utf-8",
        )
        # Isolated HOME so --global writes stay inside the temp dir.
        home_dir = tmp_path / "_home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("USERPROFILE", str(home_dir))  # Windows fallback
        monkeypatch.chdir(tmp_path)
        # Ensure no leaked opt-out env from outer shell.
        monkeypatch.delenv("BEACON_NO_BUS", raising=False)
        # Clear out env vars the new commands read.
        monkeypatch.delenv("BEACON_CHANNEL_PURGE_FILES", raising=False)
        monkeypatch.delenv("BEACON_CHANNEL_SCOPE", raising=False)
        yield tmp_path


def _write_mcp_json(project_dir: Path, with_beacon_bus: bool = True,
                    other_servers: dict | None = None):
    """Seed `.mcp.json` with a beacon-bus entry (and optionally others).

    Used to simulate an already-installed state without going through
    cmd_channel_install (which needs Node and the bundled channel/ dir).
    """
    servers: dict = {}
    if with_beacon_bus:
        servers["beacon-bus"] = {
            "command": "node",
            "args": ["/fake/path/channel/bus.mjs"],
            "env": {"BEACON_CHANNEL_ALLOWLIST": "dm"},
        }
    if other_servers:
        servers.update(other_servers)
    (project_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2),
        encoding="utf-8",
    )


def _capture(fn):
    """Run fn(), return (stdout, stderr, exit_code).

    Many of these commands call sys.exit() on the unhappy path; we
    catch SystemExit so the test can assert on the exit code and the
    captured output in one go.
    """
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            fn()
        except SystemExit as e:
            code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


# ---------------------------------------------------------------------------
# Opt-out gate (the load-bearing piece — every install path consults this)
# ---------------------------------------------------------------------------

class TestOptOutGate:

    def test_no_opt_out_by_default(self, project_dir):
        """Fresh project + no env + no global → not opted out."""
        status = commands._bus_opt_out_status()
        assert status == {"env": False, "project": False, "global": False,
                          "any": False}
        is_out, reason = commands._is_bus_opted_out()
        assert is_out is False
        assert reason == ""

    def test_env_var_triggers_opt_out(self, project_dir, monkeypatch):
        """BEACON_NO_BUS=1 should be honored without any file write."""
        monkeypatch.setenv("BEACON_NO_BUS", "1")
        status = commands._bus_opt_out_status()
        assert status["env"] is True
        assert status["any"] is True
        is_out, reason = commands._is_bus_opted_out()
        assert is_out is True
        assert "env BEACON_NO_BUS=1" in reason

    def test_project_flag_triggers_opt_out(self, project_dir):
        """Writing bus.disabled=true into project.json should opt out."""
        commands._write_project_bus_flag(True)
        status = commands._bus_opt_out_status()
        assert status["project"] is True
        assert status["any"] is True

    def test_global_flag_triggers_opt_out(self, project_dir):
        """Writing bus.disabled=true into ~/.beacon/config.json should opt out."""
        commands._write_user_beacon_config({"bus": {"disabled": True}})
        status = commands._bus_opt_out_status()
        assert status["global"] is True
        assert status["any"] is True


# ---------------------------------------------------------------------------
# beacon channel opt-out / opt-in
# ---------------------------------------------------------------------------

class TestOptOut:

    def test_project_opt_out_writes_flag(self, project_dir, monkeypatch):
        monkeypatch.setenv("BEACON_CHANNEL_SCOPE", "project")
        out, err, code = _capture(commands.cmd_channel_opt_out)
        assert code == 0
        assert commands._read_project_bus_disabled() is True
        # Re-running is idempotent and labeled as such.
        out2, _, _ = _capture(commands.cmd_channel_opt_out)
        assert "already set" in out2

    def test_global_opt_out_writes_user_config(self, project_dir, monkeypatch):
        monkeypatch.setenv("BEACON_CHANNEL_SCOPE", "global")
        out, err, code = _capture(commands.cmd_channel_opt_out)
        assert code == 0
        cfg = commands._read_user_beacon_config()
        assert cfg.get("bus", {}).get("disabled") is True

    def test_project_opt_out_requires_project_dir(self, project_dir, monkeypatch,
                                                   tmp_path):
        """Opt-out --project outside a beacon project should error out cleanly."""
        monkeypatch.setenv("BEACON_CHANNEL_SCOPE", "project")
        # CWD into a dir with no .beacon/project.json
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        out, err, code = _capture(commands.cmd_channel_opt_out)
        assert code == 1
        assert "no .beacon/project.json" in err


class TestOptIn:

    def test_opt_in_clears_project_flag(self, project_dir, monkeypatch):
        commands._write_project_bus_flag(True)
        assert commands._read_project_bus_disabled() is True
        monkeypatch.setenv("BEACON_CHANNEL_SCOPE", "project")
        out, _, code = _capture(commands.cmd_channel_opt_in)
        assert code == 0
        assert commands._read_project_bus_disabled() is False

    def test_opt_in_is_idempotent_when_not_set(self, project_dir, monkeypatch):
        monkeypatch.setenv("BEACON_CHANNEL_SCOPE", "project")
        out, _, code = _capture(commands.cmd_channel_opt_in)
        assert code == 0
        assert "was not set" in out or "nothing to clear" in out.lower()

    def test_opt_in_surfaces_remaining_opt_outs(self, project_dir, monkeypatch):
        """If only project flag is cleared but global is still on, warn the user."""
        commands._write_project_bus_flag(True)
        commands._write_user_beacon_config({"bus": {"disabled": True}})
        monkeypatch.setenv("BEACON_CHANNEL_SCOPE", "project")
        out, _, code = _capture(commands.cmd_channel_opt_in)
        assert code == 0
        # Output should mention the global flag is still active.
        assert "global" in out.lower()


# ---------------------------------------------------------------------------
# beacon channel uninstall
# ---------------------------------------------------------------------------

class TestUninstall:

    def test_default_uninstall_removes_only_mcp_entry(self, project_dir):
        _write_mcp_json(project_dir, with_beacon_bus=True,
                        other_servers={"other-thing": {"command": "x"}})
        out, _, code = _capture(commands.cmd_channel_uninstall)
        assert code == 0
        assert "Removed beacon-bus" in out
        # .mcp.json should still exist and still hold other-thing.
        data = json.loads((project_dir / ".mcp.json").read_text())
        assert "beacon-bus" not in data["mcpServers"]
        assert "other-thing" in data["mcpServers"]

    def test_uninstall_when_not_installed(self, project_dir):
        out, _, code = _capture(commands.cmd_channel_uninstall)
        assert code == 0
        # Either "(no .mcp.json found...)" or "(beacon-bus was not present...)"
        assert "no .mcp.json" in out or "was not present" in out

    def test_purge_files_moves_node_modules_to_trash(self, project_dir,
                                                     monkeypatch):
        """--purge-files should mv node_modules → .trash/ (not rm -rf).

        We can't easily put node_modules at the bundled channel/ location
        in CI, so we monkeypatch _resolve_channel_root to a temp dir we
        own and create a dummy node_modules/ there.
        """
        fake_channel = project_dir / "fake_channel"
        fake_channel.mkdir()
        (fake_channel / "node_modules").mkdir()
        (fake_channel / "node_modules" / "marker").write_text("hi")
        monkeypatch.setattr(commands, "_resolve_channel_root",
                            lambda: fake_channel)
        _write_mcp_json(project_dir)
        monkeypatch.setenv("BEACON_CHANNEL_PURGE_FILES", "1")
        out, _, code = _capture(commands.cmd_channel_uninstall)
        assert code == 0
        # Original location is gone, trash has the contents.
        assert not (fake_channel / "node_modules").exists()
        trash_entries = list((project_dir / ".trash").glob("channel-node_modules-*"))
        assert len(trash_entries) == 1
        assert (trash_entries[0] / "marker").read_text() == "hi"

    def test_default_uninstall_keeps_node_modules(self, project_dir,
                                                   monkeypatch):
        """Without --purge-files, node_modules must NOT be touched."""
        fake_channel = project_dir / "fake_channel"
        fake_channel.mkdir()
        (fake_channel / "node_modules").mkdir()
        monkeypatch.setattr(commands, "_resolve_channel_root",
                            lambda: fake_channel)
        _write_mcp_json(project_dir)
        # No BEACON_CHANNEL_PURGE_FILES env var = default keep-files mode.
        out, _, code = _capture(commands.cmd_channel_uninstall)
        assert code == 0
        assert (fake_channel / "node_modules").exists()


# ---------------------------------------------------------------------------
# beacon channel install (opt-out gate)
# ---------------------------------------------------------------------------

class TestInstallRefusedByOptOut:

    def test_install_refuses_when_env_opt_out(self, project_dir, monkeypatch):
        """BEACON_NO_BUS=1 must block install with exit code 1.

        We don't need to mock node / npm here because the opt-out check
        runs before any of that.
        """
        monkeypatch.setenv("BEACON_NO_BUS", "1")
        out, err, code = _capture(commands.cmd_channel_install)
        assert code == 1
        assert "opt-out" in err.lower()
        # No .mcp.json should have been written.
        assert not (project_dir / ".mcp.json").exists()

    def test_install_refuses_when_project_opt_out(self, project_dir):
        commands._write_project_bus_flag(True)
        out, err, code = _capture(commands.cmd_channel_install)
        assert code == 1
        assert "opt-out" in err.lower()
        assert not (project_dir / ".mcp.json").exists()

    def test_install_refuses_when_global_opt_out(self, project_dir):
        commands._write_user_beacon_config({"bus": {"disabled": True}})
        out, err, code = _capture(commands.cmd_channel_install)
        assert code == 1
        assert "opt-out" in err.lower()
        assert not (project_dir / ".mcp.json").exists()

    def test_install_outside_project_root_fails_first(self, project_dir,
                                                       monkeypatch, tmp_path):
        """Install must fail with the no-project error before opt-out check.

        Order matters for UX: a user in the wrong directory should be
        told they're in the wrong directory, not that an opt-out is set
        in some other project's flag file.
        """
        other = tmp_path / "no_project_here"
        other.mkdir()
        monkeypatch.chdir(other)
        out, err, code = _capture(commands.cmd_channel_install)
        assert code == 1
        assert "no .beacon/project.json" in err


# ---------------------------------------------------------------------------
# beacon channel status
# ---------------------------------------------------------------------------

class TestStatus:

    def test_status_clean_state(self, project_dir):
        """No install, no opt-out — status should say 'would RUN'."""
        out, _, code = _capture(commands.cmd_channel_status)
        assert code == 0
        assert "would RUN" in out or "would run" in out.lower()
        assert "no .mcp.json" in out or "× beacon-bus" in out

    def test_status_when_installed(self, project_dir):
        _write_mcp_json(project_dir)
        out, _, code = _capture(commands.cmd_channel_status)
        assert code == 0
        assert "beacon-bus MCP entry present" in out

    def test_status_when_opted_out(self, project_dir, monkeypatch):
        monkeypatch.setenv("BEACON_NO_BUS", "1")
        out, _, code = _capture(commands.cmd_channel_status)
        assert code == 0
        assert "SKIPPED" in out
        assert "BEACON_NO_BUS" in out

    def test_status_shows_all_opt_out_sources(self, project_dir, monkeypatch):
        """When all 3 sources are set, status enumerates all 3."""
        monkeypatch.setenv("BEACON_NO_BUS", "1")
        commands._write_project_bus_flag(True)
        commands._write_user_beacon_config({"bus": {"disabled": True}})
        out, _, code = _capture(commands.cmd_channel_status)
        assert code == 0
        assert "BEACON_NO_BUS" in out
        assert "project" in out.lower()
        assert "global" in out.lower()
