#!/usr/bin/env python3
"""Cross-layer drift guard for the bundled Go viewer's binary name (ms-170 e-6476).

The name ``beacon-view`` is written independently in four layers, and if any one
drifts (a rename in a single place) then ``beacon view`` silently fails to find
the viewer on some install path:

  * ``viewer/build.sh``                        — NAME=...  (what gets built: SOT)
  * ``lib/cmd_view.py``                        — resolve candidate name
  * ``.github/workflows/release-build.yml``    — stages the built binary per wheel
  * ``packaging/homebrew/beacon.rb``           — installs it onto PATH via `go build`

This extracts the actual name token from each layer with a context-anchored
regex (structural — NOT a loose substring scan) and asserts they all agree with
the build.sh source of truth. Prints the mismatch and exits 1 on drift; exits 0
when consistent.

Run standalone (``python3 scripts/check-viewer-name-drift.py``) or via the test
``tests/test_viewer_name_drift.py``.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# layer path -> (regex with one capture group for the viewer name, re flags).
# Each pattern is anchored to the *viewer usage* so it captures whatever name is
# actually there (drift shows up as a different captured value, not a miss).
_EXTRACTORS = {
    # NAME="beacon-view"
    "viewer/build.sh": (r'NAME="([A-Za-z0-9._-]+)"', 0),
    # name = "beacon-view" + ext
    "lib/cmd_view.py": (r'name\s*=\s*"([A-Za-z0-9._-]+)"\s*\+\s*ext', 0),
    # cp "viewer/dist/beacon-view-${os}-${arch}${ext}" ...  /  staged=...beacon-view${ext}
    ".github/workflows/release-build.yml":
        (r'viewer/dist/([A-Za-z0-9._-]+?)(?:-\$\{os\}|\$\{ext\})', 0),
    # inside the `cd "viewer"` go-build block: -o", bin/"beacon-view"
    "packaging/homebrew/beacon.rb":
        (r'cd\s+"viewer".*?bin/"([A-Za-z0-9._-]+)"', re.DOTALL),
}


def extract_names() -> dict:
    """layer -> set of viewer-name tokens found in that layer."""
    out = {}
    for rel, (pat, flags) in _EXTRACTORS.items():
        names = set(re.findall(pat, _read(rel), flags))
        out[rel] = names
    return out


def check() -> list:
    """Return a list of human-readable problems (empty = consistent)."""
    found = extract_names()
    problems = []

    sot = found.get("viewer/build.sh", set())
    if len(sot) != 1:
        problems.append(
            f"viewer/build.sh: expected exactly one NAME= viewer name, "
            f"found {sorted(sot) or 'none'}")
        return problems  # can't compare without a source of truth
    canonical = next(iter(sot))

    for rel, names in found.items():
        if not names:
            problems.append(
                f"{rel}: no viewer-name token matched (extractor drifted, or the "
                f"reference was removed/renamed away from {canonical!r})")
            continue
        drifted = {n for n in names if n != canonical}
        if drifted:
            problems.append(
                f"{rel}: uses {sorted(drifted)} but build.sh (source of truth) "
                f"builds {canonical!r} — rename all layers together")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("beacon-view name drift across layers:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("OK: beacon-view name is consistent across build.sh / cmd_view.py / "
          "release-build.yml / beacon.rb")
    return 0


if __name__ == "__main__":
    sys.exit(main())
