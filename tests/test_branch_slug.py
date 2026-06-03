"""Unit tests for ``lib/branch.py`` — slug + branch name generation.

These tests lock in the SPEC requirement:
    "slug 生成 (MS title → ms-XX-<slug>, 正規化 + 記号除去 + 長さ truncate)"
from ms-51 / e-932.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import branch  # noqa: E402


# ---------------------------------------------------------------------------
# slugify_title()
# ---------------------------------------------------------------------------

def test_slugify_simple_english():
    assert branch.slugify_title("Add OAuth flow") == "add-oauth-flow"


def test_slugify_empty_falls_back_to_work():
    assert branch.slugify_title("") == "work"


def test_slugify_japanese_only_falls_back_to_work():
    """Pure-Japanese titles produce no ASCII tokens → fall back to 'work'.

    The MS id already carries the canonical identity (ms-51), so we don't
    need to invent a romanisation; a stable 'work' suffix is fine.
    """
    assert branch.slugify_title("挙手と自動ブランチ") == "work"


def test_slugify_mixed_keeps_ascii_tokens():
    """Mixed CJK + ASCII titles keep the ASCII tokens, drop CJK."""
    s = branch.slugify_title("MS への挙手 → 自動 branch + join")
    assert "branch" in s
    assert "join" in s
    assert "ms" in s
    # No raw CJK in the result.
    for ch in s:
        assert ord(ch) < 128


def test_slugify_collapses_dashes_and_punct():
    assert branch.slugify_title("Hello---world!!!") == "hello-world"


def test_slugify_truncates_long_titles():
    title = "alpha " * 50  # 250+ chars
    out = branch.slugify_title(title)
    # Should not exceed the soft cap (40 chars).
    assert len(out) <= 40
    # Should still start with the prefix tokens, not be empty.
    assert out.startswith("alpha")


def test_slugify_underscores_normalised_to_dash():
    assert branch.slugify_title("foo_bar baz") == "foo-bar-baz"


# ---------------------------------------------------------------------------
# ms_branch_name()
# ---------------------------------------------------------------------------

def test_branch_name_combines_id_and_slug():
    assert (
        branch.ms_branch_name("ms-51", "MS への挙手 → 自動 branch + join")
        == "ms-51-ms-branch-join"
    )


def test_branch_name_empty_title_uses_work():
    assert branch.ms_branch_name("ms-51", "") == "ms-51-work"


def test_branch_name_handles_missing_title_arg():
    # title is optional in the signature.
    assert branch.ms_branch_name("ms-7") == "ms-7-work"


def test_branch_name_defensive_against_garbled_id():
    """If the id is garbled, fall back to extracting digits — never raise."""
    assert branch.ms_branch_name("ms-ms-12", "foo bar") == "ms-12-foo-bar"


def test_branch_name_with_no_digits_in_id_falls_back_to_zero():
    assert branch.ms_branch_name("invalid", "feature x") == "ms-0-feature-x"
