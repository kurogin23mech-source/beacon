#!/usr/bin/env python3
"""beacon-tool-record-hook — the bash-layout entry point for the record hook.

The DECISION lives in ``lib/tool_record_hook.handle``; this file only finds the
project root and hands the payload over. The twin entry point is
``beacon_cli/hooks/save_hook.py`` (the ``beacon-hook-save`` console script used
by pipx / wheel / Windows installs, which the installer actually prefers).

Keeping both legs thin is deliberate. ms-180 rewrote only this leg to consult the
tool ledger and left the Python one carrying its old hand-written Google-Drive
table, so on the preferred install path the ledger did nothing and nothing said
so — the "wiring exists but nothing runs" failure the milestone exists to remove,
reproduced inside its own fix (found by an independent philosophy review of
PR #802). One definition, two wrappers, is what makes that not recur.

Wire it on BOTH ``PostToolUse`` (record) and ``UserPromptSubmit`` (turn
boundary); see ``lib/tool_turn`` for why the boundary must come from the same
hook that consumes it.

Fails safe: any error means "record nothing" and exit 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import hook_bootstrap as hb  # noqa: E402
except Exception:
    hb = None


def main() -> None:
    if hb is None:
        return
    hook_input = hb.read_hook_input()
    if hook_input is None:
        return
    libs = hb.import_lib("tool_record_hook")
    if libs is None:
        return
    try:
        root = hb.find_beacon_root(hook_input.get("cwd") or ".")
        if root is None:
            return  # not a beacon project
        libs["tool_record_hook"].handle(hook_input, root)
    except Exception:
        return


if __name__ == "__main__":
    main()
