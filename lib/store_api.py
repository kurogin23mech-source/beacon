"""Beacon StoreApi - API-backed project storage.

Implements the Store protocol by communicating with the Beacon API server.
Used for cloud mode instead of direct Firestore access.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time


class ConflictError(RuntimeError):
    """Raised when the cloud project changed between load and save.

    The CLI mutates the whole project document and PUTs it back wholesale
    (PUT /api/projects/{id}). If another writer (another CLI, the Web UI)
    changed the cloud document after we loaded it, a blind PUT would silently
    overwrite that change (lost update). save_project() raises this instead so
    the caller can abort and re-run against fresh state. (e-841)
    """


class StoreApi:
    """Store implementation backed by Beacon API.

    Supports WebSocket-based change notification for the dashboard.
    Call start_watching() to receive push updates instead of polling.
    """

    # Per-project hash captured at load time, keyed by project_id. Used by
    # save_project() to detect concurrent cloud changes within a single CLI
    # invocation (load -> mutate -> save). Class-level so it survives across the
    # separate get_store() instances that load_project() and save_project() use.
    _load_baseline: dict = {}

    # HTTP polling interval when WebSocket is unavailable (seconds)
    _POLL_INTERVAL = 5.0
    # WebSocket reconnect delays (seconds): 1, 2, 4, 8, 16, 30, 30, ...
    _WS_RECONNECT_BASE = 1.0
    _WS_RECONNECT_MAX = 30.0

    def __init__(self, api_url: str, project_id: str, token: str = "",
                 local_cache_path: str | None = None):
        from api_client import ApiClient
        self._client = ApiClient(api_url, token)
        self._api_url = api_url
        self._project_id = project_id
        self._token = token
        self._last_hash: str | None = None
        # WebSocket push state
        self._ws_client = None
        self._ws_changed = False
        self._ws_data: dict | None = None
        self._ws_lock = threading.Lock()
        # Watching lifecycle
        self._watching = False  # True while start_watching() is active
        self._ws_stop = threading.Event()
        self._ws_reconnect_attempts = 0
        # HTTP polling throttle
        self._last_poll_time = 0.0
        # ms-95 / e-746: write-back cache path. When set, every successful
        # API read / write also writes the canonical document to this local
        # file, keeping ``.beacon/project.json`` as an up-to-date read-only
        # mirror of the cloud truth. This addresses the original ms-36
        # "cloud mode はローカル .beacon を読み取り専用キャッシュ" contract
        # that ms-84 Phase 3 (e-2037) deferred. Tauri's ``load_project_json``
        # still reads this file at startup; without write-back the first
        # render shows stale data until the WS push arrives (= e-723 era).
        # The write is best-effort and never raises — any I/O failure is
        # swallowed so a flaky disk cannot break the CLI command itself.
        self._local_cache_path = local_cache_path

    def load_project(self) -> dict:
        # If we have fresh data from WebSocket, use it
        with self._ws_lock:
            if self._ws_data is not None:
                data = self._ws_data
                self._ws_data = None
                self._last_hash = self._hash(data)
                StoreApi._load_baseline[self._project_id] = self._last_hash
                self._write_local_cache(data)
                return data
        data = self._client.get_project(self._project_id)
        self._last_hash = self._hash(data)
        StoreApi._load_baseline[self._project_id] = self._last_hash
        self._write_local_cache(data)
        return data

    def save_project(self, data: dict) -> None:
        # Lost-update guard (e-841): if the cloud document changed since we
        # loaded it, a blind whole-document PUT would clobber the concurrent
        # change. Detect and abort so the caller re-runs against fresh state.
        baseline = StoreApi._load_baseline.get(self._project_id)
        if baseline is not None:
            try:
                current = self._client.get_project(self._project_id)
            except (RuntimeError, ConnectionError):
                current = None  # cannot verify (offline/error) — fall through
            if current is not None and self._hash(current) != baseline:
                raise ConflictError(
                    "Cloud project changed since it was read — aborting to "
                    "avoid overwriting another writer's changes. "
                    "Re-run the command."
                )
        self._client.put_project(self._project_id, data)
        self._last_hash = self._hash(data)
        StoreApi._load_baseline[self._project_id] = self._last_hash
        self._write_local_cache(data)

    def _write_local_cache(self, data: dict) -> None:
        """Mirror the just-loaded/saved cloud document into the local cache.

        ms-95 / e-746 — Cloud mode previously left ``.beacon/project.json``
        as a stale snapshot from initial cloud-join (or a pre-cut-over local
        install), causing Tauri's ``load_project_json`` to render a
        ms-22-era world for several seconds until the WS push arrived.
        Writing back here keeps the local file as a read-only mirror of the
        cloud truth, refreshed on every CLI invocation.

        The write is intentionally best-effort: any OS / filesystem error
        is swallowed so a non-writable disk cannot break the CLI command
        itself. Direction is cloud→local only (= we never PUT from this
        file back to cloud), which preserves the ms-84 Phase 3 (e-2037)
        invariant that the cloud document is the single source of truth
        for writes.
        """
        # ``getattr`` default protects test fixtures that build StoreApi via
        # ``__new__`` + manual attribute seeding (= avoids ApiClient / auth /
        # WS construction in unit tests; see tests/test_cmd_purge_cloud.py).
        # Those fixtures may not seed ``_local_cache_path``; we treat that
        # the same as the explicit ``None`` case (= no cache write).
        cache_path = getattr(self, "_local_cache_path", None)
        if not cache_path:
            return
        try:
            cache_dir = os.path.dirname(cache_path)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            # Write atomically via a temp file in the same directory so a
            # partial write cannot leave a truncated project.json on disk.
            tmp_path = f"{cache_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp_path, cache_path)
        except OSError:
            # Best-effort cache; never block the CLI on a disk hiccup.
            return

    def has_changed(self) -> bool:
        """Check if the project has changed since last load/save.

        When watching via WebSocket, this just checks a flag (no HTTP).
        When not watching, falls back to throttled HTTP polling.
        """
        with self._ws_lock:
            if self._ws_client is not None:
                if self._ws_changed:
                    self._ws_changed = False
                    return True
                return False

        # Fallback: throttled HTTP polling (only used when not watching)
        now = time.monotonic()
        if now - self._last_poll_time < self._POLL_INTERVAL:
            return False
        self._last_poll_time = now

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

    def start_watching(self) -> None:
        """Start receiving push updates via WebSocket."""
        if self._watching:
            return
        self._watching = True
        self._ws_stop.clear()
        self._ws_reconnect_attempts = 0
        self._connect_ws()

    def _connect_ws(self) -> None:
        """Establish a new WebSocket connection."""
        if self._ws_stop.is_set():
            return

        from ws_client import WebSocketClient

        # Build WebSocket URL from API URL
        ws_scheme = "wss" if self._api_url.startswith("https") else "ws"
        http_part = self._api_url.split("://", 1)[1] if "://" in self._api_url else self._api_url
        base = http_part.rstrip("/")
        token = self._token() if callable(self._token) else self._token
        token_param = f"?token={token}" if token else ""
        ws_url = f"{ws_scheme}://{base}/ws/projects/{self._project_id}{token_param}"

        def on_message(text: str):
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                return
            if msg.get("type") == "project":
                data = msg.get("data", {})
                with self._ws_lock:
                    self._ws_data = data
                    self._ws_changed = True
                    # Reset reconnect counter on successful message
                    self._ws_reconnect_attempts = 0

        def on_error(e: Exception):
            with self._ws_lock:
                self._ws_client = None
            # Schedule reconnect in background
            if self._watching and not self._ws_stop.is_set():
                self._schedule_reconnect()

        client = WebSocketClient(ws_url, on_message=on_message, on_error=on_error)
        with self._ws_lock:
            self._ws_client = client
        client.connect()

    def _schedule_reconnect(self) -> None:
        """Schedule a WebSocket reconnection with exponential backoff."""
        delay = min(
            self._WS_RECONNECT_BASE * (2 ** self._ws_reconnect_attempts),
            self._WS_RECONNECT_MAX,
        )
        self._ws_reconnect_attempts += 1

        def reconnect():
            if not self._ws_stop.wait(delay):
                self._connect_ws()

        t = threading.Thread(target=reconnect, daemon=True)
        t.start()

    def stop_watching(self) -> None:
        """Stop WebSocket connection and auto-reconnect."""
        self._watching = False
        self._ws_stop.set()
        with self._ws_lock:
            client = self._ws_client
            self._ws_client = None
        if client is not None:
            client.close()

    def is_cloud(self) -> bool:
        return True

    # ms-84 Phase 2 — fine-grained mutation (purge family)

    def purge_entry(self, entry_id: str, *,
                    reason: str, index: int | None = None) -> dict:
        """Match LocalStore.purge_entry shape, applied via the cloud API."""
        try:
            response = self._client.purge_entry(
                self._project_id, entry_id, reason=reason, index=index,
            )
        except RuntimeError as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower():
                raise ValueError(f"Entry '{entry_id}' not found") from e
            if "403" in msg or "forbidden" in msg.lower():
                raise ValueError(
                    "Permission denied: only the project owner can "
                    "purge entries"
                ) from e
            if "400" in msg:
                raise ValueError(str(e)) from e
            raise
        return {
            "purged": response,
            "still_dirty": False,
            "dup_report": {},
        }

    def purge_operation(self, op_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Match LocalStore.purge_operation shape, applied via the cloud API."""
        try:
            response = self._client.purge_operation(
                self._project_id, op_id, reason=reason, index=index,
            )
        except RuntimeError as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower():
                raise ValueError(f"Operation '{op_id}' not found") from e
            if "403" in msg or "forbidden" in msg.lower():
                raise ValueError(
                    "Permission denied: only the project owner can "
                    "purge operations"
                ) from e
            if "400" in msg:
                raise ValueError(str(e)) from e
            raise
        return {
            "purged": response,
            "still_dirty": False,
            "dup_report": {},
        }

    def upsert_session_log(self, session_id: str, body: dict) -> bool:
        """Upsert via API. Swallows all failures and returns True / False
        so the caller can keep the legacy ``_push_session_log_to_cloud``
        contract (= cloud is best-effort; local cache is the primary
        source of truth on failure)."""
        try:
            self._client.upsert_session_log(self._project_id, session_id, body)
            return True
        except (RuntimeError, ConnectionError):
            return False

    def list_session_ids(self) -> list[str]:
        """List session_ids known to the cloud session registry. Returns
        ``[]`` on transport failure so the caller can union-merge without
        backend-branching."""
        try:
            rows = self._client.list_sessions(self._project_id) or []
        except (RuntimeError, ConnectionError):
            return []
        return [r.get("session_id") for r in rows if r.get("session_id")]

    def get_session_log(self, session_id: str) -> dict | None:
        """Fetch the persisted session log from the cloud. Returns None for
        404 (= no entry yet) or any transport failure so the caller can
        check truthiness instead of catching."""
        try:
            return self._client.get_session_log(self._project_id, session_id)
        except (RuntimeError, ConnectionError):
            return None

    def list_session_logs(self, limit: int = 0) -> list[dict]:
        """List persisted session logs from the cloud. Returns ``[]`` on
        transport failure so the caller can fall back to its local-disk
        listing without backend-branching."""
        try:
            return self._client.list_session_logs(
                self._project_id, limit=limit
            ) or []
        except (RuntimeError, ConnectionError):
            return []

    def purge_milestone(self, ms_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        """Match LocalStore.purge_milestone shape, applied via the cloud API.

        The server enforces owner-only access + post-purge validation, so
        ``still_dirty`` is always False here — duplicate-id recovery in
        cloud mode is the server's responsibility (= it does not let an
        invalid project document persist). 404 / 403 / 400 are translated
        to ``ValueError`` so cmd code can branch on a single exception
        type regardless of backend.
        """
        try:
            response = self._client.purge_milestone(
                self._project_id, ms_id, reason=reason, index=index,
            )
        except RuntimeError as e:
            msg = str(e)
            if "404" in msg or "not found" in msg.lower():
                raise ValueError(f"Milestone '{ms_id}' not found") from e
            if "403" in msg or "forbidden" in msg.lower():
                raise ValueError(
                    "Permission denied: only the project owner can "
                    "purge milestones"
                ) from e
            if "400" in msg:
                raise ValueError(str(e)) from e
            raise
        return {
            "purged": response,
            "still_dirty": False,
            "dup_report": {},
        }

    # ms-84 Phase 1 — fine-grained reads

    def get_milestone(self, ms_id: str) -> dict:
        """Match LocalStore.get_milestone shape, sourced from the cloud API.

        Maps HTTP 404 → ``ValueError`` so callers can present the same
        ``Milestone '<id>' not found`` message regardless of backend.
        """
        try:
            return self._client.get_milestone(self._project_id, ms_id)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(f"Milestone '{ms_id}' not found") from e
            raise

    # ms-84 Phase 2 — trek read passthrough.

    def list_treks(self, *, actor_id: str | None = None,
                   status: str = "", include_archived: bool = False,
                   all_actors: bool = False) -> list[dict]:
        """List trek docs via the cloud API.

        ``actor_id`` is ignored on cloud (= server filters by auth token's
        user); ``all_actors`` requires admin role server-side (non-admin
        gets 403 as RuntimeError, which propagates up to the CLI for
        display, matching legacy cloud branch behavior).
        """
        return self._client.list_treks(
            status=status or "",
            include_archived=include_archived,
            all_actors=all_actors,
        )

    def get_trek(self, trek_id: str) -> dict:
        """Match LocalStore.get_trek shape, sourced from the cloud API.

        Maps HTTP 404 → ``ValueError`` so callers can present the same
        ``trek '<id>' not found`` message regardless of backend.
        Auth / 403 / transport errors propagate as RuntimeError, matching
        the legacy cloud branch behavior (= cmd_trek_show sys.exit(1) で
        Error: <msg> を出す経路に乗せたまま)。
        """
        try:
            return self._client.get_trek(trek_id)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(f"trek '{trek_id}' not found") from e
            raise

    # ms-118 / e-4231 — Organizations (cloud API, /api/orgs)

    def create_org(self, *, name: str, creator_user_id: str = "",
                   creator_email: str = "") -> dict:
        """Create a team org via the cloud API.

        ``creator_user_id`` / ``creator_email`` are ignored on cloud (= the
        server resolves the owner from the auth token so the client can't
        spoof it); they exist for LocalStore parity。Auth / transport errors
        propagate as RuntimeError.
        """
        return self._client.create_org(name=name)

    def list_orgs(self, *, user_id: str | None = None) -> list[dict]:
        """List orgs via the cloud API.

        ``user_id`` is ignored on cloud (= server filters by the auth token's
        user). 403 / transport errors propagate as RuntimeError, matching the
        legacy cloud branch behavior.
        """
        return self._client.list_orgs()

    def get_org(self, org_id: str) -> dict:
        """Match LocalStore.get_org shape, sourced from the cloud API.

        Maps HTTP 404 → ``ValueError`` (= member でない / 存在しない org は
        存在を漏らさず not found)。Auth / transport errors propagate as
        RuntimeError.
        """
        try:
            return self._client.get_org(org_id)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(f"org '{org_id}' not found") from e
            raise

    def add_org_member(self, org_id: str, *, email: str,
                       role: str = "member") -> dict:
        """Add an org member via the cloud API (participation-only).

        Server resolves email → user, checks the caller is an org member, and
        writes only the org doc. 404 (unknown org / email) → ``ValueError``;
        auth / 403 / 409 / transport → ``RuntimeError`` (= 既存 cloud path と一致)。
        """
        try:
            return self._client.add_org_member(org_id, email=email, role=role)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(str(e)) from e
            raise

    def remove_org_member(self, org_id: str, *, target: str) -> dict:
        """Remove an org member via the cloud API (owner/admin only server-side)."""
        try:
            return self._client.remove_org_member(org_id, target=target)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(str(e)) from e
            raise

    def rehome_project(self, project_id: str, *, target_org_id: str) -> dict:
        """Re-home a project into an org via the cloud API (ms-118 / e-4233).

        Server checks the caller owns the project and is a member of the target
        org, then rewrites only the project's ``org_id`` (disclosure re-evaluates
        live). 404 (unknown project / org, or non-member of the target org) →
        ``ValueError``; auth / 403 / transport → ``RuntimeError``.
        """
        try:
            return self._client.rehome_project(
                project_id, target_org_id=target_org_id)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(str(e)) from e
            raise

    def list_documents(self) -> list:
        """List documents from cloud API."""
        try:
            return self._client.list_documents(self._project_id)
        except (RuntimeError, ConnectionError):
            return []

    def get_document(self, doc_id: str) -> dict:
        """Get a single document from cloud API."""
        try:
            return self._client.get_document(self._project_id, doc_id)
        except (RuntimeError, ConnectionError):
            return {}

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document via cloud API."""
        try:
            self._client.delete_document(self._project_id, doc_id)
            return True
        except (RuntimeError, ConnectionError):
            return False

    @staticmethod
    def _hash(data: dict) -> str:
        return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()
