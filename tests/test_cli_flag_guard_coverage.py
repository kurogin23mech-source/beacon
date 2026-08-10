"""Every bin/beacon flag-parsing loop must reject flag-like tokens (ms-120 e-3896).

AX 原則 2 (silent no-op 禁止) + 原則 6 (構造で塞ぐ): a `*)` catch-all that stores
`$1` into a positional variable silently swallows any unknown `--flag` as a value.
That produced misleading failures like `beacon milestone start --reason x` →
"Milestone not found: x" (真因は --reason の誤流入, e-3898) and bogus "--help"
milestones (e-902).

This test is the forcing function: it greps the real bin/beacon source and fails
if ANY positional catch-all inside a `case "$1" in` argument loop lacks a
preceding flag guard (`-?*)` → `_guard_flag` / `_guard_positional`). A newly added
parser without a guard cannot merge — the structure is enforced by CI, not by
reviewer memory.

The behavioral tests below pin the two user-visible outcomes the guard delivers:
a flag-like token stops with its *real* cause, and a valid id/value still passes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "beacon"
BASH = shutil.which("bash")


def _flag_loop_catchall_lines() -> list[tuple[int, str]]:
    """Return (line_no, text) for every `*) var="$1" ...` catch-all that sits
    inside a `case "$1" in` argument-parsing loop and lacks a flag guard above."""
    lines = BIN.read_text(encoding="utf-8").split("\n")
    offenders: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not re.match(r"^\*\)", s):
            continue
        # Two silent-no-op shapes: a catch-all that stores $1 into a positional
        # variable (mis-assign → misleading error), or one that just `shift`s
        # (pure drop of an unknown flag). Both must be guarded.
        assigns_positional = re.search(r'=("?\$1|"?\$\{1)', s)
        pure_drop = re.match(r"^\*\)\s*shift\s*;;", s)
        if not (assigns_positional or pure_drop):
            continue
        # e-3896 (PR #475 review): the window must reach back past a long
        # inline `--help` heredoc between `case "$1" in` and the catch-all —
        # 14 lines missed search / sessions / rollback / morning (30+ line help
        # blocks). Widen to 70 so those loops are in scope. To avoid matching a
        # *different* command's earlier case, stop at the enclosing function/case
        # boundary heuristically by requiring the nearest `case "$1" in` above.
        window = lines[max(0, i - 70):i]
        in_flag_loop = any(re.search(r'case\s+"\$1"\s+in', w) for w in window)
        if not in_flag_loop:
            continue
        guarded = any(
            re.search(r"-\?\*\)|_guard_flag|_guard_positional", w) for w in window
        )
        if not guarded:
            offenders.append((i + 1, s[:80]))
    return offenders


def test_every_positional_catchall_has_a_flag_guard():
    offenders = _flag_loop_catchall_lines()
    assert offenders == [], (
        "These bin/beacon positional catch-alls swallow unknown flags silently "
        "(ms-120 e-3896). Add `-?*) _guard_flag \"$1\" ;;` above each `*)`:\n"
        + "\n".join(f"  L{ln}: {txt}" for ln, txt in offenders)
    )


# --- behavioral guards (need bash; skip on bare Windows) --------------------

pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".beacon").mkdir()
    (tmp_path / ".beacon" / "project.json").write_text(
        '{"name": "t", "milestones": [{"id": "ms-1", "title": "x", '
        '"status": "todo"}]}',
        encoding="utf-8",
    )
    return tmp_path


def _run(proj, *args):
    return subprocess.run(
        [BASH, str(BIN), *args], cwd=proj, capture_output=True, text=True
    )


@pytestmark_bash
def test_milestone_start_flag_reports_real_cause_not_not_found(proj):
    # The e-3898 regression: --reason (invalid for `start`) used to land in
    # ms_id and fail with a misleading "Milestone not found".
    r = _run(proj, "milestone", "start", "--reason", "foo")
    assert r.returncode == 1
    assert "not a valid flag for 'beacon milestone start'" in r.stderr
    assert "not found" not in (r.stdout + r.stderr).lower()


@pytestmark_bash
def test_unknown_flag_rejected_on_id_command(proj):
    r = _run(proj, "task", "done", "e-1", "--bogus")
    assert r.returncode == 1
    assert "not a valid flag" in r.stderr


@pytestmark_bash
def test_valid_id_is_not_mistaken_for_a_flag(proj):
    # ms-1 starts with a letter, never a dash — the guard must not fire.
    r = _run(proj, "milestone", "start", "ms-1", "--no-branch", "--no-assignee")
    assert "not a valid flag" not in r.stderr
    assert r.returncode == 0


@pytestmark_bash
def test_unresolved_ms_reference_is_clean_error_not_traceback(proj):
    # e-3896: an unresolved -m/--ms (a well-formed flag whose VALUE names no
    # milestone) used to bubble a raw Python traceback out of the commands.py
    # dispatcher. It must surface as a clean 1-line error + exit 1, never a
    # stacktrace.
    # ms-143: task_add / task_list now resolve their target through the profession-
    # generic occupation.resolve_target (not the dev-concrete find_target_milestone),
    # so an unresolved id reads "Target not found: <id>" — a clean, correct 1-line
    # error (the guard this test enforces), generalized from the old dev-only
    # "Milestone not found".
    for args in (("task", "list", "-m", "ms-999"),
                 ("task", "add", "x", "-m", "ms-999")):
        r = _run(proj, *args)
        assert r.returncode == 1, args
        assert "Target not found" in r.stderr, args
        assert "Traceback" not in (r.stdout + r.stderr), args
