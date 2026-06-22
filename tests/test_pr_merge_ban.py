"""ms-92 / e-2169 — AI-session individual PR merge ban + Trek-finalize escape.

CORE doc ``pr-review-autonomy-boundary`` establishes that AI sessions
may approve / reject / request-changes on PRs but **must not** merge
individual PRs from inside an autonomous session. Merge belongs to
the Trek-unit user collective confirmation (= ``/beacon-trek-finalize``).

This test pins the ban + the 3 escape hatches:

  1. ``BEACON_TREK_FINALIZE_CONSENT=1`` — the Trek-finalize Skill is
     the merger (= 1-confirm collective approval path).
  2. ``BEACON_PR_MERGE_USER_OVERRIDE=1`` — user explicit opt-in escape
     hatch for one-off merges (= user prompt phrased the merge directly).
  3. ``BEACON_SESSION_KIND=human`` — non-AI session declares itself
     human-driven (= straight terminal use).

Unset env (= the AI-session default) must refuse with the multi-line
error that names all 3 escape paths so a stuck operator can pick the
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
        assert commands._ai_session_merge_ban_active() is True


def test_merge_ban_bypassed_by_trek_finalize_consent():
    """Escape #1: BEACON_TREK_FINALIZE_CONSENT=1 (= Skill 1-confirm path)."""
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": "1",
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_merge_ban_active() is False


def test_merge_ban_bypassed_by_user_override():
    """Escape #2: BEACON_PR_MERGE_USER_OVERRIDE=1 (= explicit one-off opt-in)."""
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": "1",
        "BEACON_SESSION_KIND": None,
    }):
        assert commands._ai_session_merge_ban_active() is False


def test_merge_ban_bypassed_by_human_session_kind():
    """Escape #3: BEACON_SESSION_KIND=human (= non-AI session, terminal use)."""
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "human",
    }):
        assert commands._ai_session_merge_ban_active() is False


def test_merge_ban_session_kind_case_insensitive():
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "Human",  # any casing
    }):
        assert commands._ai_session_merge_ban_active() is False
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": None,
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "  HUMAN  ",  # padded
    }):
        assert commands._ai_session_merge_ban_active() is False


def test_merge_ban_unknown_session_kind_still_blocks():
    """Anything other than ``human`` → still treated as AI for safety."""
    for kind in ("claude", "anthropic", "ai", "bot", "agent", ""):
        with _with_env({
            "BEACON_TREK_FINALIZE_CONSENT": None,
            "BEACON_PR_MERGE_USER_OVERRIDE": None,
            "BEACON_SESSION_KIND": kind,
        }):
            assert commands._ai_session_merge_ban_active() is True, kind


def test_trek_finalize_consent_active_helper_pinned():
    """The Skill exports this env before delegating; test the read side."""
    with _with_env({"BEACON_TREK_FINALIZE_CONSENT": "1"}):
        assert commands._trek_finalize_consent_active() is True
    with _with_env({"BEACON_TREK_FINALIZE_CONSENT": "0"}):
        assert commands._trek_finalize_consent_active() is False
    with _with_env({"BEACON_TREK_FINALIZE_CONSENT": None}):
        assert commands._trek_finalize_consent_active() is False


# ---------------------------------------------------------------------------
# /beacon-trek-finalize Skill markdown — structural enforcement
# ---------------------------------------------------------------------------


FINALIZE_SKILL = REPO / "skills" / "beacon-trek-finalize.md"


def test_trek_finalize_skill_exists():
    """The Skill markdown must exist — it's the user-facing entry to the
    1-confirm merge path."""
    assert FINALIZE_SKILL.exists(), (
        "skills/beacon-trek-finalize.md must exist (= ms-92 / e-2169 "
        "Trek 終結 merge UX)"
    )


def test_trek_finalize_skill_references_e_2169_and_core_doc():
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "e-2169" in body
    assert "pr-review-autonomy-boundary" in body, (
        "the Skill must cite the CORE doc that establishes the AI / Trek-unit "
        "user / user role split — drilling there is the path to the rationale"
    )


def test_trek_finalize_skill_documents_three_segment_boundary():
    """The 3-segment boundary (approve = AI / merge = Trek user / release =
    user) is the Skill's reason for existing — must be inline."""
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "approve = AI" in body
    assert "merge = Trek 単位 user" in body
    assert "release = user" in body


def test_trek_finalize_skill_uses_consent_env_to_bypass_ban():
    """The Skill must export BEACON_TREK_FINALIZE_CONSENT to bypass the
    AI-session merge ban — otherwise the merge call refuses inside the
    Skill itself."""
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "BEACON_TREK_FINALIZE_CONSENT=1" in body, (
        "the Skill must export BEACON_TREK_FINALIZE_CONSENT=1 before "
        "delegating to `beacon pr merge`, otherwise the e-2169 ban refuses"
    )


def test_trek_finalize_skill_documents_drilldown_and_partial_paths():
    """The 4-choice prompt (yes / drilldown / partial / cancel) must be
    in the Skill body so the user has escape routes from blind 1-confirm."""
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "drilldown" in body
    assert "partial" in body
    assert "cancel" in body


def test_trek_finalize_skill_documents_conflict_pre_detection():
    """AC #7: conflict pre-detection must be in the Skill body."""
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "CONFLICTING" in body or "conflict" in body.lower()


def test_trek_finalize_skill_links_to_release_path():
    """AC #5: post-finalize, the Skill must surface the release ceremony
    path (= /beacon-push) as a follow-on action."""
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "/beacon-push" in body or "release.yml" in body


def test_trek_finalize_skill_documents_leader_role_gate():
    """The Skill must refuse to run for executor-role sessions so the
    leader stance contract (e-2166) holds."""
    body = FINALIZE_SKILL.read_text(encoding="utf-8")
    assert "leader" in body.lower()
    assert "executor" in body.lower()
