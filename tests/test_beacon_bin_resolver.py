"""Tests for ms-93 / e-2276 BEACON_BIN resolver (lib/beacon_bin_resolver.py).

Pins the resolution policy + hard/soft verdict split that the Codex /
Claude Code install Skills depend on. The 2026-06-25 Codex dogfood
confirmed three load-bearing behaviours these tests must keep true:

1. Selected binary's own ``bus``/``sessions`` probe is the authoritative
   capability check — doctor's PATH signals are advisory only.
2. ``doctor --json`` failing to parse is a soft warn (= not a hard fail)
   when the candidate still has bus/sessions; it just degrades
   observability.
3. ``env BEACON_BIN`` → install_root/bin → PATH fallback is the
   resolution order; we must never silently demote the env's choice.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import beacon_bin_resolver as resolver


# ------------------------------------------------------------------ #
# Pure helpers
# ------------------------------------------------------------------ #


class TestParseVersion:
    def test_bare_semver(self):
        assert resolver._parse_version_tuple("0.48.0") == (0, 48, 0)

    def test_beacon_prefix(self):
        assert resolver._parse_version_tuple("beacon 0.48.0") == (0, 48, 0)

    def test_unknown_returns_none(self):
        assert resolver._parse_version_tuple("?") is None

    def test_empty_returns_none(self):
        assert resolver._parse_version_tuple("") is None

    def test_orders_below_floor(self):
        assert resolver._parse_version_tuple("beacon 0.2.1") < (0, 30, 0)
        assert resolver._parse_version_tuple("beacon 0.48.0") > (0, 30, 0)


# ------------------------------------------------------------------ #
# Candidate ordering
# ------------------------------------------------------------------ #


class TestResolveCandidates:
    def test_env_first(self):
        cands = resolver._resolve_candidates(
            env_bin="/explicit/env/beacon",
            install_root="/root",
            allow_path_fallback=False,
        )
        assert cands[0] == ("/explicit/env/beacon", "env")

    def test_install_root_second(self):
        cands = resolver._resolve_candidates(
            env_bin="/explicit/env/beacon",
            install_root="/root",
            allow_path_fallback=False,
        )
        assert cands[1] == ("/root/bin/beacon", "install_root")

    def test_path_fallback_last_when_allowed(self, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: "/usr/bin/beacon")
        cands = resolver._resolve_candidates(
            env_bin="",
            install_root="/root",
            allow_path_fallback=True,
        )
        # install_root + path
        assert ("/root/bin/beacon", "install_root") in cands
        assert ("/usr/bin/beacon", "path") in cands

    def test_path_excluded_when_disallowed(self, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: "/usr/bin/beacon")
        cands = resolver._resolve_candidates(
            env_bin="",
            install_root="/root",
            allow_path_fallback=False,
        )
        assert all(src != "path" for _, src in cands)

    def test_dedup_when_env_equals_install_root(self):
        cands = resolver._resolve_candidates(
            env_bin="/root/bin/beacon",
            install_root="/root",
            allow_path_fallback=False,
        )
        # env wins, install_root duplicate dropped.
        assert cands == [("/root/bin/beacon", "env")]


# ------------------------------------------------------------------ #
# Categorisation policy
# ------------------------------------------------------------------ #


def _probe_with(**kwargs) -> resolver.ProbeResult:
    """Build a ProbeResult quickly for categoriser tests."""
    return resolver.ProbeResult(
        bin_path="/fake/beacon",
        source="env",
        json_ok=kwargs.get("json_ok", True),
        version_raw=kwargs.get("version_raw", "beacon 0.48.0"),
        path_signals=kwargs.get("path_signals", []),
        missing_subcommands=kwargs.get("missing_subcommands", ()),
        error=kwargs.get("error", ""),
    )


class TestCategorise:
    def test_missing_subcommand_is_hard_fail(self):
        probe = _probe_with(missing_subcommands=("bus", "sessions"))
        hard, _soft = resolver._categorise_signals(probe)
        assert "bus-sessions-unavailable" in hard

    def test_old_version_is_hard_fail(self):
        probe = _probe_with(version_raw="beacon 0.2.1")
        hard, _soft = resolver._categorise_signals(probe)
        assert "selected-version-too-old" in hard

    def test_fresh_version_no_hard_fail(self):
        probe = _probe_with(version_raw="beacon 0.48.0")
        hard, _soft = resolver._categorise_signals(probe)
        assert "selected-version-too-old" not in hard

    def test_doctor_json_unavailable_is_soft_warn(self):
        probe = _probe_with(error="doctor-json-unavailable", json_ok=False)
        hard, soft = resolver._categorise_signals(probe)
        # Still soft only — bus/sessions present (missing empty), version fresh.
        assert "doctor-json-unavailable" in soft
        assert hard == []

    def test_path_multiple_binaries_is_soft_only(self):
        signal = {"code": "multiple-binaries", "category": "PATH"}
        probe = _probe_with(path_signals=[signal])
        hard, soft = resolver._categorise_signals(probe)
        assert "multiple-binaries" in soft
        assert hard == []

    def test_path_signals_about_other_binary_dont_block(self):
        # doctor reports PATH primary has these problems, but our
        # selected binary is the install_root one (already filtered up
        # the stack). The categoriser must NOT promote them to hard.
        signals = [
            {"code": "selected-version-too-old", "category": "PATH"},
            {"code": "bus-sessions-unavailable", "category": "PATH"},
        ]
        probe = _probe_with(path_signals=signals)
        hard, _soft = resolver._categorise_signals(probe)
        # No subcommand missing on THIS bin, version fresh, so no hard.
        assert hard == []


# ------------------------------------------------------------------ #
# End-to-end: resolve_beacon_bin with monkeypatched probe
# ------------------------------------------------------------------ #


def _make_probe(bin_path, source, **overrides):
    return resolver.ProbeResult(
        bin_path=bin_path,
        source=source,
        json_ok=overrides.get("json_ok", True),
        version_raw=overrides.get("version_raw", "beacon 0.48.0"),
        path_signals=overrides.get("path_signals", []),
        missing_subcommands=overrides.get("missing_subcommands", ()),
        error=overrides.get("error", ""),
    )


class TestResolveBeaconBin:
    def test_env_fresh_wins(self, monkeypatch):
        def fake_probe(bin_path, source, timeout=5.0):
            return _make_probe(bin_path, source)
        monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
        result = resolver.resolve_beacon_bin(
            env_bin="/explicit/beacon",
            install_root="/root",
            allow_path_fallback=False,
        )
        assert result.verdict == "ok"
        assert result.selected_bin == "/explicit/beacon"
        assert result.selected_source == "env"

    def test_env_hard_fail_falls_through_to_install_root(self, monkeypatch):
        def fake_probe(bin_path, source, timeout=5.0):
            if source == "env":
                return _make_probe(bin_path, source,
                                   version_raw="beacon 0.2.1",
                                   missing_subcommands=("bus", "sessions"))
            return _make_probe(bin_path, source)
        monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
        result = resolver.resolve_beacon_bin(
            env_bin="/stale/beacon",
            install_root="/root",
            allow_path_fallback=False,
        )
        assert result.verdict == "ok"
        assert result.selected_source == "install_root"

    def test_pe_codex_scenario_hard_fails(self, monkeypatch):
        # Every candidate is the same stale 0.2.1 (= user has no install_root
        # in scope and PATH primary is stale).
        def fake_probe(bin_path, source, timeout=5.0):
            return _make_probe(bin_path, source,
                               version_raw="beacon 0.2.1",
                               missing_subcommands=("bus", "sessions"),
                               error="doctor-json-unavailable",
                               json_ok=False)
        monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
        result = resolver.resolve_beacon_bin(
            env_bin="/opt/homebrew/bin/beacon",
            install_root="",
            allow_path_fallback=False,
        )
        assert result.verdict == "hard_fail"
        assert "bus-sessions-unavailable" in result.hard_fail_codes
        assert "selected-version-too-old" in result.hard_fail_codes
        # Advice steers user to set BEACON_BIN.
        assert "BEACON_BIN" in result.advice

    def test_multiple_binaries_soft_warn_does_not_block(self, monkeypatch):
        def fake_probe(bin_path, source, timeout=5.0):
            return _make_probe(
                bin_path, source,
                path_signals=[{"code": "multiple-binaries", "category": "PATH"}],
            )
        monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
        result = resolver.resolve_beacon_bin(
            env_bin="/explicit/beacon",
            install_root="",
            allow_path_fallback=False,
        )
        assert result.verdict == "soft_warn"
        assert "multiple-binaries" in result.soft_warn_codes

    def test_no_candidate_when_nothing_resolves(self, monkeypatch):
        monkeypatch.setattr(resolver.shutil, "which", lambda _name: None)

        def fake_probe(bin_path, source, timeout=5.0):
            return _make_probe(bin_path, source, error="candidate-not-executable")
        monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
        result = resolver.resolve_beacon_bin(
            env_bin="",
            install_root="",
            allow_path_fallback=True,
        )
        assert result.verdict == "no-candidate"
        assert result.selected_bin == ""

    def test_doctor_json_unavailable_alone_is_not_hard_fail(self, monkeypatch):
        # Candidate has bus/sessions and fresh version, but doctor --json
        # not understood. Should be selected (= usable) with soft warn.
        def fake_probe(bin_path, source, timeout=5.0):
            return _make_probe(
                bin_path, source,
                version_raw="beacon 0.30.0",  # at the floor, just OK
                error="doctor-json-unavailable",
                json_ok=False,
            )
        monkeypatch.setattr(resolver, "_probe_candidate", fake_probe)
        result = resolver.resolve_beacon_bin(
            env_bin="/borderline/beacon",
            install_root="",
            allow_path_fallback=False,
        )
        assert result.verdict == "soft_warn"
        assert "doctor-json-unavailable" in result.soft_warn_codes


# ------------------------------------------------------------------ #
# Direct subprocess probe — integration with a real fake `beacon`
# ------------------------------------------------------------------ #


@pytest.fixture
def fake_beacon_factory(tmp_path):
    """Create a fake `beacon` script that emits controlled stdout per subcommand."""
    def _factory(version_line: str, doctor_stdout: str,
                 bus_known: bool, sessions_known: bool) -> str:
        script_dir = tmp_path / f"fake-{version_line.replace(' ', '-')}"
        script_dir.mkdir(parents=True, exist_ok=True)
        path = script_dir / "beacon"
        path.write_text(
            f"#!/usr/bin/env python3\n"
            f"import sys\n"
            f"args = sys.argv[1:]\n"
            f"if args and args[0] == '--version':\n"
            f"    print({version_line!r})\n"
            f"elif args and args[0] == 'doctor' and '--json' in args:\n"
            f"    print({doctor_stdout!r})\n"
            f"elif args and args[0] == 'bus':\n"
            f"    print({'usage: beacon bus' if bus_known else 'Unknown command: bus'!r})\n"
            f"    sys.exit(0 if {bus_known!r} else 1)\n"
            f"elif args and args[0] == 'sessions':\n"
            f"    print({'usage: beacon sessions' if sessions_known else 'Unknown command: sessions'!r})\n"
            f"    sys.exit(0 if {sessions_known!r} else 1)\n"
            f"else:\n"
            f"    print('OK: all checks passed.')\n"
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)
    return _factory


class TestProbeCandidateRealSubprocess:
    """Integration: probe a real (fake) `beacon` script end-to-end."""

    def test_fresh_with_json_doctor(self, fake_beacon_factory):
        doctor_json = json.dumps({"ok": True, "signals": []})
        bin_path = fake_beacon_factory(
            version_line="beacon 0.48.0",
            doctor_stdout=doctor_json,
            bus_known=True,
            sessions_known=True,
        )
        probe = resolver._probe_candidate(bin_path, "env")
        assert probe.json_ok is True
        assert probe.missing_subcommands == ()
        assert probe.version_raw == "beacon 0.48.0"

    def test_stale_no_json_doctor(self, fake_beacon_factory):
        bin_path = fake_beacon_factory(
            version_line="beacon 0.2.1",
            doctor_stdout="OK: all checks passed.",  # NOT JSON
            bus_known=False,
            sessions_known=False,
        )
        probe = resolver._probe_candidate(bin_path, "env")
        assert probe.json_ok is False
        assert probe.error == "doctor-json-unavailable"
        assert probe.missing_subcommands == ("bus", "sessions")
        # Categorise: hard fail on both subcommand absence AND version.
        hard, soft = resolver._categorise_signals(probe)
        assert "bus-sessions-unavailable" in hard
        assert "selected-version-too-old" in hard
        assert "doctor-json-unavailable" in soft
