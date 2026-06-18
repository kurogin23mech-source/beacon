"""Tests for `beacon doctor` entry-writing-principle marker detection (ms-68 / e-1640).

Verifies that doctor surfaces a WARN [entry-writing-principle] line when
CLAUDE.md exists but lacks the `<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->`
marker, and stays silent when the marker is present or when CLAUDE.md is
not in cwd at all.

The principle (= `entry-writing-principle` CORE doc) must live in CLAUDE.md
so it is in context for every session / every tool call. If a project's
CLAUDE.md was hand-edited and the marker was dropped, or the section was
never installed (legacy project predating ms-68), AI silently breaks the
reader-first / loanword-tier / ID-context / no-truncation rules. This check
catches that case at warning level (never blocking).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")
sys.path.insert(0, _LIB)


@pytest.fixture
def in_tmp_cwd():
    """Run the check in a clean temp cwd so other CLAUDE.md files don't leak in."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    original_cwd = os.getcwd()
    os.chdir(root)
    try:
        yield root
    finally:
        os.chdir(original_cwd)
        tmp.cleanup()


def test_warns_when_marker_absent(in_tmp_cwd):
    """CLAUDE.md exists but no marker = warning surfaced."""
    from commands import _doctor_check_claude_md_principle_marker

    (in_tmp_cwd / "CLAUDE.md").write_text(
        "# Project\n\n## Beacon Project Management\n\nsome rules here without the marker\n",
        encoding="utf-8",
    )
    warnings = _doctor_check_claude_md_principle_marker()
    assert len(warnings) == 1
    w = warnings[0]
    assert "WARN [entry-writing-principle]" in w
    assert "<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->" in w
    # AC: includes the repair hint
    assert "beacon common-setup" in w


def test_silent_when_marker_present(in_tmp_cwd):
    """CLAUDE.md exists with marker = no warning."""
    from commands import _doctor_check_claude_md_principle_marker

    (in_tmp_cwd / "CLAUDE.md").write_text(
        "# Project\n\n"
        "<!-- BEACON_ENTRY_WRITING_PRINCIPLE -->\n"
        "principle section body\n"
        "<!-- /BEACON_ENTRY_WRITING_PRINCIPLE -->\n",
        encoding="utf-8",
    )
    warnings = _doctor_check_claude_md_principle_marker()
    assert warnings == []


def test_silent_when_claude_md_absent(in_tmp_cwd):
    """No CLAUDE.md = not a beacon-managed project from this cwd, skip silently."""
    from commands import _doctor_check_claude_md_principle_marker

    # No CLAUDE.md in cwd
    assert not (in_tmp_cwd / "CLAUDE.md").exists()
    warnings = _doctor_check_claude_md_principle_marker()
    assert warnings == []
