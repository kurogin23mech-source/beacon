"""Tests for ms-93 / e-2276 doctor PATH signals + --json output.

Pins the three Codex / Claude Code wrapper contract codes:
  - multiple-binaries
  - selected-version-too-old
  - bus-sessions-unavailable

and the structured shape of `beacon doctor --json` output. The Codex
wrapper / Skill gate reads this JSON to decide whether to inject
BEACON_BIN=<absolute path> or to surface a 1-line WARN to the user.
"""

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import commands


class TestParseVersion:
    """`_doctor_parse_version` must tolerate both shapes and bad input."""

    def test_parses_beacon_prefix(self):
        assert commands._doctor_parse_version("beacon 0.48.0") == (0, 48, 0)

    def test_parses_bare_semver(self):
        assert commands._doctor_parse_version("0.30.0") == (0, 30, 0)

    def test_returns_none_for_unknown(self):
        assert commands._doctor_parse_version("?") is None

    def test_returns_none_for_empty(self):
        assert commands._doctor_parse_version("") is None

    def test_version_too_old_orders_correctly(self):
        assert commands._doctor_parse_version("beacon 0.2.1") < commands._doctor_parse_version("0.30.0")
        assert commands._doctor_parse_version("beacon 0.48.0") > commands._doctor_parse_version("0.30.0")


class TestProbeMissingSubcommands:
    """`_doctor_probe_missing_subcommands` returns Unknown-command subs only."""

    def test_returns_empty_when_all_subs_present(self, monkeypatch):
        class _OK:
            stdout = "usage: beacon bus ..."
            stderr = ""

        monkeypatch.setattr(commands, "subprocess", _FakeSubprocess(returncode_pattern=_OK))
        assert commands._doctor_probe_missing_subcommands("/fake/beacon", ("bus", "sessions")) == ()

    def test_returns_subs_that_print_unknown(self, monkeypatch):
        def _run(args, **kwargs):
            return _FakeResult(stdout="", stderr=f"Unknown command: {args[1]}")
        monkeypatch.setattr(commands.subprocess, "run", _run)
        assert commands._doctor_probe_missing_subcommands("/fake/beacon", ("bus", "sessions")) == ("bus", "sessions")

    def test_subprocess_failure_does_not_create_false_positive(self, monkeypatch):
        def _run(args, **kwargs):
            raise OSError("ENOENT")
        monkeypatch.setattr(commands.subprocess, "run", _run)
        # Transport error → skip; we don't pretend the sub is missing.
        assert commands._doctor_probe_missing_subcommands("/fake/beacon", ("bus", "sessions")) == ()


