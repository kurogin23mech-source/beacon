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


DEFAULT_LIST_SINCE_DAYS = 3
DEFAULT_LIST_LIMIT = 20
# Promoted from a bare literal that appeared twice in cmd_note_reap
# (maintainability review, 2026-10-26): a default with no name has no canonical
# location, so the two copies could drift from each other and the help text had
# a third copy of the digit.
DEFAULT_REAP_LIVENESS_WINDOW_MIN = 5


def _int_or_notice(env_name: str, flag_name: str, default: int) -> int:
    """Read an int from ``env_name``, saying so when the value is unusable.

    AX review (2026-10-26): every numeric flag in this family used to swallow a
    non-numeric value and substitute the default in silence — ``--limit twenty``
    returned the ordinary 20-note window with no hint the word had been
    discarded. That is the "type mismatch silently recast as something else"
    failure the AX principles name directly, and it was the more galling for
    being in the same file as ``_format_omitted_notice``, which exists precisely
    to tell the caller that something was withheld. One helper now owns the
    rule, so a third numeric flag cannot reintroduce the silence by writing its
    own ``try/except ValueError: pass``.

    Unset / blank is not a mistake (it means "use the default"), so only a
    present-but-unparseable value is reported.
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print("Note: %s に数値でない値 %r が渡されたので、既定の %d を使います。"
              % (flag_name, raw, default), file=sys.stderr)
        return default


def _list_window():
    """Resolve the read window for ``note list`` (ms-178 / e-7150 AC7).

    ``note list`` had no bound of any kind and printed every note in full.
    ``/beacon-session-start`` runs it on every startup, so the whole store
    landed in the context window before any work began: 180 notes / 370,047
    chars, which is 18–37% of a 1M context at 1–2 chars per token for
    Japanese. The handoff value of notes was being outweighed by the cost of
    reading them.

    This bound is needed **independently** of the session_id fix in the same
    task: making ``note clear`` work again stops the 24-day pile-up, but a day
    on which several parallel sessions each write a long note reproduces the
    same pressure. So the default is a window, not a cleanup.

    Two limits rather than one, because each misses what the other catches: a
    day limit alone lets one busy day through, and a count limit alone lets a
    handful of very long notes through. ``--all`` turns both off for the cases
    that genuinely want the whole store (an audit, a migration).
    """
    if os.environ.get("BEACON_NOTE_LIST_ALL") == "1":
        return None, None
    days = _int_or_notice("BEACON_NOTE_LIST_SINCE_DAYS", "--since-days",
                          DEFAULT_LIST_SINCE_DAYS)
    limit = _int_or_notice("BEACON_NOTE_LIST_LIMIT", "--limit",
                           DEFAULT_LIST_LIMIT)
    # 0 / negative reads as "no bound on this axis" so a caller can cap only
    # the one it cares about without having to restate the other.
    return (days if days > 0 else None), (limit if limit > 0 else None)


def _apply_list_window(notes, days, limit):
    """Return ``(shown, omitted)`` — newest-first windowing, oldest dropped.

    Notes are ordered oldest-first by the caller (that is the reading order a
    handoff wants), so the window is taken from the END and the order is
    preserved. An unparseable ``ts`` is kept rather than dropped: a note we
    cannot date is not evidence that it is old.
    """
    if days is None and limit is None:
        return list(notes), 0

    kept = list(notes)
    if days is not None:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        windowed = []
        for note in kept:
            raw = (note.get("ts") or "").strip()
            if not raw:
                windowed.append(note)
                continue
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                windowed.append(note)
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed >= cutoff:
                windowed.append(note)
        kept = windowed
    if limit is not None and len(kept) > limit:
        kept = kept[-limit:]
    return kept, len(notes) - len(kept)


def _format_omitted_notice(omitted, days, limit):
    """One line stating what was withheld and how to see it.

    A truncation the reader cannot detect is worse than a long list: the reader
    concludes "that is all the notes there are". So the count is always stated
    when anything was dropped, together with the escape hatch.
    """
    bounds = []
    if days is not None:
        bounds.append("直近 %d 日" % days)
    if limit is not None:
        bounds.append("最大 %d 件" % limit)
    scope = " / ".join(bounds) if bounds else "既定の範囲"
    return ("… 他 %d 件は省略しました (%s で表示中)。"
            "全件は `beacon note list --all`、範囲変更は "
            "`--since-days <N>` / `--limit <N>`。" % (omitted, scope))


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

    # AC7: bound what a reader (above all /beacon-session-start) pulls in.
    # Applied to BOTH output modes — the JSON mode is the one session-start
    # actually calls, so capping only the human mode would leave the defect
    # exactly where it was measured.
    days, limit = _list_window()
    total_before = len(merged)
    merged, omitted = _apply_list_window(merged, days, limit)

    if json_mode:
        print(json.dumps(merged, ensure_ascii=False))
        if omitted:
            print(_format_omitted_notice(omitted, days, limit), file=sys.stderr)
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
        if total_before:
            # The store is NOT empty — the window emptied the view. Saying
            # "no notes" here would be a false negative of exactly the kind
            # the omitted-count notice exists to prevent.
            print("(表示範囲内のメモはありません — %s)"
                  % _format_omitted_notice(omitted, days, limit))
        elif project_id and not cloud_error:
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
    if omitted:
        # Before the list, not after: a reader who stops at the first screen
        # still learns that this is a window rather than the whole store.
        print("  " + _format_omitted_notice(omitted, days, limit))
    for n in merged:
        ctx = f" [{n['context']}]" if n.get("context") else ""
        mark = " (他セッション)" if n.get("origin") == "cloud" else ""
        print(f"  {n['ts'][:16]}{ctx}: {n['text']}{mark}")



def _clear_backup_desc(local, cloud, project_id) -> str:
    """Write the backups `note restore` reads (ms-178 e-6656): local pre-clear
    content at ``.jsonl.bak``, cloud pre-clear content at ``.cloud.bak``.

    Scoped clearing must keep writing BOTH. An earlier draft of e-6714 replaced
    them with one combined file and silently orphaned ``note restore`` — the
    recovery path e-6656 exists to provide. Caught only because e-6656's tests
    are written against the contract rather than the implementation.
    """
    path = _get_notes_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".bak", "w", encoding="utf-8") as f:
        for n in local:
            f.write(json.dumps(n, ensure_ascii=False) + "\n")
    if project_id:
        cb = _cloud_backup_path()
        os.makedirs(os.path.dirname(cb) or ".", exist_ok=True)
        with open(cb, "w", encoding="utf-8") as f:
            for n in cloud:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
        return path + ".bak + " + cb
    return path + ".bak"


def _combined_backup_desc(local, cloud, project_id) -> str:
    """One file holding both stores, for purge-probes. Deliberately NOT the
    clear backups: either operation must not destroy the other's recovery
    route."""
    dest = _purge_backup_path()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        for n in local:
            f.write(json.dumps(dict(n, origin="local"), ensure_ascii=False) + "\n")
        for n in cloud:
            f.write(json.dumps(dict(n, origin="cloud"), ensure_ascii=False) + "\n")
    return dest


# What an unconfirmed invocation does. Named rather than bare literals, and
# required at every call site: the value is matched at more than one branch, so
# a typo or a third caller must fail loudly rather than quietly land on
# whichever branch it happens to equal (maintainability review, PR #782 M-2).
UNCONFIRMED_REFUSE = "refuse"      # destructive verb: state the stake, exit non-zero
UNCONFIRMED_PREVIEW = "preview"    # opt-in cleanup: dry run, exit 0


def _selective_delete(should_delete, *, label, write_backups, confirm,
                      rerun_hint, on_unconfirmed, preview=None,
                      touches_other_sessions=True):
    """Delete the notes ``should_delete(note)`` picks; keep the rest.

    One mechanism, two policies. ``note clear`` (this session's notes) and
    ``note purge-probes`` (machine garbage) are the same operation with a
    different predicate; writing it twice would give the backup ordering, the
    re-post accounting and the failure reporting two chances each to be wrong.

    The cloud store has no delete-one, so its leg is snapshot → clear → re-post
    the survivors. That window can lose a note another session writes while it
    runs; disclosed rather than designed away, because a server endpoint needs
    a production deploy this project does not currently do.

    Ordering is the guarantee: no backup ⇒ no delete.

    ``on_unconfirmed`` is "refuse" for the destructive verb (state the stake on
    stderr, exit non-zero) and "preview" for the opt-in cleanup (dry run on
    stdout, exit 0). Returns the number removed, or -1 when nothing was done.
    """
    if on_unconfirmed not in (UNCONFIRMED_REFUSE, UNCONFIRMED_PREVIEW):
        raise ValueError("unknown on_unconfirmed policy: %r" % (on_unconfirmed,))
    path = _get_notes_path()
    project_id = _cloud_project_id()

    local = _read_local_notes(path)
    local_go = [n for n in local if should_delete(n)]
    local_keep = [n for n in local if not should_delete(n)]

    cloud, cloud_go, cloud_keep, cloud_error = [], [], [], ""
    if project_id:
        cloud, cloud_error = _fetch_cloud_notes(project_id)
        if cloud_error and confirm:
            # A store we cannot read is a store we cannot back up, so when we
            # are about to delete we delete from neither. While only SIZING
            # the stake, an unreadable cloud is reported as "count unknown" —
            # refusing to describe the operation would hide the stake instead
            # of stating it (ms-178 e-6656).
            print("Aborted: cloud のメモを取得できず退避が取れません "
                  "(%s)。" % cloud_error, file=sys.stderr)
            print("  何も削除していません (退避の取れない削除は行いません)。",
                  file=sys.stderr)
            sys.exit(1)
        cloud_go = [n for n in cloud if should_delete(n)]
        cloud_keep = [n for n in cloud if not should_delete(n)]

    total = len(local_go) + len(cloud_go)

    if total == 0 and on_unconfirmed == UNCONFIRMED_REFUSE:
        # Nothing of ours to clear. Exit 0 so the session-end routine
        # COMPLETES — being unable to finish it while others were working is
        # the defect e-6714 is about, and refusing here would reproduce it in
        # a new place. Say what is left and how to reach it.
        others = len(local) + len(cloud) - total
        print("%s はありません (削除するものなし)。" % label)
        if others and touches_other_sessions:
            print("  他セッション分が %d 件あります。消す必要があるなら "
                  "'beacon note clear --include-other-sessions --yes' を明示してください。" % others)
        elif others:
            # AX review (2026-10-26): this line used to print unconditionally,
            # so the two callers that pass touches_other_sessions=False — the
            # scoped `note clear` and `note reap` — recommended
            # `--include-other-sessions`, which deletes every LIVE session's
            # handoff notes. For `reap` that is the exact operation it exists to
            # make unnecessary: the safe verb's own empty-result path pointed at
            # the unsafe one. It also called the remaining notes 他セッション分
            # when the bucket includes this session's own and untagged notes.
            # The parameter the callers already pass now gates this, so the
            # recommendation cannot outlive the scope that justified it.
            print("  残り %d 件はこの口の対象外です (稼働中セッションのメモ / "
                  "セッション未記録のメモ)。" % others)
        return -1

    if not confirm and on_unconfirmed == UNCONFIRMED_REFUSE:
        # A confirmation gate that hides the stake is not a gate (ms-178, AX
        # review of PR#766). Size it on stderr and exit non-zero: this is a
        # refusal, not a successful preview.
        print("Refusing to clear %d session note(s) without confirmation (%s)."
              % (len(local_go), label), file=sys.stderr)
        print("  local: %s (moved to %s.bak, recoverable)" % (path, path),
              file=sys.stderr)
        if project_id and touches_other_sessions:
            if cloud_error:
                others = "件数不明 — cloud を確認できません"
            else:
                local_keys = {_note_key(n) for n in local}
                n_other = sum(1 for n in cloud_go
                              if _note_key(n) not in local_keys)
                others = ("他セッション分 %d 件を含む計 %d 件"
                          % (n_other, len(cloud_go)))
            print("  cloud: this project's notes are SHARED by every session "
                  "(%s) — clearing removes other sessions' handoff notes too. "
                  "Snapshotted to %s first; restore with 'beacon note restore'."
                  % (others, _cloud_backup_path()), file=sys.stderr)
        elif project_id:
            # The scoped delete touches only this session's notes, so the
            # shared-store warning would be false here. Saying it anyway made
            # the refusal explain itself with a reason that does not apply —
            # and would push a careful agent into stopping a safe operation
            # (AX review, PR #782).
            print("  cloud: 対象はこのセッションが書いたメモだけです "
                  "(他セッションの引き継ぎメモは消えません)。退避は %s。"
                  % _cloud_backup_path(), file=sys.stderr)
        if preview:
            preview(local_go, cloud_go, local_keep, cloud_keep)
        print("Re-run as '%s' to proceed." % rerun_hint, file=sys.stderr)
        sys.exit(1)

    if total == 0:
        print("%s は見つかりませんでした。削除するものはありません。" % label)
        return -1

    print("削除対象 (%s): local %d 件 / cloud %d 件"
          % (label, len(local_go), len(cloud_go)))
    print("残すメモ: local %d 件 / cloud %d 件"
          % (len(local_keep), len(cloud_keep)))
    if preview:
        preview(local_go, cloud_go, local_keep, cloud_keep)
    if not confirm:
        print()
        print("これは下見です (まだ何も変更していません)。")
        if project_id:
            print("  実行すると cloud のメモを一度すべて消してから、残すメモを"
                  "投稿し直します。")
            print("  この間に別セッションが書いたメモは失われます "
                  "(退避は取るので復元の手当ては可能)。")
        print("  実行する: %s" % rerun_hint)
        return -1

    try:
        backup_desc = write_backups(local, cloud, project_id)
    except OSError as exc:
        print("Aborted: 退避を書けません (%s)。何も削除していません。" % exc,
              file=sys.stderr)
        sys.exit(1)

    if local_go:
        if local_keep:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                for n in local_keep:
                    f.write(json.dumps(n, ensure_ascii=False) + "\n")
        elif os.path.exists(path):
            # Nothing survives: the file goes away rather than being left
            # empty — the post-condition `note restore` was written against.
            os.remove(path)

    reposted, failed = 0, []
    if project_id and cloud_go:
        try:
            client, error = _note_api_client()
            if error:
                raise RuntimeError(error)
            client.clear_notes(project_id)
        except Exception as exc:
            print("Warning: cloud のメモを削除できませんでした (%s)。"
                  "ローカルのみクリアされ、cloud 側は残っています。" % exc,
                  file=sys.stderr)
            cloud_keep = []
        for n in cloud_keep:
            payload = {k: v for k, v in n.items() if k != "origin"}
            push_error = _push_note_to_cloud_or_error(payload)
            if push_error:
                failed.append((n, push_error))
                continue
            reposted += 1

    print("Session notes cleared.")
    print("  退避: %s" % backup_desc)
    if project_id:
        print("  cloud に戻したメモ: %d / %d 件" % (reposted, len(cloud_keep)))
    print("  復元: beacon note restore")
    if failed:
        # Counting attempts as successes is the ms-178 e-6656 defect; report
        # the shortfall instead of inventing it.
        print("Warning: cloud への再投稿に %d 件失敗しました。退避 %s に全文が"
              "残っています。" % (len(failed), backup_desc), file=sys.stderr)
        for n, why in failed[:5]:
            print("  - %s %s: %s" % ((n.get("ts") or "?")[:16],
                                     (n.get("text") or "")[:40], why),
                  file=sys.stderr)
        sys.exit(1)
    return total


def _session_label(note) -> str:
    sid = (note.get("session_id") or "").strip()
    return sid[-12:] if sid else "(セッション未記録)"


def cmd_note_clear():
    """Clear session notes — by default ONLY this session's (ms-160 e-6714).

    Before this, `clear` wiped the whole shared store. The cloud notes belong
    to every session on the project, so one session tidying up took the other
    sessions' handoff notes with it (2026-09-28: a fork lost 3 notes to the
    parent's cleanup; 2026-10-01: 28 notes of which 10 were other sessions').

    The heavier consequence was not the lost notes but that it made the
    session-end routine *unperformable* while anyone else was working — so
    notes accumulated instead of being promoted. Scoping the default makes the
    routine completable whatever else is running. `--include-other-sessions`
    still clears
    everything and says whose notes it is about to take first.
    """
    confirm = os.environ.get("BEACON_NOTE_CLEAR_YES") == "1"
    want_all = os.environ.get("BEACON_NOTE_CLEAR_ALL") == "1"
    session_id = (_resolve_session_id() or "").strip()

    if want_all:
        def _preview(local_go, cloud_go, local_keep, cloud_keep):
            # "N 件消えます" does not let the operator weigh the cost; whose
            # notes, and how many each, does.
            by = {}
            for n in local_go + cloud_go:
                by[_session_label(n)] = by.get(_session_label(n), 0) + 1
            mine = _session_label({"session_id": session_id})
            out = sys.stderr if not confirm else sys.stdout
            print("  内訳 (セッション別):", file=out)
            for lab in sorted(by, key=lambda k: -by[k]):
                tag = " ← このセッション" if (lab == mine and session_id) else ""
                print("    %s: %d 件%s" % (lab, by[lab], tag), file=out)
            if session_id and any(k != mine for k in by):
                print("  ⚠ 他セッションの引き継ぎメモが含まれます。"
                      "自分の分だけなら --include-other-sessions を外してください。", file=out)

        _selective_delete(lambda n: True, label="全セッションのメモ",
                          write_backups=_clear_backup_desc, confirm=confirm,
                          rerun_hint="beacon note clear --include-other-sessions --yes",
                          on_unconfirmed=UNCONFIRMED_REFUSE, preview=_preview)
        return

    if not session_id:
        # Without an id we cannot tell our notes from anyone else's. Deleting
        # "probably mine" is the exact failure this scoping exists to stop.
        print("Error: このセッションの id が解決できないため、自分のメモだけを"
              "選べません。", file=sys.stderr)
        print("  全件消すなら 'beacon note clear --include-other-sessions --yes' を明示してください "
              "(他セッションのメモも消えます)。", file=sys.stderr)
        sys.exit(1)

    _selective_delete(
        lambda n: (n.get("session_id") or "").strip() == session_id,
        label="このセッション (%s) のメモ" % _session_label(
            {"session_id": session_id}),
        write_backups=_clear_backup_desc, confirm=confirm,
        rerun_hint="beacon note clear --yes",
        on_unconfirmed=UNCONFIRMED_REFUSE, touches_other_sessions=False)


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


def _purge_backup_path() -> str:
    """Where `note purge-probes` snapshots BOTH stores before touching them.

    Deliberately NOT the paths `note clear` uses (.bak / .cloud.bak): a purge
    must not overwrite a clear's backup, or running one would destroy the other
    one's only recovery route (`beacon note restore` reads those two).
    """
    return _get_notes_path().replace(".jsonl", "") + ".purge.bak"


def _is_probe_note(note: dict) -> bool:
    """True for a note whose text is the AX surface probe's bogus token.

    Matched against the one definition in cli_surface, never a local copy — a
    second spelling would make this skip the garbage it exists to remove.
    Exact match (after stripping), not a substring: a human note *mentioning*
    the sentinel (this task's own handoff notes do) must survive.
    """
    import cli_surface
    return (note.get("text") or "").strip() == cli_surface.SURFACE_PROBE_SENTINEL


def _collect_live_session_ids(since_minutes: int):
    """Gather every source that can say "this session is still running".

    Returns what :func:`note_reap.resolve_live_ids` needs, having already done
    the IO. Split out from :func:`cmd_note_reap` so the decision (pure, tested)
    and the gathering (impure, best-effort per source) stay separable.

    The local sources are read first and unconditionally, because they work in
    local mode and when the network is down. The server is consulted only in
    cloud mode, where it is the *only* source that can see sessions on another
    machine or in a sibling worktree — so its failure is reported, never
    swallowed.
    """
    import session as _session

    current_sid = (_resolve_session_id() or "").strip()
    session_json_sid = ((_session.read_session() or {}).get("session_id") or "").strip()

    bridge_sids = []
    bridge_error = ""
    try:
        for claim in _session.read_all_bridge_claims():
            pid = claim.get("pid")
            if pid and _session._pid_alive(pid):
                bridge_sids.append((claim.get("session_id") or "").strip())
    except Exception as exc:
        # A claim directory we cannot read is not evidence of absence — it is
        # the *other* live sessions in this working directory going invisible.
        # Report it rather than swallowing it: in local mode this is the only
        # source that can see them, so note_reap.resolve_live_ids refuses on it
        # (an earlier version passed silently here and the module's "refuses
        # rather than guesses" promise was false in local mode).
        bridge_error = str(exc) or exc.__class__.__name__

    project_id = _cloud_project_id()
    server_sids, server_error = None, ""
    if project_id:
        client, err = _note_api_client()
        if err or client is None:
            server_error = err or "api client unavailable"
        else:
            try:
                rows = client.list_sessions(
                    project_id, live_only=True, since_minutes=since_minutes)
                server_sids = [
                    (r.get("session_id") or "").strip() for r in (rows or [])]
            except Exception as exc:
                server_error = str(exc) or exc.__class__.__name__

    return {
        "current_sid": current_sid,
        "session_json_sid": session_json_sid,
        "bridge_sids": bridge_sids,
        "bridge_error": bridge_error,
        "server_sids": server_sids,
        "server_error": server_error,
        "cloud_mode": bool(project_id),
    }


def cmd_note_reap():
    """Delete notes no live session can ever claim (ms-178 / e-7150).

    ``note clear`` can only ever match the running session's own notes, so
    notes outlive their only authorised cleaner: a session that ended without
    clearing leaves notes that nothing but
    ``--include-other-sessions`` can reach, and that flag also takes the
    handoff notes of sessions that are still working. This verb is the missing
    third option — "take what is provably finished, keep what is provably
    running" — so tidying up never requires risking a live session's notes.

    Not time-based on purpose: an old note belonging to a live session is still
    that session's note. The criterion is liveness of the writer, never age.

    Refuses rather than guesses when liveness cannot be established (see
    ``note_reap.resolve_live_ids``) — an incomplete live set would reclassify
    running sessions' notes as orphans.
    """
    import note_reap

    confirm = os.environ.get("BEACON_NOTE_REAP_YES") == "1"
    include_untagged = os.environ.get("BEACON_NOTE_REAP_UNTAGGED") == "1"
    window_minutes = _int_or_notice(
        "BEACON_NOTE_REAP_SINCE_MIN", "--since-min",
        DEFAULT_REAP_LIVENESS_WINDOW_MIN)

    sources = _collect_live_session_ids(window_minutes)

    # AX review (2026-10-26): the flag reached the handler, was parsed, and in a
    # local-mode project was then discarded without a word — the liveness window
    # only means anything to the server query. A caller who widened it to protect
    # a slow session had no way to learn it had protected nothing.
    if (os.environ.get("BEACON_NOTE_REAP_SINCE_MIN", "").strip()
            and not sources["cloud_mode"]):
        print("Note: --since-min はサーバに稼働中セッションを問い合わせる"
              "ときだけ効きます。このプロジェクトはローカルなので、指定は結果を"
              "変えていません。", file=sys.stderr)

    live_ids, refusal = note_reap.resolve_live_ids(**sources)

    if refusal:
        detail, recovery = {
            note_reap.REFUSE_SERVER_UNREADABLE: (
                sources["server_error"] or "live session list unavailable",
                "ネットワーク / 認証を確認して再実行してください。"),
            note_reap.REFUSE_BRIDGE_UNREADABLE: (
                sources["bridge_error"] or "bridge claims unreadable",
                "この作業フォルダの .beacon/bridges/ が読めるか確認して"
                "再実行してください。"),
        }.get(refusal, (refusal, "再実行してください。"))
        print("Aborted: 稼働中セッションの一覧を取得できないため、どのメモが"
              "孤児かを判定できません (%s)。" % detail, file=sys.stderr)
        print("  何も削除していません。判定できないまま消すと、動いている"
              "セッションの引き継ぎメモを孤児として巻き込みます。",
              file=sys.stderr)
        print("  %s" % recovery, file=sys.stderr)
        sys.exit(1)

    path = _get_notes_path()
    project_id = _cloud_project_id()
    local = _read_local_notes(path)
    cloud, cloud_error = ([], "")
    if project_id:
        cloud, cloud_error = _fetch_cloud_notes(project_id)
    merged, seen = [], set()
    for note in list(local) + list(cloud):
        key = _note_key(note)
        if key in seen:
            continue
        seen.add(key)
        merged.append(note)

    verdict = note_reap.classify(merged, live_ids,
                                 include_untagged=include_untagged)

    def _preview(local_go, cloud_go, local_keep, cloud_keep):
        out = sys.stdout if confirm else sys.stderr
        print("  稼働中と判定したセッション: %d 件" % len(live_ids), file=out)
        print("  孤児 (どの稼働中セッションにも属さない): %d 件"
              % len(verdict["orphans"]), file=out)
        for sid in sorted(verdict["by_session"],
                          key=lambda k: -verdict["by_session"][k]):
            print("    %s: %d 件" % (_session_label({"session_id": sid}),
                                     verdict["by_session"][sid]), file=out)
        if verdict["kept"]:
            print("  残す (稼働中セッションのメモ): %d 件"
                  % len(verdict["kept"]), file=out)
        if verdict["untagged"]:
            # Stated either way: silently keeping them looks like "there were
            # none", and silently taking them deletes on missing information.
            state = "削除します" if include_untagged else "残します"
            print("  セッション未記録のメモ: %d 件 — %s "
                  "(--include-untagged で削除対象に含められます)"
                  % (len(verdict["untagged"]), state), file=out)

    removed = _selective_delete(
        lambda n: note_reap.is_doomed(n, live_ids,
                                      include_untagged=include_untagged),
        label="終了したセッションのメモ",
        write_backups=_clear_backup_desc, confirm=confirm,
        rerun_hint="beacon note reap --yes",
        on_unconfirmed=UNCONFIRMED_REFUSE, preview=_preview,
        touches_other_sessions=False)
    return removed


def cmd_note_purge_probes():
    """Remove the probe notes an unguarded AX surface audit wrote (ms-160 e-6715).

    The read-only gate stops NEW ones; this clears the backlog. Shares the
    delete mechanism with `note clear` (e-6714) — same operation, different
    predicate — so the backup ordering and the re-post accounting exist once.
    """
    import cli_surface
    _selective_delete(
        _is_probe_note,
        label="点検メモ (%s)" % cli_surface.SURFACE_PROBE_SENTINEL,
        write_backups=_combined_backup_desc,
        confirm=os.environ.get("BEACON_NOTE_PURGE_CONFIRM") == "1",
        rerun_hint="beacon note purge-probes --confirm",
        on_unconfirmed=UNCONFIRMED_PREVIEW)


