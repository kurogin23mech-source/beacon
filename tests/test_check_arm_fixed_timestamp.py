"""Guard test for scripts/check-arm-fixed-timestamp.py (ms-169 e-6305 follow-up).

The guard forbids a hardcoded absolute `at=` timestamp in untrusted_turn.arm()
test calls — such a fixed past time self-prunes via _STALE_HOURS and turns the
test RED once the wall clock passes it (the 2026-09-09 date-bomb). This locks the
guard's own behavior: it must flag the anti-pattern (single- and multi-line) and
must NOT flag clean code or a non-arm `at=` string.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-arm-fixed-timestamp.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_arm_fixed_timestamp", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Build the bomb fixtures via a year variable so THIS test file's own source does
# not literally contain the `at="20XX-..."` pattern (which would make the guard
# flag its own guard-test — a self-referential false positive). The WRITTEN
# fixture still contains the rendered pattern, which is what _scan reads.
_Y = "2026"
_AT = f'at="{_Y}-09-07T07:00:00Z"'          # renders to at="2026-09-07T07:00:00Z"
_AT2 = f'at="20{"30"}-01-02T03:04:05Z"'      # renders to at="2030-01-02T03:04:05Z"


def test_flags_single_line_hardcoded_at(tmp_path):
    guard = _load()
    p = tmp_path / "test_x.py"
    p.write_text(f'def t():\n    ut.arm(root, "k", event_ids=["e-1"], {_AT})\n')
    hits = guard._scan(str(p))
    assert len(hits) == 1
    assert hits[0][0] == 2  # lineno


def test_flags_multiline_arm_with_at(tmp_path):
    guard = _load()
    p = tmp_path / "test_ml.py"
    p.write_text(
        'def t():\n'
        '    ut.arm(root, "k",\n'
        '           sources=[du.build_source(ev)],\n'
        f'           {_AT2})\n')
    hits = guard._scan(str(p))
    assert len(hits) == 1


def test_does_not_flag_clean_or_nonarm(tmp_path):
    guard = _load()
    p = tmp_path / "test_clean.py"
    p.write_text(
        'def t():\n'
        '    ut.arm(root, "k", event_ids=["e-1"])  # at defaults to now\n'
        f'    label = "{_AT} in a string, not a real arm call arg"\n'
        '    created_at = "2026-01-01"  # created_at= must NOT match (\\bat boundary)\n')
    assert guard._scan(str(p)) == []


def test_repo_tests_are_clean():
    """The actual tests/ tree must have no date-bomb (the e-6305 fix removed the
    one that existed). This is the live guard, not just a synthetic case."""
    guard = _load()
    violations = []
    for path in guard._iter_test_files([]):
        violations.extend(guard._scan(path))
    assert violations == [], f"date-bomb arm() at= found: {violations}"
