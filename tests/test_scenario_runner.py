"""ms-136 e-4698 — local-mode scenario runner tests.

Integration tests: they drive the REAL ``beacon`` CLI as a subprocess against a
throwaway local-mode project (pytest ``tmp_path`` = managed, recoverable
cleanup — no destructive teardown in runner code). Each assertion cites the
e-4698 AC it covers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import scenario_runner as sr  # noqa: E402


BALL_SELF_SRC = "SPEC §6: 最新 inbound → ボールは自分 (BALL_SELF)"
BALL_CP_SRC = "SPEC §6: 最新 outbound → ボールは相手 (BALL_COUNTERPART)"


def _sales_reply_journey():
    """A real journey: 商談を立て→提案送付(ball相手)→顧客返信を擬似注入→ball自分。"""
    return {
        "name": "初回提案→返信で ball 復帰",
        "spec_ref": "y2gy76tVfnzKVfFy4elM",
        "seed": {"profession": "sales", "name": "Acme Sales",
                 "objective": "close deals"},
        "steps": [
            {"kind": "persona_cli", "argv": ["opportunity", "add", "Acme Deal"],
             "label": "商談を立てる"},
            {"kind": "persona_cli",
             "argv": ["communication", "add", "opp-1", "提案を送付",
                      "--direction", "outbound", "--channel", "email"],
             "label": "提案を送る"},
            {"kind": "persona_cli",
             "argv": ["communication", "list", "opp-1", "--json"],
             "label": "送信後の ball を観測"},
            {"kind": "assert", "assert": "json_path", "path": "ball",
             "value": "counterpart", "spec_source": BALL_CP_SRC},
            {"kind": "inbound_stimulus", "target": "opp-1",
             "summary": "検討します、と返信あり", "channel": "email",
             "expect_ingested": True, "label": "顧客が返信 (環境刺激)"},
            {"kind": "persona_cli",
             "argv": ["communication", "list", "opp-1", "--json"],
             "label": "取り込み後の ball を観測"},
            {"kind": "assert", "assert": "json_path", "path": "ball",
             "value": "self", "spec_source": BALL_SELF_SRC},
        ],
    }


# --- AC #1 + #2: setup → run → assert on real CLI output / state -------------

def test_full_sales_journey_passes(tmp_path):
    report = sr.run_scenario(_sales_reply_journey(), workdir=str(tmp_path))
    assert report["passed"] is True, report["failure"]
    assert report["failure"] is None
    # all steps ran and are ok
    assert len(report["steps"]) == 7
    assert all(s["ok"] for s in report["steps"])
    # the inbound_stimulus reported a real ingest (ball flipped)
    inj = next(s for s in report["steps"] if s["kind"] == "inbound_stimulus")
    assert inj["inject"]["ingested"] is True
    assert inj["inject"]["ball_before"] == "counterpart"
    assert inj["inject"]["ball_after"] == "self"


def test_project_is_local_mode_no_outward(tmp_path):
    # AC #3: the throwaway project stays local — no cloud.json is ever created,
    # so the CLI reaches no cloud store and nothing外向き can fire.
    sr.run_scenario(_sales_reply_journey(), workdir=str(tmp_path))
    assert (tmp_path / ".beacon" / "project.json").exists()
    assert not (tmp_path / ".beacon" / "cloud.json").exists()


# --- failure reporting (feeds e-4700 bisect) --------------------------------

def test_assertion_mismatch_is_reported_not_raised(tmp_path):
    scenario = _sales_reply_journey()
    # Corrupt the final oracle: claim the ball is still the counterpart's.
    scenario["steps"][-1]["value"] = "counterpart"
    report = sr.run_scenario(scenario, workdir=str(tmp_path))
    assert report["passed"] is False
    assert report["failure"]["index"] == 6
    assert report["failure"]["kind"] == "assert"
    # the failing assert keeps its SPEC provenance in the failure record
    assert report["failure"]["spec_source"] == BALL_SELF_SRC


def test_persona_cli_unexpected_error_is_failure(tmp_path):
    scenario = {
        "seed": {"profession": "sales", "name": "S", "objective": "o"},
        "steps": [
            {"kind": "persona_cli",
             "argv": ["communication", "list", "opp-999", "--json"],
             "label": "存在しない商談を観測 (壊れた操作)"},
        ],
    }
    report = sr.run_scenario(scenario, workdir=str(tmp_path))
    assert report["passed"] is False
    assert report["failure"]["index"] == 0
    assert report["failure"]["kind"] == "persona_cli"


# --- discipline enforced structurally ---------------------------------------

def test_assert_without_spec_source_raises(tmp_path):
    scenario = {
        "seed": {"profession": "sales", "name": "S", "objective": "o"},
        "steps": [
            {"kind": "persona_cli", "argv": ["opportunity", "add", "D"]},
            {"kind": "persona_cli",
             "argv": ["communication", "list", "opp-1", "--json"]},
            # missing spec_source → must be refused (方針3)
            {"kind": "assert", "assert": "json_path", "path": "target",
             "value": "opp-1"},
        ],
    }
    with pytest.raises(sr.ScenarioError):
        sr.run_scenario(scenario, workdir=str(tmp_path))


def test_unknown_step_kind_raises(tmp_path):
    scenario = {"steps": [{"kind": "teleport", "argv": []}]}
    with pytest.raises(sr.ScenarioError):
        sr.run_scenario(scenario, workdir=str(tmp_path))


def test_dev_seed_journey_runs(tmp_path):
    # profession-agnostic: a dev project seeds and a persona op runs black-box.
    scenario = {
        "name": "dev seed smoke",
        "seed": {"profession": "dev", "name": "Devproj", "objective": "ship"},
        "steps": [
            {"kind": "persona_cli",
             "argv": ["milestone", "add", "First MS", "--priority", "high"],
             "label": "MS を立てる"},
            {"kind": "persona_cli", "argv": ["status", "--json"],
             "label": "状態を JSON で観測"},
            {"kind": "assert", "assert": "json_path", "path": "profession",
             "value": "dev",
             "spec_source": "SPEC スコープ: profession-agnostic に journey を回す"},
        ],
    }
    report = sr.run_scenario(scenario, workdir=str(tmp_path))
    assert report["passed"] is True, report["failure"]
