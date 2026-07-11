#!/usr/bin/env python3
"""beacon-bus receive-capability check (ms-54 e-1173, extracted ms-85 e-3178).

Detects whether the current cwd has a beacon-bus MCP entry wired, so
session-start can warn about the **send works / receive silently dies**
asymmetry. Previously this logic lived inline in the session-start Skill
(Step 1i) as an embedded ``python3 - <<'PY'`` heredoc; e-3178 extracts it
to a tested script so the branching (status -> warning band) is code, not
prose the agent has to re-derive.

Behavior is unchanged: the script inspects ``.mcp.json`` in cwd and, when
the ``beacon-bus`` server entry is absent or the file is missing/malformed,
prints the exact warning band the Skill used to build by hand. When the
entry is present (or detection is impossible), it prints nothing.

Always exits 0 — warn-only, never blocks session-start.

Call sites:
  * /beacon-session-start Skill (Step 1i)
  * Anyone running it manually from a cwd they're unsure about
"""
from __future__ import annotations

import json
import os
import sys


def detect_status(cwd: str = ".") -> str:
    """Return one of OK / NO_MCP_JSON / NO_BEACON_BUS_ENTRY / MCP_JSON_MALFORMED."""
    path = os.path.join(cwd, ".mcp.json")
    if not os.path.exists(path):
        return "NO_MCP_JSON"
    try:
        with open(path) as f:
            d = json.load(f)
        servers = d.get("mcpServers") or {}
        if not isinstance(servers, dict) or "beacon-bus" not in servers:
            return "NO_BEACON_BUS_ENTRY"
    except Exception:
        return "MCP_JSON_MALFORMED"
    return "OK"


def format_warning(status: str) -> str:
    """Return the warning band for a non-OK status, or "" when nothing to warn.

    The band text is byte-for-byte the message the Skill (Step 1i) used to
    emit inline, so the observable session-start output is unchanged.
    """
    if status in ("OK", "UNKNOWN"):
        return ""
    return (
        "⚠ この cwd は「送信専用」の恐れ (受信 bridge 未設置、ms-93 recipient-stability)\n"
        f"  detail: {status}\n"
        "  非対称に注意: `beacon bus send` は CLI push なので効きます (= 繋がって見える) が、\n"
        "    他セッションからの DM は live-wake せず、次回 prompt の catch-up でのみ届きます。\n"
        "  よくある原因: git worktree に手で cd した等で、起動 cwd と別の .beacon session\n"
        "    (別 session_id) になり、その session に受信 bridge が無い状態。\n"
        "  対処: この cwd で `beacon channel install` を実行して受信を有効化してください。"
    )


def main() -> int:
    try:
        status = detect_status(".")
    except Exception:
        # python-level failure = UNKNOWN, silent skip (never block).
        return 0
    band = format_warning(status)
    if band:
        print(band)
    return 0


if __name__ == "__main__":
    sys.exit(main())
