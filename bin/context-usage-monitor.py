#!/usr/bin/env python3
"""bin/context-usage-monitor.py — standalone Stop hook (ms-44 e-854).

Single-file Python port of ``bin/context-usage-monitor.sh`` so Windows
pipx users (no bash, no jq) can register Claude Code's Stop hook by
absolute path:

    {
      "hooks": {
        "Stop": [{
          "matcher": "",
          "hooks": [{
            "type": "command",
            "command": "python C:/path/to/beacon/bin/context-usage-monitor.py"
          }]
        }]
      }
    }

This script delegates to ``beacon_cli.hooks.context_monitor.main`` if
the package is importable (source / pipx layout), and otherwise falls
back to executing the in-tree copy of the module via importlib. Either
way the behavior is byte-identical to ``beacon-hook-context-monitor``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _try_package_import() -> int | None:
    try:
        from beacon_cli.hooks.context_monitor import main  # type: ignore
    except Exception:
        return None
    return main()


def _try_in_tree_import() -> int | None:
    """Source-tree fallback: add ``<repo>/`` to sys.path so we can import
    ``beacon_cli.hooks.context_monitor`` even without a pipx install.

    Used when running the script straight out of a checkout (e.g.
    ``python ./bin/context-usage-monitor.py`` during development)."""
    here = Path(__file__).resolve().parent  # <repo>/bin
    repo_root = here.parent                  # <repo>
    sys.path.insert(0, str(repo_root))
    try:
        from beacon_cli.hooks.context_monitor import main  # type: ignore
    except Exception:
        return None
    return main()


def main() -> int:
    rc = _try_package_import()
    if rc is not None:
        return rc
    rc = _try_in_tree_import()
    if rc is not None:
        return rc
    # Last-ditch: stay silent so the Stop event is never blocked.
    sys.stderr.write("[context-monitor] beacon_cli not importable — skipping\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
