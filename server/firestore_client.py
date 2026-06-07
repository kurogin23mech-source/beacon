"""Firestore client wrapper for Beacon API."""

from __future__ import annotations

import os

from google.cloud import firestore

_db: firestore.Client | None = None

PROJECT_ID = "beacon-cloud-96f5f"

# Environment-based collection prefix: dev uses "projects-dev", prod uses "projects"
_ENV = os.environ.get("BEACON_ENV", "dev")
COLLECTION = "projects" if _ENV == "prod" else "projects-dev"
USERS_COLLECTION = "users" if _ENV == "prod" else "users-dev"


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID)
    return _db


def get_project(project_id: str) -> dict | None:
    """Load a project document. Returns None if not found."""
    doc = get_db().collection(COLLECTION).document(project_id).get()
    if not doc.exists:
        return None
    return doc.to_dict()


def save_project(project_id: str, data: dict) -> None:
    """Save a project document (full replace)."""
    get_db().collection(COLLECTION).document(project_id).set(data)


def list_projects(user_id: str | None = None, include_archived: bool = False) -> list[dict]:
    """List projects. If user_id is given, only return projects owned by or shared with that user."""
    query = get_db().collection(COLLECTION)
    docs = query.stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        if not include_archived and data.get("archived"):
            continue
        if user_id:
            owner = data.get("owner")
            # Projects without owner are visible to all (migration period)
            if owner:
                members = [m.get("user_id") for m in data.get("members", [])]
                if owner != user_id and user_id not in members:
                    continue
        result.append({
            "project_id": doc.id,
            "name": data.get("name", ""),
            "objective": data.get("objective", ""),
            "archived": data.get("archived", False),
        })
    return result


# ---------------------------------------------------------------------------
# Users (collection: users/{user_id})
# ---------------------------------------------------------------------------


def get_user(user_id: str) -> dict | None:
    """Get a user document. Returns None if not found."""
    doc = get_db().collection(USERS_COLLECTION).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def get_or_create_user(user_id: str, email: str) -> dict:
    """Get or create a user document. Returns user data."""
    import datetime

    doc_ref = get_db().collection(USERS_COLLECTION).document(user_id)
    doc = doc_ref.get()
    if doc.exists:
        user_data = doc.to_dict()
        # Update email if changed
        if user_data.get("email") != email:
            doc_ref.update({"email": email})
            user_data["email"] = email
        return user_data

    user_data = {
        "email": email,
        "role": "user",
        "created_at": datetime.datetime.now().isoformat(),
    }
    doc_ref.set(user_data)
    return user_data


def list_users() -> list[dict]:
    """List all users."""
    docs = get_db().collection(USERS_COLLECTION).stream()
    return [{"user_id": doc.id, **doc.to_dict()} for doc in docs]


