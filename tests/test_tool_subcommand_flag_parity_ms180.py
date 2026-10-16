"""ms-180 — the per-subcommand flag table must stay honest in BOTH frontends.

Background. ``beacon tool`` is parsed twice: ``bin/beacon`` has a ``case`` arm per
subcommand, and ``beacon_cli/dispatch.py`` declares every ``tool`` flag on ONE
shared argparse parser and narrows it with the hand-written
``_TOOL_SUBCOMMAND_FLAGS`` table. That table was added to close a real defect
(``tool add --recorder evidence`` parsed on the Python side and was silently
ignored, while bash rejected it) — but the fix was a hand-written MIRROR pinned by
four example tests.

An independent maintainability review of PR #802 showed that was false safety:
widening one side by a single entry left bash rejecting and Python accepting, with
the whole test subset AND ``ci-strict-drift-guards.sh`` still green. The repo-wide
CLI-drift guard cannot see it — it compares per VERB, and since all ``tool`` flags
share one parser by design, the verb-level sets match trivially.

So these tests cover the GUARD, not just the tables: a guard that cannot go red is
worse than no guard, because a green one gets trusted. Each inversion mutates one
side and asserts the guard fails — and, having been burned once already, asserts
the mutation actually landed before trusting its verdict (an inversion that edits
the wrong place "passes" vacuously, which is a false negative in the test of the
test).
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check-tool-subcommand-flag-parity.py"
BIN_BEACON = REPO / "bin" / "beacon"
DISPATCH = REPO / "beacon_cli" / "dispatch.py"


def _load():
    spec = importlib.util.spec_from_file_location("_tool_parity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parity = _load()


def _run(*extra: str) -> int:
    """Exit code only — deliberately NOT through a pipe.

    Measuring a guard's verdict through ``| tail`` reports the pipe's status, not
    the guard's; that mistake made an inversion look like it had passed while
    writing these.

    ``extra`` carries ``--bin`` / ``--dispatch`` so an inversion can point the
    guard at a COPY. See ``_broken_pair``.
    """
    return subprocess.run([sys.executable, str(SCRIPT), *extra],
                          capture_output=True, text=True, timeout=60).returncode


def _broken_pair(tmp_path, *, bin_text: str = "", dispatch_text: str = ""):
    """Copy both sides into ``tmp_path``, optionally replacing their contents.

    Inversions MUST run against copies. Editing the repo's own ``bin/beacon``
    (or ``beacon_cli/dispatch.py``) in place and restoring it afterwards leaves
    a window during which every other test worker that spawns the CLI executes
    the half-written file. That is not hypothetical: it surfaced as
    `$1: unbound variable` reported at a line number PAST the end of the
    pristine 5221-line file, and as `Text file busy`, failing whichever
    unrelated test happened to spawn `beacon` at that moment — one victim per
    run, a different one each time. The repo under test is shared state.

    Returns the ``--bin``/``--dispatch`` argv for ``_run`` plus both paths.
    """
    bin_copy = tmp_path / "beacon"
    dispatch_copy = tmp_path / "dispatch.py"
    bin_copy.write_text(bin_text or BIN_BEACON.read_text(encoding="utf-8"),
                        encoding="utf-8")
    dispatch_copy.write_text(
        dispatch_text or DISPATCH.read_text(encoding="utf-8"), encoding="utf-8")
    argv = ("--bin", str(bin_copy), "--dispatch", str(dispatch_copy))
    return argv, bin_copy, dispatch_copy


# ---------------------------------------------------------------------------
# The live tree agrees, and the guard is actually looking at something.
# ---------------------------------------------------------------------------

def test_the_two_frontends_agree_today():
    assert _run() == 0


def test_the_guard_actually_parsed_subcommands_from_both_sides():
    """A guard that found nothing would report OK. Pin that it sees real arms on
    both sides, and the same set of subcommands."""
    result = parity.audit()
    assert len(result["bash"]) >= 7, result["bash"]
    assert set(result["bash"]) == set(result["python"])
    assert result["problems"] == []


def test_it_compares_every_subcommand_not_a_sample():
    """The predecessor's weakness was sampling four combinations. Every
    subcommand bash exposes must appear in the comparison."""
    result = parity.audit()
    for sub in ("add", "list", "show", "remove", "lookup", "used",
                "unregistered"):
        assert sub in result["bash"], sub
        assert sub in result["python"], sub


def test_universal_flags_are_excluded_from_both_sides():
    """``--json`` / ``--help`` are accepted everywhere; counting them would make
    every subcommand look mismatched and the guard would be switched off."""
    result = parity.audit()
    for flags in list(result["bash"].values()) + list(result["python"].values()):
        assert "--json" not in flags
        assert "--help" not in flags


# ---------------------------------------------------------------------------
# Inversions — both directions, plus the loud-failure path.
# ---------------------------------------------------------------------------

def test_inversion_widening_only_the_python_side_goes_red(tmp_path):
    """The judge's exact edit: one entry added to the Python table."""
    text = DISPATCH.read_text(encoding="utf-8")
    assert '    "show": set(),' in text, "anchor moved; this inversion is stale"
    argv, _bin_copy, dispatch_copy = _broken_pair(
        tmp_path,
        dispatch_text=text.replace('    "show": set(),',
                                   '    "show": {"target"},', 1))
    assert parity.python_tool_subcommand_flags(
        str(dispatch_copy))["show"] == {"--target"}, (
        "the mutation did not land, so this inversion proves nothing")
    assert _run(*argv) == 1


