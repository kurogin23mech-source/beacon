"""Tests for approved_actions syntax / matcher / SPEC frontmatter parser.

ms-60 / e-1339 — covers the T2 envelope action grammar agreed in ms-60 SPEC
§ 設計方針 4 and the ms-47 e-594 Phase 1 design alignment.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import approved_actions as aa  # noqa: E402


# ---------------------------------------------------------------------------
# validate_action — strict (T1/T3/T5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [
    "deploy",
    "deploy:v0.21.x",
    "task done:e-123",
    "doc add:scope=memo",
    "extract:profile:user-1",
])
def test_strict_accepts_enumerated_actions(action):
    aa.validate_action(action)  # must not raise


@pytest.mark.parametrize("action", [
    "deploy:*",
    "extract:profile:*",
    "task done:e-*",
])
def test_strict_rejects_wildcards(action):
    with pytest.raises(aa.ApprovedActionsError):
        aa.validate_action(action)


# ---------------------------------------------------------------------------
# validate_action — scope (T2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [
    "extract:profile:*",
    "task done:e-*",
    "deploy:v0.21.x",
    "doc add:scope=memo",
    "extract:profile:user-1",
    "deploy:*",                # T2 may wildcard the last segment
])
def test_scope_accepts_last_segment_wildcard(action):
    aa.validate_action(action, allow_last_segment_wildcard=True)


@pytest.mark.parametrize("action", [
    "*:profile:daily",         # verb wildcard
    "extract:*:user",          # middle wildcard
    "extract:profile**",       # double *
    "extract:profile:*x",      # * not at end of segment
    "extract:profile:a*b",     # * in middle of last segment
])
def test_scope_rejects_misplaced_wildcards(action):
    with pytest.raises(aa.ApprovedActionsError):
        aa.validate_action(action, allow_last_segment_wildcard=True)


@pytest.mark.parametrize("action", [
    "",
    " deploy",
    "deploy ",
    "deploy\n",
    "deploy:",
    ":deploy",
    "deploy::v1",
    'deploy:"v1"',             # quotes not in segment char class
    "deploy;rm -rf /",         # shell metachar
    "deploy/v1",               # forward slash not in segment char class
])
def test_scope_rejects_structural_garbage(action):
    with pytest.raises(aa.ApprovedActionsError):
        aa.validate_action(action, allow_last_segment_wildcard=True)


def test_validate_action_rejects_non_string():
    with pytest.raises(aa.ApprovedActionsError):
        aa.validate_action(123)  # type: ignore[arg-type]


def test_validate_actions_rejects_non_list():
    with pytest.raises(aa.ApprovedActionsError):
        aa.validate_actions("not a list")  # type: ignore[arg-type]


def test_validate_actions_empty_list_ok():
    aa.validate_actions([])  # T5 ping-only envelopes pass empty actions


# ---------------------------------------------------------------------------
# matches — wildcard-aware matcher
# ---------------------------------------------------------------------------

def test_matches_exact():
    assert aa.matches(["deploy:v0.21.x"], "deploy:v0.21.x")
    assert not aa.matches(["deploy:v0.21.x"], "deploy:v0.22.0")


def test_matches_last_segment_star():
    assert aa.matches(["extract:profile:*"], "extract:profile:user-1")
    assert aa.matches(["extract:profile:*"], "extract:profile:anything")


def test_matches_prefix_wildcard():
    assert aa.matches(["task done:e-*"], "task done:e-1")
    assert aa.matches(["task done:e-*"], "task done:e-9999")
    assert not aa.matches(["task done:e-*"], "task done:user-1")


def test_matches_requires_same_depth():
    # extract:profile:* matches 3-segment actions only.
    assert not aa.matches(["extract:profile:*"], "extract:profile:user:extra")
    assert not aa.matches(["extract:profile:*"], "extract:profile")


def test_matches_non_last_must_be_exact():
    assert not aa.matches(["extract:profile:*"], "deploy:profile:user")


def test_matches_rejects_wildcard_in_requested():
    # Defensive: requested action must be a literal, not a pattern.
    assert not aa.matches(["extract:profile:*"], "extract:profile:*")
    assert not aa.matches(["extract:profile:*"], "extract:*:user")


def test_matches_multiple_patterns():
    auth = ["extract:profile:*", "task done:e-*", "deploy:v0.21.x"]
    assert aa.matches(auth, "extract:profile:user-1")
    assert aa.matches(auth, "task done:e-123")
    assert aa.matches(auth, "deploy:v0.21.x")
    assert not aa.matches(auth, "deploy:v0.22.0")


def test_matches_empty_requested():
    assert not aa.matches(["*"], "")


# ---------------------------------------------------------------------------
# parse_spec_frontmatter
# ---------------------------------------------------------------------------

def test_parse_block_form():
    content = (
        "---\n"
        "scope: spec\n"
        "operation: op-1\n"
        "approved_actions:\n"
        "  - extract:profile:*\n"
        "  - task done:e-*\n"
        "---\n"
        "# Body\n"
    )
    assert aa.parse_spec_frontmatter(content) == [
        "extract:profile:*",
        "task done:e-*",
    ]


def test_parse_inline_form():
    content = (
        "---\n"
        "approved_actions: [extract:profile:*, task done:e-*]\n"
        "---\n"
        "body\n"
    )
    assert aa.parse_spec_frontmatter(content) == [
        "extract:profile:*",
        "task done:e-*",
    ]


def test_parse_inline_quoted():
    content = (
        "---\n"
        'approved_actions: ["deploy:v0.21.x", "task done:e-*"]\n'
        "---\n"
        "body\n"
    )
    assert aa.parse_spec_frontmatter(content) == [
        "deploy:v0.21.x",
        "task done:e-*",
    ]


def test_parse_empty_block():
    content = (
        "---\n"
        "approved_actions:\n"
        "---\n"
        "body\n"
    )
    assert aa.parse_spec_frontmatter(content) == []


def test_parse_empty_inline():
    content = (
        "---\n"
        "approved_actions: []\n"
        "---\n"
        "body\n"
    )
    assert aa.parse_spec_frontmatter(content) == []


def test_parse_no_frontmatter():
    assert aa.parse_spec_frontmatter("# Just a body\n") is None


def test_parse_frontmatter_no_field():
    content = (
        "---\n"
        "scope: spec\n"
        "milestone: ms-29\n"
        "---\n"
        "body\n"
    )
    assert aa.parse_spec_frontmatter(content) is None


def test_parse_inline_malformed_raises():
    # Inline value that isn't a YAML list — refuse rather than silently
    # interpret as a natural-language string.
    content = (
        "---\n"
        "approved_actions: deploy and stuff\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(aa.ApprovedActionsError):
        aa.parse_spec_frontmatter(content)


def test_parse_non_string_returns_none():
    assert aa.parse_spec_frontmatter(None) is None  # type: ignore[arg-type]
    assert aa.parse_spec_frontmatter(123) is None  # type: ignore[arg-type]