def update_user(user_id: str, updates: dict) -> bool:
    """Update user fields. Returns True if user existed."""
    doc_ref = get_db().collection(USERS_COLLECTION).document(user_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.update(updates)
    return True


def delete_user(user_id: str) -> bool:
    """Delete a user. Returns True if existed."""
    doc_ref = get_db().collection(USERS_COLLECTION).document(user_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True


def list_all_projects() -> list[dict]:
    """List all projects (admin). Returns summary only, no entries."""
    import datetime
    docs = get_db().collection(COLLECTION).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        result.append({
            "project_id": doc.id,
            "name": data.get("name", ""),
            "owner": data.get("owner", ""),
            "member_count": len(data.get("members", [])),
            "milestone_count": len(data.get("milestones", [])),
            "updated_at": data.get("updated_at", ""),
        })
    return result


def _delete_subcollection(col_ref) -> None:
    """Delete all documents in a subcollection (Firestore doesn't auto-delete these)."""
    for doc in col_ref.stream():
        doc.reference.delete()


def delete_project(project_id: str) -> bool:
    """Delete a project and ALL its subcollections. Returns True if existed."""
    doc_ref = get_db().collection(COLLECTION).document(project_id)
    if not doc_ref.get().exists:
        return False
    # Delete known subcollections first (Firestore does not cascade).
    # Use literal names here to avoid forward-reference issues with the constants.
    # Changelog is project-scoped: when the project is deleted, the audit
    # trail goes with it (no orphan access path).
    for subcol_name in ("documents", "retros", "members", "triggers", "notes",
                        "changelog"):
        _delete_subcollection(doc_ref.collection(subcol_name))
    doc_ref.delete()
    return True


def find_user_by_email(email: str) -> tuple[str, dict] | None:
    """Find a user by email. Returns (user_id, user_data) or None."""
    docs = (
        get_db()
        .collection(USERS_COLLECTION)
        .where("email", "==", email)
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.id, doc.to_dict()
    return None


# ---------------------------------------------------------------------------
# Retros (subcollection: projects/{project_id}/retros/{week})
# ---------------------------------------------------------------------------

RETRO_SUBCOLLECTION = "retros"


def list_retros(project_id: str) -> list[dict]:
    """List all retro documents for a project (week + updated_at only)."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(RETRO_SUBCOLLECTION)
        .order_by("week", direction=firestore.Query.DESCENDING)
        .stream()
    )
    return [{"week": doc.id, **doc.to_dict()} for doc in docs]


def get_retro(project_id: str, week: str) -> dict | None:
    """Get a single retro document."""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(RETRO_SUBCOLLECTION)
        .document(week)
        .get()
    )
    if not doc.exists:
        return None
    return {"week": doc.id, **doc.to_dict()}


# ---------------------------------------------------------------------------
# Documents (subcollection: projects/{project_id}/documents/{doc_id})
# ---------------------------------------------------------------------------

DOCS_SUBCOLLECTION = "documents"
DOC_REVISIONS_SUBCOLLECTION = "document_revisions"


def list_documents(project_id: str) -> list[dict]:
    """List all documents for a project. Soft-deleted docs are excluded."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .order_by("updated_at", direction=firestore.Query.DESCENDING)
        .stream()
    )
    result = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("deleted"):
            continue
        # milestone: Firestore field first, fallback to frontmatter
        milestone = data.get("milestone") or _extract_frontmatter_field(
            data.get("content", ""), "milestone"
        )
        entry = {
            "doc_id": doc.id,
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if milestone:
            entry["milestone"] = milestone
        result.append(entry)
    return result


def get_document(project_id: str, doc_id: str) -> dict | None:
    """Get a single document."""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .document(doc_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"doc_id": doc.id, **doc.to_dict()}


def _extract_frontmatter_field(content: str, field: str, default: str = "") -> str:
    """Extract a field from YAML frontmatter in content."""
    if not content.startswith("---"):
        return default
    end = content.find("\n---", 3)
    if end == -1:
        return default
    for line in content[4:end].split("\n"):
        line = line.strip()
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return default


def _extract_scope(content: str) -> str:
    """Extract scope from YAML frontmatter in content. Default: memo."""
    val = _extract_frontmatter_field(content, "scope", "memo")
    return val if val in ("core", "spec", "memo") else "memo"


def save_document(project_id: str, doc_id: str, title: str, content: str,
                  scope: str | None = None, updated_by: str = "unknown") -> str:
    """Save a document. If doc_id is empty, auto-generate one.
    When updating an existing document, the current content is saved as a revision first.
    """
    import datetime
    resolved_scope = scope if scope in ("core", "spec", "memo") else _extract_scope(content)
    milestone = _extract_frontmatter_field(content, "milestone")

    col = get_db().collection(COLLECTION).document(project_id).collection(DOCS_SUBCOLLECTION)
    data = {
        "title": title,
        "content": content,
        "scope": resolved_scope,
        "updated_at": datetime.datetime.now().isoformat(),
        "updated_by": updated_by,
    }
    if milestone:
        data["milestone"] = milestone

    if doc_id:
        doc_ref = col.document(doc_id)
        existing = doc_ref.get()
        if existing.exists:
            # Save current content as a revision before overwriting
            rev_col = doc_ref.collection(DOC_REVISIONS_SUBCOLLECTION)
            existing_data = existing.to_dict()
            rev_docs = rev_col.order_by("rev", direction="DESCENDING").limit(1).stream()
            last_rev = 0
            for r in rev_docs:
                last_rev = r.to_dict().get("rev", 0)
            rev_col.add({
                "rev": last_rev + 1,
                "content": existing_data.get("content", ""),
                "title": existing_data.get("title", ""),
                "ts": existing_data.get("updated_at", ""),
                "saved_by": existing_data.get("updated_by", "unknown"),
            })
        doc_ref.set(data)
        return doc_id
    else:
        ref = col.add(data)
        return ref[1].id


def list_document_revisions(project_id: str, doc_id: str) -> list:
    """List all revisions of a document (newest first)."""
    revs = (
        get_db()
        .collection(COLLECTION).document(project_id)
        .collection(DOCS_SUBCOLLECTION).document(doc_id)
        .collection(DOC_REVISIONS_SUBCOLLECTION)
        .order_by("rev", direction="DESCENDING")
        .stream()
    )
    return [{"rev": r.to_dict().get("rev"), "ts": r.to_dict().get("ts"), "saved_by": r.to_dict().get("saved_by")} for r in revs]


def get_document_revision(project_id: str, doc_id: str, rev: int) -> dict | None:
    """Get a specific revision of a document."""
    revs = (
        get_db()
        .collection(COLLECTION).document(project_id)
        .collection(DOCS_SUBCOLLECTION).document(doc_id)
        .collection(DOC_REVISIONS_SUBCOLLECTION)
        .where("rev", "==", rev)
        .limit(1)
        .stream()
    )
    for r in revs:
        d = r.to_dict()
        return {"rev": d.get("rev"), "content": d.get("content", ""), "title": d.get("title", ""), "ts": d.get("ts"), "saved_by": d.get("saved_by")}
    return None


def delete_document(project_id: str, doc_id: str, deleted_by: str = "unknown",
                    reason: str = "") -> bool:
    """Soft-delete a document (sets deleted flag, never physically removes).

    Optional ``reason`` is stored as ``trash_reason`` for audit symmetry
    with local mode's frontmatter (ms-14 e-991). Returns True if existed.
    """
    import datetime
    doc_ref = (
        get_db()
        .collection(COLLECTION).document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .document(doc_id)
    )
    doc = doc_ref.get()
    if not doc.exists:
        return False
    update = {
        "deleted": True,
        "deleted_at": datetime.datetime.now().isoformat(),
        "deleted_by": deleted_by,
        # Clear any prior restore stamps so audit fields reflect the
        # current trash event.
        "restored_at": firestore.DELETE_FIELD,
        "restored_by": firestore.DELETE_FIELD,
        "restore_reason": firestore.DELETE_FIELD,
    }
    if reason:
        update["trash_reason"] = reason
    else:
        update["trash_reason"] = firestore.DELETE_FIELD
    doc_ref.update(update)
    return True


def sweep_trashed_documents(project_id: str, *, days: int = 30,
                            dry_run: bool = False) -> list[str]:
    """Hard-delete soft-deleted docs older than ``days`` (ms-14 e-991).

    Companion to ``core.sweep_trashed_in_project`` for the milestone /
    task case — docs live in this subcollection rather than in the
    project json blob so they need their own sweep path.

    Returns the list of ``doc_id`` that were (or would be) deleted.
    ``dry_run=true`` returns ids without removing.

    Docs with ``deleted=true`` but missing ``deleted_at`` are NOT swept
    (mirrors the MS / task rule — no timestamp, no proof the window has
    passed). Operator can purge them manually.
    """
    import datetime
    cutoff_iso = (datetime.datetime.now()
                  - datetime.timedelta(days=max(1, days))).isoformat()
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
    )
    snaps = col.where("deleted", "==", True).stream()
    purged: list[str] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        deleted_at = data.get("deleted_at", "")
        if not deleted_at or deleted_at >= cutoff_iso:
            continue
        purged.append(snap.id)
        if not dry_run:
            # Also remove the doc's revisions subcollection so we don't
            # orphan storage. Firestore has no cascade.
            _delete_subcollection(
                snap.reference.collection(DOC_REVISIONS_SUBCOLLECTION)
            )
            snap.reference.delete()
    return purged


# ---------------------------------------------------------------------------
# Changelog (subcollection: projects/{project_id}/changelog/{doc_id})
# ---------------------------------------------------------------------------
#
# Append-only audit trail of mutating operations (ms-14 e-825). Every
# write that crosses the API boundary should also land an entry here so
# that:
#
#   - data-destructive operations (purge, delete, sweep) are traceable
#     beyond the lifetime of the data they removed,
#   - multi-user projects can show "who did what" in the Web UI, and
#   - the 30-day soft-delete window (e-826 / e-991) has a permanent
#     companion record after the trashed item itself ages out.
#
# Document IDs are millisecond-prefixed iso8601 timestamps with a random
# suffix so:
#   - they sort naturally in Firestore ASC ordering, and
#   - two writes in the same millisecond don't collide.
#
# No DELETE function exists. Removal must go through Firestore Admin SDK
# (maintainer-only, leaves an admin audit trail of its own).

CHANGELOG_SUBCOLLECTION = "changelog"


def append_changelog(project_id: str, entry: dict) -> str:
    """Append one structured entry to the project's changelog subcollection.

    Best-effort: returns the document id on success, empty string on
    failure. Callers MUST NOT raise on failure — the audit trail is a
    non-functional concern that must never break the actual write.

    The caller controls the entry's contents. Recommended fields (none
    are enforced here so the schema can evolve without breaking older
    readers):
        ts          ISO8601 UTC timestamp
        op          short action key (\"milestone.delete\", \"trash.sweep\", ...)
        actor       user sub (or \"system\" for cron-driven sweeps)
        email       resolved email of the actor
        reason      human-readable explanation, optional
        target      {\"type\": ..., \"id\": ...}, optional
        ip          requester IP if available
        user_agent  HTTP UA if available
        payload     free-form dict for extra context (small)
    """
    import datetime
    import secrets

    # Millisecond-resolution ISO timestamp + 4 hex chars to break ties
    # between concurrent writes. The Firestore SDK orders string IDs
    # lexicographically so this gives us natural time-ordering.
    now = datetime.datetime.now(datetime.timezone.utc)
    doc_id = now.strftime("%Y%m%dT%H%M%S.%f") + "-" + secrets.token_hex(2)

    # Ensure ts is always present, even if the caller forgot.
    payload = dict(entry)
    payload.setdefault("ts", now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    try:
        (
            get_db()
            .collection(COLLECTION)
            .document(project_id)
            .collection(CHANGELOG_SUBCOLLECTION)
            .document(doc_id)
            .set(payload)
        )
        return doc_id
    except Exception:  # noqa: BLE001 - best-effort write; never propagate.
        return ""


def list_changelog(project_id: str, *, since: str | None = None,
                   limit: int = 100) -> list[dict]:
    """Return changelog entries for a project, newest first.

    ``since`` (ISO8601 string) filters to entries with ``ts > since`` —
    useful for incremental polling. ``limit`` is capped at 500 to keep
    pagination payloads bounded.
    """
    limit = max(1, min(500, int(limit)))
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(CHANGELOG_SUBCOLLECTION)
    )
    query = col.order_by("ts", direction=firestore.Query.DESCENDING).limit(limit)
    if since:
        # Use string comparison since `ts` is ISO8601 and lexicographically
        # ordered. The composite query with order_by + where on different
        # fields would require a Firestore index; staying single-field.
        query = (
            col.where("ts", ">", since)
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
    out = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        data["id"] = doc.id
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Session Notes (subcollection: projects/{project_id}/notes/{note_id})
# ---------------------------------------------------------------------------

NOTES_SUBCOLLECTION = "notes"


def add_note(project_id: str, note: dict) -> str:
    """Add a session note. Returns the generated note ID."""
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(NOTES_SUBCOLLECTION)
    )
    ref = col.add(note)
    return ref[1].id


def list_notes(project_id: str) -> list[dict]:
    """List session notes ordered by timestamp."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(NOTES_SUBCOLLECTION)
        .order_by("ts")
        .stream()
    )
    return [doc.to_dict() for doc in docs]


def clear_notes(project_id: str) -> None:
    """Delete all session notes for a project."""
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(NOTES_SUBCOLLECTION)
    )
    _delete_subcollection(col)


# ---------------------------------------------------------------------------
# Bus events (subcollection: projects/{project_id}/bus_events/{auto-id})
# ms-54 / e-996: append-only event log for the agent-to-agent / session-to-
# session real-time bus. Read API is polling-friendly via ``since`` cursor;
# WS push is added on top in e-997 (separate task). Schema is intentionally
# minimal at this slice:
#   - channel: routing tag (e.g. "session-dm", "operation-due")
#   - sender_session_id: who emitted (typically from ms-57 session registry)
#   - payload: arbitrary JSON dict, event-specific
#   - created_at: server-assigned ISO8601 UTC (cursor key)
# The recipient/delivery-target fields land in e-1135 (delivery policy task).
# ---------------------------------------------------------------------------

BUS_EVENTS_SUBCOLLECTION = "bus_events"


def append_bus_event(project_id: str, data: dict) -> str:
    """Append a bus event (auto-id). Returns the generated event_id.

    The Firestore auto-id is lexically sortable but for chronological
    polling we order by ``created_at`` in :func:`list_bus_events` so the
    sender can pass any ISO8601 timestamp it last saw as the ``since``
    cursor without needing to track Firestore's id format.
    """
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
    )
    ref = col.add(data)
    return ref[1].id


# ---------------------------------------------------------------------------
# Bus cursors (subcollection: projects/{project_id}/bus_cursors/{recipient_id})
# ms-54 / e-998: per-recipient read cursor. Together with the append-only event
# log this gives "受信者ごとに既読位置が保持され、重複配信が起きない" — see ms-39 lost
# update教訓: cursors are advance-only so a stale client can never rewind another
# client's progress. recipient_id is opaque (session_id is the typical value).
# ---------------------------------------------------------------------------

BUS_CURSORS_SUBCOLLECTION = "bus_cursors"


def get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    """Return the cursor doc for ``recipient_id`` or an empty dict when unset.

    Schema (when set): ``{"last_seen_at": "<ISO8601>", "updated_at": "<ISO8601>"}``.
    Empty dict means "never seen anything" — callers should treat that as
    cursor=epoch (i.e. return all events).
    """
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_CURSORS_SUBCOLLECTION)
        .document(recipient_id)
        .get()
    )
    return doc.to_dict() or {} if doc.exists else {}


def advance_bus_cursor(project_id: str, recipient_id: str,
                       last_seen_at: str) -> dict:
    """Forward-only cursor advance. Returns the resulting cursor.

    If the request's ``last_seen_at`` is *not* strictly greater than the
    existing cursor, the existing one is preserved (no rewind, no overwrite).
    This is the structural defense against "stale client commits older
    cursor and replays events for live clients" — same lost-update class as
    ms-39's project.json overwrite bug.
    """
    import datetime
    ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_CURSORS_SUBCOLLECTION)
        .document(recipient_id)
    )
    snap = ref.get()
    existing = snap.to_dict() if snap.exists else None
    existing_seen = (existing or {}).get("last_seen_at", "")
    if existing_seen and last_seen_at <= existing_seen:
        # Idempotent no-op; return what's already stored.
        return dict(existing or {})
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    data = {"last_seen_at": last_seen_at, "updated_at": now}
    ref.set(data, merge=True)
    return data


def list_bus_events(project_id: str, since: str = "", channel: str = "",
                    limit: int = 100) -> list[dict]:
    """List bus events ordered by created_at.

    ``since``: ISO8601 timestamp; only events with ``created_at > since`` are
    returned. Empty string returns from the beginning.
    ``channel``: optional equality filter for routing.
    ``limit``: cap to keep wire payload bounded. Default 100 — callers that
    fall behind will iterate by advancing ``since`` to the last seen
    ``created_at`` until they catch up.
    """
    q = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
        .order_by("created_at")
    )
    if since:
        q = q.where("created_at", ">", since)
    if channel:
        q = q.where("channel", "==", channel)
    if limit:
        q = q.limit(limit)
    return [{"event_id": doc.id, **doc.to_dict()} for doc in q.stream()]


# ---------------------------------------------------------------------------
# Session registry (subcollection: projects/{project_id}/sessions/{session_id})
# ms-57 / e-1063: cloud-visible per-session state, used by Web UI for "who is
# active right now" and by session-start for cross-machine rescue lookups.
# Each session document is keyed by session_id (not auto-id) so CLI clients
# can upsert idempotently from any machine.
# ---------------------------------------------------------------------------

SESSIONS_SUBCOLLECTION = "sessions"


def upsert_session(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session document by session_id (merge=True).

    merge=True is critical so that a heartbeat-only update (just last_active)
    does not wipe other fields written by a previous create call.
    """
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSIONS_SUBCOLLECTION)
        .document(session_id)
        .set(data, merge=True)
    )


