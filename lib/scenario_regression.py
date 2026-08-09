"""ms-136 e-4702 — saved scenarios as CI regression + attainment evidence.

One saved-scenario asset serves two jobs (SPEC 方針7), with a hard boundary the
leader ratified between them:

  (a) CI regression — replay every saved scenario; a failure blocks (exit 1).
      Each failure is classified **infra** (the scenario harness/setup itself
      broke — a flaky/broken net) vs **product** (a real product operation or
      assertion diverged from its SPEC-derived journey), so a red CI is
      actionable: "our net is flaky" ≠ "the product regressed". The e-4700
      layer bisect rides along to say WHERE (L_cli / L_engine / L_store).

  (b) Attainment evidence — at MS close, replay that MS's scenarios and supply
      the result as **journey-pass evidence**, NOT an attainment verdict. A
      green scenario proves "an implemented SPEC-derived journey runs"; it does
      NOT prove "the MS achieved its purpose" (掃除機がある≠掃除した). Auto-close
      on green would miss unimplemented highest/high intents, prompt-layer
      enforcement gaps, and letter-met/spirit-violated. So this returns evidence
      with an explicit non-verdict label; the spirit + completeness judgment
      stays with the human/leader review (ms-119 レビュー機構).

Origin classification (infra vs product): ``scenario_runner.run_scenario``
raises ``ScenarioError`` when the harness/setup fails (seed ``beacon init``
crash, malformed/unloadable asset) — that is **infra**. When the journey runs
but a step diverges, the run returns a report with ``passed=False`` — that is a
**product** regression (the product behaves differently than the SPEC-derived
journey), further localized by the bisect layer.
"""

from __future__ import annotations

from typing import Optional

import scenario_bisect
import scenario_runner
import scenario_store

ORIGIN_INFRA = "infra"        # the scenario harness/setup broke (flaky net)
ORIGIN_PRODUCT = "product"    # a real product op/assert diverged (regression)


def _run_one(row: dict) -> dict:
    """Replay one saved scenario, returning its result with origin + (on
    failure) the layer bisect diagnosis."""
    base = {"path": row["path"], "name": row["name"],
            "spec_ref": row["spec_ref"], "milestone": row["milestone"]}
    try:
        scenario = scenario_store.load_scenario(row["path"])
        report = scenario_runner.run_scenario(scenario)
    except scenario_store.ScenarioError as e:
        # harness/asset failure (bad load/validate or seed crash) = infra.
        return {**base, "passed": False, "origin": ORIGIN_INFRA,
                "reason": str(e), "diagnosis": None}
    if report["passed"]:
        return {**base, "passed": True, "origin": None, "diagnosis": None}
    # the journey ran but diverged = a product regression, localized by bisect.
    diag = scenario_bisect.diagnose_failure(scenario, report)
    return {**base, "passed": False, "origin": ORIGIN_PRODUCT,
            "reason": (report.get("failure") or {}).get("reason", ""),
            "responsible_layer": diag.get("responsible_layer"),
            "diagnosis": diag}


def run_saved_scenarios(*, repo_root=None, milestone: Optional[str] = None) -> dict:
    """Replay all saved scenarios (optionally one MS's). Returns::

        {
          "results": [ {path,name,spec_ref,milestone,passed,origin,...}, ... ],
          "all_passed": bool,
          "product_regressions": [ ... ],   # actionable: the product regressed
          "infra_failures": [ ... ],        # the net is flaky/broken, not product
        }
    """
    rows = scenario_store.list_scenarios(repo_root=repo_root, milestone=milestone)
    results = [_run_one(r) for r in rows]
    return {
        "results": results,
        "all_passed": all(r["passed"] for r in results),
        "product_regressions": [r for r in results
                                if not r["passed"] and r["origin"] == ORIGIN_PRODUCT],
        "infra_failures": [r for r in results
                           if not r["passed"] and r["origin"] == ORIGIN_INFRA],
    }


# The non-verdict label carried on attainment evidence — the boundary the leader
# ratified, kept as data so the wording is single-sourced and reviewable.
ATTAINMENT_EVIDENCE_LABEL = (
    "これは『SPEC 由来 journey が実際に動く』構造的証拠 (journey-pass evidence) "
    "であって、『この MS が目的を達成した』という verdict ではない。あるべき意図の "
    "網羅性 (未実装の highest/high) や spirit の遵守 (letter-met/spirit-violated) は "
    "この証拠では答えられない — 最終的な達成判定は人間/leader のレビューが持つ "
    "(auto-close しない)。"
)


def attainment_evidence(milestone: str, *, repo_root=None) -> dict:
    """Journey-pass evidence for an MS's attainment review — NOT a verdict.

    Replays the MS's saved scenarios and returns which SPEC-derived journeys are
    green/red, wrapped in an explicit non-verdict label. Feeds the human/leader
    attainment review (ms-119); it never decides attainment or closes the MS.
    """
    run = run_saved_scenarios(repo_root=repo_root, milestone=milestone)
    return {
        "milestone": milestone,
        "kind": "journey-pass-evidence",
        "is_verdict": False,
        "label": ATTAINMENT_EVIDENCE_LABEL,
        "all_journeys_green": run["all_passed"],
        "journey_count": len(run["results"]),
        "journeys": [{"name": r["name"], "spec_ref": r["spec_ref"],
                      "passed": r["passed"], "origin": r["origin"],
                      "responsible_layer": r.get("responsible_layer")}
                     for r in run["results"]],
    }
