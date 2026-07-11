#!/usr/bin/env python3
"""Single-pass Trek status for session-start (ms-85 e-3180).

Consolidates the two Trek detectors session-start previously ran as separate
blocks:

  * Step 1o   — list of joined Treks + blanket-approval reminder
                (``beacon trek list --joined --json``)
  * Step 1o-2 — armed (autonomous-execution) self-check for active Treks
                (``beacon bus auto-execute list --json`` +
                 ``beacon bus budget show --json``)

e-3180 renders both sections from one script so session-start has a single
call site, and only runs the armed CLIs when at least one **active** Trek is
joined (Step 1o-2's own precondition). The displayed text is unchanged.

Output contract (unchanged):
  * no joined treks -> print nothing.
  * joined treks -> the trek list section (one block per trek: id/title,
    status, HALTED marker, goal, other-member count) plus, when any trek is
    active, the blanket-approval reminder line; and, when an active trek is
    joined but the session is not armed, the not-armed warning line.

Fail-safe: all CLIs are best-effort; a failure degrades that part to empty.
Always exits 0.

Call sites:
  * /beacon-session-start Skill (Steps 1o + 1o-2, merged)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "lib"))

from trek_status import (  # noqa: E402
    BLANKET_APPROVAL_REMINDER,
    NOT_ARMED_WARNING,
    format_joined_treks,
    has_active_trek,
    is_armed,
)


def _cli_json(args: list[str]):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout or "null")
    except Exception:
        return None


def main() -> int:
    treks = _cli_json(["beacon", "trek", "list", "--joined", "--json"])
    if not treks:
        return 0  # no joined treks -> section omitted entirely

    blocks = [format_joined_treks(treks)]

    active = has_active_trek(treks)
    if active:
        blocks.append(BLANKET_APPROVAL_REMINDER)
        # Step 1o-2 only runs when an active trek is joined.
        channels = _cli_json(["beacon", "bus", "auto-execute", "list", "--json"])
        budget = _cli_json(["beacon", "bus", "budget", "show", "--json"])
        if not is_armed(channels, budget):
            blocks.append(NOT_ARMED_WARNING)

    out = "\n".join(b for b in blocks if b)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
