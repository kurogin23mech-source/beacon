#!/usr/bin/env python3
"""Codex entry point for Beacon's cross-platform PostToolUse hook.

The detection implementation is shared with Claude Code through
``beacon_cli.hooks.post_commit``.  This tiny script exists so the Codex bridge
can resolve the hook from either a source checkout or the wheel-bundled scripts
directory in exactly the same way as the inbox hook.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_beacon_cli_importable() -> None:
    script = Path(__file__).resolve()
    # Source: <repo>/scripts/file.py -> add <repo>.
    source_root = script.parent.parent
    if (source_root / "beacon_cli").is_dir():
        candidate = source_root
    # Wheel: <site-packages>/beacon_cli/_bundled_scripts/file.py -> add the
    # site-packages directory.  Direct script execution by /usr/bin/python3
    # does not automatically add the venv containing this file to sys.path.
    elif script.parent.name == "_bundled_scripts":
        candidate = script.parent.parent.parent
    else:
        candidate = source_root
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def main() -> int:
    _ensure_beacon_cli_importable()
    from beacon_cli.hooks.post_commit import main as post_commit_main

    return post_commit_main()


if __name__ == "__main__":
    raise SystemExit(main())
