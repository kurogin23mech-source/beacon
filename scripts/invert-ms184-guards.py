#!/usr/bin/env python3
"""Inversion harness for the ms-184 guards (e-7015 / e-7016).

Why
===
A test that has never been watched failing proves nothing. This repo has
shipped false-green guards three times — a substring check that matched a help
string, an alias table that mapped every verb to itself, a "move" written as
an "add" — each time looking exactly as reassuring as a real guard. So each
ms-184 fix is RE-BROKEN here, in a throwaway copy of the tree, and the harness
fails unless at least one named test goes red.

Run it directly (``python3 scripts/invert-ms184-guards.py``) or let
``tests/test_progress_window_ms184.py::test_inversion_harness_is_wired`` run
it. Deliberately pytest-INDEPENDENT at the top level (it shells out), so it
also works from a lint-only context with no pytest plugins loaded.

Adding a fix to ms-184's surface means adding an INVERSION here. If a fix
cannot be inverted into a form that turns a test red, it has no guard.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = "tests/test_progress_window_ms184.py"
#: Runs the harness itself — excluded or the harness would recurse forever.
SELF_TEST = "test_inversion_harness_is_wired"

# (name, relative file, snippet to replace, replacement, tests that must go red)
INVERSIONS = [
    (
        "absence folds back into a match (the original e-7016 bug)",
        "lib/date_window.py",
        "    if not value:\n        return UNDATED\n",
        "    if not value:\n        return IN_WINDOW\n",
        ["test_absent_timestamp_is_undated_not_a_match",
         "test_search_excludes_undated_and_reports_how_many",
         "test_derive_returns_nothing_for_an_unrelated_period",
         "test_derive_reports_records_it_could_not_judge"],
    ),
    (
        "a date-only upper bound stops covering its own day",
        "lib/date_window.py",
        "    if end and _DATE_ONLY.match(value):\n        return value + _END_OF_DAY\n",
        "",
        ["test_date_only_upper_bound_covers_the_whole_day",
         "test_search_window_covers_the_whole_end_day"],
    ),
    (
        "classify_any demands every stamp be in window",
        "lib/date_window.py",
        "    if IN_WINDOW in verdicts:\n        return IN_WINDOW\n",
        "    if all(v == IN_WINDOW for v in verdicts):\n        return IN_WINDOW\n",
        ["test_classify_any_needs_only_one_real_stamp",
         "test_derive_counts_each_in_window_moment_of_a_record"],
    ),
    (
        "search goes back to the two-valued inline comparison",
        "lib/search.py",
        '    if not _in_window(created_at, from_date, to_date, entity_type, undated):\n'
        '        return False\n',
        "    if from_date and created_at and created_at < from_date:\n"
        "        return False\n"
        "    if to_date and created_at and created_at > to_date:\n"
        "        return False\n",
        ["test_search_excludes_undated_and_reports_how_many"],
    ),
    (
        "search stops reporting what it could not judge",
        "lib/search.py",
        '        "undated": {\n'
        '            "excluded": sum(undated.values()),\n'
        '            "by_type": dict(sorted(undated.items())),\n'
        '        },\n',
        '        "undated": {"excluded": 0, "by_type": {}},\n',
        ["test_search_excludes_undated_and_reports_how_many"],
    ),
    (
        "progress is judged by the Target's own creation date again",
        "lib/progress_window.py",
        '    # ---- evidence source 1: child records on the Target\'s declared arms --\n',
        '    for _tid, _row in per_target.items():\n'
        '        _add(_tid, "target-created", _tid, "2026-10-14T00:00:00Z", "created")\n'
        '    # ---- evidence source 1: child records on the Target\'s declared arms --\n',
        ["test_derive_returns_the_target_that_moved_not_the_one_created",
         "test_derive_returns_nothing_for_an_unrelated_period"],
    ),
    (
        "a class with no declared arms is read strictly (drops Operations)",
        "lib/progress_window.py",
        "    return declared or all_arms\n",
        "    return declared\n",
        ["test_derive_sees_an_operation_whose_arms_are_undeclared"],
    ),
    (
        "the arm name is hardcoded instead of read from the declaration",
        "lib/progress_window.py",
        '    declared = tuple((_occ.target_decomposition(project).get(collection)\n'
        '                      or {}).get("arms") or ())\n'
        "    return declared or all_arms\n",
        '    return ("entries",)\n',
        ["test_derive_is_occupation_generic_without_naming_an_arm",
         "test_progress_window_hardcodes_no_arm_or_collection_name"],
    ),
    (
        "unreadable projects are skipped instead of reported",
        "lib/progress_window.py",
        '        if row.get("error"):\n'
        '            unavailable.append({\n'
        '                "project_id": pid, "name": name, "error": str(row["error"]),\n'
        '            })\n'
        "            continue\n",
        '        if row.get("error"):\n'
        "            continue\n",
        ["test_aggregate_drops_non_movers_but_keeps_unreadable_projects"],
    ),
    (
        "non-movers come back into the cross-project list",
        "lib/progress_window.py",
        '        if not result.get("moved"):\n            continue\n',
        '        if False:\n            continue\n',
        ["test_aggregate_drops_non_movers_but_keeps_unreadable_projects"],
    ),
    (
        "sources never report themselves as unconsulted",
        "lib/progress_window.py",
        "        (consulted if value is not None else unavailable).append(name)\n",
        "        consulted.append(name)\n",
        ["test_derive_says_which_sources_it_did_not_consult"],
    ),
    (
        "capping the evidence list also lowers the reported count",
        "lib/progress_window.py",
        '            "evidence_count": len(evidence),\n'
        '            "evidence_by_kind": by_kind,\n',
        '            "evidence_count": len(evidence if evidence_limit <= 0\n'
        '                                  else evidence[:evidence_limit]),\n'
        '            "evidence_by_kind": by_kind,\n',
        ["test_derive_caps_evidence_rows_without_understating_the_count"],
    ),
    (
        "an unparseable period silently becomes the default window",
        "lib/date_window.py",
        '    raise ValueError(\n'
        '        f"unparseable period {spec!r}. Accepted: " + "; ".join(SINCE_FORMS)\n'
        "    )\n",
        "    return _d(base - datetime.timedelta(days=6)), _d(base)\n",
        ["test_parse_since_refuses_rather_than_substituting_a_window"],
    ),
]


def _scratch_tree() -> str:
    """Copy the source roots a test needs into a throwaway directory.

    A copy (not an in-place patch) means an interrupted run can never leave
    the real tree broken — which matters because an inversion here deliberately
    reintroduces a data-corrupting bug.
    """
    tmp = tempfile.mkdtemp(prefix="ms184-inversion-")
    for sub in ("lib", "scripts", "tests"):
        shutil.copytree(os.path.join(REPO, sub), os.path.join(tmp, sub),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp


def _run(tree: str, tests) -> subprocess.CompletedProcess:
    expr = " or ".join(tests)
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", TEST_FILE, "-q",
         "-p", "no:randomly", "-p", "no:cacheprovider",
         "-k", f"({expr}) and not {SELF_TEST}"],
        cwd=tree, capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def main() -> int:
    tree = _scratch_tree()
    failures = []
    try:
        all_tests = sorted({t for inv in INVERSIONS for t in inv[4]})
        base = _run(tree, all_tests)
        if base.returncode != 0:
            print("BASELINE IS NOT GREEN — fix the tests before inverting:")
            print(base.stdout[-4000:])
            return 1
        print(f"baseline green ({len(all_tests)} tests)\n")

        for name, rel, old, new, tests in INVERSIONS:
            path = os.path.join(tree, rel)
            with open(path, encoding="utf-8") as f:
                original = f.read()
            if original.count(old) != 1:
                failures.append(
                    f"{name}: snippet not found exactly once in {rel} "
                    f"(found {original.count(old)}). The code moved — "
                    "update this inversion, do not delete it.")
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(original.replace(old, new, 1))
            try:
                proc = _run(tree, tests)
                if proc.returncode == 0:
                    failures.append(
                        f"{name}: reverted the fix in {rel} and "
                        f"{', '.join(tests)} STILL PASSED — these tests do not "
                        "actually guard it.")
                    print(f"  FALSE GREEN  {name}")
                else:
                    print(f"  red as expected  {name}")
            finally:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(original)
    finally:
        shutil.rmtree(tree, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} inversion(s) did not produce a red test:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK: all {len(INVERSIONS)} inversions turn a named test red.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
