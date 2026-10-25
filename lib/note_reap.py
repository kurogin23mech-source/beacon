"""Decide which session notes no live session can ever claim (ms-178 / e-7150).

Why a reaper exists at all
--------------------------
``beacon note clear`` is scoped to "notes whose session_id == mine" (ms-160
e-6714), which is what makes the session-end routine performable while other
sessions are working. But that scoping has a consequence nobody closed: a
session is the **only** party authorised to clear its own notes, and a session
does not outlive itself. When a session ends without clearing — it crashed, it
was killed, or (ms-178 / e-7150) its session_id churned so ``clear`` could not
find its own notes — those notes become unreachable. The only remaining verb
was ``--include-other-sessions``, which also takes every *live* session's
handoff notes. So the safe move was always "leave them", and they accumulated:
180 notes / 370,047 chars / 16 distinct session ids as of 2026-10-25, 97% of
them belonging to sessions that no longer exist.

This module answers one question — *can any live session still claim this
note?* — and nothing else. The deletion itself reuses
``cmd_note._selective_delete`` so the backup-before-delete ordering has exactly
one implementation.

The safety rule
---------------
Reaping is only safe when "live" is known **completely**. An incomplete live
set silently promotes live sessions' notes to orphans, and a reaper that eats
the handoff notes of a running session is worse than no reaper. So the live set
is a union of every source, and a source that fails to answer does not quietly
shrink it: :func:`resolve_live_ids` returns a refusal instead. Failing closed
is cheap here (the user re-runs later); failing open destroys notes that were
the whole point of the mechanism.
"""

from __future__ import annotations

# Returned as the ``refusal`` reason when liveness could not be established.
REFUSE_SERVER_UNREADABLE = "server-unreadable"


def resolve_live_ids(
    *,
    current_sid: str = "",
    session_json_sid: str = "",
    bridge_sids=(),
    server_sids=None,
    server_error: str = "",
    cloud_mode: bool = False,
):
    """Return ``(live_ids, refusal)`` — the set of claimable session ids.

    Sources are unioned, never intersected:

    * ``current_sid`` — whoever is running this command. Always trusted; if we
      cannot even keep our own notes the verb is broken.
    * ``session_json_sid`` — the id in ``.beacon/session.json``. In a shared
      cwd this is "some live session's id", not necessarily ours, which is
      precisely why it belongs in the keep set rather than the delete set.
    * ``bridge_sids`` — ids of alive bridge claims in this cwd (the caller
      filters to alive pids; liveness of a pid is an OS question, not ours).
    * ``server_sids`` — ids the server reports live. Cross-machine and
      cross-worktree, so it covers the sessions the two local sources cannot
      see.

    In cloud mode the server is not optional: a project whose notes live in the
    cloud has sessions this machine cannot enumerate. ``server_error`` (or a
    ``None`` ``server_sids``) therefore yields ``refusal`` =
    :data:`REFUSE_SERVER_UNREADABLE` and an empty set, so the caller stops.
    Local-mode projects have no such sessions, and the local sources are
    complete by construction.
    """
    if cloud_mode and (server_error or server_sids is None):
        return set(), REFUSE_SERVER_UNREADABLE

    live = set()
    for sid in (current_sid, session_json_sid):
        cleaned = (sid or "").strip()
        if cleaned:
            live.add(cleaned)
    for sid in bridge_sids or ():
        cleaned = (sid or "").strip()
        if cleaned:
            live.add(cleaned)
    for sid in server_sids or ():
        cleaned = (sid or "").strip()
        if cleaned:
            live.add(cleaned)
    return live, ""


def classify(notes, live_ids, *, include_untagged: bool = False) -> dict:
    """Split ``notes`` into what the reaper may take and what it must keep.

    Three buckets, because they have three different justifications:

    * ``orphans`` — tagged with a session id that is not live. Nobody can ever
      run ``note clear`` and match these again, so they are the reaper's
      subject.
    * ``untagged`` — no session id at all (written before session tagging, or
      by a caller whose identity was unresolvable). Also unclaimable, but
      "unclaimable" is the only thing we know about them: there is no evidence
      their session ended, only that it never said who it was. Kept unless
      ``include_untagged``, so the default never deletes on an absence of
      information.
    * ``kept`` — a live session's notes. Never the reaper's subject, whatever
      their age; "old" is not "finished".

    ``by_session`` counts orphans per session id so the caller can show whose
    notes, and how many each, before deleting anything.
    """
    orphans, untagged, kept = [], [], []
    for note in notes:
        sid = (note.get("session_id") or "").strip()
        if not sid:
            untagged.append(note)
        elif sid in live_ids:
            kept.append(note)
        else:
            orphans.append(note)

    by_session = {}
    for note in orphans:
        sid = (note.get("session_id") or "").strip()
        by_session[sid] = by_session.get(sid, 0) + 1

    doomed = list(orphans)
    if include_untagged:
        doomed.extend(untagged)

    return {
        "orphans": orphans,
        "untagged": untagged,
        "kept": kept,
        "doomed": doomed,
        "by_session": by_session,
    }


def is_doomed(note, live_ids, *, include_untagged: bool = False) -> bool:
    """Predicate form of :func:`classify` for ``_selective_delete``.

    Kept in sync with :func:`classify` by construction — both read the same
    two conditions in the same order — because the predicate is what actually
    deletes while ``classify`` is what the operator was shown.
    """
    sid = (note.get("session_id") or "").strip()
    if not sid:
        return bool(include_untagged)
    return sid not in live_ids
