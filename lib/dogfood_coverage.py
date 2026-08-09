"""ms-136 e-4703 — dogfood coverage: ms-136's own SPEC as the first subject.

The capstone (SPEC AC8): run the auto-debug基盤 on its own SPEC and honestly
record, per acceptance criterion, HOW it is verified. The taxonomy is 3-way
(leader 裁定) — every AC lands in exactly one, so the coverage map cannot
silently drop an intent:

  - ``scenario``          — turned into a runnable SPEC-derived journey
                            (evidence = the saved scenario path; it replays green
                            in the regression suite).
  - ``verified-by-test``  — a self-referential BASE capability of the基盤 itself
                            (a generator exists / bisect localizes / CI両用) that
                            does not become an end-user journey, but IS verified
                            by a dedicated unit/integration test (evidence = the
                            test path). Same frame as a structural-invariant
                            assert (e-4702): verified, just not via a scenario —
                            NOT a quality_signal.
  - ``quality-signal``    — genuinely NOT covered: either observably-unwritable
                            (needs-observable-rewrite = a SPEC defect) or only
                            observable past Beacon's boundary
                            (out-of-scope-boundary = 方針4, not a defect).

Keeping ``verified-by-test`` out of quality_signals (leader 裁定, mirrors the
e-4699 quality_signals purification) keeps the taxonomy honest: quality_signals
stays strictly "not covered", so it remains a real gap signal rather than a
dumping ground for "covered elsewhere".
"""

from __future__ import annotations

import scenario_runner

DISP_SCENARIO = "scenario"
DISP_VERIFIED_BY_TEST = "verified-by-test"
DISP_QS_NEEDS_REWRITE = scenario_runner.QS_NEEDS_REWRITE      # SPEC 品質欠陥
DISP_QS_OUT_OF_SCOPE = scenario_runner.QS_OUT_OF_SCOPE         # 方針4 縁の外

VALID_DISPOSITIONS = {
    DISP_SCENARIO,
    DISP_VERIFIED_BY_TEST,
    DISP_QS_NEEDS_REWRITE,
    DISP_QS_OUT_OF_SCOPE,
}


class CoverageError(Exception):
    """The dogfood coverage map is malformed (bad disposition, missing evidence,
    or an AC left undispositioned)."""


def validate_coverage(coverage: dict) -> None:
    """Validate a dogfood coverage artifact: it names the milestone + spec_ref,
    and every AC entry carries a valid disposition and non-empty evidence.
    Raises CoverageError. This is what keeps the map honest — an AC cannot be
    added without saying HOW it is verified."""
    if not isinstance(coverage, dict):
        raise CoverageError("coverage must be an object")
    if not (coverage.get("milestone") or "").strip():
        raise CoverageError("coverage needs a 'milestone'")
    if not (coverage.get("spec_ref") or "").strip():
        raise CoverageError("coverage needs a 'spec_ref' (the SPEC being dogfooded)")
    acs = coverage.get("acs")
    if not isinstance(acs, list) or not acs:
        raise CoverageError("coverage needs a non-empty 'acs' list")
    seen = set()
    for i, ac in enumerate(acs):
        if not isinstance(ac, dict):
            raise CoverageError(f"acs[{i}] must be an object")
        ac_id = (ac.get("ac") or "").strip()
        if not ac_id:
            raise CoverageError(f"acs[{i}] needs an 'ac' id (e.g. 'AC4')")
        if ac_id in seen:
            raise CoverageError(f"acs[{i}]: duplicate AC id {ac_id!r}")
        seen.add(ac_id)
        if ac.get("disposition") not in VALID_DISPOSITIONS:
            raise CoverageError(
                f"acs[{i}] ({ac_id}): disposition must be one of "
                f"{sorted(VALID_DISPOSITIONS)} — 3-way taxonomy (scenario / "
                f"verified-by-test / quality-signal); got {ac.get('disposition')!r}")
        if not (ac.get("evidence") or "").strip():
            raise CoverageError(
                f"acs[{i}] ({ac_id}): needs 'evidence' (scenario path / test path "
                "/ reason) — an AC cannot be dispositioned without saying HOW")


def summarize(coverage: dict) -> dict:
    """A by-disposition tally + the covered-vs-quality-signal split, for a
    human-readable dogfood一巡 report."""
    by = {d: 0 for d in VALID_DISPOSITIONS}
    for ac in coverage.get("acs", []):
        by[ac["disposition"]] = by.get(ac["disposition"], 0) + 1
    covered = by[DISP_SCENARIO] + by[DISP_VERIFIED_BY_TEST]
    gaps = by[DISP_QS_NEEDS_REWRITE] + by[DISP_QS_OUT_OF_SCOPE]
    return {"by_disposition": by, "covered": covered, "quality_signals": gaps,
            "total": len(coverage.get("acs", []))}
