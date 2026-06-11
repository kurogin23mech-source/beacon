"""beacon-find-root — Python entry-point for the project-root walk-up helper.

Mirrors the behaviour of ``bin/beacon-find-root`` (shell script) so that
``pip install beacon`` produces a ``beacon-find-root`` / ``beacon-find-root.exe``
binary in the user's Scripts directory. The Skills shipped under
``skills/*.md`` call this binary as their prerequisite check, replacing the
cwd-only ``test -f .beacon/project.json`` pattern (ms-62 / e-1512).

The walk-up is what lets a bclaude started inside a git worktree
(``.worktrees/<ms>/``) bootstrap correctly even though only the parent
repo carries ``.beacon/``.

Usage:

    beacon-find-root                       # prints root, exits 0/1
    beacon-find-root && echo OK || echo NO_BEACON
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_beacon_root(start: Path | None = None) -> Path | None:
    """Return the directory containing ``.beacon/project.json`` walking up
    from ``start`` (default: cwd). Returns ``None`` when no root is found.
    """
    d = (start or Path.cwd()).resolve()
    while True:
        if (d / ".beacon" / "project.json").is_file():
            return d
        parent = d.parent
        if parent == d:
            return None
        d = parent


def main(argv: list[str] | None = None) -> int:
    root = find_beacon_root()
    if root is None:
        return 1
    sys.stdout.write(str(root) + os.linesep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
