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


def list_projects(user_id: str | None = None) -> list[dict]:
    """List projects. If user_id is given, only return projects owned by or shared with that user."""
    query = get_db().collection(COLLECTION)
    docs = query.stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
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
    for subcol_name in ("documents", "retros", "members", "triggers"):
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


def list_documents(project_id: str) -> list[dict]:
    """List all documents for a project."""
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
                  scope: str | None = None) -> str:
    """Save a document. If doc_id is empty, auto-generate one."""
    import datetime

    # Scope priority: explicit param > frontmatter > default
    resolved_scope = scope if scope in ("core", "spec", "memo") else _extract_scope(content)
    milestone = _extract_frontmatter_field(content, "milestone")

    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
    )
    data = {
        "title": title,
        "content": content,
        "scope": resolved_scope,
        "updated_at": datetime.datetime.now().isoformat(),
    }
    if milestone:
        data["milestone"] = milestone
    if doc_id:
        col.document(doc_id).set(data)
        return doc_id
    else:
        ref = col.add(data)
        return ref[1].id


def delete_document(project_id: str, doc_id: str) -> bool:
    """Delete a document. Returns True if existed."""
    doc_ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .document(doc_id)
    )
    doc = doc_ref.get()
    if not doc.exists:
        return False
    doc_ref.delete()
    return True


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
