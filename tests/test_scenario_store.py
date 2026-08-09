"""ms-136 e-4699 — scenario store tests (validate + save/load/list).

Pins the deterministic asset gatekeeper: a generated scenario is validated
against the held contract (both oracle provenance axes + categorized quality
signals) before it becomes a diffable repo artifact. Uses tmp_path as repo_root
so nothing touches the real scenarios/ tree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import scenario_store as ss  # noqa: E402


def _valid_scenario():
    return {
        "name": "初回提案→返信で ball 復帰",
        "milestone": "ms-136",
        "spec_ref": "y2gy76tVfnzKVfFy4elM",
        "seed": {"profession": "sales", "name": "Acme", "objective": "close"},
        "steps": [
            {"kind": "persona_cli", "argv": ["opportunity", "add", "Acme"]},
            {"kind": "inbound_stimulus", "target": "opp-1", "summary": "返信"},
            {"kind": "persona_cli",
             "argv": ["communication", "list", "opp-1", "--json"]},
            {"kind": "assert", "assert": "json_path", "path": "ball",
             "value": "self",
             "spec_source": "SPEC §6: 最新 inbound → ボールは自分",
             "observation_basis": "communication list --json の 'ball'; "
                                  "SPEC のユーザー可視概念『手番』の派生値"},
        ],
        "quality_signals": [
            {"ac": "AC: 実際にメールが届く", "reason_type": ss.QS_OUT_OF_SCOPE,
             "note": "transmission は方針4で縁の外、検証しないのが正しい"},
        ],
    }


# --- save / load round-trip + path layout -----------------------------------

def test_save_writes_diffable_file_and_loads_back(tmp_path):
    sc = _valid_scenario()
    path = ss.save_scenario(sc, repo_root=tmp_path)
    assert path == tmp_path / "scenarios" / "ms-136" / path.name
    assert path.suffix == ".json"
    # pretty + trailing newline = diff-friendly
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text)["spec_ref"] == "y2gy76tVfnzKVfFy4elM"
    # round-trips through validation
    loaded = ss.load_scenario(path)
    assert loaded["name"] == sc["name"]


def test_regeneration_overwrites_same_slug(tmp_path):
    sc = _valid_scenario()
    p1 = ss.save_scenario(sc, repo_root=tmp_path)
    sc["steps"][-1]["value"] = "counterpart"  # a changed oracle
    p2 = ss.save_scenario(sc, repo_root=tmp_path)
    assert p1 == p2  # same slug → same file (a reviewable diff, not a dup)
    assert ss.load_scenario(p2)["steps"][-1]["value"] == "counterpart"


# --- save-time required fields ----------------------------------------------

def test_missing_milestone_refused(tmp_path):
    sc = _valid_scenario()
    del sc["milestone"]
    with pytest.raises(ss.ScenarioError):
        ss.save_scenario(sc, repo_root=tmp_path)


def test_missing_spec_ref_refused(tmp_path):
    sc = _valid_scenario()
    sc["spec_ref"] = ""
    with pytest.raises(ss.ScenarioError):
        ss.save_scenario(sc, repo_root=tmp_path)


# --- oracle provenance enforced at save time too ----------------------------

def test_assert_missing_observation_basis_refused(tmp_path):
    sc = _valid_scenario()
    del sc["steps"][-1]["observation_basis"]
    with pytest.raises(ss.ScenarioError):
        ss.save_scenario(sc, repo_root=tmp_path)


def test_assert_missing_spec_source_refused(tmp_path):
    sc = _valid_scenario()
    del sc["steps"][-1]["spec_source"]
    with pytest.raises(ss.ScenarioError):
        ss.save_scenario(sc, repo_root=tmp_path)


# --- quality_signals categorization (論点3) ----------------------------------

def test_quality_signal_uncategorized_refused(tmp_path):
    sc = _valid_scenario()
    sc["quality_signals"][0]["reason_type"] = "dunno"
    with pytest.raises(ss.ScenarioError):
        ss.save_scenario(sc, repo_root=tmp_path)


def test_quality_signal_needs_rewrite_accepted(tmp_path):
    sc = _valid_scenario()
    sc["quality_signals"] = [
        {"ac": "AC: うまくいく", "reason_type": ss.QS_NEEDS_REWRITE,
         "note": "観測可能に書けていない=SPEC 品質欠陥"},
    ]
    path = ss.save_scenario(sc, repo_root=tmp_path)
    assert ss.load_scenario(path)["quality_signals"][0]["reason_type"] == \
        ss.QS_NEEDS_REWRITE


# --- listing ----------------------------------------------------------------

def test_list_scenarios_indexes_saved(tmp_path):
    ss.save_scenario(_valid_scenario(), repo_root=tmp_path)
    other = _valid_scenario()
    other["name"] = "別ジャーニー"
    other["milestone"] = "ms-999"
    ss.save_scenario(other, repo_root=tmp_path)

    all_rows = ss.list_scenarios(repo_root=tmp_path)
    assert len(all_rows) == 2
    ms136 = ss.list_scenarios(repo_root=tmp_path, milestone="ms-136")
    assert len(ms136) == 1
    assert ms136[0]["spec_ref"] == "y2gy76tVfnzKVfFy4elM"
    assert ms136[0]["quality_signal_count"] == 1
    assert ms136[0]["step_count"] == 4


def test_list_empty_when_no_scenarios(tmp_path):
    assert ss.list_scenarios(repo_root=tmp_path) == []