def test_inversion_widening_only_the_bash_side_goes_red(tmp_path):
    """The other direction. NOTE: the ``show)`` arm must be located INSIDE the
    ``tool|tools)`` block — an unscoped search finds ``task``'s ``show)`` first,
    which is how a first attempt at this test silently edited the wrong arm and
    reported a false pass."""
    text = BIN_BEACON.read_text(encoding="utf-8")
    tm = re.search(r"^    tool\|tools\)\n", text, re.MULTILINE)
    assert tm, "the tool|tools) block moved; this inversion is stale"
    nxt = re.search(r"^    [a-z][a-z0-9|_\-]*\)\n", text[tm.end():],
                    re.MULTILINE)
    blk_start = tm.end()
    blk_end = tm.end() + nxt.start() if nxt else len(text)
    block = text[blk_start:blk_end]
    m = re.search(r"^            show\)\n", block, re.MULTILINE)
    assert m, "tool's own show) arm not found inside the tool block"
    end = block.find("                ;;", m.end())
    arm = block[m.end():end]
    needle = '--json) _tool_json="1"; shift ;;'
    assert needle in arm, arm
    arm2 = arm.replace(
        needle,
        '--target) _tool_target="${2:-}"; shift 2 ;;\n'
        '                        ' + needle, 1)
    argv, bin_copy, _dispatch_copy = _broken_pair(
        tmp_path,
        bin_text=(text[:blk_start] + block[:m.end()] + arm2 + block[end:]
                  + text[blk_end:]))

    drift = parity._load_drift_helpers()
    assert "--target" in parity.bash_tool_subcommand_flags(
        drift, str(bin_copy))["show"], (
        "the mutation did not land, so this inversion proves nothing")
    assert _run(*argv) == 1


def test_renaming_the_python_table_fails_loudly_instead_of_passing(tmp_path):
    """If the table is renamed, "found nothing" must not read as "nothing is
    wrong" — the guard has to say it lost its anchor."""
    text = DISPATCH.read_text(encoding="utf-8")
    argv, _bin_copy, dispatch_copy = _broken_pair(
        tmp_path,
        dispatch_text=text.replace("_TOOL_SUBCOMMAND_FLAGS = {",
                                   "_TOOL_SUBCOMMAND_FLAGS_RENAMED = {", 1))
    with pytest.raises(RuntimeError):
        parity.python_tool_subcommand_flags(str(dispatch_copy))
    assert _run(*argv) == 1


def test_a_missing_bash_block_fails_loudly(tmp_path):
    """Same property on the bash side, via a stub file rather than by mutating
    the real one."""
    stub = tmp_path / "beacon"
    stub.write_text("#!/usr/bin/env bash\ncase \"$1\" in\n    other)\n        :\n        ;;\nesac\n",
                    encoding="utf-8")
    drift = parity._load_drift_helpers()
    with pytest.raises(RuntimeError):
        parity.bash_tool_subcommand_flags(drift, str(stub))


def test_the_guard_is_wired_into_the_strict_ci_gate():
    """A guard nobody runs is documentation. It must be on the gate that blocks
    both a merge and a release."""
    gate = (REPO / "scripts" / "ci-strict-drift-guards.sh").read_text(
        encoding="utf-8")
    assert "check-tool-subcommand-flag-parity.py" in gate


def test_the_guard_needs_no_pytest_to_run():
    """``ci-strict-drift-guards.sh`` is also called from the lint-docs job, which
    has no pytest installed — a guard importing it would be green locally and red
    in CI (this repo has already shipped that mistake once)."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import pytest" not in source
    assert "pytest" not in source.replace("tests/test_tool_recorder", "")