class TestDoctorJsonOutputPathSignals:
    """End-to-end: cmd_doctor --json fires the right PATH signal codes."""

    def _run_doctor_json(self, monkeypatch, *, paths, primary_version, missing_subs):
        """Drive cmd_doctor under controlled probes and capture --json output."""
        monkeypatch.setattr(sys, "argv", ["beacon", "doctor", "--json"])
        monkeypatch.setattr(commands, "_find_all_on_path", lambda _name: list(paths))
        monkeypatch.setattr(commands, "_doctor_probe_beacon_version", lambda p: primary_version if p == paths[0] else "beacon 0.2.1")
        monkeypatch.setattr(commands, "_doctor_probe_missing_subcommands", lambda p, subs: tuple(missing_subs))
        # Suppress other doctor checks that read disk / network so the test
        # focuses on PATH signals. Each helper just returns "no warnings".
        monkeypatch.setattr(commands, "_doctor_check_skill_cli_drift", lambda home: [])
        monkeypatch.setattr(commands, "_doctor_check_project_staleness", lambda: [])
        monkeypatch.setattr(commands, "_doctor_check_claude_md_principle_marker", lambda: [])
        monkeypatch.setattr(commands, "_doctor_check_cloud_state_consistency", lambda: [])
        monkeypatch.setattr(commands, "_doctor_check_ms81_state_machine", lambda: [])
        # Skip env-var-gated path that probes the FS for cloud.json / config.json.
        monkeypatch.setenv("BEACON_DOCTOR_SKIP_CLOUD_STATE", "1")

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured)
        with pytest.raises(SystemExit):
            commands.cmd_doctor()
        return json.loads(captured.getvalue().strip().splitlines()[-1])

    def test_pe_scenario_fires_all_three_signals(self, monkeypatch, tmp_path, capsys):
        # PE / Codex repro: homebrew stale 0.2.1 first, fresh source 0.48.0 second.
        result = self._run_doctor_json(
            monkeypatch,
            paths=["/opt/homebrew/bin/beacon", "/Users/r/tools/beacon/bin/beacon"],
            primary_version="beacon 0.2.1",
            missing_subs=("bus", "sessions"),
        )
        codes = [s["code"] for s in result["signals"]]
        assert "multiple-binaries" in codes
        assert "selected-version-too-old" in codes
        assert "bus-sessions-unavailable" in codes
        # Rich metadata on the primary signal of interest.
        too_old = next(s for s in result["signals"] if s["code"] == "selected-version-too-old")
        assert too_old["metadata"]["primary"] == "/opt/homebrew/bin/beacon"
        assert too_old["metadata"]["primary_version"] == "beacon 0.2.1"
        bus_missing = next(s for s in result["signals"] if s["code"] == "bus-sessions-unavailable")
        assert bus_missing["metadata"]["missing_subcommands"] == ["bus", "sessions"]
        assert result["ok"] is False

    def test_healthy_primary_does_not_fire_path_signals(self, monkeypatch):
        result = self._run_doctor_json(
            monkeypatch,
            paths=["/Users/r/tools/beacon/bin/beacon"],
            primary_version="beacon 0.48.0",
            missing_subs=(),
        )
        codes = [s["code"] for s in result["signals"]]
        assert "selected-version-too-old" not in codes
        assert "bus-sessions-unavailable" not in codes
        # multiple-binaries also skipped when only one binary on PATH.
        assert "multiple-binaries" not in codes

    def test_single_old_binary_still_fires_version_and_subcommand_signals(self, monkeypatch):
        # An old solo install is equally broken — fire even without shadowing.
        result = self._run_doctor_json(
            monkeypatch,
            paths=["/opt/homebrew/bin/beacon"],
            primary_version="beacon 0.2.1",
            missing_subs=("bus", "sessions"),
        )
        codes = [s["code"] for s in result["signals"]]
        assert "multiple-binaries" not in codes
        assert "selected-version-too-old" in codes
        assert "bus-sessions-unavailable" in codes

    def test_min_version_env_override(self, monkeypatch):
        # User can raise the floor via env (= forward compat for major bumps).
        monkeypatch.setenv("BEACON_DOCTOR_MIN_VERSION", "0.50.0")
        result = self._run_doctor_json(
            monkeypatch,
            paths=["/Users/r/tools/beacon/bin/beacon"],
            primary_version="beacon 0.48.0",
            missing_subs=(),
        )
        codes = [s["code"] for s in result["signals"]]
        assert "selected-version-too-old" in codes


class TestDoctorJsonShape:
    """Schema pin for the Codex wrapper / Skill contract."""

    def test_signal_has_required_keys(self, monkeypatch):
        # Use the same scaffold as the e2e tests above.
        monkeypatch.setattr(sys, "argv", ["beacon", "doctor", "--json"])
        monkeypatch.setattr(commands, "_find_all_on_path", lambda _name: ["/opt/homebrew/bin/beacon"])
        monkeypatch.setattr(commands, "_doctor_probe_beacon_version", lambda p: "beacon 0.2.1")
        monkeypatch.setattr(commands, "_doctor_probe_missing_subcommands", lambda p, subs: ("bus",))
        monkeypatch.setattr(commands, "_doctor_check_skill_cli_drift", lambda home: [])
        monkeypatch.setattr(commands, "_doctor_check_project_staleness", lambda: [])
        monkeypatch.setattr(commands, "_doctor_check_claude_md_principle_marker", lambda: [])
        monkeypatch.setattr(commands, "_doctor_check_cloud_state_consistency", lambda: [])
        monkeypatch.setattr(commands, "_doctor_check_ms81_state_machine", lambda: [])
        monkeypatch.setenv("BEACON_DOCTOR_SKIP_CLOUD_STATE", "1")

        captured = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured)
        with pytest.raises(SystemExit):
            commands.cmd_doctor()
        result = json.loads(captured.getvalue().strip().splitlines()[-1])

        assert "ok" in result
        assert "signals" in result
        assert isinstance(result["signals"], list)
        for s in result["signals"]:
            assert "code" in s
            assert "category" in s
            assert "severity" in s
            assert "message" in s


# ------------------------------------------------------------------ #
# Test helpers — fake subprocess for probe tests
# ------------------------------------------------------------------ #


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeSubprocess:
    """Stand-in for commands.subprocess used by the probe tests above."""

    def __init__(self, returncode_pattern):
        self._pattern = returncode_pattern

    def run(self, args, **kwargs):
        return _FakeResult(stdout=self._pattern.stdout, stderr=self._pattern.stderr)
