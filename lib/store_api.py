"""Beacon StoreApi - API-backed project storage.

Implements the Store protocol by communicating with the Beacon API server.
Used for cloud mode instead of direct Firestore access.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time


class StoreApi:
    """Store implementation backed by Beacon API.

    Supports WebSocket-based change notification for the dashboard.
    Call start_watching() to receive push updates instead of polling.
    """

    # HTTP polling interval when WebSocket is unavailable (seconds)
    _POLL_INTERVAL = 5.0
    # WebSocket reconnect delays (seconds): 1, 2, 4, 8, 16, 30, 30, ...
    _WS_RECONNECT_BASE = 1.0
    _WS_RECONNECT_MAX = 30.0

    def __init__(self, api_url: str, project_id: str, token: str = ""):
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

    def load_project(self) -> dict:
        # If we have fresh data from WebSocket, use it
        with self._ws_lock:
            if self._ws_data is not None:
                data = self._ws_data
                self._ws_data = None
                self._last_hash = self._hash(data)
                return data
        data = self._client.get_project(self._project_id)
        self._last_hash = self._hash(data)
        return data

    def save_project(self, data: dict) -> None:
        self._client.put_project(self._project_id, data)
        self._last_hash = self._hash(data)

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

    @staticmethod
    def _hash(data: dict) -> str:
        return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()
