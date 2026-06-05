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
    for subcol_name in ("documents", "retros", "members", "triggers", "notes"):
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


def list_documents(project_id: str, include_trashed: bool = False) -> list[dict]:
    """List all documents for a project.

    By default soft-deleted docs are filtered out. ``include_trashed=True``
    keeps them in the list with a ``trashed: true`` marker so the Web UI
    Trash tab can render them without a second roundtrip (ms-14 e-991).
    """
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
        is_trashed = bool(data.get("deleted"))
        if is_trashed and not include_trashed:
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
        if is_trashed:
            entry["trashed"] = True
            entry["trashed_at"] = data.get("deleted_at", "")
            entry["trashed_by"] = data.get("deleted_by", "")
            if data.get("trash_reason"):
                entry["trash_reason"] = data["trash_reason"]
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


def restore_document(project_id: str, doc_id: str, restored_by: str = "unknown",
                     reason: str = "") -> bool:
    """Reverse a soft-delete (ms-14 e-991).

    Clears ``deleted`` + trash audit fields and stamps ``restored_at`` /
    ``restored_by``. Returns ``False`` if the doc doesn't exist or isn't
    currently trashed (no silent no-op).
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
    data = doc.to_dict() or {}
    if not data.get("deleted"):
        return False
    update = {
        "deleted": firestore.DELETE_FIELD,
        "deleted_at": firestore.DELETE_FIELD,
        "deleted_by": firestore.DELETE_FIELD,
        "trash_reason": firestore.DELETE_FIELD,
        "restored_at": datetime.datetime.now().isoformat(),
        "restored_by": restored_by,
    }
    if reason:
        update["restore_reason"] = reason
    doc_ref.update(update)
    return True


def list_trashed_documents(project_id: str, days: int | None = 30) -> list[dict]:
    """Return soft-deleted docs within the last ``days`` window (ms-14 e-991).

    ``days=None`` returns the full set (no time filter). Items are
    ordered by ``deleted_at`` descending so the most recent trash shows
    first. ``days_ago`` is computed for convenience in the Web UI.
    """
    import datetime
    cutoff_iso = None
    if days is not None:
        cutoff_iso = (datetime.datetime.now()
                      - datetime.timedelta(days=days)).isoformat()
    now = datetime.datetime.now()
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .where("deleted", "==", True)
        .stream()
    )
    out = []
    for doc in docs:
        data = doc.to_dict() or {}
        deleted_at = data.get("deleted_at", "")
        if cutoff_iso and deleted_at and deleted_at < cutoff_iso:
            continue
        days_ago = None
        if deleted_at:
            try:
                # deleted_at is local-tz iso (no Z suffix); reuse it as-is.
                dt = datetime.datetime.fromisoformat(deleted_at)
                days_ago = max(0, (now - dt).days)
            except (ValueError, TypeError):
                days_ago = None
        milestone = data.get("milestone") or _extract_frontmatter_field(
            data.get("content", ""), "milestone"
        )
        item = {
            "doc_id": doc.id,
            "title": data.get("title", "") or doc.id,
            "scope": data.get("scope", "memo"),
            "trashed_at": deleted_at or None,
            "trashed_by": data.get("deleted_by", ""),
            "trash_reason": data.get("trash_reason", ""),
            "days_ago": days_ago,
        }
        if milestone:
            item["milestone"] = milestone
        out.append(item)
    out.sort(key=lambda x: x["trashed_at"] or "", reverse=True)
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
