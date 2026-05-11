"""Beacon Store - Storage abstraction layer.

Provides a protocol for project data storage and a factory function
to select the appropriate backend (local JSON or Firestore).
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """Protocol for beacon project data storage."""

    def load_project(self) -> dict:
        """Load the full project data."""
        ...

    def save_project(self, data: dict) -> None:
        """Save the full project data."""
        ...

    def has_changed(self) -> bool:
        """Check if project data has changed since last load.

        Used by the dashboard for refresh detection.
        """
        ...

    def is_cloud(self) -> bool:
        """Return True if this store is cloud-backed."""
        ...


def get_store(project_file: str | None = None) -> Store:
    """Return the appropriate Store instance.

    If .beacon/cloud.json exists alongside the project file,
    returns a FirestoreStore (future). Otherwise returns LocalStore.
    """
    if project_file is None:
        project_file = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")

    # Check for cloud config alongside the project file
    beacon_dir = os.path.dirname(project_file) or ".beacon"
    cloud_config = os.path.join(beacon_dir, "cloud.json")

    # Check mode from config.json or BEACON_CLOUD env var
    config_path = os.path.join(beacon_dir, "config.json")
    cloud_mode = os.environ.get("BEACON_CLOUD") == "1"
    if not cloud_mode and os.path.exists(config_path):
        import json as _json
        with open(config_path, "r", encoding="utf-8") as f:
            config = _json.load(f)
        cloud_mode = config.get("mode") == "cloud"

    if cloud_mode and os.path.exists(cloud_config):
        import json
        with open(cloud_config, "r", encoding="utf-8") as f:
            cloud_data = json.load(f)
        project_id = cloud_data.get("project_id")
        if not project_id:
            raise ValueError("cloud.json must contain 'project_id'")
        from store_firestore import FirestoreStore
        return FirestoreStore(project_id)

    from store_local import LocalStore
    return LocalStore(project_file)
