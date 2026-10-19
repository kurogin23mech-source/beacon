#!/usr/bin/env python3
"""Only one place may read which Skill a tool call names (ms-180 follow-up).

The harness names a Skill call ``Skill`` and carries the skill itself in
``tool_input``. Reading it out of that payload is the step that decides WHICH
道具 the ledger, the debt list, the turn fold and the approval gate are all
talking about. If a second place reads it, the two can disagree about the same
payload — and the interrupts would then gate one tool and record another.

So the parse lives in exactly one function (``tool_effect._skill_name``, reached
through ``tool_effect.ledger_name``) and this guard holds that to one.

Why a script and not a substring assertion in a test: the first version of this
check compared source text against ``'get("skill")'``. An independent
maintainability review planted ``tool_input.get('skill')`` — the same read with
single quotes — and the check stayed green. A guard whose green is read as
safety must not be defeated by a quote style, so this one reads the syntax tree:
``x.get("skill")``, ``x.get('skill')``, ``x["skill"]`` and ``x['skill']`` are
all the same node shape once parsed, and all are caught.

Scanned: production Python under ``lib/``, ``bin/`` and ``beacon_cli/``. Tests
are not scanned — they BUILD such payloads on purpose, and flagging that would
leave switching the guard off as the only way to get it green.

Known limit, stated rather than left implicit: only the key ``skill`` is
flagged. ``name`` and ``command`` are also read by the one parser as fallbacks,
but those two spellings are ordinary dictionary keys all over this codebase, so
flagging them would produce false positives — and a guard that cries wolf gets
switched off, which protects nothing.

Exit 0 = one parser. Exit 1 = a second place reads the key.

Deliberately NOT a pytest test: the CI drift guards also run from jobs that have
no pytest installed (see scripts/ci-strict-drift-guards.sh).
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

#: The unambiguous key. See "Known limit" above for why it is the only one.
SKILL_KEYS = frozenset({"skill"})

#: The one function allowed to read it, and the file it lives in.
OWNER_RELPATH = "lib/tool_effect.py"


def _key_of(node: ast.AST) -> str:
    """The string key this node reads, or "" if it reads none.

    Covers both spellings of the same read — ``x.get("k")`` and ``x["k"]`` —
    because they are the same act, and a guard that knew only one of them would
    be defeated by choosing the other.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
    if isinstance(node, ast.Subscript):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            return index.value
    return ""


def audit_file(path: pathlib.Path) -> list:
    """``[(line, source_excerpt)]`` for every read of a Skill key."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.split("\n")
    found = []
    for node in ast.walk(tree):
        if _key_of(node) in SKILL_KEYS and _key_of(node):
            excerpt = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            found.append((node.lineno, excerpt[:120]))
    return found


def census(repo: pathlib.Path) -> list:
    """Production Python files to scan, in a stable order."""
    paths = []
    for pattern in ("lib/*.py", "bin/*.py", "beacon_cli/**/*.py"):
        paths.extend(sorted(repo.glob(pattern)))
    return [p for p in paths if p.is_file()]


def main() -> int:
    repo = pathlib.Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(repo))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    root = pathlib.Path(args.repo)

    owner = (root / OWNER_RELPATH).resolve()
    scanned = 0
    offenders = []
    owner_reads = 0
    for path in census(root):
        scanned += 1
        reads = audit_file(path)
        if not reads:
            continue
        if path.resolve() == owner:
            owner_reads += len(reads)
            continue
        for line, excerpt in reads:
            offenders.append((path.relative_to(root), line, excerpt))

    if offenders:
        print(f"FAIL: {len(offenders)} place(s) outside {OWNER_RELPATH} read "
              f"which Skill a call names.", file=sys.stderr)
        print("That decision has one owner (tool_effect._skill_name, reached "
              "through ledger_name) because two readers can disagree about the "
              "same payload — and then one tool is gated while another is "
              "recorded. Call ledger_name() instead of parsing the payload.",
              file=sys.stderr)
        for rel, line, excerpt in offenders:
            print(f"  {rel}:{line}  {excerpt}", file=sys.stderr)
        return 1

    if owner_reads == 0:
        # "Nobody reads it" is not success: the owner must still be there, or
        # this guard is passing by finding nothing (the failure mode it is meant
        # to make impossible).
        print(f"FAIL: {OWNER_RELPATH} does not read the Skill key at all, so "
              f"this guard has lost its anchor and would pass on an empty "
              f"census. Check that tool_effect._skill_name still exists.",
              file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"OK: {scanned} production file(s) scanned; only "
              f"{OWNER_RELPATH} reads which Skill a call names "
              f"({owner_reads} read(s) there).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
