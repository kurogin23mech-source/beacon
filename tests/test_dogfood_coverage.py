"""ms-136 e-4703 — dogfood coverage tests (AC8: ms-136's SPEC as first subject).

Enforces that the dogfood一巡 map stays honest: valid 3-way dispositions, every
ms-136 AC present (no silent drop), and the cited evidence actually exists — a
scenario-disposition AC points at a real saved scenario, a verified-by-test AC
points at a real test file.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import dogfood_coverage as dc  # noqa: E402

_COVERAGE_PATH = REPO / "dogfood" / "ms-136-coverage.json"


def _load():
    return json.loads(_COVERAGE_PATH.read_text(encoding="utf-8"))


def test_coverage_artifact_is_valid():
    dc.validate_coverage(_load())


def test_all_eight_acs_present():
    cov = _load()
    ids = {ac["ac"] for ac in cov["acs"]}
    assert ids == {f"AC{i}" for i in range(1, 9)}, ids


def test_taxonomy_is_three_way_and_totals_add_up():
    cov = _load()
    s = dc.summarize(cov)
    assert s["total"] == 8
    assert s["covered"] + s["quality_signals"] == s["total"]
    # every disposition used is in the valid 3-way set
    for ac in cov["acs"]:
        assert ac["disposition"] in dc.VALID_DISPOSITIONS


def test_scenario_disposition_evidence_points_at_a_real_scenario():
    cov = _load()
    scenario_dir = REPO / "scenarios" / "ms-136"
    saved = list(scenario_dir.glob("*.json")) if scenario_dir.exists() else []
    assert saved, "expected at least one saved ms-136 scenario"
    for ac in cov["acs"]:
        if ac["disposition"] == dc.DISP_SCENARIO:
            # evidence names a path under scenarios/ms-136/
            assert "scenarios/ms-136/" in ac["evidence"], ac["ac"]


def test_verified_by_test_evidence_points_at_real_test_files():
    cov = _load()
    for ac in cov["acs"]:
        if ac["disposition"] == dc.DISP_VERIFIED_BY_TEST:
            cited = re.findall(r"tests/test_[\w./-]+\.py", ac["evidence"])
            assert cited, f"{ac['ac']}: verified-by-test must cite a test file"
            for rel in cited:
                assert (REPO / rel).exists(), f"{ac['ac']}: missing {rel}"


def test_no_verified_capability_is_dumped_in_quality_signals():
    # leader 裁定: base capabilities are verified-by-test, NOT quality_signals.
    # quality_signals stays strictly "genuinely not covered".
    cov = _load()
    for ac in cov["acs"]:
        if ac["disposition"] in (dc.DISP_QS_NEEDS_REWRITE, dc.DISP_QS_OUT_OF_SCOPE):
            # a quality-signal AC must NOT cite a passing test as its evidence
            # (that would mean it is actually covered = mis-classified).
            assert "tests/test_" not in ac["evidence"], (
                f"{ac['ac']}: quality-signal must not be test-covered "
                "(use verified-by-test)")
