"""A guard that does not run is documentation (ms-180 follow-up).

Two guards added in ms-180 — one holding "only one place reads which Skill a
call names", one holding "no test writes to the checkout" — were wired into
`scripts/ci-strict-drift-guards.sh` and then did not run on the commits they
exist to catch: the workflow that calls that script had a `paths` filter listing
neither `lib/*.py` nor `tests/**`. Measured, not inferred — of three consecutive
commits touching those trees, the job fired only on the one that also happened
to touch `scripts/`.

Worse, the test asserting each guard was "wired into the strict CI gate" passed
the whole time, because it grepped the shell script for the guard's filename.
The guard was present and inert, and the check for it was green — the same
false-safety shape as a `pytest.skip` that never un-skips.

So "wired in" is checked at both ends here: the script calls the guard, AND the
workflow that calls the script actually fires for the trees the guard walks.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
GATE_SCRIPT = REPO / "scripts" / "ci-strict-drift-guards.sh"


def _workflows_calling_the_gate() -> list:
    """Workflow files that invoke the strict drift-guard script."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if "ci-strict-drift-guards" in path.read_text(encoding="utf-8"):
            found.append(path)
    return found


def _triggers(path: pathlib.Path) -> dict:
    """The workflow's `on:` block. PyYAML parses the bare key `on` as True."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("on", data.get(True)) or {}


def test_the_gate_script_is_called_by_at_least_one_workflow():
    assert _workflows_calling_the_gate(), (
        "no workflow calls scripts/ci-strict-drift-guards.sh, so every guard "
        "in it is documentation")


@pytest.mark.parametrize(
    "guard",
    ["check-single-skill-parser.py", "check-tests-dont-mutate-repo.py",
     "check-tool-subcommand-flag-parity.py", "check-mandated-tools.py"])
def test_each_guard_is_called_by_the_gate_script(guard):
    """One end: the script runs the guard."""
    assert guard in GATE_SCRIPT.read_text(encoding="utf-8"), (
        f"{guard} is not called by the strict gate script")


def test_the_pr_gate_fires_for_every_pull_request():
    """The other end, and the one that was broken.

    A `paths` filter here is a second, hand-kept copy of scopes that each guard
    declares for itself in Python (one walks lib/ bin/ beacon_cli/, another walks
    tests/ and conftest). Keeping a YAML list in step with those by hand is the
    drift this repo removes rather than polices, and it had already drifted. If a
    filter is wanted again, it must be derived from the guards' own scopes — not
    retyped — and this test is where that belongs.
    """
    offenders = []
    for path in _workflows_calling_the_gate():
        triggers = _triggers(path)
        if "pull_request" not in triggers:
            continue  # a release-only workflow is not the PR gate
        pr = triggers["pull_request"]
        if isinstance(pr, dict) and pr.get("paths"):
            offenders.append((path.name, pr["paths"]))
    assert offenders == [], (
        "a workflow running the strict drift guards filters pull requests by "
        "path, so a change inside a guard's own scan scope can skip the guard "
        "entirely while CI stays green:\n" + "\n".join(
            f"  {name}: {paths}" for name, paths in offenders))


def test_the_guards_scan_trees_the_pr_gate_would_see():
    """The scopes themselves, so this test keeps meaning if a filter returns.

    Asserts that the trees the two ms-180 guards walk are the ordinary source
    trees — if a guard is ever narrowed to something exotic, the reasoning above
    ("no filter, so everything is covered") needs re-examining.
    """
    parser_guard = (REPO / "scripts" / "check-single-skill-parser.py").read_text(
        encoding="utf-8")
    assert re.search(r'"lib/\*\.py"', parser_guard), parser_guard[:0]
    assert "beacon_cli/**/*.py" in parser_guard

    mutation_guard = (REPO / "scripts" / "check-tests-dont-mutate-repo.py").read_text(
        encoding="utf-8")
    assert "test_*.py" in mutation_guard
    assert "conftest.py" in mutation_guard
