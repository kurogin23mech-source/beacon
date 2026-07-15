#!/usr/bin/env python3
"""PreToolUse hook: block cross-user DM sends via the bus primitive (ms-110 / e-3444).

Reads the Claude Code PreToolUse event on stdin and, when the tool call is a
cross-project (potentially cross-user) DM sent by calling the bus primitive
directly — not via /beacon-dm-send and without a valid one-time Skill token —
emits a ``deny`` decision that redirects the AI to the Skill.

Decision logic + token handling live in ``lib/dm_send_guard.py`` (unit-tested);
this entrypoint is thin glue: resolve this session's project id, read a fresh
token, call ``decide``, consume the token on a token-based allow, and print the
PreToolUse hook JSON.

Fail-open: any error (bad stdin, missing lib, unreadable cloud.json) prints
nothing and exits 0, so a hook bug can never wedge the user's tool calls. The
server backstop (e-3443) still enforces correctness regardless.
"""

from __future__ import annotations

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lib"))


def _local_project_id(cwd: str) -> str:
    """Best-effort: read project_id from <cwd>/.beacon/cloud.json (cloud mode).

    Local-mode projects have no cloud.json / project_id; returns "" then, which
    makes every recipient look same-project (= the guard stays quiet for purely
    local projects, which have no cross-user surface anyway)."""
    path = os.path.join(cwd or ".", ".beacon", "cloud.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return str(data.get("project_id") or "")
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0  # fail-open: unparseable input never blocks

    if not isinstance(event, dict):
        return 0

    try:
        import dm_send_guard as guard
    except Exception:
        return 0  # fail-open: guard unavailable (old checkout) never blocks

    tool_name = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or os.getcwd())

    try:
        target = guard.parse_send_target(tool_name, tool_input)
        beacon_dir = os.path.join(cwd, ".beacon")
        token = guard.read_valid_token(beacon_dir)
        decision = guard.decide(
            target,
            local_project_id=_local_project_id(cwd),
            token=token,
        )
    except Exception:
        return 0  # fail-open on any decision error

    if decision["action"] != guard.ACTION_DENY:
        # Consume a one-time token that authorised this send so it is single-use.
        if decision.get("consume_token"):
            try:
                guard.consume_token(beacon_dir)
            except Exception:
                pass
        return 0  # allow (no output needed)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": decision.get("message")
            or "cross-user DM must go through /beacon-dm-send",
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
