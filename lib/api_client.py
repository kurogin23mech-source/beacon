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
