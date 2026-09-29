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


def _note_api_client():
    """Return ``(client, error)`` for this project's notes API.

    ms-178 (maintainability review, PR#766): the credential load + token-provider
    closure + ApiClient construction was written out three times in this module
    (push / fetch / clear). commands_shared._get_api_client() already owns that
    idiom but `sys.exit(1)`s on failure, which a best-effort caller cannot use —
    hence one local factory that REPORTS the failure instead of exiting, shared by
    all three call sites so an auth/transport change lands in one place.

    ``error`` is "" on success; a client is returned only when error is "".
    """
    try:
        project_id = _cloud_project_id()
        if not project_id:
            return None, "local mode (cloud.json 無し)"
        from auth import load_credentials
        if load_credentials() is None:
            return None, "未認証 (beacon cloud login が必要)"
        from api_client import ApiClient

        def _token() -> str:
            from auth import load_credentials as _lc
            c = _lc()
            return _extract_token(c) if c else ""

        return ApiClient(_resolve_active_api_url(), _token), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _push_note_to_cloud_or_error(note: dict) -> str:
    """Push one note to the cloud. Returns "" on success, else a reason.

    ms-178 (AX + maintainability review consensus, PR#766): the fire-and-forget
    sibling below is fine for `note add` — the note is on local disk either way,
    so silence loses nothing. It is NOT fine for `note restore`, which re-posts
    the ONLY surviving copy of notes `note clear` already deleted from the shared
    cloud store. There, swallowing the failure turns "a silent write failure" into
    "an invented success", and the operator closes the incident while the shared
    notes are still gone — with no further backup behind it. So restore uses this
    reporting variant.
    """
    client, error = _note_api_client()
    if error:
        return error
    try:
        client.add_note(_cloud_project_id(), note)
        return ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _push_note_to_cloud(note: dict) -> None:
    """Push a session note to cloud API. Best-effort: silently ignores all errors.

    Deliberately silent: `note add` has already written the note to the local
    file, so a failed push costs visibility, not data. Recovery paths must use
    _push_note_to_cloud_or_error instead (see its docstring).
    """
    _push_note_to_cloud_or_error(note)


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
    client, error = _note_api_client()
    if error:
        return [], error
    try:
        notes = client.list_notes(project_id)
        return (notes if isinstance(notes, list) else []), ""
    except Exception as exc:  # network / server error
        return [], f"{type(exc).__name__}: {exc}"


