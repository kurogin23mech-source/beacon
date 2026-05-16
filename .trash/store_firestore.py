"""Beacon FirestoreStore - Cloud-backed project storage.

Stores the entire project.json as a single Firestore document.
Collection: projects/{project_id}
"""

from __future__ import annotations

import json
from datetime import datetime


class FirestoreStore:
    """Store implementation backed by Google Cloud Firestore."""

    def __init__(self, project_id: str):
        self._project_id = project_id
        self._last_update_time: datetime | None = None
        self._db = None

    def _get_db(self):
        if self._db is None:
            from auth import load_credentials
            from google.cloud import firestore

            creds = load_credentials()
            if creds is None:
                raise RuntimeError(
                    "Not logged in. Run: beacon auth login"
                )
            self._db = firestore.Client(project="beacon-cloud-96f5f", credentials=creds)
        return self._db

    def _doc_ref(self):
        return self._get_db().collection("projects").document(self._project_id)

    def load_project(self) -> dict:
        doc = self._doc_ref().get()
        if not doc.exists:
            raise FileNotFoundError(
                f"Project '{self._project_id}' not found in Firestore."
            )
        self._last_update_time = doc.update_time
        return doc.to_dict()

    def save_project(self, data: dict) -> None:
        self._doc_ref().set(data)
        # Re-fetch to get the new update_time
        doc = self._doc_ref().get()
        self._last_update_time = doc.update_time

    def has_changed(self) -> bool:
        """Check if the Firestore document has changed since last load/save."""
        doc = self._doc_ref().get()
        if not doc.exists:
            return self._last_update_time is not None
        if self._last_update_time is None:
            self._last_update_time = doc.update_time
            return True
        if doc.update_time != self._last_update_time:
            self._last_update_time = doc.update_time
            return True
        return False

    def is_cloud(self) -> bool:
        return True
