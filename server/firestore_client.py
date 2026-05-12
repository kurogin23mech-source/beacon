"""Firestore client wrapper for Beacon API."""

from __future__ import annotations

from google.cloud import firestore

_db: firestore.Client | None = None

PROJECT_ID = "beacon-cloud-96f5f"
COLLECTION = "projects"


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

USERS_COLLECTION = "users"


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
        "created_at": datetime.datetime.now().isoformat(),
    }
    doc_ref.set(user_data)
    return user_data


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
