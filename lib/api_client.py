"""Beacon API Client - HTTP client for cloud API access.

Used by CLI cloud commands to communicate with the Beacon API server
instead of directly accessing Firestore.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error


class ApiClient:
    """Simple HTTP client for Beacon API with auth token support."""

    def __init__(self, base_url: str, token=""):
        self._base_url = base_url.rstrip("/")
        # Accept either a static token string or a callable that returns one
        self._token = token

    def _get_token(self) -> str:
        if callable(self._token):
            return self._token()
        return self._token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        token = self._get_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(error_body).get("detail", error_body)
            except (json.JSONDecodeError, AttributeError):
                detail = error_body
            raise RuntimeError(f"API error {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Cannot connect to API ({self._base_url}): {e.reason}"
            ) from e
        except OSError as e:
            raise ConnectionError(
                f"Network error ({self._base_url}): {e}"
            ) from e

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, body)

    def put(self, path: str, body: dict) -> dict:
        return self._request("PUT", path, body)

    def patch(self, path: str, body: dict) -> dict:
        return self._request("PATCH", path, body)

    def delete(self, path: str, body: dict | None = None) -> dict:
        return self._request("DELETE", path, body)

    # Convenience methods for beacon operations

    def list_projects(self) -> list:
        return self.get("/api/projects")

    def get_project(self, project_id: str) -> dict:
        return self.get(f"/api/projects/{project_id}")

    def put_project(self, project_id: str, data: dict) -> dict:
        return self.put(f"/api/projects/{project_id}", data)

    def create_project(self, project_id: str, name: str, objective: str = "") -> dict:
        return self.post(f"/api/projects/{project_id}",
                         {"name": name, "objective": objective})

    # Document operations

    def list_documents(self, project_id: str) -> list:
        return self.get(f"/api/projects/{project_id}/documents")

    def get_document(self, project_id: str, doc_id: str) -> dict:
        return self.get(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}")

    def create_document(self, project_id: str, title: str, content: str,
                        scope: str | None = None) -> dict:
        body = {"title": title, "content": content}
        if scope:
            body["scope"] = scope
        return self.post(f"/api/projects/{project_id}/documents", body)

    def update_document(self, project_id: str, doc_id: str, title: str, content: str,
                        scope: str | None = None) -> dict:
        body = {"title": title, "content": content}
        if scope:
            body["scope"] = scope
        return self.put(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}", body)

    def put_document(self, project_id: str, doc_id: str, title: str, content: str,
                     scope: str | None = None) -> dict:
        """Create or update a document by ID (upsert)."""
        body = {"title": title, "content": content}
        if scope:
            body["scope"] = scope
        return self.put(f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}", body)

    def delete_document(self, project_id: str, doc_id: str, reason: str = "") -> dict:
        body = {"reason": reason} if reason else None
        return self.delete(
            f"/api/projects/{project_id}/documents/{urllib.parse.quote(doc_id, safe='')}",
            body,
        )

    # Purge operations (owner-only, hard-delete for duplicate-ID recovery — e-1030)

    def purge_milestone(self, project_id: str, ms_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        body: dict = {"reason": reason}
        if index is not None:
            body["index"] = index
        return self.post(
            f"/api/projects/{project_id}/milestones/"
            f"{urllib.parse.quote(ms_id, safe='')}/purge",
            body,
        )

    def purge_entry(self, project_id: str, entry_id: str, *,
                    reason: str, index: int | None = None) -> dict:
        body: dict = {"reason": reason}
        if index is not None:
            body["index"] = index
        return self.post(
            f"/api/projects/{project_id}/entries/"
            f"{urllib.parse.quote(entry_id, safe='')}/purge",
            body,
        )

    def purge_operation(self, project_id: str, op_id: str, *,
                        reason: str, index: int | None = None) -> dict:
        body: dict = {"reason": reason}
        if index is not None:
            body["index"] = index
        return self.post(
            f"/api/projects/{project_id}/operations/"
            f"{urllib.parse.quote(op_id, safe='')}/purge",
            body,
        )

    # Retro operations

    def save_retro(self, project_id: str, week: str, content: str) -> dict:
        return self.post(f"/api/projects/{project_id}/retros/{week}",
                         {"content": content})

    # Session Note operations

    def add_note(self, project_id: str, note: dict) -> dict:
        return self.post(f"/api/projects/{project_id}/notes", note)

    def list_notes(self, project_id: str) -> list:
        return self.get(f"/api/projects/{project_id}/notes")

    def clear_notes(self, project_id: str) -> dict:
        return self.delete(f"/api/projects/{project_id}/notes")

    # Session registry operations (ms-57 / e-1063)

    def upsert_session(self, project_id: str, session_id: str, data: dict) -> dict:
        """Upsert a session document by session_id (server uses merge=True)."""
        return self.put(
            f"/api/projects/{project_id}/sessions/{urllib.parse.quote(session_id, safe='')}",
            data,
        )

    def list_sessions(self, project_id: str) -> list:
        return self.get(f"/api/projects/{project_id}/sessions")

    # Session log operations (ms-57 / e-1037)

    def upsert_session_log(self, project_id: str, session_id: str, data: dict) -> dict:
        return self.put(
            f"/api/projects/{project_id}/session_logs/{urllib.parse.quote(session_id, safe='')}",
            data,
        )

    def get_session_log(self, project_id: str, session_id: str) -> dict:
        return self.get(
            f"/api/projects/{project_id}/session_logs/{urllib.parse.quote(session_id, safe='')}"
        )

    def list_session_logs(self, project_id: str, limit: int = 0) -> list:
        suffix = f"?limit={limit}" if limit else ""
        return self.get(f"/api/projects/{project_id}/session_logs{suffix}")

    # Bus events (ms-54 / e-996)

    def post_bus_event(self, project_id: str, channel: str, *,
                       sender_session_id: str = "", payload: dict | None = None) -> dict:
        return self.post(f"/api/projects/{project_id}/bus", {
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
        })

    def list_bus_events(self, project_id: str, *, since: str = "",
                        channel: str = "", limit: int = 100) -> list:
        qs = []
        if since:
            qs.append(f"since={urllib.parse.quote(since)}")
        if channel:
            qs.append(f"channel={urllib.parse.quote(channel)}")
        if limit:
            qs.append(f"limit={limit}")
        suffix = "?" + "&".join(qs) if qs else ""
        return self.get(f"/api/projects/{project_id}/bus{suffix}")

    # Bus cursors (ms-54 / e-998) — per-recipient at-least-once delivery.

    def list_unread_bus_events(self, project_id: str, recipient_id: str, *,
                               channel: str = "", limit: int = 100) -> list:
        qs = [f"recipient_id={urllib.parse.quote(recipient_id)}"]
        if channel:
            qs.append(f"channel={urllib.parse.quote(channel)}")
        if limit:
            qs.append(f"limit={limit}")
        return self.get(f"/api/projects/{project_id}/bus/unread?" + "&".join(qs))

    def advance_bus_cursor(self, project_id: str, recipient_id: str,
                           last_seen_at: str) -> dict:
        return self.post(
            f"/api/projects/{project_id}/bus/cursors/{recipient_id}",
            {"last_seen_at": last_seen_at},
        )

    def get_bus_cursor(self, project_id: str, recipient_id: str) -> dict:
        return self.get(f"/api/projects/{project_id}/bus/cursors/{recipient_id}")
