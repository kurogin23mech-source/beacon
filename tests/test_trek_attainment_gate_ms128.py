"""ms-128 方針6 / e-4386 — 完遂ゲートの attainment mode 化。

Trek の唯一の terminal ``user_review`` へ倒す「合格判定」を、実行者の外に固定した
全 met の attainment verdict でのみ通す。self_judgment (= 直前に状態を stamp した
session が自分で倒す) と、素の verdict なし approve を構造的に塞ぐ。

Pinned invariants (pure layer — server / CLI wiring is exercised separately):
  1. ``evaluate_attainment_verdict``: 全 met のみ all_met=True。未評価 (欠落 / 空 /
     未知の値) は 未達扱い。空 list は has_verdict=False。
  2. 非 terminal 遷移は gate 素通り (allowed=True)。
  3. forward-to-user (人間エスカレーション) は user_review でも gate 対象外。
  4. self_judgment (caller == 直前 stamper) は attainment verdict の有無に関わらず
     leader_review へ留置。
  5. verdict 欠落の完成 approve は leader_review へ留置。
  6. verdict はあるが未達 (partial/not-met) は working へ差し戻し。
  7. 実行者の外が出した全 met verdict のみ user_review を許す。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek  # noqa: E402


# --- evaluate_attainment_verdict --------------------------------------------

def test_all_met_passes():
    r = trek.evaluate_attainment_verdict([
        {"criterion": "AC1", "verdict": "met"},
        {"criterion": "AC2", "verdict": "met"},
    ])
    assert r == {"has_verdict": True, "all_met": True, "unmet": []}


def test_any_partial_is_unmet():
    r = trek.evaluate_attainment_verdict([
        {"criterion": "AC1", "verdict": "met"},
        {"criterion": "AC2", "verdict": "partial"},
    ])
    assert r["has_verdict"] is True
    assert r["all_met"] is False
    assert r["unmet"] == ["AC2"]


def test_not_met_is_unmet():
    r = trek.evaluate_attainment_verdict([
        {"criterion": "AC1", "verdict": "not-met"},
    ])
    assert r["all_met"] is False
    assert r["unmet"] == ["AC1"]


def test_missing_or_unknown_verdict_counts_as_unmet():
    # 未評価 (verdict 欠落) や未知の値は met で "ない" ので 未達扱い。
    r = trek.evaluate_attainment_verdict([
        {"criterion": "AC1"},
        {"criterion": "AC2", "verdict": "MET?"},
        {"criterion": "AC3", "verdict": "met"},
    ])
    assert r["all_met"] is False
    assert set(r["unmet"]) == {"AC1", "AC2"}


def test_met_is_case_insensitive():
    r = trek.evaluate_attainment_verdict([{"criterion": "AC1", "verdict": " MET "}])
    assert r["all_met"] is True


def test_empty_verdict_has_no_judgment():
    assert trek.evaluate_attainment_verdict([]) == {
        "has_verdict": False, "all_met": False, "unmet": []}
    assert trek.evaluate_attainment_verdict(None)["has_verdict"] is False


# --- completion_gate_decision ------------------------------------------------

_ALL_MET = [{"criterion": "AC1", "verdict": "met"}]


def _decide(**kw):
    # Default: the completion-approval path (leader_review → user_review).
    base = dict(
        effective_state="user_review",
        from_state="leader_review",
        verdict="",
        caller_sid="judge-sid",
        prior_stamper_sid="executor-sid",
        attainment_verdict=None,
    )
    base.update(kw)
    return trek.completion_gate_decision(**base)


def test_non_terminal_transition_passes():
    d = _decide(effective_state="leader_review", from_state="working")
    assert d["allowed"] is True and d["forced_state"] is None


def test_forward_to_user_is_not_gated():
    # 人間エスカレーションは verdict 無しでも user_review に到達できる。
    d = _decide(verdict="forward-to-user", attainment_verdict=None)
    assert d["allowed"] is True and d["forced_state"] is None


def test_bare_working_to_user_review_escalation_is_not_gated():
    # verdict 無しの working→user_review は forward-to-user エスカレーション扱い。
    # 完遂の主張ではないので gate せず開けておく (既存 executor escalation 経路)。
    d = _decide(from_state="working", verdict="", attainment_verdict=None)
    assert d["allowed"] is True and d["forced_state"] is None


def test_explicit_approve_from_working_is_gated():
    # working からでも明示 approve は完遂合格 → gate 対象 (verdict 必須)。
    d = _decide(from_state="working", verdict="approve", attainment_verdict=None)
    assert d["allowed"] is False
    assert d["code"] == "attainment_verdict_required"


def test_self_judgment_is_detained_even_with_all_met():
    # 判定主体を実行者の外に固定する: caller == 直前 stamper は verdict があっても不可。
    d = _decide(caller_sid="x", prior_stamper_sid="x",
                attainment_verdict=_ALL_MET)
    assert d["allowed"] is False
    assert d["forced_state"] == "leader_review"
    assert d["code"] == "attainment_self_judgment_banned"


def test_approve_without_verdict_is_detained():
    d = _decide(attainment_verdict=None)
    assert d["allowed"] is False
    assert d["forced_state"] == "leader_review"
    assert d["code"] == "attainment_verdict_required"


def test_not_all_met_is_reworked():
    d = _decide(attainment_verdict=[{"criterion": "AC2", "verdict": "partial"}])
    assert d["allowed"] is False
    assert d["forced_state"] == "working"
    assert d["code"] == "attainment_not_all_met"


def test_external_all_met_passes():
    d = _decide(caller_sid="judge", prior_stamper_sid="executor",
                attainment_verdict=_ALL_MET)
    assert d["allowed"] is True and d["forced_state"] is None
