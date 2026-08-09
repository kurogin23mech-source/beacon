"""ms-136 e-4702 — CI regression + attainment evidence tests.

Pins the leader-ratified boundaries: attainment is journey-pass EVIDENCE (not a
verdict, no auto-close), and CI failures are classified infra (harness/net) vs
product (real regression). The all-green replay over the real scenarios/ tree is
itself the CI regression gate (SPEC 方針7 / AC6).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import scenario_regression as sreg  # noqa: E402
import scenario_store as ss  # noqa: E402


# --- CI regression: the committed scenarios all replay green -----------------

def test_saved_scenarios_all_green_over_repo():
    run = sreg.run_saved_scenarios()
    assert run["all_passed"], (run["product_regressions"], run["infra_failures"])


# --- attainment is EVIDENCE, never a verdict (掃除機≠掃除) --------------------

def test_attainment_evidence_is_not_a_verdict():
    ev = sreg.attainment_evidence("ms-136")
    assert ev["is_verdict"] is False
    assert ev["kind"] == "journey-pass-evidence"
    assert "verdict ではない" in ev["label"]
    assert "auto-close しない" in ev["label"]
    assert ev["journey_count"] >= 1
    assert ev["all_journeys_green"] is True
    # per-journey rows carry the SPEC provenance, not a pass/fail verdict on the MS
    for j in ev["journeys"]:
        assert "spec_ref" in j and "passed" in j


# --- infra vs product classification (leader 裁定) ---------------------------

def test_malformed_saved_asset_is_infra(tmp_path):
    d = tmp_path / "scenarios" / "ms-x"
    d.mkdir(parents=True)
    # invalid step kind → load/validate raises ScenarioError → harness/asset = infra
    (d / "bad.json").write_text(
        '{"name":"bad","milestone":"ms-x","spec_ref":"s","seed":{},'
        '"steps":[{"kind":"teleport"}]}', encoding="utf-8")
    run = sreg.run_saved_scenarios(repo_root=tmp_path)
    assert run["all_passed"] is False
    assert len(run["infra_failures"]) == 1
    assert run["infra_failures"][0]["origin"] == "infra"
    assert run["product_regressions"] == []


def test_journey_divergence_is_product_with_bisect_layer(tmp_path):
    # a scenario that RUNS but diverges (asserts the wrong ball after outbound)
    # = a product regression, and the bisect layer rides along.
    scenario = {
        "name": "wrong oracle divergence", "milestone": "ms-x",
        "spec_ref": "y2gy76tVfnzKVfFy4elM",
        "seed": {"profession": "sales", "name": "S", "objective": "o"},
        "steps": [
            {"kind": "persona_cli", "argv": ["opportunity", "add", "D"]},
            {"kind": "persona_cli",
             "argv": ["communication", "add", "opp-1", "提案", "--direction",
                      "outbound"]},
            {"kind": "persona_cli",
             "argv": ["communication", "list", "opp-1", "--json"]},
            # after an outbound send the ball is the counterpart's; asserting
            # "self" makes the journey diverge (a product-side divergence).
            {"kind": "assert", "assert": "json_path", "path": "ball",
             "value": "self", "spec_source": "SPEC §6",
             "observation_basis": "communication list --json の ball"},
        ],
    }
    ss.save_scenario(scenario, repo_root=tmp_path)
    run = sreg.run_saved_scenarios(repo_root=tmp_path)
    assert run["all_passed"] is False
    assert len(run["product_regressions"]) == 1
    reg = run["product_regressions"][0]
    assert reg["origin"] == "product"
    assert reg.get("responsible_layer")  # bisect localized the layer
