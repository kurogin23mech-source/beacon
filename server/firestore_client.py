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


def list_projects() -> list[dict]:
    """List all projects (id + name only)."""
    docs = get_db().collection(COLLECTION).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        result.append({
            "project_id": doc.id,
            "name": data.get("name", ""),
            "objective": data.get("objective", ""),
        })
    return result


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
