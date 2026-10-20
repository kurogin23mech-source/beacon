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
#: Every ms-184 test file the inversions select from. A fix guarded in one
#: file must still be reachable when the harness runs, so the selection spans
#: all of them rather than assuming one home.
TEST_FILES = ("tests/test_progress_window_ms184.py",
              "tests/test_cmd_recap_ms184.py",
              "tests/test_recap_entrypath_ms184.py",
              "tests/test_recap_frontend_parity_ms184.py",
              # ms-181 e-7101 brought `beacon retro` onto this same period
              # surface (it had its own text comparisons and no validation at
              # all). One surface, one harness — a second inversion script
              # would drift from this one.
              "tests/test_retro_period_ms181.py")
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
        "the arm walk stops recursing (loses nested evidence)",
        "lib/progress_window.py",
        "            out.append(item)\n"
        "            _walk_arms(item, arms, out, task_index, target_id)\n",
        "            out.append(item)\n",
        ["test_derive_reaches_evidence_nested_two_levels_deep",
         "test_derive_reaches_evidence_three_levels_deep"],
    ),
    (
        "the cross-project fold drops the caveats of the projects it omits",
        "lib/progress_window.py",
        "        undated_total += int((result.get(\"undated\") or {}).get(\"count\") or 0)\n"
        "        for src in result.get(\"sources_unavailable\") or []:\n"
        "            if src not in missing_sources:\n"
        "                missing_sources.append(src)\n",
        "",
        ["test_aggregate_carries_the_caveats_of_projects_it_dropped"],
    ),
    (
        "the fold accumulates caveats only for the movers it keeps",
        "lib/progress_window.py",
        "        undated_total += int((result.get(\"undated\") or {}).get(\"count\") or 0)\n"
        "        for src in result.get(\"sources_unavailable\") or []:\n"
        "            if src not in missing_sources:\n"
        "                missing_sources.append(src)\n"
        "        if not result.get(\"moved\"):\n"
        "            continue\n",
        "        if not result.get(\"moved\"):\n"
        "            continue\n"
        "        undated_total += int((result.get(\"undated\") or {}).get(\"count\") or 0)\n"
        "        for src in result.get(\"sources_unavailable\") or []:\n"
        "            if src not in missing_sources:\n"
        "                missing_sources.append(src)\n",
        ["test_aggregate_carries_the_caveats_of_projects_it_dropped"],
    ),
    (
        "a one-sided --from/--to is accepted again (silent all-time window)",
        "lib/cmd_recap.py",
        "    if bool(from_date) != bool(to_date):\n",
        "    if False:\n",
        ["test_one_sided_explicit_range_is_refused"],
    ),
    (
        "a negative count means 'all' again",
        "lib/cmd_recap.py",
        "    if value < 0:\n",
        "    if False:\n",
        ["test_negative_count_is_refused_not_read_as_all"],
    ),
    (
        "the fallback reason goes back to process-lifetime state",
        "lib/cmd_recap.py",
        "            if fallback_reason:\n"
        '                result["fallback_reason"] = fallback_reason\n',
        "",
        ["test_projects_falls_back_and_carries_the_reason_it_observed",
         "test_fallback_reason_does_not_leak_between_calls"],
    ),
    (
        "bash checks the project before it answers `recap` with no subcommand",
        "bin/beacon",
        "            targets|projects) ensure_project; shift ;;\n",
        "            targets|projects) shift ;;\n",
        ["test_both_frontends_agree_on_the_no_project_help_path"],
    ),
    (
        "the Python front stops declaring --project on `projects`",
        "beacon_cli/dispatch.py",
        '    p_recap_projects.add_argument("--project", dest="recap_project", default="")\n',
        "",
        ["test_both_frontends_refuse_the_misplaced_project_flag"],
    ),
    (
        "the range-order rule is copied back into the CLI boundary",
        "lib/date_window.py",
        # ms-181 e-7101 parametrised the flag names in this message; the
        # snippet follows the code rather than the inversion being dropped.
        '    if lo and hi and lo > hi:\n'
        '        return f"{lower_flag} {lo} is after {upper_flag} {hi}."\n',
        '    return ""  # noqa\n    if False:\n        return ""\n',
        ["test_inverted_explicit_range_is_refused"],
    ),
    (
        "a failed document read becomes [] (= indistinguishable from zero)",
        "lib/cmd_recap.py",
        "    except _REMOTE_ERRORS + (ImportError,):\n        return None\n",
        "    except _REMOTE_ERRORS + (ImportError,):\n        return []\n",
        ["test_document_read_failure_is_not_reported_as_zero"],
    ),
    (
        "bash stops carrying --evidence to commands.py",
        "bin/beacon",
        '                --evidence)  recap_evidence="${2:-}"; shift 2 ;;\n',
        "",
        ["test_both_frontends_hand_commands_py_the_same_env"],
    ),
    (
        "bash sends `recap targets` to the projects backend",
        "bin/beacon",
        'python3 "$COMMANDS_PY" recap_targets',
        'python3 "$COMMANDS_PY" recap_projects',
        ["test_both_frontends_hand_commands_py_the_same_env"],
    ),
    (
        "the python front sends `recap targets` to the projects backend",
        "beacon_cli/dispatch.py",
        '_run_commands_py(root, "recap_targets", env)',
        '_run_commands_py(root, "recap_projects", env)',
        ["test_both_frontends_hand_commands_py_the_same_env"],
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
    # --- ms-181 e-7101: `beacon retro` joins this period surface -----------
    (
        "retro takes the period strings on trust again (the e-7101 bug)",
        "lib/cmd_retro.py",
        "    since, until = _resolve_window(since_spec, until_spec)\n",
        "    since, until = since_spec, until_spec\n",
        ["test_an_unreadable_period_exits_nonzero_instead_of_an_empty_week",
         "test_retro_reads_the_same_period_words_as_recap",
         "test_an_inverted_window_is_refused_in_this_verb_s_own_flag_names"],
    ),
    (
        "retro's resolved window goes back to echoing what was typed",
        "lib/cmd_retro.py",
        '            "requested": {"since": since_spec, "until": until_spec},\n',
        '            "requested": {"since": since, "until": until},\n',
        ["test_the_resolved_window_is_not_the_echo_of_what_was_typed"],
    ),
    (
        "retro's entry walker compares the stamps as raw text again",
        "lib/core.py",
        "        verdict = date_window.classify(date, since, until)\n"
        "        in_range = verdict == date_window.IN_WINDOW\n",
        "        in_range = True\n"
        "        if since and date < since:\n"
        "            in_range = False\n"
        "        if until and date > until:\n"
        "            in_range = False\n"
        "        verdict = date_window.classify(date, since, until)\n",
        ["test_a_full_timestamp_on_the_final_day_is_inside_the_window",
         "test_every_facet_of_one_payload_sees_the_same_record",
         "test_retro_reads_the_same_period_words_as_recap"],
    ),
    (
        "retro's deploy facet goes back to truncate-then-compare",
        "lib/cmd_retro.py",
        '        dep_verdict = _dw.classify(dep.get("date") or "", since, until)\n'
        '        if dep_verdict == _dw.UNDATED:\n'
        '            undated["deploy"] = undated.get("deploy", 0) + 1\n'
        "        if dep_verdict == _dw.IN_WINDOW:\n",
        '        dep_date = (dep.get("date") or "")[:10]\n'
        "        if (not since or dep_date >= since) and (not until or dep_date <= until):\n",
        ["test_a_deploy_with_no_date_is_counted_too"],
    ),
    (
        "retro stops reporting the records it could not judge",
        "lib/cmd_retro.py",
        '        "undated": {\n'
        '            "excluded": sum(undated.values()),\n'
        '            "by_type": dict(sorted(undated.items())),\n'
        "        },\n",
        '        "undated": {"excluded": 0, "by_type": {}},\n',
        ["test_undated_entries_are_excluded_and_reported",
         "test_a_deploy_with_no_date_is_counted_too"],
    ),
    (
        "the inverted-range message names flags the verb does not have",
        "lib/date_window.py",
        '        return f"{lower_flag} {lo} is after {upper_flag} {hi}."\n',
        '        return f"--from {lo} is after --to {hi}."\n',
        ["test_an_inverted_window_is_refused_in_this_verb_s_own_flag_names"],
    ),
    (
        "the python front resolves the period itself (a second parser)",
        "beacon_cli/dispatch.py",
        "    if not since:\n        since = _compute_default_since()\n",
        "    import sys as _s\n"
        '    _s.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))\n'
        "    import date_window as _dw2\n"
        "    if since:\n        since = _dw2.parse_since(since)[0]\n"
        "    if not since:\n        since = _compute_default_since()\n",
        ["test_neither_front_interprets_the_period_itself"],
    ),
    (
        "`beacon retro --help` stops listing the flags it reads",
        "lib/commands.py",
        '{"command": "beacon retro", "flags": ["--prepare", "--catch-up", '
        '"--since <period>", "--until <period>"]',
        '{"command": "beacon retro", "flags": []',
        ["test_the_help_registry_documents_the_flags_and_their_period"],
    ),
]


