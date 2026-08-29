"""Tests for lib/version_skew.py (= ms-93 / e-3121).

The detector must be fail-open (never raise) and only warn on genuine skew:
multiple beacon versions on PATH, or a running daemon whose version differs from
the current CLI. These are the "動いているようで別物を見ている" cases Codex
flagged as a launch blocker outside A/B/C/D.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import version_skew as vs  # noqa: E402


def test_no_skew_single_binary():
    binaries = [{"path": "/usr/local/bin/beacon", "version_raw": "beacon 0.56.1"}]
    assert vs.format_skew_report("0.56.1", binaries) == []
    assert vs.has_skew("0.56.1", binaries) is False


def test_multiple_binary_versions_warn():
    binaries = [
        {"bin_path": "/Users/x/tools/beacon/bin/beacon", "version_raw": "beacon 0.56.1"},
        {"bin_path": "/opt/homebrew/bin/beacon", "version_raw": "beacon 0.2.1"},
    ]
    report = vs.format_skew_report("0.56.1", binaries)
    assert report, "expected a skew warning for two PATH versions"
    joined = "\n".join(report)
    assert "0.56.1" in joined and "0.2.1" in joined
    # both offending paths must be listed so the user can act
    assert "/opt/homebrew/bin/beacon" in joined
    assert vs.has_skew("0.56.1", binaries) is True


def test_same_version_on_multiple_paths_is_not_skew():
    # Two binaries but the SAME version = harmless (e.g. symlink); no warning.
    binaries = [
        {"path": "/a/beacon", "version_raw": "beacon 0.56.1"},
        {"path": "/b/beacon", "version_raw": "beacon 0.56.1"},
    ]
    assert vs.format_skew_report("0.56.1", binaries) == []


def test_daemon_version_mismatch_warns():
    report = vs.format_skew_report("0.56.1", [], daemon_version="0.53.1")
    assert report
    joined = "\n".join(report)
    assert "daemon=0.53.1" in joined and "CLI=0.56.1" in joined


def test_daemon_version_match_no_warning():
    assert vs.format_skew_report("0.56.1", [], daemon_version="0.56.1") == []


def test_bare_version_strings_accepted():
    # A row may carry a bare "0.56.1" (no "beacon " prefix).
    binaries = [
        {"path": "/a/beacon", "version": "0.56.1"},
        {"path": "/b/beacon", "version": "0.40.0"},
    ]
    assert vs.has_skew("0.56.1", binaries) is True


def test_prefixed_cli_version_vs_bare_hook_is_not_skew():
    """ms-160 e-5800/parity: `beacon --version` prints `beacon 0.62.1` while the
    hook stamps a bare `0.62.1`. Those are the SAME version — comparing them raw
    used to report a phantom skew on every bcodex launch."""
    assert vs.format_skew_report("beacon 0.62.1", [], hook_version="0.62.1") == []
    assert vs.format_skew_report("beacon 0.62.1", [], daemon_version="0.62.1") == []


def test_prefixed_cli_version_still_detects_real_skew():
    """Normalization must not mask a genuine mismatch."""
    report = vs.format_skew_report("beacon 0.62.1", [], hook_version="0.61.0")
    assert report and "hook=0.61.0" in "\n".join(report)


def test_fail_open_on_malformed_input():
    # Malformed rows / missing fields must not raise — they degrade to skipped.
    assert vs.format_skew_report("0.56.1", [None, {}, {"path": "/x"}, 42]) == []
    assert vs.format_skew_report("", None, daemon_version="") == []
    # A single readable version among junk is not "multiple" → no warning.
    assert vs.format_skew_report(
        "0.56.1", [{"version_raw": "beacon 0.56.1"}, {}, None]
    ) == []


def test_distinct_versions_helper():
    assert vs.distinct_versions([
        {"version_raw": "beacon 0.56.1"},
        {"version_raw": "beacon 0.2.1"},
        {"version": "0.56.1"},
    ]) == ["0.2.1", "0.56.1"]


# ---------------------------------------------------------------------------
# e-3135: daemon-version read-back from the receive-loop session pointer.
# ---------------------------------------------------------------------------


def _write_daemon_state(cwd, *, pid, beacon_version):
    import json

    base = cwd / ".beacon" / "codex"
    base.mkdir(parents=True, exist_ok=True)
    (base / "receive-loop.pid").write_text(str(pid))
    (base / "receive-loop.session.json").write_text(
        json.dumps({"session_id": "x", "project_id": "p",
                    "beacon_version": beacon_version})
    )


def test_probe_daemon_version_live_pid(tmp_path):
    """A live daemon's stamped version is returned (pid of this test process)."""
    _write_daemon_state(tmp_path, pid=os.getpid(), beacon_version="0.2.1")
    assert vs._probe_daemon_version(str(tmp_path)) == "0.2.1"


