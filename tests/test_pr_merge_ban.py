"""ms-92 / e-2169 — AI-session individual PR merge ban.

CORE doc ``pr-review-autonomy-boundary`` establishes that AI sessions
may approve / reject / request-changes on PRs but **must not** merge
individual PRs from inside an autonomous session. Merge belongs to the
user, so the AI cannot self-loop "I wrote it, I approved it, I merged it".

This test pins the ban + the 2 escape hatches:

  1. ``BEACON_PR_MERGE_USER_OVERRIDE=1`` — user explicit opt-in escape
     hatch for one-off merges (= user prompt phrased the merge directly).
  2. ``BEACON_SESSION_KIND=human`` — non-AI session declares itself
     human-driven (= straight terminal use).

There used to be a third: ``BEACON_TREK_FINALIZE_CONSENT=1``, exported by
the Trek-finalize Skill before it delegated the merge. ms-185 e-7069 deleted
that Skill and e-7079 removed the bypass, because nothing could set it any
more while it still decided whether the ban fired. The removal is pinned by
``test_trek_finalize_consent_bypass_is_gone`` below.

Unset env (= the AI-session default) must refuse with the multi-line
error that names both escape paths so a stuck operator can pick the
right one.

The tests drive ``_ai_session_merge_ban_active`` directly (= pure
function, no fixtures needed) and the Skill markdown is asserted
structurally for the same 3 escape hatches so the docs and code don't
drift apart.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import commands  # noqa: E402
import cmd_pr  # noqa: E402  (ms-127 e-4856: pr family moved here)


# ---------------------------------------------------------------------------
# _ai_session_merge_ban_active — pure decision function
# ---------------------------------------------------------------------------


def _with_env(env: dict):
    """Context-manager-flavoured env swap so the tests don't pollute global env."""
    return _EnvOverride(env)


class _EnvOverride:
    def __init__(self, env: dict):
        self._wanted = env
        self._prior: dict = {}

    def __enter__(self):
        for k, v in self._wanted.items():
            self._prior[k] = os.environ.get(k)
            if v is None:
                if k in os.environ:
                    del os.environ[k]
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, v in self._prior.items():
            if v is None:
                if k in os.environ:
                    del os.environ[k]
            else:
                os.environ[k] = v


def test_merge_ban_default_active_for_ai_session():
    """Default env (= no escape signals) → ban fires."""
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert cmd_pr._ai_session_merge_ban_active() is True


def test_merge_ban_bypassed_by_user_override():
    """Escape #2: BEACON_PR_MERGE_USER_OVERRIDE=1 (= explicit one-off opt-in)."""
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": "1",
        "BEACON_SESSION_KIND": None,
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False


def test_merge_ban_bypassed_by_human_session_kind():
    """Escape #3: BEACON_SESSION_KIND=human (= non-AI session, terminal use)."""
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "human",
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False


def test_merge_ban_session_kind_case_insensitive():
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "Human",  # any casing
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "  HUMAN  ",  # padded
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False


def test_merge_ban_unknown_session_kind_still_blocks():
    """Anything other than ``human`` → still treated as AI for safety."""
    for kind in ("claude", "anthropic", "ai", "bot", "agent", ""):
        with _with_env({
            "BEACON_TREK_FINALIZE_CONSENT": None,
            "BEACON_PR_MERGE_USER_OVERRIDE": None,
            "BEACON_SESSION_KIND": kind,
        }):
            assert cmd_pr._ai_session_merge_ban_active() is True, kind


def test_trek_finalize_consent_bypass_is_gone():
    """The removed bypass must not come back by accident (ms-185 e-7079).

    `BEACON_TREK_FINALIZE_CONSENT=1` used to switch the merge ban off. The
    Skill that exported it was deleted, nothing sets it any more, and the
    refusal message advertises only two bypass paths — so the variable must no
    longer have any effect. Pinned both ways: the helper is gone, and setting
    the variable does not disarm the ban.
    """
    assert not hasattr(cmd_pr, "_trek_finalize_consent_active")
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": "1",
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "ai",
    }):
        assert cmd_pr._ai_session_merge_ban_active() is True


