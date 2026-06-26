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

    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                            "additionalContext": "BEACON BUS INBOX — 1 new event ...\\n..."}}

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
    try:
        import api_client as ac  # noqa: E402
    except Exception:
        ac = None
    try:
        import codex_session as cs  # noqa: E402
    except Exception:
        cs = None
    return crl, ac, cs


def _resolve_codex_session(cs_module, cwd: str):
    """Find the active receive-loop session so the hook can fire opened
    ack against the right recipient sid.

    Resolution order (= e-2502 Codex 2026-06-26 dogfood fix):

    1. ``<cwd>/.beacon/codex/receive-loop.session.json`` — the daemon
       publishes its sid here on startup, removes on shutdown. Works
       regardless of the hook's parent_pid (= manual hook runs and
       Codex hook runs see the same file).
    2. ``parent_pid`` derivation — legacy path; only matches when the
       hook runs as a direct child of the Codex process that launched
       the daemon. Kept as a fallback.

    Returns ``(session_id, project_id)`` or ``("", "")`` on failure —
    the hook degrades gracefully (event still archives, just no opened
    ack call).
    """
    # Path 1: cwd-level pointer file (= daemon-published).
    try:
        pointer = Path(cwd) / ".beacon" / "codex" / "receive-loop.session.json"
        if pointer.is_file():
            with pointer.open("r", encoding="utf-8") as f:
                data = json.load(f)
            sid = (data.get("session_id") or "").strip()
            pid = (data.get("project_id") or "").strip()
            if sid and pid:
                return (sid, pid)
    except Exception:
        pass

    # Path 2: parent_pid fallback (= the original scheme).
    if cs_module is None:
        return ("", "")
    try:
        cloud_path = Path(cwd) / ".beacon" / "cloud.json"
        if not cloud_path.is_file():
            return ("", "")
        with cloud_path.open("r", encoding="utf-8") as f:
            project_id = json.load(f).get("project_id", "")
        if not project_id:
            return ("", "")
        parent_pid = os.getppid()
        key = cs_module.derive_stable_instance_key(cwd, parent_pid)
        sess_path = cs_module.codex_session_path(project_id, key)
        record = cs_module.read_codex_session(sess_path)
        if record is None:
            return ("", "")
        return (record.session_id, project_id)
    except Exception:
        return ("", "")


def _build_api_client(ac_module, cwd: str):
    """Build an ApiClient with the standard auth token resolution.

    Mirrors ``scripts/codex-receive-loop.py::_load_cloud_config`` so the
    hook can fire opened ack the same way the daemon fires delivered.
    Returns ``None`` when nothing usable is found.
    """
    if ac_module is None:
        return None
    cloud_path = Path(cwd) / ".beacon" / "cloud.json"
    if not cloud_path.is_file():
        return None
    try:
        with cloud_path.open("r", encoding="utf-8") as f:
            cloud = json.load(f)
    except Exception:
        return None
    api_url = cloud.get("api_url")
    if not api_url:
        return None
    token = cloud.get("id_token", "") or os.environ.get("BEACON_AUTH_TOKEN", "")
    if not token:
        creds_path = Path(os.path.expanduser("~/.beacon/credentials.json"))
        if creds_path.is_file():
            try:
                with creds_path.open("r", encoding="utf-8") as f:
                    token = json.load(f).get("token", "")
            except Exception:
                token = ""
    if not token:
        return None
    try:
        return ac_module.ApiClient(api_url, token=token)
    except Exception:
        return None


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


def _hook_output(context: str, hook_event_name: str = "UserPromptSubmit") -> dict:
    """Return Codex/Claude-compatible hook output for context injection."""
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": context,
        }
    }


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
    crl, ac, cs = _import_modules(install_root)

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
        # The archive call is also where the ``opened`` ack fires
        # (= e-2502 SPEC §2-B closing drift #3). Best-effort: if we
        # can't find the session record or build an API client, the
        # event still archives but the ack is skipped.
        session_id, project_id = _resolve_codex_session(cs, cwd)
        api = _build_api_client(ac, cwd)
        for e in entries:
            crl.archive_inbox_event(
                e["path"],
                cwd=cwd,
                api=api,
                project_id=project_id,
                recipient_session_id=session_id,
            )

    print(json.dumps(_hook_output(additional), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