def test_probe_daemon_version_dead_pid_is_silent(tmp_path):
    """A stale pointer from a dead daemon must not report a version."""
    _write_daemon_state(tmp_path, pid=999999, beacon_version="0.2.1")
    assert vs._probe_daemon_version(str(tmp_path)) == ""


def test_probe_daemon_version_missing_files(tmp_path):
    assert vs._probe_daemon_version(str(tmp_path)) == ""


def test_main_cwd_autofills_daemon_version_and_warns(tmp_path, capsys):
    """`--cwd` auto-discovers a live daemon's version and surfaces the skew."""
    _write_daemon_state(tmp_path, pid=os.getpid(), beacon_version="0.2.1")
    rc = vs.main(["--current-version", "0.56.1", "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon=0.2.1" in out and "CLI=0.56.1" in out


def test_main_explicit_daemon_version_beats_cwd(tmp_path, capsys):
    """An explicit --daemon-version is not overridden by --cwd discovery."""
    _write_daemon_state(tmp_path, pid=os.getpid(), beacon_version="0.2.1")
    rc = vs.main([
        "--current-version", "0.56.1",
        "--daemon-version", "0.55.0",
        "--cwd", str(tmp_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "daemon=0.55.0" in out


# ---------------------------------------------------------------------------
# e-3135 AC 3: hook-version skew (Codex UserPromptSubmit hook vs CLI).
# ---------------------------------------------------------------------------


def test_hook_version_skew_warns():
    report = vs.format_skew_report("0.56.1", [], hook_version="0.2.1")
    joined = "\n".join(report)
    assert "hook=0.2.1" in joined and "CLI=0.56.1" in joined


def test_hook_version_match_no_warning():
    assert vs.format_skew_report("0.56.1", [], hook_version="0.56.1") == []


def test_read_beacon_version_from_root_both_layouts(tmp_path):
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "lib" / "commands.py").write_text('__version__ = "1.2.3"\n')
    assert vs._read_beacon_version_from_root(src) == "1.2.3"
    whl = tmp_path / "beacon_cli"
    (whl / "_bundled_lib").mkdir(parents=True)
    (whl / "_bundled_lib" / "commands.py").write_text('__version__ = "4.5.6"\n')
    assert vs._read_beacon_version_from_root(whl) == "4.5.6"
    assert vs._read_beacon_version_from_root(tmp_path / "none") == ""


def _write_codex_hook(codex_home, install_root):
    import json

    codex_home.mkdir(parents=True, exist_ok=True)
    script = install_root / "scripts" / "codex-inbox-hook.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n")
    (codex_home / "hooks.json").write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [{"hooks": [{
            "type": "command",
            "command": f"/usr/bin/python3 {script} --cwd /x",
            "timeout": 10,
        }]}]}
    }))


def test_probe_hook_version_reads_install_root(tmp_path, monkeypatch):
    """Derives the hook's install version from $CODEX_HOME/hooks.json."""
    install_root = tmp_path / "oldinstall"
    (install_root / "lib").mkdir(parents=True)
    (install_root / "lib" / "commands.py").write_text('__version__ = "0.2.1"\n')
    codex_home = tmp_path / "codexhome"
    _write_codex_hook(codex_home, install_root)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert vs._probe_hook_version() == "0.2.1"


def test_probe_hook_version_no_hooks_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    assert vs._probe_hook_version() == ""


def test_main_no_hook_probe_suppresses(tmp_path, monkeypatch, capsys):
    """--no-hook-probe skips the global hooks.json read."""
    install_root = tmp_path / "oldinstall"
    (install_root / "lib").mkdir(parents=True)
    (install_root / "lib" / "commands.py").write_text('__version__ = "0.2.1"\n')
    codex_home = tmp_path / "codexhome"
    _write_codex_hook(codex_home, install_root)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    rc = vs.main(["--current-version", "0.56.1", "--no-hook-probe"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "hook=" not in out
