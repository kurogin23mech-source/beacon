"""Beacon Store - Storage abstraction layer.

Provides a protocol for project data storage and a factory function
to select the appropriate backend (local JSON or cloud API).
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

    def start_watching(self) -> None:
        """Start receiving push notifications for changes.

        Cloud stores use WebSocket; local stores may no-op.
        """
        ...

    def stop_watching(self) -> None:
        """Stop receiving push notifications."""
        ...

    # ms-84 Phase 1 — fine-grained reads.
    # The legacy ``load_project()`` returns the whole project document and
    # the dashboard and CLI both pivot on it. Fine-grained reads let CLI
    # branches that currently call ``client.get(...)`` or scan ``data`` go
    # through the Store, which is what ms-84 Phase 2 then exploits to
    # collapse the 27+ ``_is_cloud_mode()`` branches into a single Store
    # call site.

    def get_milestone(self, ms_id: str) -> dict:
        """Fetch a single milestone (with task counts + entries).

        The returned dict carries ``total_tasks`` / ``done_tasks`` and a
        JSON-serialised ``entries`` list, matching the cloud
        ``GET /milestones/{ms_id}`` shape. Raises ``ValueError`` when the
        milestone is unknown so callers can show a CLI-friendly error
        without distinguishing local vs cloud.
        """
        ...


def get_store(project_file: str | None = None) -> Store:
    """Return the appropriate Store instance.

    If .beacon/cloud.json exists alongside the project file,
    returns a StoreApi (cloud API). Otherwise returns LocalStore.
    """
    if project_file is None:
        project_file = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")

    # Check for cloud config alongside the project file
    beacon_dir = os.path.dirname(project_file) or ".beacon"
    cloud_config = os.path.join(beacon_dir, "cloud.json")

    # e-1861 (ms-61): cloud.json existence is the sole source of truth.
    # The legacy ``config.json["mode"] == "cloud"`` dual-check was retired
    # because a sub-agent rewriting config.json to ``{"mode": "local"}``
    # would silently flip every subsequent CLI call back to the stale
    # LocalStore branch, producing apparent user data loss (2026-06-15
    # incident). BEACON_CLOUD=1 still forces cloud for test harnesses.
    cloud_mode = (
        os.environ.get("BEACON_CLOUD") == "1"
        or os.path.exists(cloud_config)
    )

    if cloud_mode and os.path.exists(cloud_config):
        import json
        with open(cloud_config, "r", encoding="utf-8") as f:
            cloud_data = json.load(f)
        project_id = cloud_data.get("project_id")
        if not project_id:
            raise ValueError("cloud.json must contain 'project_id'")
        # ms-64 / e-1458: route api_url through the profile resolver so the
        # env > cwd cloud.json > profile.json > default precedence chain is
        # the single source of truth. Falls back to the bare cloud.json read
        # if profile.py is unimportable for any reason.
        try:
            import profile as _profile  # type: ignore[import-not-found]
            api_url = _profile.resolve_active_profile().api_url
        except Exception:
            api_url = cloud_data.get("api_url") or "https://beacon-ai.dev"
        from store_api import StoreApi

        def _token_provider():
            from auth import load_credentials
            creds = load_credentials()
            if not creds:
                return ""
            # Web auth mode: creds is a dict with id_token
            if isinstance(creds, dict):
                return creds.get("token", "")
            return (creds.id_token or creds.token) if creds else ""

        return StoreApi(api_url, project_id, _token_provider)

    from store_local import LocalStore
    return LocalStore(project_file)
