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
    # Last-ditch: both imports failed, so the monitor can do nothing this Stop.
    # Never BLOCK the Stop event (return 0), but do NOT let the no-op stay silent
    # (#755 review AX-F3): a hook that quietly becomes a no-op is exactly the
    # class of silent non-function ms-159 is fixing — a broken install would then
    # drop context% / threshold notices forever with no visible signal. Surface
    # it LOUDLY in-band via the Stop hook's own additionalContext channel (the
    # same one the monitor uses to talk to Claude) in addition to stderr, so the
    # breakage reaches the human/AI instead of being buried. This only fires when
    # beacon_cli is genuinely unimportable (a broken install), never in the happy
    # path where _try_package_import already returned above.
    warn = ("⚠ Beacon context-usage monitor could not import beacon_cli — this "
            "Stop hook is a no-op, so context-usage % and 20/40/60/80% notices "
            "will NOT update until the install is repaired (reinstall beacon via "
            "pipx, or check the hook command path in settings.json).")
    sys.stderr.write(f"[context-monitor] {warn}\n")
    try:
        import json as _json
        sys.stdout.write(_json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": warn,
            }
        }, ensure_ascii=False))
    except Exception:  # pragma: no cover - stdout emit is best-effort
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
