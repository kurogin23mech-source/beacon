"""Tests for the contract work-item + 成約ガード (ms-174).

商談の「合意済み(握った)」と「成約(締結して確定)」を別事実として構造化する契約
(contract) work-item と、成約 terminal の前提ガードを固定する。SPEC ms-174:

  - e-6433 (entity+互換): contract を Opportunity 配下の第一級 work-item として持つ。
    未締結/締結済み(+締結日)・gating(成約の前提か)・任意 ref。pnhATs 3条互換で
    contract 未導入の既存商談を壊さない。
  - e-6435 (成約ガード): 成約(won)の決着は gating な締結済み契約が1つ以上無ければ
    block し、合意済みのまま留める。失注/不成立は締結不要 (AC2-4)。

現実の照合 (SPEC 検証): 覚書=本契約 未締結の商談は 成約 block、NDA(gating=False)だけ
締結済みでも 成約 に進めない (= 2026-09-11 EAGLE 事故の再発防止)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import core  # noqa: E402
import sales_entities as se  # noqa: E402
import work_model  # noqa: E402


def _fresh():
    """A sales project with one open opportunity at 合意済み (契約を締結するフェーズ)."""
    data = se.build_sales_project("Acme Sales", "close deals")
    opp = se.opportunity_add(data, "EAGLE", created_at="T0")
    se.phase_set(data, opp, "合意済み", at="T1")  # 契約締結フェーズへ
    return data, opp


# --- entity: contract_add (e-6433) -----------------------------------------

def test_contract_add_defaults_unsigned():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "9月覚書", gating=True, ref="https://x")
    assert ctr == "ctr-1"
    _o, c = se.find_contract(data, ctr)
    assert c["status"] == se.CONTRACT_UNSIGNED
    assert c["gating"] is True
    assert c["signed_date"] == ""
    assert c["ref"] == "https://x"
    assert c["created_in_phase"] == "合意済み"  # e-3555 phase attribution default


def test_contract_add_gating_defaults_false():
    data, opp = _fresh()
    _o, c = se.find_contract(data, se.contract_add(data, opp, "NDA"))
    assert c["gating"] is False  # NDA は残すが成約の前提ではない


def test_contract_add_requires_opportunity_and_desc():
    data, opp = _fresh()
    with pytest.raises(ValueError):
        se.contract_add(data, "opp-99", "x")
    with pytest.raises(ValueError):
        se.contract_add(data, opp, "   ")


def test_next_contract_id_increments_across_opportunities():
    data, opp1 = _fresh()
    opp2 = se.opportunity_add(data, "Deal2")
    assert se.contract_add(data, opp1, "A") == "ctr-1"
    assert se.contract_add(data, opp2, "B") == "ctr-2"
    assert se.contract_add(data, opp1, "C") == "ctr-3"


# --- entity: contract_sign (e-6433) ----------------------------------------

def test_contract_sign_records_date_and_status():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "覚書", gating=True)
    c = se.contract_sign(data, ctr, signed_date="2026-09-14")
    assert c["status"] == se.CONTRACT_SIGNED
    assert c["signed_date"] == "2026-09-14"


def test_contract_sign_defaults_date_from_at():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "覚書", gating=True)
    c = se.contract_sign(data, ctr, at="2026-09-14T08:00:00Z")
    assert c["signed_date"] == "2026-09-14"  # date part of the ISO stamp


def test_contract_sign_idempotent_keeps_original_date():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "覚書", gating=True)
    se.contract_sign(data, ctr, signed_date="2026-09-14")
    se.contract_sign(data, ctr, at="2026-12-31T00:00:00Z")  # no explicit date
    _o, c = se.find_contract(data, ctr)
    assert c["signed_date"] == "2026-09-14"  # unchanged


def test_contract_sign_updates_ref_when_given():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "覚書", gating=True)
    se.contract_sign(data, ctr, signed_date="2026-09-14", ref="https://signed")
    _o, c = se.find_contract(data, ctr)
    assert c["ref"] == "https://signed"


def test_contract_sign_unknown_raises():
    data, _opp = _fresh()
    with pytest.raises(ValueError):
        se.contract_sign(data, "ctr-99")


# --- entity: contract_cancel + contracts_of (e-6433) -----------------------

def test_contract_cancel_drops_from_live_list_but_kept_with_all():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "誤覚書", gating=True)
    se.contract_cancel(data, ctr, reason="誤起票")
    assert se.contracts_of(data, opp) == []  # live view excludes cancelled
    allc = se.contracts_of(data, opp, include_cancelled=True)
    assert len(allc) == 1 and work_model.is_cancelled(allc[0])


def test_contract_sign_refuses_cancelled():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "覚書", gating=True)
    se.contract_cancel(data, ctr)
    with pytest.raises(ValueError):
        se.contract_sign(data, ctr)


# --- entity: has_gating_signed_contract predicate (e-6433) -----------------

def test_predicate_false_without_any_contract():
    data, opp = _fresh()
    assert se.has_gating_signed_contract(data, opp) is False


def test_predicate_false_for_nda_only_signed():
    data, opp = _fresh()
    se.contract_sign(data, se.contract_add(data, opp, "NDA", gating=False),
                     signed_date="2026-08-01")
    assert se.has_gating_signed_contract(data, opp) is False


def test_predicate_false_for_unsigned_gating():
    data, opp = _fresh()
    se.contract_add(data, opp, "覚書", gating=True)  # not signed
    assert se.has_gating_signed_contract(data, opp) is False


def test_predicate_true_for_signed_gating():
    data, opp = _fresh()
    se.contract_sign(data, se.contract_add(data, opp, "覚書", gating=True),
                     signed_date="2026-09-14")
    assert se.has_gating_signed_contract(data, opp) is True


def test_predicate_false_after_cancelling_the_gating_signed():
    data, opp = _fresh()
    ctr = se.contract_add(data, opp, "覚書", gating=True)
    se.contract_sign(data, ctr, signed_date="2026-09-14")
    se.contract_cancel(data, ctr)
    assert se.has_gating_signed_contract(data, opp) is False


# --- 成約ガード: block reason (e-6435, AC2-4) ------------------------------

def test_block_reason_none_for_lost_and_abandoned():
    data, opp = _fresh()
    assert se.won_terminal_contract_block_reason(data, opp, "失注") is None
    assert se.won_terminal_contract_block_reason(data, opp, "不成立") is None


def test_block_reason_set_for_won_without_contract():
    data, opp = _fresh()
    r = se.won_terminal_contract_block_reason(data, opp, "成約")
    assert r is not None and "成約の前提" in r


def test_block_reason_none_when_gating_signed():
    data, opp = _fresh()
    se.contract_sign(data, se.contract_add(data, opp, "覚書", gating=True),
                     signed_date="2026-09-14")
    assert se.won_terminal_contract_block_reason(data, opp, "成約") is None


# --- 成約ガード: terminal_transition enforcement (e-6435) ------------------

def test_terminal_won_blocked_keeps_deal_open_and_in_phase():
    data, opp = _fresh()
    with pytest.raises(ValueError):
        se.terminal_transition(data, opp, "成約", at="T2")
    o = se.find_opportunity(data, opp)
    assert o["phase"] == "合意済み"      # 合意済み維持 (AC2)
    assert o["status"] == "open"         # not won — nothing was written
    # the open advance gate is NOT settled by a blocked terminal
    assert se.current_gate(data, opp) is not None


def test_terminal_lost_needs_no_contract():
    data, opp = _fresh()
    se.terminal_transition(data, opp, "失注", at="T2")  # must not raise
    assert se.find_opportunity(data, opp)["phase"] == "失注"


def test_terminal_won_passes_after_gating_signed():
    data, opp = _fresh()
    se.contract_sign(data, se.contract_add(data, opp, "覚書", gating=True),
                     signed_date="2026-09-14")
    se.terminal_transition(data, opp, "成約", at="T2")
    assert se.find_opportunity(data, opp)["phase"] == "成約"


def test_terminal_won_blocked_with_nda_only_signed():
    data, opp = _fresh()
    se.contract_sign(data, se.contract_add(data, opp, "NDA", gating=False),
                     signed_date="2026-08-01")
    with pytest.raises(ValueError):
        se.terminal_transition(data, opp, "成約", at="T2")
    assert se.find_opportunity(data, opp)["phase"] == "合意済み"


# --- 互換 (e-6433 pnhATs 3条 / AC5) ----------------------------------------

def test_tolerant_read_for_opportunity_without_contracts_key():
    """contract 未導入の既存商談 (contracts キー無し) は空として読め、KeyError にも
    ならず、失注も従来どおり決着できる (遡及 block しない)。"""
    data, opp = _fresh()
    o = se.find_opportunity(data, opp)
    assert "contracts" not in o  # opportunity_add は contracts キーを持たない (additive)
    assert se.contracts_of(data, opp) == []
    assert se.has_gating_signed_contract(data, opp) is False
    se.terminal_transition(data, opp, "失注", at="T2")  # unaffected
    assert se.find_opportunity(data, opp)["phase"] == "失注"


def test_sales_project_with_contracts_passes_validator():
    data, opp = _fresh()
    se.contract_add(data, opp, "覚書", gating=True)
    core.validate_project(data)  # 新 nested key があっても shared validator を通る


# --- status / Web UI 投影 (e-6436, AC6) ------------------------------------

def test_projection_surfaces_contracts_summary_not_as_work_items():
    """status / session-start / Web UI が読む投影の detail に契約サマリが乗り、かつ
    契約は activity(work_items)に混ざらない (別枠でレンダリングできる, AC6)。"""
    data, opp = _fresh()
    se.contract_sign(data, se.contract_add(data, opp, "覚書", gating=True),
                     signed_date="2026-09-14")
    se.contract_add(data, opp, "NDA", gating=False)  # unsigned NDA
    row = next(t for t in se.project_targets(data) if t["id"] == opp)
    assert row["detail"]["contracts"] == {"total": 2, "signed": 1, "gating_signed": True}
    assert row["work_items_total"] == 0  # 契約は work_items に数えない


def test_projection_contracts_excludes_cancelled():
    data, opp = _fresh()
    se.contract_cancel(data, se.contract_add(data, opp, "誤覚書", gating=True))
    row = next(t for t in se.project_targets(data) if t["id"] == opp)
    assert row["detail"]["contracts"] == {"total": 0, "signed": 0, "gating_signed": False}
