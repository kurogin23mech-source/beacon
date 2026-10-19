#!/usr/bin/env python3
"""No test may write to a file of the repository it is testing.

The test suite runs in parallel (pytest-xdist). The checkout is shared by every
worker, so a test that edits a repo source file — even one that faithfully
restores it afterwards — leaves a window in which the other workers read or
execute the half-written file.

That is not a theoretical hazard. An inversion test in
``tests/test_tool_subcommand_flag_parity_ms180.py`` mutated ``bin/beacon`` in
place to prove its guard could go red. During that window other workers spawned
the CLI and died with::

    bin/beacon: line 5222: $1: unbound variable     # past EOF of the 5221-line file
    OSError: [Errno 26] Text file busy: .../bin/beacon

One victim per run, a different unrelated test each time, never reproducible on
a single-process local run. It cost three CI rounds to localise, because every
piece of evidence pointed at the victim rather than the writer.

The fix for such a test is always the same shape: copy the file, break the copy,
and point the guard at the copy (make the guard's inputs addressable if they are
not already). This script enforces that rule.

Scanned: every ``tests/test_*.py``, every ``conftest.py`` under ``tests/``,
and the repository-root ``conftest.py`` — a conftest runs in every worker, so a
write there has the same reach as one in a test module.

What it flags: a call that mutates a path bound at module level to something
built from the repository root (``REPO / "bin" / "beacon"``,
``Path(__file__).resolve().parents[1] / ...``, …). Names built from ``tmp_path``
are not repo-rooted and are never flagged — writing scratch files is the normal
way to write a test.

Exit 0 = no test mutates the checkout. Exit 1 = at least one does.

Deliberately NOT a pytest test: the CI drift guards also run from jobs that have
no pytest installed (see scripts/ci-strict-drift-guards.sh).
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

#: A module-level assignment whose source text mentions one of these is treated
#: as a path into the checkout. Substrings, because the spellings in this repo
#: vary (``REPO``, ``REPO_ROOT``, ``ROOT``, ``parents[1]``, ``parent.parent``).
REPO_ROOT_HINTS = ("REPO", "ROOT", "parents[1]", "parents[2]", "parent.parent")

#: Mutating methods on a pathlib.Path.
MUTATING_METHODS = frozenset({
    "write_text", "write_bytes", "touch", "unlink", "rename", "replace",
    "rmdir", "chmod", "symlink_to", "hardlink_to",
})

#: ``shutil`` functions whose DESTINATION is their second argument.
SHUTIL_DEST_SECOND = frozenset({"copy", "copy2", "copyfile", "move", "copytree"})


def _repo_rooted_names(tree: ast.Module, source: str) -> dict:
    """Module-level names that denote a path inside the checkout."""
    names = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        segment = ast.get_source_segment(source, node.value) or ""
        if not any(hint in segment for hint in REPO_ROOT_HINTS):
            continue
        if "tmp" in segment.lower():
            continue  # a scratch path, not the checkout
        for target in node.targets:
            if isinstance(target, ast.Name):
                names[target.id] = segment
    return names


def _name_of(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else ""


def audit_file(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """``[(line, what, how the name was bound)]`` for one test module."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A test module that does not parse is a different problem, and pytest
        # will say so far more clearly than this guard could.
        return []
    names = _repo_rooted_names(tree, source)
    if not names:
        return []

    findings: list[tuple[int, str, str]] = []

    def flag(node, what, bound_name):
        findings.append((node.lineno, what, names[bound_name]))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # PATH.write_text(...) and friends
        if isinstance(func, ast.Attribute) and func.attr in MUTATING_METHODS:
            target = _name_of(func.value)
            if target in names:
                flag(node, f"{target}.{func.attr}()", target)
        # open(PATH, "w") / open(PATH, "a")
        if isinstance(func, ast.Name) and func.id == "open" and node.args:
            target = _name_of(node.args[0])
            mode = ""
            if len(node.args) > 1:
                mode = ast.get_source_segment(source, node.args[1]) or ""
            for kw in node.keywords:
                if kw.arg == "mode":
                    mode = ast.get_source_segment(source, kw.value) or ""
            if target in names and any(c in mode for c in ("w", "a", "+", "x")):
                flag(node, f"open({target}, {mode})", target)
        # shutil.copy(src, PATH) / shutil.move(src, PATH)
        if (isinstance(func, ast.Attribute) and func.attr in SHUTIL_DEST_SECOND
                and len(node.args) > 1):
            target = _name_of(node.args[1])
            if target in names:
                flag(node, f"shutil.{func.attr}(..., {target})", target)

    return findings


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", default=str(repo / "tests"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    tests_dir = pathlib.Path(args.tests_dir)
    scanned = 0
    all_findings = []
    # conftest.py too: it runs in every worker, so a write there has the same
    # reach as one in a test module (and a wider blast radius).
    targets = sorted(tests_dir.glob("test_*.py")) + sorted(
        tests_dir.glob("**/conftest.py")) + [
        p for p in [repo / "conftest.py"] if p.exists()]
    for path in targets:
        scanned += 1
        for line, what, bound in audit_file(path):
            all_findings.append((path.name, line, what, bound))

    if all_findings:
        print(f"FAIL: {len(all_findings)} place(s) in the test suite write to a "
              f"file of the checkout.", file=sys.stderr)
        print("The suite runs in parallel over ONE checkout, so the other "
              "workers read or execute the half-written file. Restoring it "
              "afterwards does not close the window.", file=sys.stderr)
        print("Copy the file, break the COPY, and point the guard at the copy "
              "(give the guard a path argument if it has none).", file=sys.stderr)
        for name, line, what, bound in all_findings:
            print(f"  tests/{name}:{line}  {what}\n"
                  f"      bound as: {bound}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"OK: {scanned} test module(s) scanned; none writes to the "
              f"checkout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
