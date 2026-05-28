"""Unit tests for release.py README / CHANGELOG auto-update (e-582).

These tests cover only the *file-mutating* helper functions, which are pure
enough to test on a tmp directory. The full release pipeline is integration-
level and out of scope for unit testing (it touches network and brew).
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_RELEASE_PY = os.path.normpath(os.path.join(_HERE, "..", "scripts", "release.py"))


def _load_release_module():
    """release.py imports lib/version_rules.py at function-call time, not
    module import — so we can load it directly without side effects."""
    spec = importlib.util.spec_from_file_location("release_mod", _RELEASE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def release_mod():
    return _load_release_module()


# ---------------------------------------------------------------------------
# _update_readme_version
# ---------------------------------------------------------------------------

def test_readme_update_replaces_marker_block(tmp_path, release_mod):
    readme = tmp_path / "README.md"
    readme.write_text("# Beacon\n\nCurrent: <!-- BEACON_VERSION -->v0.3.0<!-- /BEACON_VERSION -->\n")
    changed = release_mod._update_readme_version(str(tmp_path), "0.4.0")
    assert changed is True
    assert "v0.4.0" in readme.read_text()
    assert "v0.3.0" not in readme.read_text()


def test_readme_update_replaces_shields_io_badge(tmp_path, release_mod):
    readme = tmp_path / "README.md"
    readme.write_text("![version](https://img.shields.io/badge/version-v0.3.0-blue)\n")
    changed = release_mod._update_readme_version(str(tmp_path), "0.4.0")
    assert changed is True
    assert "version-v0.4.0-blue" in readme.read_text()


def test_readme_update_returns_false_when_no_marker(tmp_path, release_mod):
    """README that doesn't opt in to the badge convention must not be
    modified — that would silently rewrite the maintainer's prose."""
    readme = tmp_path / "README.md"
    readme.write_text("# Beacon\n\nNo version markup here.\n")
    changed = release_mod._update_readme_version(str(tmp_path), "0.4.0")
    assert changed is False
    assert readme.read_text() == "# Beacon\n\nNo version markup here.\n"


def test_readme_update_returns_false_when_missing(tmp_path, release_mod):
    """No README.md at all → silent no-op, never crash."""
    changed = release_mod._update_readme_version(str(tmp_path), "0.4.0")
    assert changed is False


def test_readme_update_dry_run_does_not_write(tmp_path, release_mod):
    readme = tmp_path / "README.md"
    original = "Current: <!-- BEACON_VERSION -->v0.3.0<!-- /BEACON_VERSION -->\n"
    readme.write_text(original)
    changed = release_mod._update_readme_version(str(tmp_path), "0.4.0", dry_run=True)
    assert changed is True  # signals "would have changed"
    assert readme.read_text() == original  # but file is untouched


# ---------------------------------------------------------------------------
# _update_changelog
# ---------------------------------------------------------------------------

def _info_with_commits(commits):
    return {"commits": [{"hash": h[:7], "message": m} for h, m in commits]}


def test_changelog_creates_file_when_missing(tmp_path, release_mod):
    info = _info_with_commits([
        ("abcdef1", "feat(cli): beacon update one-shot upgrade"),
        ("234567a", "feat(skill): pr-create dialogue Skill"),
    ])
    changed = release_mod._update_changelog(str(tmp_path), "v0.4.0", info)
    assert changed is True
    content = (tmp_path / "CHANGELOG.md").read_text()
    assert "# Changelog" in content
    assert "## [v0.4.0]" in content
    assert "feat(cli): beacon update" in content


def test_changelog_prepends_to_existing(tmp_path, release_mod):
    existing = (
        "# Changelog\n\n"
        "Intro paragraph.\n\n"
        "## [v0.3.0] - 2026-05-01\n"
        "- old stuff\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(existing)
    info = _info_with_commits([("aaa1111", "feat: new")])
    changed = release_mod._update_changelog(str(tmp_path), "v0.4.0", info)
    assert changed is True
    content = (tmp_path / "CHANGELOG.md").read_text()
    # New entry must come before the old one
    new_pos = content.index("[v0.4.0]")
    old_pos = content.index("[v0.3.0]")
    assert new_pos < old_pos
    assert "feat: new" in content


def test_changelog_idempotent_when_version_already_present(tmp_path, release_mod):
    """Re-running release.py for the same version must not double-insert."""
    existing = (
        "# Changelog\n\n"
        "## [v0.4.0] - 2026-05-28\n"
        "- already there\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(existing)
    info = _info_with_commits([("aaa1111", "feat: new")])
    changed = release_mod._update_changelog(str(tmp_path), "v0.4.0", info)
    assert changed is False
    assert (tmp_path / "CHANGELOG.md").read_text() == existing


def test_changelog_handles_no_info(tmp_path, release_mod):
    """An explicit --version release has info=None; entry should still be
    created with just the version header (no commit bullet list)."""
    changed = release_mod._update_changelog(str(tmp_path), "v0.4.0", None)
    assert changed is True
    content = (tmp_path / "CHANGELOG.md").read_text()
    assert "## [v0.4.0]" in content