def _cloud_backup_path() -> str:
    """Where `note clear` snapshots the cloud notes before deleting them.

    Sits beside the local .bak so the two legs of a clear are recovered from one
    place (ms-178 e-6656: the local .bak alone made "already restored" look true
    while a cloud-only note stayed lost)."""
    return _get_notes_path().replace(".jsonl", "") + ".cloud.bak"


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
        pid = _cloud_project_id()
        if pid:
            # AX review (PR#766): sizing only the local leg let the operator see
            # an exact number for "my" notes and a vague "some others exist" for
            # the SHARED store it is about to destroy — it could not tell 0 from
            # 200. A confirmation gate that hides the stake is not a gate.
            cloud_notes, cloud_error = _fetch_cloud_notes(pid)
            if cloud_error:
                others = "件数不明 — cloud を確認できません"
            else:
                local_keys = {_note_key(n) for n in _read_local_notes(path)}
                n_other = sum(1 for n in cloud_notes
                              if _note_key(n) not in local_keys)
                others = f"他セッション分 {n_other} 件を含む計 {len(cloud_notes)} 件"
            print(f"  cloud: this project's notes are SHARED by every session "
                  f"({others}) — clearing removes other sessions' handoff notes "
                  f"too. Snapshotted to {_cloud_backup_path()} first; restore "
                  f"with 'beacon note restore'.", file=sys.stderr)
        print("Re-run as 'beacon note clear --yes' to proceed.", file=sys.stderr)
        sys.exit(1)
    # ms-178 e-6656: take the CLOUD snapshot before deleting anything. The local
    # file was always moved to .bak, but the cloud notes were deleted outright —
    # so ".bak exists, therefore it is recovered" was structurally false, and a
    # note really did stay lost after a restore was reported as complete.
    #
    # Ordering is the guarantee: no backup ⇒ no delete. If the cloud cannot be
    # read we abort BOTH legs rather than clearing local and leaving the two
    # stores disagreeing about what happened.
    project_id = _cloud_project_id()
    cloud_notes = []
    if project_id:
        cloud_notes, cloud_error = _fetch_cloud_notes(project_id)
        if cloud_error:
            print(f"Aborted: cloud のメモを取得できず退避が取れません ({cloud_error})。",
                  file=sys.stderr)
            print("  何も削除していません (退避の取れない削除は行いません)。"
                  "接続を回復してから再実行してください。", file=sys.stderr)
            sys.exit(1)
        backup = _cloud_backup_path()
        try:
            os.makedirs(os.path.dirname(backup) or ".", exist_ok=True)
            with open(backup, "w", encoding="utf-8") as f:
                for n in cloud_notes:
                    f.write(json.dumps(n, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"Aborted: cloud の退避を書けません ({backup}: {exc})。"
                  f"何も削除していません。", file=sys.stderr)
            sys.exit(1)

    if os.path.exists(path):
        import shutil
        shutil.move(path, path + ".bak")

    cloud_cleared = True
    if project_id:
        try:
            client, error = _note_api_client()
            if error:
                raise RuntimeError(error)
            client.clear_notes(project_id)
        except Exception as exc:
            # Previously swallowed: a failed cloud delete still printed
            # "Session notes cleared.", so the two stores silently diverged.
            cloud_cleared = False
            print(f"Warning: cloud のメモを削除できませんでした ({exc})。"
                  f"ローカルのみクリアされ、cloud 側は残っています。", file=sys.stderr)

    print("Session notes cleared.")
    if os.path.exists(path + ".bak"):
        print(f"  local 退避: {path}.bak")
    if project_id and cloud_cleared:
        print(f"  cloud 退避: {_cloud_backup_path()} ({len(cloud_notes)} 件)")
    print("  復元: beacon note restore")


def cmd_note_restore():
    """Restore session notes from the backups `note clear` left (ms-178 e-6656).

    A backup nobody can restore from is not a backup, so the recovery path is a
    first-class verb rather than a documented hand-written loop. Restoring is
    additive and idempotent: notes already present are matched by `_note_key`
    and skipped, so running it twice does not duplicate anything.
    """
    path = _get_notes_path()
    local_bak = path + ".bak"
    cloud_bak = _cloud_backup_path()
    if not os.path.exists(local_bak) and not os.path.exists(cloud_bak):
        print(f"復元できる退避がありません ({local_bak} / {cloud_bak})。",
              file=sys.stderr)
        sys.exit(1)

    # --- local leg: union current ∪ backup, keeping timeline order ---
    current = _read_local_notes(path)
    restored_local = 0
    if os.path.exists(local_bak):
        have = {_note_key(n) for n in current}
        merged = list(current)
        for n in _read_local_notes(local_bak):
            if _note_key(n) not in have:
                have.add(_note_key(n))
                merged.append(n)
                restored_local += 1
        if restored_local:
            merged.sort(key=lambda n: n.get("ts", ""))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for n in merged:
                    f.write(json.dumps(n, ensure_ascii=False) + "\n")

    # --- cloud leg: re-post only what the cloud is missing ---
    restored_cloud = 0
    project_id = _cloud_project_id()
    if project_id and os.path.exists(cloud_bak):
        live, cloud_error = _fetch_cloud_notes(project_id)
        if cloud_error:
            print(f"Warning: cloud の現状を取得できず cloud への復元は行いません "
                  f"({cloud_error})。退避 {cloud_bak} は残っているので接続回復後に"
                  f"再実行してください。", file=sys.stderr)
        else:
            have = {_note_key(n) for n in live}
            failed = []
            for n in _read_local_notes(cloud_bak):
                if _note_key(n) in have:
                    continue
                # Strip the provenance tag the merge adds on read; it is a view
                # concern, not part of the stored note.
                payload = {k: v for k, v in n.items() if k != "origin"}
                # Count only CONFIRMED re-posts (AX + maintainability review
                # consensus, PR#766): counting the attempt reported a restore
                # that never happened, on the one path with no backup behind it.
                push_error = _push_note_to_cloud_or_error(payload)
                if push_error:
                    failed.append((n, push_error))
                    continue
                have.add(_note_key(n))
                restored_cloud += 1
            if failed:
                print(f"Warning: cloud への再投稿に {len(failed)} 件失敗しました。"
                      f"退避 {cloud_bak} は残してあるので、原因を解消してから "
                      f"'beacon note restore' を再実行してください "
                      f"(復元済みの分は重複しません)。", file=sys.stderr)
                for n, why in failed[:5]:
                    print(f"  - {n.get('ts', '?')[:16]} "
                          f"{(n.get('text') or '')[:40]}: {why}", file=sys.stderr)

    print(f"復元しました: local {restored_local} 件 / cloud {restored_cloud} 件")
    if restored_local == 0 and restored_cloud == 0:
        print("  (どちらの退避も既に反映済みでした — 重複は作りません)")
