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

    # ms-84 Phase 2 — fine-grained mutation (purge family).
    # The cmd_milestone_purge cloud branch currently delegates to a
    # dedicated _cloud_milestone_purge helper; folding it into Store
    # lets the CLI drop the _is_cloud_mode branch entirely (= 受入条件 10
    # で要請される直接呼びの削減)。

    def purge_entry(self, entry_id: str, *,
                    reason: str, index: int | None = None) -> dict:
        """Hard-delete an entry record (= タスク / コミット / ノート等の物理削除)。

        Same contract as purge_milestone — returns ``{purged, still_dirty,
        dup_report}`` and translates HTTP / value errors uniformly so the
        CLI does not have to branch on backend.
        """
        ...

    def purge_operation(self, op_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Hard-delete an operation record (= 運用ジョブの物理削除)。

        Same contract as purge_milestone / purge_entry.
        """
        ...

    def upsert_session_log(self, session_id: str, body: dict) -> bool:
        """Upsert a session log document by session_id (= セッション集約の保存)。

        Returns True on successful persistence, False on failure or no-op.
        LocalStore returns False (= the cloud session log subcollection is
        a cloud-only artifact; local sessions persist via _write_local_session_log
        on the caller side). StoreApi calls the upsert endpoint with the
        same failure-swallowing contract as the legacy
        ``_push_session_log_to_cloud`` (= network / auth / 4xx all → False),
        so the caller does not need to branch on backend.
        """
        ...

    def list_session_ids(self) -> list[str]:
        """List session_ids visible to the backend (= cloud registry の列挙)。

        Used by the session rescue path to discover other sessions whose
        entries the local cache may have missed (= cross-machine orphans).
        LocalStore returns ``[]`` because there is no remote registry to
        consult; StoreApi calls the API and swallows transport failures,
        same best-effort contract as ``upsert_session_log``. The caller
        merges this result with locally-known ids without checking backend.
        """
        ...

    def get_session_log(self, session_id: str) -> dict | None:
        """Fetch a persisted session log document by session_id, or None
        if the backend has no record (= 404 / local 不在 / transport 失敗)。

        Used by the session aggregation path so the merge-with-remote step
        is uniform regardless of backend. LocalStore returns None
        (= session_log subcollection is cloud-only); StoreApi calls the
        API and translates 404 / transport failures to None so the caller
        can just check truthiness.
        """
        ...

    def list_session_logs(self, limit: int = 0) -> list[dict]:
        """List persisted session log documents (= most-recent-first)。

        LocalStore returns ``[]`` because session log persistence in local
        mode is per-file on disk and the caller handles directory listing
        directly (cmd_session_log_list keeps this fallback). StoreApi calls
        the API and returns the rows, swallowing transport failures.
        """
        ...

    def purge_milestone(self, ms_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Hard-delete a milestone record (= 物理削除、duplicate-ID 回復用、Issue #14)。

        Returns a dict shaped::

            {
                "purged": {...the removed milestone fields...},
                "still_dirty": bool,    # True iff residual duplicates remain (local only)
                "dup_report": dict,     # find_duplicate_ids output (local only; {} in cloud)
            }

        Cloud-backed implementations return ``still_dirty=False`` + empty
        ``dup_report`` because the server enforces single-record purge per
        request and re-validates the project document afterwards. Raises
        ``ValueError`` on invalid input (missing reason, unknown id,
        out-of-range index, etc.) so the CLI can branch uniformly.
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
