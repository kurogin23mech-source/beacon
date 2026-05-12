"""Beacon StoreApi - API-backed project storage.

Implements the Store protocol by communicating with the Beacon API server.
Used for cloud mode instead of direct Firestore access.
"""

from __future__ import annotations

import hashlib
import json


class StoreApi:
    """Store implementation backed by Beacon API."""

    def __init__(self, api_url: str, project_id: str, token: str = ""):
        from api_client import ApiClient
        self._client = ApiClient(api_url, token)
        self._project_id = project_id
        self._last_hash: str | None = None

    def load_project(self) -> dict:
        data = self._client.get_project(self._project_id)
        self._last_hash = self._hash(data)
        return data

    def save_project(self, data: dict) -> None:
        self._client.put_project(self._project_id, data)
        self._last_hash = self._hash(data)

    def has_changed(self) -> bool:
        """Check if the project has changed since last load/save."""
        try:
            data = self._client.get_project(self._project_id)
        except (RuntimeError, ConnectionError):
            return False
        current_hash = self._hash(data)
        if self._last_hash is None:
            self._last_hash = current_hash
            return True
        if current_hash != self._last_hash:
            self._last_hash = current_hash
            return True
        return False

    def is_cloud(self) -> bool:
        return True

    @staticmethod
    def _hash(data: dict) -> str:
        return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()
