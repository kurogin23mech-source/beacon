"""No shell function in bin/beacon is defined twice (ms-178, PR#766 follow-up).

bin/beacon is one 4000+ line shell script with ~100 helper functions. In bash a
second definition SILENTLY replaces the first, so adding a helper whose name is
already taken rewires every existing call site to different semantics with no
error at definition time and no error at call time — only a wrong result.

This actually happened: a new "reject an unexpected non-flag argument" helper was
added under the name `_guard_positional`, which was already taken by a helper
that rejects a FLAG-LIKE token in a slot that does take a value. The two have
opposite meanings and incompatible argument orders ((cmd, token) vs (token,
usage)), so the four existing call sites began printing the offending token and
the usage string in each other's places. Local testing missed it because the
run was filtered to the feature under change; CI caught it.

A duplicate name is never intentional here, so pin it structurally rather than
relying on the next author to grep first.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin" / "beacon"

# `name() {` at the start of a line — how every helper in this file is declared.
_DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", re.MULTILINE)


def test_no_function_is_defined_twice():
    names = _DEF.findall(BIN.read_text(encoding="utf-8"))
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    assert dupes == {}, (
        "bin/beacon defines these shell functions more than once; the later "
        "definition silently wins and rewires every earlier call site: "
        + ", ".join(f"{n} ({c}x)" for n, c in sorted(dupes.items()))
    )


def test_the_detector_actually_catches_a_duplicate(tmp_path):
    """A guard that cannot fail is useless: prove the regex sees a redefinition."""
    fake = tmp_path / "beacon"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "_guard_positional() {\n  echo a\n}\n"
        "other() {\n  echo b\n}\n"
        "_guard_positional() {\n  echo c\n}\n",
        encoding="utf-8",
    )
    names = _DEF.findall(fake.read_text(encoding="utf-8"))
    dupes = {n: c for n, c in Counter(names).items() if c > 1}
    assert dupes == {"_guard_positional": 2}, dupes


def test_both_argument_guards_exist_and_are_distinct():
    """The two guards must stay separate names: one rejects a flag-like token in
    a value slot, the other an unexpected non-flag token where none is taken."""
    src = BIN.read_text(encoding="utf-8")
    assert "_guard_positional() {" in src
    assert "_guard_no_positional() {" in src
    assert "_guard_flag() {" in src
