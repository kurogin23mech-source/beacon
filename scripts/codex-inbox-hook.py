#!/usr/bin/env python3
"""Codex inbox hook (= ms-93 / e-2497 layer 3).

Designed to be invoked from a Codex ``UserPromptSubmit`` hook. Reads
any DM events the receive-loop daemon (= scripts/codex-receive-loop.py)
has persisted to ``.beacon/codex/inbox/*.json`` and prints them in a
form Codex's hook contract can attach as ``additionalContext`` for the
upcoming AI prompt.

Usage from a Codex hook entry:

    "command": "python3 /abs/path/beacon/scripts/codex-inbox-hook.py"

Output: a single JSON object on stdout, e.g.::

    {"additionalContext": "BEACON BUS INBOX — 1 new event ...\\n..."}

When the inbox is empty, the output is ``{}`` (= no context added).
Each successfully-rendered event is archived into
``<inbox>/.read/<event_id>.json`` so the next prompt does not see it
again, mirroring the bus.mjs ``opened`` semantic on the Claude Code side.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _import_modules(install_root: Path):
    lib_dir = str(install_root / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import codex_receive_loop as crl  # noqa: E402
    return crl


def _format_entry(entry: dict) -> str:
    evt = entry.get("event", {}) or {}
    event_id = str(evt.get("event_id") or "?")
    channel = str(evt.get("channel") or "?")
    sender = str(evt.get("sender_session_id") or "?")
    created_at = str(evt.get("created_at") or "")
    payload = evt.get("payload") or {}
    text = payload.get("text") if isinstance(payload, dict) else ""
    text_str = str(text) if text else json.dumps(payload, ensure_ascii=False)
    return (
        f"  - [{event_id}] channel={channel} from={sender[:32]}"
        f" at={created_at}\n"
        f"    payload: {text_str}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Codex inbox hook (= ms-93 / e-2497).",
    )
    parser.add_argument("--cwd", default="", help="Override working directory.")
    parser.add_argument(
        "--install-root", default="",
        help="Beacon source checkout (defaults to script-relative).",
    )
    parser.add_argument(
        "--no-archive", action="store_true",
        help="Do not archive events after rendering (= for diagnostics).",
    )
    args = parser.parse_args()

    install_root = Path(args.install_root or Path(__file__).resolve().parent.parent)
    cwd = args.cwd or os.getcwd()
    crl = _import_modules(install_root)

    entries = crl.list_inbox_events(cwd=cwd)
    if not entries:
        # Empty additionalContext = no injection. Hook contract:
        # printing {} is acceptable; Codex ignores empty additionalContext.
        print(json.dumps({}))
        return 0

    header = (
        f"BEACON BUS INBOX — {len(entries)} new event(s)\n"
        "Each entry is a DM addressed to this Codex session.\n"
    )
    body = "".join(_format_entry(e) for e in entries)
    additional = header + body

    if not args.no_archive:
        for e in entries:
            crl.archive_inbox_event(e["path"], cwd=cwd)

    print(json.dumps({"additionalContext": additional}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
