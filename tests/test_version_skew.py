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
