"""ms-136 e-4699 dogfood — replay saved scenarios generated from ms-136's SPEC.

Proves the leader 論点1 芯: a saved scenario is a decision-free, deterministic
replay asset (non-determinism was isolated to generation time). Loading each
scenarios/ms-136/*.json and running it green here is the seed of e-4702 (CI
regression + MS-close attainment reuse) — the same artifact serves as both the
generator's output and a永続 regression test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import scenario_store as ss  # noqa: E402
import scenario_runner as sr  # noqa: E402

_SAVED = sorted((REPO / "scenarios" / "ms-136").glob("*.json")) \
    if (REPO / "scenarios" / "ms-136").exists() else []


@pytest.mark.skipif(not _SAVED, reason="no saved ms-136 scenarios yet")
@pytest.mark.parametrize("path", _SAVED, ids=[p.stem for p in _SAVED])
def test_saved_ms136_scenario_replays_green(path):
    scenario = ss.load_scenario(path)          # validates on load
    report = sr.run_scenario(scenario)
    assert report["passed"] is True, report["failure"]
    # every oracle carries both provenance axes (leader 論点2) — assert it on
    # the saved asset, not just at runtime.
    for step in scenario["steps"]:
        if step.get("kind") == "assert":
            assert step.get("spec_source", "").strip()
            assert step.get("observation_basis", "").strip()