def list_sessions(project_id: str) -> list[dict]:
    """List all session documents for a project, ordered by last_active desc.

    Used by session-start rescue, by the Web UI "active sessions" view, and
    by ms-54 / e-1134 directory query. The Firestore doc ID **is** the
    session_id, so we merge ``doc.id`` into each returned dict — otherwise
    the directory picker has no way to address a chosen row in
    ``beacon bus send --sender <session_id>``. Existing keys win, so this
    is purely additive for callers that already had session_id from another
    source.
    """
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSIONS_SUBCOLLECTION)
        .order_by("last_active", direction=firestore.Query.DESCENDING)
        .stream()
    )
    # doc.id is the authoritative session_id; put it last so it wins over
    # any stray same-named field that might appear in older docs.
    return [{**doc.to_dict(), "session_id": doc.id} for doc in docs]


# ---------------------------------------------------------------------------
# Session log (subcollection: projects/{project_id}/session_logs/{session_id})
# ms-57 / e-1037: per-session aggregated entry. summary is the durable
# content (decision trail surviving past entry GC); *_ids are best-effort
# back-references that work only while the linked entries remain. Doc id
# == session_id so session-end and rescue (e-1038/1039) can upsert
# idempotently without an ID lookup step.
# ---------------------------------------------------------------------------

