"""ms-119 / e-4006 + e-4008 — attainment gate structural guards.

The independent 目的達成 review of ms-119 (fable, context-zero, real-code) found
that "構造発火・非迂回" did not hold on the approval side (AC2):

  - gap(a) / e-4008: the completion gate was *opt-in*. A bare
    `beacon milestone done` (no --review) applied the completion and only left
    an advisory nudge, so the blocking review was skippable.
  - gap(b) / e-4006: `beacon target approve` had no human-limited guard, so an
    AI session could assemble evidence AND press the approve button on the same
    target — the exact self-approval this session did to ms-119.

Both guards mirror the PR merge ban (``_ai_session_merge_ban_active``): the ban
fires by default for AI sessions (unset ``BEACON_SESSION_KIND`` = AI) and is
bypassed only by an explicit human signal (a per-action USER_OVERRIDE=1 or
``BEACON_SESSION_KIND=human``). These tests pin the decision functions directly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

import commands  # noqa: E402


class _EnvOverride:
    """Context-manager env swap so tests don't pollute global env."""

    def __init__(self, env: dict):
        self._wanted = env
        self._prior: dict = {}

    def __enter__(self):
        for k, v in self._wanted.items():
            self._prior[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, v in self._prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _with_env(env: dict):
    return _EnvOverride(env)


# ---------------------------------------------------------------------------
# e-4006 — _ai_session_attainment_approve_ban_active
# ---------------------------------------------------------------------------


def test_approve_ban_default_active_for_ai_session():
    """Unset session kind (= AI default) → self-approval refused."""
    with _with_env({
        "BEACON_TARGET_APPROVE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_attainment_approve_ban_active() is True


def test_approve_ban_bypassed_by_user_override():
    with _with_env({
        "BEACON_TARGET_APPROVE_USER_OVERRIDE": "1",
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_attainment_approve_ban_active() is False


def test_approve_ban_bypassed_by_human_session_kind():
    with _with_env({
        "BEACON_TARGET_APPROVE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "human",
    }):
        assert commands._ai_session_attainment_approve_ban_active() is False


def test_approve_ban_human_kind_case_insensitive():
    for kind in ("Human", "  HUMAN  "):
        with _with_env({
            "BEACON_TARGET_APPROVE_USER_OVERRIDE": None,
            "BEACON_SESSION_KIND": kind,
        }):
            assert commands._ai_session_attainment_approve_ban_active() is False, kind


def test_approve_ban_unknown_kind_still_blocks():
    """Anything but `human` → treated as AI (self-approval refused)."""
    for kind in ("claude", "ai", "bot", "agent", ""):
        with _with_env({
            "BEACON_TARGET_APPROVE_USER_OVERRIDE": None,
            "BEACON_SESSION_KIND": kind,
        }):
            assert commands._ai_session_attainment_approve_ban_active() is True, kind


# ---------------------------------------------------------------------------
# e-4008 — _ai_session_direct_completion_ban_active
# ---------------------------------------------------------------------------


def test_completion_ban_default_active_for_ai_session():
    """Unset session kind (= AI default) → gate-bypassing direct done refused."""
    with _with_env({
        "BEACON_TARGET_COMPLETE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_direct_completion_ban_active() is True


def test_completion_ban_bypassed_by_user_override():
    with _with_env({
        "BEACON_TARGET_COMPLETE_USER_OVERRIDE": "1",
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_direct_completion_ban_active() is False


def test_completion_ban_bypassed_by_human_session_kind():
    with _with_env({
        "BEACON_TARGET_COMPLETE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "human",
    }):
        assert commands._ai_session_direct_completion_ban_active() is False


def test_completion_ban_unknown_kind_still_blocks():
    for kind in ("claude", "ai", "bot", ""):
        with _with_env({
            "BEACON_TARGET_COMPLETE_USER_OVERRIDE": None,
            "BEACON_SESSION_KIND": kind,
        }):
            assert commands._ai_session_direct_completion_ban_active() is True, kind


def test_two_overrides_are_independent():
    """The approve override must not unlock direct completion, and vice versa —
    they are distinct human decisions (confirm a verdict vs bypass the gate)."""
    with _with_env({
        "BEACON_TARGET_APPROVE_USER_OVERRIDE": "1",
        "BEACON_TARGET_COMPLETE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_attainment_approve_ban_active() is False
        assert commands._ai_session_direct_completion_ban_active() is True
    with _with_env({
        "BEACON_TARGET_APPROVE_USER_OVERRIDE": None,
        "BEACON_TARGET_COMPLETE_USER_OVERRIDE": "1",
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_attainment_approve_ban_active() is True
        assert commands._ai_session_direct_completion_ban_active() is False
