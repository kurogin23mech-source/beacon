#!/usr/bin/env python3
"""check-tool-subcommand-flag-parity — the two frontends must agree PER SUBCOMMAND.

Why this guard exists
---------------------
``beacon tool`` is parsed twice: ``bin/beacon`` has a ``case`` arm per subcommand
that accepts a specific flag list, and ``beacon_cli/dispatch.py`` declares EVERY
``tool`` flag on ONE shared argparse parser and then narrows it with the
hand-written ``_TOOL_SUBCOMMAND_FLAGS`` table.

That table was added to fix exactly one defect: ``tool add --recorder evidence``
(a flag belonging to the SIBLING subcommand) parsed cleanly on the Python side
and was silently ignored, registering the tool with an ``undecided`` record sink
while exiting 0, where bash rejected it. The fix was correct and the defect is
gone — but the fix was a HAND-WRITTEN MIRROR of the bash case arms, pinned by
four example tests, and an independent maintainability review of PR #802 showed
that nothing detected a divergence: widening ``_TOOL_SUBCOMMAND_FLAGS["show"]``
by one entry left bash rejecting the flag and Python accepting it, with the full
test subset AND ``ci-strict-drift-guards.sh`` still green.

The repo-wide CLI-drift guard structurally cannot see this. It compares flag sets
per VERB (``beacon tool`` as a whole), and since every ``tool`` flag lives on one
shared Python parser by design, the verb-level sets match trivially. The mismatch
only exists one level down, per subcommand.

So this guard compares the two per-subcommand tables EXHAUSTIVELY — every
subcommand, both directions — rather than sampling a few combinations. It reuses
``check-cli-help-drift``'s own bash case-arm parsers so there is one definition of
"read the flags out of a bash arg loop" rather than a second copy here.

Exit 0 when the two agree; exit 1 with the per-subcommand diff otherwise.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIN_BEACON = os.path.join(_REPO, "bin", "beacon")
_DISPATCH = os.path.join(_REPO, "beacon_cli", "dispatch.py")

#: Flags every subcommand accepts, excluded from the comparison on both sides.
UNIVERSAL_FLAGS = frozenset({"--json", "--help", "-h"})

#: argparse dest -> flag spelling, read from dispatch.py so the mapping is not
#: a third copy of the same knowledge.
_DEST_MAP_NAME = "_TOOL_FLAG_SPELLING"
_TABLE_NAME = "_TOOL_SUBCOMMAND_FLAGS"


def _load_drift_helpers():
    """Import ``scripts/check-cli-help-drift.py`` for its bash case parsers.

    Reused rather than reimplemented: "read the flags out of a bash arg loop" has
    one definition in this repo (``_case_label_flags``), and a second copy here
    could disagree with the guard printed next to it.
    """
    path = os.path.join(_REPO, "scripts", "check-cli-help-drift.py")
    spec = importlib.util.spec_from_file_location("_cli_help_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bash_tool_subcommand_flags(drift, bin_path: str = "") -> dict:
    """``{subcommand: {flags}}`` parsed out of bin/beacon's ``tool|tools)`` block.

    Raises ``RuntimeError`` when the block cannot be located — a loud failure is
    correct, because "no arms found" and "arms accept nothing" are different facts
    and collapsing them would make this guard pass by finding nothing.
    """
    with open(bin_path or _BIN_BEACON, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"^    tool\|tools\)\n", text, re.MULTILINE)
    if m is None:
        raise RuntimeError("bin/beacon に `tool|tools)` の case arm が見つかりません")
    nxt = re.search(r"^    [a-z][a-z0-9|_\-]*\)\n", text[m.end():], re.MULTILINE)
    block = text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]

    out = {}
    for start, label_end, end, alternatives in drift._outer_case_arms(block):
        flags = drift._case_flags(block[label_end:end]) - set(UNIVERSAL_FLAGS)
        for alt in alternatives:
            alt = alt.strip()
            if not alt or alt == "*":
                continue  # the usage fallthrough accepts nothing
            out[alt] = set(flags)
    if not out:
        raise RuntimeError(
            "`tool|tools)` 内にサブコマンドの case arm を 1 つも読めませんでした "
            "(= このガードが『見つからないので合格』になる状態)")
    return out


def python_tool_subcommand_flags(dispatch_path: str = "") -> dict:
    """``{subcommand: {flags}}`` read from dispatch.py's declared table.

    Parsed from the AST rather than imported, so this guard runs without
    importing the CLI (and without its import side effects).
    """
    with open(dispatch_path or _DISPATCH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    tables = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in (
                    _TABLE_NAME, _DEST_MAP_NAME):
                tables[target.id] = ast.literal_eval(node.value)
    missing = {_TABLE_NAME, _DEST_MAP_NAME} - set(tables)
    if missing:
        raise RuntimeError(
            f"beacon_cli/dispatch.py に {sorted(missing)} が見つかりません "
            "(改名したらこのガードも更新してください)")
    spelling = tables[_DEST_MAP_NAME]
    out = {}
    for sub, dests in tables[_TABLE_NAME].items():
        flags = set()
        for dest in dests:
            if dest not in spelling:
                raise RuntimeError(
                    f"_TOOL_SUBCOMMAND_FLAGS['{sub}'] の dest '{dest}' が "
                    f"{_DEST_MAP_NAME} に無く、flag 名に解決できません")
            flags.add(spelling[dest])
        out[sub] = flags - set(UNIVERSAL_FLAGS)
    return out


def audit(bin_path: str = "", dispatch_path: str = "") -> dict:
    """Compare the two sides. Both inputs are addressable (ms-180 follow-up).

    The paths default to this repo's own files, which is what CI wants. They are
    arguments because the *inversion* tests — the ones that prove this guard can
    go red — have to hand it a deliberately broken pair, and the only other way
    to do that is to edit the real ``bin/beacon`` in place. That is what the
    first version of those tests did, and under parallel test execution every
    other worker that spawned ``bin/beacon`` during the edit window executed the
    half-written file: observed as `$1: unbound variable` at a line number past
    the end of the pristine file, and as `Text file busy`, in unrelated tests.
    A guard must never require mutating the thing it guards.
    """
    drift = _load_drift_helpers()
    bash = bash_tool_subcommand_flags(drift, bin_path)
    python = python_tool_subcommand_flags(dispatch_path)

    problems = []
    for sub in sorted(set(bash) | set(python)):
        if sub not in bash:
            problems.append({"subcommand": sub, "kind": "python-only"})
            continue
        if sub not in python:
            problems.append({"subcommand": sub, "kind": "bash-only"})
            continue
        only_bash = sorted(bash[sub] - python[sub])
        only_python = sorted(python[sub] - bash[sub])
        if only_bash or only_python:
            problems.append({"subcommand": sub, "kind": "flag-mismatch",
                             "bash_only": only_bash,
                             "python_only": only_python})
    return {"bash": {k: sorted(v) for k, v in bash.items()},
            "python": {k: sorted(v) for k, v in python.items()},
            "problems": problems}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bin", default="",
                    help="path to the bash dispatcher to read "
                         "(default: this repo's bin/beacon)")
    ap.add_argument("--dispatch", default="",
                    help="path to dispatch.py to read "
                         "(default: this repo's beacon_cli/dispatch.py)")
    args = ap.parse_args()

    try:
        result = audit(args.bin, args.dispatch)
    except RuntimeError as exc:
        print(f"[tool-subcommand-parity] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["problems"] else 0

    if result["problems"]:
        print("[tool-subcommand-parity] bash と Python で "
              "`beacon tool <サブコマンド>` が受け付ける flag が食い違っています:")
        for row in result["problems"]:
            sub = row["subcommand"]
            if row["kind"] == "python-only":
                print(f"  - {sub}: Python にだけ在り、bash の case arm に無い")
            elif row["kind"] == "bash-only":
                print(f"  - {sub}: bash にだけ在り、"
                      f"_TOOL_SUBCOMMAND_FLAGS に無い")
            else:
                if row["bash_only"]:
                    print(f"  - tool {sub}: bash は受けるが Python は拒否 "
                          f"→ {', '.join(row['bash_only'])}")
                if row["python_only"]:
                    print(f"  - tool {sub}: Python は受けるが bash は拒否 "
                          f"→ {', '.join(row['python_only'])}")
        print("")
        print("  片方だけ直すと、同じ入力で一方が成功し他方が失敗します "
              "(= ms-180 が閉じたはずの穴の再発)。")
        print("  bin/beacon の `tool|tools)` の case arm と "
              "beacon_cli/dispatch.py の _TOOL_SUBCOMMAND_FLAGS を揃えてください。")
        return 1

    covered = len(result["bash"])
    print(f"[tool-subcommand-parity] OK: `beacon tool` の {covered} "
          f"サブコマンドすべてで bash と Python の受け付け flag が一致しています "
          f"(例示ではなく全件比較)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