def _scratch_tree() -> str:
    """Copy the source roots a test needs into a throwaway directory.

    A copy (not an in-place patch) means an interrupted run can never leave
    the real tree broken — which matters because an inversion here deliberately
    reintroduces a data-corrupting bug.
    """
    tmp = tempfile.mkdtemp(prefix="ms184-inversion-")
    for sub in ("lib", "scripts", "tests", "bin", "beacon_cli"):
        shutil.copytree(os.path.join(REPO, sub), os.path.join(tmp, sub),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp


def _run(tree: str, tests, *, tag: str = "base") -> subprocess.CompletedProcess:
    """Run the named tests against ``tree``, with the bytecode cache isolated.

    ``-B`` / ``PYTHONDONTWRITEBYTECODE`` stop this process WRITING ``.pyc``
    files; they do not stop it READING a cache written earlier. On macOS the
    system Python keeps one under ``~/Library/Caches/com.apple.python`` keyed
    by absolute source path, so a harness that mutates a file and puts it back
    can be answered by the mutated version's bytecode — a false red, or (worse,
    since it reads as safety) a false green when the inversion is the thing
    cached. The scratch tree already lives at a fresh absolute path per run;
    ``PYTHONPYCACHEPREFIX`` per inversion closes the remaining door between
    one inversion and the next within a run.
    """
    expr = " or ".join(tests)
    cache = os.path.join(tempfile.gettempdir(),
                         f"ms184-inv-pyc-{os.getpid()}-{tag}")
    return subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *TEST_FILES, "-q",
         "-p", "no:randomly", "-p", "no:cacheprovider",
         "-k", f"({expr}) and not {SELF_TEST}"],
        cwd=tree, capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPYCACHEPREFIX": cache},
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

        for idx, (name, rel, old, new, tests) in enumerate(INVERSIONS):
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
                proc = _run(tree, tests, tag=f"inv{idx}")
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

        # Every inversion was put back — prove it. A restore that silently
        # failed (or a stale bytecode cache answering for a mutated file)
        # would otherwise leave the LAST inversion's red counted as a pass
        # for the next run, which is the failure mode this harness exists to
        # catch, turned on itself.
        after = _run(tree, all_tests, tag="final")
        if after.returncode != 0:
            failures.append(
                "the tree did not come back green after the inversions were "
                "reverted — a restore or the bytecode cache is at fault, so "
                "every 'red as expected' above is unproven:\n"
                + after.stdout[-2000:])
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
