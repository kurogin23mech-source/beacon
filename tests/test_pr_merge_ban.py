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
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert cmd_pr._ai_session_merge_ban_active() is True


def test_the_removed_trek_hatch_no_longer_bypasses_the_ban():
    """ms-185 e-7069 — 消した抜け道が、環境変数だけで生き残っていないこと。

    **消した裏返しを固定する。** 以前は ``BEACON_TREK_FINALIZE_CONSENT=1`` が
    抜け道 #1 で、Trek 終結 Skill (= 1 confirm で一括 merge) がこれを export して
    から merge を呼んでいた。その Skill と Trek の動線を落とした以上、裏付けの
    無い環境変数だけが残る形は **環境変数 1 つで AI セッションが merge できる
    抜け道**になる。判定ごと消したので、立てても ban は firing し続ける。
    """
    with _with_env({
        "BEACON_TREK_FINALIZE_CONSENT": "1",   # 消した抜け道
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": None,
    }):
        assert cmd_pr._ai_session_merge_ban_active() is True
    assert not hasattr(cmd_pr, "_trek_finalize_consent_active"), (
        "判定関数が残っている — 誰もセットしない判定を残さない")


def test_merge_ban_bypassed_by_user_override():
    """Escape #1: BEACON_PR_MERGE_USER_OVERRIDE=1 (= explicit one-off opt-in)."""
    with _with_env({
        "BEACON_PR_MERGE_USER_OVERRIDE": "1",
        "BEACON_SESSION_KIND": None,
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False


def test_merge_ban_bypassed_by_human_session_kind():
    """Escape #2: BEACON_SESSION_KIND=human (= non-AI session, terminal use)."""
    with _with_env({
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "human",
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False


def test_merge_ban_session_kind_case_insensitive():
    with _with_env({
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "Human",  # any casing
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False
    with _with_env({
        "BEACON_PR_MERGE_USER_OVERRIDE": None,
        "BEACON_SESSION_KIND": "  HUMAN  ",  # padded
    }):
        assert cmd_pr._ai_session_merge_ban_active() is False


def test_merge_ban_unknown_session_kind_still_blocks():
    """Anything other than ``human`` → still treated as AI for safety."""
    for kind in ("claude", "anthropic", "ai", "bot", "agent", ""):
        with _with_env({
            "BEACON_PR_MERGE_USER_OVERRIDE": None,
            "BEACON_SESSION_KIND": kind,
        }):
            assert cmd_pr._ai_session_merge_ban_active() is True, kind


# ---------------------------------------------------------------------------
# 落とした入口 — 消した裏返しを固定する (ms-185 e-7069)
# ---------------------------------------------------------------------------
#
# ここには元々 ``/beacon-trek-finalize`` Skill の markdown に対する構造試験が
# 8 件あった (= 存在する / 3 段境界を書いている / 抜け道の環境変数を export する /
# 衝突の事前検知を書いている 等)。Skill と Trek の動線を落としたので、その 8 件は
# 固定する対象を失った。元のブロックは ``.trash/ms-185-e7069/`` に退避してある。
#
# **空にせず裏返しを置く理由**: 入口を落としたことの証拠は、落ちていることを
# 名指しで測るしかない。復活させたら赤くなる形にしておく。


def test_the_trek_finalize_skill_is_gone_from_every_copy():
    """3 コピーすべてから消えている (= 1 箇所に残ると そこから起動できる)。"""
    leftovers = [
        p for p in (
            REPO / "skills" / "beacon-trek-finalize.md",
            REPO / "shared" / "skills" / "beacon-trek-finalize" / "SKILL.md",
            REPO / "plugins" / "beacon" / "skills" / "beacon-trek-finalize" / "SKILL.md",
        ) if p.exists()
    ]
    assert not leftovers, f"落としたはずの Skill が残っている: {leftovers}"


def test_the_merge_ban_error_does_not_point_at_the_removed_entry():
    """ban の案内文が、消えた入口を「ここから merge しろ」と指していない。

    消えた動線を指す案内は、読み手を存在しないコマンドへ送る (= Skill を直した
    のと同じ理由)。残る抜け道 2 つは名前で出ていること。
    """
    src = (REPO / "lib" / "cmd_pr.py").read_text(encoding="utf-8")
    i = src.index("individual PR merge from an AI session is refused")
    block = src[i:i + 1200]
    assert "/beacon-trek-finalize" not in block, (
        "ban の案内が消えた Skill を指している")
    assert "BEACON_PR_MERGE_USER_OVERRIDE=1" in block
    assert "BEACON_SESSION_KIND=human" in block
