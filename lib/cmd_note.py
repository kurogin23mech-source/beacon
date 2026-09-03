#!/usr/bin/env python3
"""cmd_note.py — the `beacon note *` command family (ms-127 e-4320).

Extracted verbatim from commands.py (god-module split). Depends only on
commands_shared (upward) + leaf domain modules, never on commands.py — acyclic
(SPEC 方針4). commands.py re-imports these names for dispatch + `commands.X`.
"""

import json
import os
import sys

from commands_shared import (
    _resolve_session_id,
    _extract_token,
    _get_cloud_config_path,
    _get_notes_path,
    _refuse_if_bus_origin,
    _resolve_active_api_url,
    load_project,
    resolve_worked_target_ids,
)


def _push_note_to_cloud(note: dict) -> None:
    """Push a session note to cloud API. Best-effort: silently ignores all errors."""
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        project_id = config.get("project_id", "")
        api_url = _resolve_active_api_url()
        if not project_id:
            return
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return
        from api_client import ApiClient
        def _token():
            from auth import load_credentials as _lc
            c = _lc()
            return _extract_token(c) if c else ""
        client = ApiClient(api_url, _token)
        client.add_note(project_id, note)
    except Exception:
        pass


def cmd_note_add():
    import datetime
    text = os.environ.get("BEACON_NOTE_TEXT", "")
    context = os.environ.get("BEACON_NOTE_CONTEXT", "")
    if not text:
        print("Error: note text required")
        sys.exit(1)
    # ms-54 / e-1293: persistence poisoning defense — refuse writes whose
    # source is a bus DM. See module-level "Persistence poisoning defense"
    # block for the threat model.
    if _refuse_if_bus_origin(
        "note_add",
        {"text_preview": text[:80], "context": context},
    ):
        sys.exit(1)
    note = {
        "ts": datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "text": text,
    }
    if context:
        note["context"] = context
    # ms-57 / e-1036: tag the note with the current session_id so session-end
    # / rescue can aggregate it (notes WHERE session_id == X). Forward-only —
    # past notes stay untagged. Empty session_id is the "no session" sentinel
    # and is omitted, mirroring the commit/PR tagging convention (e-1062).
    session_id = _resolve_session_id()
    if session_id:
        note["session_id"] = session_id
    # ms-164 e-5943: attribute the note to the worked Target(s) so it is reachable
    # from the root AND each child Target (SPEC 方針3), not just project-wide. A note
    # is written mid-session before any commit, so it carries no entry set — the
    # resolver falls back to the fork Target (in a fork worktree) or the active
    # Target(s). Routed through the SAME rule as session log / push / deploy so
    # attribution never diverges. Best-effort: never fail a note over attribution.
    try:
        worked_ids = resolve_worked_target_ids(load_project(), entry_target_ids=[])
    except Exception:
        worked_ids = []
    if worked_ids:
        note["target_ids"] = worked_ids
        note["target_id"] = worked_ids[0]  # back-compat first-of-set
    path = _get_notes_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")
    _push_note_to_cloud(note)
    print(f"Note: {text[:60]}{'...' if len(text) > 60 else ''}")


def cmd_note_list():
    path = _get_notes_path()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    notes = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        notes.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    if json_mode:
        print(json.dumps(notes, ensure_ascii=False))
        return
    if not notes:
        print("(メモなし)")
        return
    for n in notes:
        ctx = f" [{n['context']}]" if n.get("context") else ""
        print(f"  {n['ts'][:16]}{ctx}: {n['text']}")


def cmd_note_clear():
    path = _get_notes_path()
    if os.path.exists(path):
        import shutil
        shutil.move(path, path + ".bak")
    try:
        config_path = _get_cloud_config_path()
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            project_id = config.get("project_id", "")
            api_url = _resolve_active_api_url()
            if project_id:
                from auth import load_credentials
                creds = load_credentials()
                if creds:
                    from api_client import ApiClient
                    def _token():
                        from auth import load_credentials as _lc
                        c = _lc()
                        return _extract_token(c) if c else ""
                    ApiClient(api_url, _token).clear_notes(project_id)
    except Exception:
        pass
    print("Session notes cleared.")
