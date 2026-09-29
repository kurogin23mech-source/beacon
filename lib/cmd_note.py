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
        # ms-178 e-6655: share one project_id resolver with the read path so the
        # two directions can never disagree about whether this is cloud mode.
        project_id = _cloud_project_id()
        if not project_id:
            return
        api_url = _resolve_active_api_url()
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
    except Exception as exc:
        # Best-effort, but SURFACE the failure (AX review PR#708): a silent
        # swallow makes an unattributed note indistinguishable from a genuine
        # no-active-target. Warn on stderr so the note still records but the
        # attribution miss is visible.
        worked_ids = []
        print(f"Warning: worked-target attribution failed ({exc}); "
              f"note recorded without target_ids", file=sys.stderr)
    if worked_ids:
        note["target_ids"] = worked_ids
        note["target_id"] = worked_ids[0]  # back-compat first-of-set
    path = _get_notes_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")
    _push_note_to_cloud(note)
    print(f"Note: {text[:60]}{'...' if len(text) > 60 else ''}")


def _read_local_notes(path: str) -> list:
    """Parse the local JSONL note file. Missing file = no notes (not an error)."""
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
    return notes


def _cloud_project_id() -> str:
    """The cloud project_id for this working directory, or "" in local mode."""
    try:
        config_path = _get_cloud_config_path()
        if not os.path.exists(config_path):
            return ""
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f).get("project_id", "") or ""
    except Exception:
        return ""


def _fetch_cloud_notes(project_id: str):
    """Return ``(notes, error)`` for the project's cloud notes.

    ms-178 e-6655: notes are written to BOTH stores but were only ever read
    back from the local file, so a fork worktree (its own .beacon/) could not
    see notes the parent session wrote — they read as "no notes exist" and an
    assignment was missed for a day.

    ``error`` is a short human string on failure and MUST be surfaced, never
    swallowed: the whole defect was "unreadable" being indistinguishable from
    "empty". The WRITE path may stay best-effort silent (the note is still on
    disk), but a silent read failure invents absence.
    """
    try:
        api_url = _resolve_active_api_url()
        from auth import load_credentials
        creds = load_credentials()
        if creds is None:
            return [], "未認証 (beacon cloud login が必要)"
        from api_client import ApiClient

        def _token():
            from auth import load_credentials as _lc
            c = _lc()
            return _extract_token(c) if c else ""

        notes = ApiClient(api_url, _token).list_notes(project_id)
        return (notes if isinstance(notes, list) else []), ""
    except Exception as exc:  # network / auth / server error
        return [], f"{type(exc).__name__}: {exc}"


def _note_key(note: dict):
    """Dedup identity of a note across the two stores.

    The CLI stamps ``ts`` itself and the server persists that same value
    (routers_projects.add_note uses ``body.ts or now()``), so a note written in
    cloud mode lands in both stores with an identical ts/text/session_id. That
    triple is the join key; nothing else is stable (the cloud copy's Firestore
    document id is not stored in the document).
    """
    return (note.get("ts", ""), note.get("text", ""), note.get("session_id", ""))


def cmd_note_list():
    path = _get_notes_path()
    json_mode = os.environ.get("BEACON_JSON", "") == "1"
    local = _read_local_notes(path)

    project_id = _cloud_project_id()
    cloud, cloud_error = ([], "")
    if project_id:
        cloud, cloud_error = _fetch_cloud_notes(project_id)

    # Merge local ∪ cloud, tagging provenance so a reader can tell "this came
    # from another session / worktree" from "this is mine" (AC4).
    merged = []
    seen = {}
    for n in local:
        item = dict(n)
        item["origin"] = "local"
        seen[_note_key(n)] = item
        merged.append(item)
    for n in cloud:
        key = _note_key(n)
        if key in seen:
            seen[key]["origin"] = "both"
            continue
        item = dict(n)
        item["origin"] = "cloud"
        seen[key] = item
        merged.append(item)
    merged.sort(key=lambda n: n.get("ts", ""))

    if json_mode:
        print(json.dumps(merged, ensure_ascii=False))
        if cloud_error:
            print(f"Warning: cloud のメモを取得できませんでした ({cloud_error})。"
                  f"下の一覧はこの作業フォルダのローカル分のみで、"
                  f"他セッションのメモが欠けている可能性があります。", file=sys.stderr)
        return

    if cloud_error:
        print(f"Warning: cloud のメモを取得できませんでした ({cloud_error})。"
              f"表示はローカル分のみです — 「メモなし」= 存在しない、とは判断できません。",
              file=sys.stderr)
    if not merged:
        if project_id and not cloud_error:
            # Distinguish "checked both stores, genuinely empty" from the
            # local-only reading that used to be reported the same way.
            print("(メモなし — この作業フォルダも cloud も空です)")
        else:
            print("(メモなし)")
        return
    # AC4: when nothing was written from THIS working directory, say so, so a
    # fork worktree does not read the parent's notes as its own.
    if project_id and not local and cloud:
        print(f"(この作業フォルダのメモはありません。以下 {len(cloud)} 件は"
              f"cloud にある他セッション由来のメモです)")
    for n in merged:
        ctx = f" [{n['context']}]" if n.get("context") else ""
        mark = " (他セッション)" if n.get("origin") == "cloud" else ""
        print(f"  {n['ts'][:16]}{ctx}: {n['text']}{mark}")


def cmd_note_clear():
    path = _get_notes_path()
    # ms-178 e-6654: refuse without an explicit confirmation. Clearing removes
    # the local file (recoverable from .bak) AND the project's cloud notes,
    # which are a store SHARED by every session on the project — one session
    # tidying up deletes the other sessions' handoff notes (observed 2026-09-28:
    # a fork's 3 notes were lost to the parent's cleanup). The gate lives here,
    # not only in bin/beacon, so the python entrypoint is safe no matter which
    # front end (bash dispatcher, Windows/Codex shim, direct call) reaches it.
    if os.environ.get("BEACON_NOTE_CLEAR_YES") != "1":
        count = 0
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                count = sum(1 for line in f if line.strip())
        print(f"Refusing to clear {count} session note(s) without confirmation.",
              file=sys.stderr)
        print(f"  local: {path} (moved to {path}.bak, recoverable)",
              file=sys.stderr)
        if os.path.exists(_get_cloud_config_path()):
            print("  cloud: this project's notes are SHARED by every session — "
                  "clearing removes other sessions' handoff notes too.",
                  file=sys.stderr)
        print("Re-run as 'beacon note clear --yes' to proceed.", file=sys.stderr)
        sys.exit(1)
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
