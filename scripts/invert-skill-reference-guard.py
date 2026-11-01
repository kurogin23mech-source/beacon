#!/usr/bin/env python3
"""Inversion harness for check-skill-reference-drift.py (ms-185 e-7079).

A guard that is green is *trusted*, so a guard that cannot go red is worse than
no guard at all — it converts "nobody checked" into "someone checked and it was
fine". This repo has shipped that mistake more than once (a substring match that
passed on help text, an allowlist that silently covered its own subject), so a
new guard does not count as landed until something has broken it on purpose and
watched it fail.

This script injects each defect the guard claims to catch, runs the guard, and
restores the file immediately. It asserts RED for every defect and GREEN for the
untouched tree. Any case that stays green is a hole in the guard, not a pass.

Run:  python3 -B scripts/invert-skill-reference-guard.py
      (-B matters: a stale __pycache__ entry can answer from the previous
       version of the guard and produce a green that means nothing.)

Exit 0 = the guard is load-bearing. Exit 1 = at least one defect slipped past.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "check-skill-reference-drift.py"


def run_guard() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-B", str(GUARD), "--strict"],
        cwd=REPO, capture_output=True, text=True,
    )
    return r.returncode, (r.stdout + r.stderr)


# The fake Skill names are assembled at runtime. Writing them as literals
# would put "/beacon-<missing>" into THIS file, and the guard scans scripts/ —
# so the harness would trip its own subject and the baseline could never be
# green. Composing them keeps the harness out of its own scan without needing
# an exemption (an exemption here would be the first thing to rot).
_S = "/" + "beacon" + "-"
MISSING_A = _S + "this-skill-does-not-exist"
MISSING_B = _S + "some-other-missing-skill"


class Mutation:
    """Apply a text edit, run the guard, restore the file no matter what."""

    def __init__(self, label: str, path: pathlib.Path, old: str, new: str,
                 expect_in_output: str):
        self.label = label
        self.path = path
        self.old = old
        self.new = new
        self.expect_in_output = expect_in_output

    def check(self) -> tuple[bool, str]:
        original = self.path.read_text(encoding="utf-8")
        if self.old not in original:
            return False, f"anchor not found in {self.path.name}: {self.old[:60]!r}"
        try:
            self.path.write_text(original.replace(self.old, self.new, 1),
                                 encoding="utf-8")
            rc, out = run_guard()
        finally:
            self.path.write_text(original, encoding="utf-8")
        if rc == 0:
            return False, "guard stayed GREEN with the defect injected"
        if self.expect_in_output not in out:
            return False, (f"went red, but the message never mentions "
                           f"{self.expect_in_output!r} — the reader would not "
                           f"know what to fix")
        return True, "RED, and the message names the defect"


MUTATIONS = [
    # 1. The actual defect of PR #827: production code tells an AI to run a
    #    Skill that does not exist. Injected into a file that has no Trek
    #    references of its own, so a pass cannot come from the allowlist.
    Mutation(
        "a production file names a non-existent Skill",
        REPO / "lib" / "tick_health.py",
        '"""',
        '"""Run ' + MISSING_A + ' to continue.\n\n',
        MISSING_A.lstrip("/"),
    ),
    # 2. The same defect inside a file that IS allowlisted — the allowlist is
    #    keyed by (file, skill), so it must not blanket-excuse the file.
    Mutation(
        "an allowlisted file names a DIFFERENT non-existent Skill",
        REPO / "lib" / "trek.py",
        '"""',
        '"""See ' + MISSING_B + ' for details.\n\n',
        MISSING_B.lstrip("/"),
    ),
    # 3. An allowlist row outliving its subject. Renaming the row's Skill makes
    #    it match nothing, which is exactly how an exemption silently re-opens
    #    the hole it covered.
    Mutation(
        "an allowlist row no longer matches anything (stale exemption)",
        GUARD,
        '("lib/trek.py", "beacon-trek-pulse"):',
        '("lib/trek.py", "beacon-trek-pulse-renamed-away"):',
        "no longer match anything",
    ),
    # 4. Loosening the detector itself: if the token-boundary rule is dropped,
    #    every URL and path matches and the report floods. A flooded report is
    #    not a working guard either, so the harness pins the boundary rule by
    #    requiring that removing it changes the verdict.
    Mutation(
        "the token-boundary rule is removed from the detector",
        GUARD,
        r'''    r"(?:(?<=^)|(?<=[\s`'\"(\[*=,:]))"''',
        r'''    r""''',
        "Drift detected",
    ),
]


def main() -> int:
    rc, out = run_guard()
    if rc != 0:
        print("PRECONDITION FAILED: the guard is already red on an untouched "
              "tree, so nothing below can be concluded.\n")
        print(out)
        return 1
    print(f"baseline: GREEN ({len(MUTATIONS)} defects to inject)\n")

    failures = 0
    for m in MUTATIONS:
        ok, detail = m.check()
        mark = "PASS" if ok else "HOLE"
        if not ok:
            failures += 1
        print(f"  [{mark}] {m.label}\n         {detail}")

    rc_after, _ = run_guard()
    print()
    if rc_after != 0:
        print("RESTORE FAILED: the tree is red after the run — a mutation was "
              "not rolled back. Check `git diff`.")
        return 1
    print("tree restored: GREEN")

    if failures:
        print(f"\n{failures} of {len(MUTATIONS)} defects slipped past the guard. "
              "The guard is not load-bearing for those cases.")
        return 1
    print(f"\nall {len(MUTATIONS)} defects were caught; the guard is load-bearing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
