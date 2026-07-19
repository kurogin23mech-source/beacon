"""Tests for ms-106 e-3526 (商談の自由記述 description) + e-3527 (提案準備へ進む
際の 想定金額 require_amount 警告).

e-3526: an Opportunity gains a free-text 背景 / 経緯 / メモ field, set at 起票
(``--description``) or later (``opportunity describe``).

e-3527: moving into a phase whose ``require_amount`` flag is set, while the deal
has neither a goal nor a deal amount, surfaces a non-blocking warning. A tolerant
backfill covers projects whose phases were seeded before the flag existed.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BEACON_BIN = REPO / "bin" / "beacon"

sys.path.insert(0, str(REPO / "lib"))
import sales_entities as se  # noqa: E402


# ---------------------------------------------------------------------------
# e-3526 — description field (unit)
# ---------------------------------------------------------------------------


def _sales_data():
    return {
        "profession": "sales",
        "opportunities": [],
        "accounts": [],
        "opportunity_phases": copy.deepcopy(se.DEFAULT_OPPORTUNITY_PHASES),
        "account_phases": copy.deepcopy(se.DEFAULT_ACCOUNT_PHASES),
    }


def test_opportunity_add_stores_description():
    data = _sales_data()
    oid = se.opportunity_add(data, "Acme deal", description="  紹介経由。3月締め。 ")
    opp = se.find_opportunity(data, oid)
    assert opp["description"] == "紹介経由。3月締め。"  # stripped


def test_opportunity_add_default_description_empty():
    data = _sales_data()
    oid = se.opportunity_add(data, "No-desc deal")
    assert se.find_opportunity(data, oid)["description"] == ""


def test_opportunity_set_description_sets_and_clears():
    data = _sales_data()
    oid = se.opportunity_add(data, "Deal")
    se.opportunity_set_description(data, oid, "背景メモ")
    assert se.find_opportunity(data, oid)["description"] == "背景メモ"
    se.opportunity_set_description(data, oid, "")
    assert se.find_opportunity(data, oid)["description"] == ""


def test_opportunity_set_description_unknown_raises():
    with pytest.raises(ValueError):
        se.opportunity_set_description(_sales_data(), "opp-999", "x")


# ---------------------------------------------------------------------------
# e-3527 — require_amount warning (unit)
# ---------------------------------------------------------------------------


def test_proposal_phase_without_amount_warns():
    data = _sales_data()
    w = se.opportunity_phase_warnings(data, "商談準備", "提案準備", goal_amount=None)
    assert any("想定金額" in s for s in w), w


def test_proposal_phase_with_goal_amount_no_warning():
    data = _sales_data()
    w = se.opportunity_phase_warnings(data, "商談準備", "提案準備", goal_amount=500000)
    assert not any("想定金額" in s for s in w)


def test_proposal_phase_with_deal_amount_no_warning():
    # amount (set later via `opportunity amount`) also satisfies the check.
    data = _sales_data()
    w = se.opportunity_phase_warnings(data, "商談準備", "提案準備",
                                      goal_amount=None, amount=300000)
    assert not any("想定金額" in s for s in w)


def test_商談準備_entry_does_not_require_amount():
    # The entry phase (商談準備) has no require_amount → no warning.
    data = _sales_data()
    w = se.opportunity_phase_warnings(data, "", "商談準備", goal_amount=None)
    assert not any("想定金額" in s for s in w)


def test_zero_amount_counts_as_unset():
    data = _sales_data()
    w = se.opportunity_phase_warnings(data, "商談準備", "提案準備",
                                      goal_amount=0, amount=0)
    assert any("想定金額" in s for s in w)


def test_backfill_for_phases_seeded_before_flag():
    # Simulate a project whose opportunity_phases were seeded from the shipped
    # default BEFORE require_amount existed (strip the flag).
    data = _sales_data()
    data["opportunity_phases"] = [
        {k: v for k, v in ph.items() if k != "require_amount"}
        for ph in data["opportunity_phases"]
    ]
    assert all("require_amount" not in ph for ph in data["opportunity_phases"])
    w = se.opportunity_phase_warnings(data, "商談準備", "提案準備", goal_amount=None)
    assert any("想定金額" in s for s in w), "backfill by phase name should still warn"


def test_explicit_require_amount_false_opts_out():
    # A company can explicitly opt a phase out of the amount requirement.
    data = _sales_data()
    for ph in data["opportunity_phases"]:
        if ph["name"] == "提案準備":
            ph["require_amount"] = False
    w = se.opportunity_phase_warnings(data, "商談準備", "提案準備", goal_amount=None)
    assert not any("想定金額" in s for s in w), "explicit False must be honored"


# ---------------------------------------------------------------------------
# CLI wiring (subprocess) — --description + describe subcommand round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def sales_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run([str(BEACON_BIN), "init"],
                   input="e3526-test\nobj\n5\n1\n",
                   text=True, check=True, capture_output=True)
    # Flip to a sales project and seed the phase vocabulary from the default.
    pf = tmp_path / ".beacon" / "project.json"
    d = json.loads(pf.read_text())
    d["profession"] = "sales"
    d["opportunity_phases"] = copy.deepcopy(se.DEFAULT_OPPORTUNITY_PHASES)
    d["account_phases"] = copy.deepcopy(se.DEFAULT_ACCOUNT_PHASES)
    pf.write_text(json.dumps(d, ensure_ascii=False))
    subprocess.run([str(BEACON_BIN), "account", "add", "Acme"],
                   check=True, capture_output=True)
    return tmp_path


def _opp(project_dir):
    d = json.loads((project_dir / ".beacon" / "project.json").read_text())
    return d["opportunities"][0]


def test_cli_add_description_and_describe(sales_project):
    subprocess.run([str(BEACON_BIN), "opportunity", "add", "Acme deal",
                    "--account", "acc-1", "--description", "紹介経由"],
                   check=True, capture_output=True)
    assert _opp(sales_project)["description"] == "紹介経由"
    subprocess.run([str(BEACON_BIN), "opportunity", "describe", "opp-1", "更新メモ"],
                   check=True, capture_output=True)
    assert _opp(sales_project)["description"] == "更新メモ"


def test_cli_phase_to_proposal_warns_without_amount(sales_project):
    subprocess.run([str(BEACON_BIN), "opportunity", "add", "Acme deal",
                    "--account", "acc-1"], check=True, capture_output=True)
    r = subprocess.run([str(BEACON_BIN), "opportunity", "phase", "opp-1", "提案準備"],
                       text=True, capture_output=True)
    assert "想定金額" in (r.stdout + r.stderr), (r.stdout, r.stderr)