SESSION_LOGS_SUBCOLLECTION = "session_logs"


def upsert_session_log(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session log entry by session_id.

    Uses merge=True so a rescue-path partial upsert (just summary + recovered
    flag) does not wipe note_ids / commit_ids / pr_ids written by a richer
    session-end aggregation that happened earlier — important when both
    paths race on the same session.
    """
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSION_LOGS_SUBCOLLECTION)
        .document(session_id)
        .set(data, merge=True)
    )


def list_session_logs(project_id: str, limit: int | None = None) -> list[dict]:
    """List session log entries ordered by last_aggregated_at desc.

    session-start reads the top N (typically 3 per SPEC §10) so the most
    recent prior sessions surface first. ``limit`` is applied server-side
    to keep the wire payload small.
    """
    q = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSION_LOGS_SUBCOLLECTION)
        .order_by("last_aggregated_at", direction=firestore.Query.DESCENDING)
    )
    if limit:
        q = q.limit(limit)
    return [doc.to_dict() for doc in q.stream()]


def get_session_log(project_id: str, session_id: str) -> dict | None:
    """Fetch a single session log entry, or None if absent."""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSION_LOGS_SUBCOLLECTION)
        .document(session_id)
        .get()
    )
    return doc.to_dict() if doc.exists else None


def save_retro(project_id: str, week: str, content: str) -> None:
    """Save a retro document."""
    import datetime

    get_db().collection(COLLECTION).document(project_id).collection(
        RETRO_SUBCOLLECTION
    ).document(week).set(
        {
            "week": week,
            "content": content,
            "updated_at": datetime.datetime.now().isoformat(),
        }
    )
