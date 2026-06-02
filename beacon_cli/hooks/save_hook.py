"""Cross-platform MCP save hook (PostToolUse, matcher: mcp__).

Python translation of ``bin/beacon-save-hook.sh`` (ms-44 e-777). Watches
for MCP write operations against the Google Drive integration and
auto-records them as ``beacon save`` entries under the currently active
milestone.

Bash → Python contract notes:
  * Reads PostToolUse JSON from stdin.
  * Exits silently when:
      - no ``.beacon/project.json`` in cwd,
      - tool name doesn't match a monitored source,
      - operation is a read/get/list/search (filtered the same way),
      - 0 or 2+ active milestones,
      - ``beacon`` CLI is not resolvable.
  * Errors from ``beacon save`` are swallowed (best-effort).

This hook intentionally does NOT walk up the directory tree (unlike
post-commit) because the bash version uses a plain ``.beacon/project.json``
existence check from the hook's cwd. Behaviour is preserved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


_MONITORED_SOURCES = {
    "mcp__google-drive__": "google_drive",
    "mcp__claude_ai_Google_Drive__": "google_drive",
}

_SKIP_OPERATION_PREFIXES = (
    "read", "get", "list", "search", "describe", "auth", "download", "export",
)


def _source_for(tool_name: str) -> Optional[str]:
    for prefix, source in _MONITORED_SOURCES.items():
        if tool_name.startswith(prefix):
            return source
    return None


def _is_write_operation(operation: str) -> bool:
    op_lower = operation.lower()
    for prefix in _SKIP_OPERATION_PREFIXES:
        if op_lower.startswith(prefix):
            return False
    return True


def _extract_name(tool_input: dict) -> str:
    for key in ("title", "name", "fileName", "file_name", "documentTitle"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _extract_url(tool_input: dict) -> str:
    for key in ("url", "fileUrl", "file_url"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _active_milestone_id(project_path: Path) -> Optional[str]:
    """Return the unique in_progress milestone id, or None if 0/2+ active."""
    try:
        with open(project_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    active = [
        ms for ms in (data.get("milestones") or [])
        if ms.get("status") == "in_progress"
    ]
    if len(active) != 1:
        return None
    return active[0].get("id") or None


def _resolve_beacon_cli() -> Optional[str]:
    """``beacon`` on PATH, else sibling ``beacon`` in this script's directory."""
    found = shutil.which("beacon")
    if found:
        return found
    # Sibling lookup mirrors the bash ``[ -x "$(dirname "$0")/beacon" ]`` guard
    # for relocatable installs.
    here = Path(__file__).resolve().parent
    candidate = here / "beacon"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def main(argv: Optional[list] = None) -> int:
    try:
        project_file = Path(".beacon") / "project.json"
        if not project_file.is_file():
            return 0

        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return 0

        tool_name = payload.get("tool_name") or payload.get("tool") or ""
        if not tool_name:
            return 0

        source = _source_for(tool_name)
        if source is None:
            return 0

        # operation = portion after the last "__"
        operation = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
        if not _is_write_operation(operation):
            return 0

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        name = _extract_name(tool_input)
        url = _extract_url(tool_input)
        description = f"[{operation}] {name}" if name else f"[{operation}]"

        ms_id = _active_milestone_id(project_file)
        if ms_id is None:
            return 0  # ambiguous — let the user handle manually

        beacon = _resolve_beacon_cli()
        if beacon is None:
            return 0

        args: List[str] = [
            beacon, "save", description, "-m", ms_id,
            "--source", source, "--json",
        ]
        if url:
            args.extend(["--url", url])

        try:
            subprocess.run(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
