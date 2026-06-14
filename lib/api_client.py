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

    # Cloud-first identity (ms-62 / e-1509)

    def me_list_projects(self) -> list:
        """List the user's project memberships with role (server-issued).

        Returns ``[{"id": ..., "name": ..., "role": ...}, ...]``. Raises
        ``RuntimeError`` on 404 (= old server without ms-62 endpoint, the
        caller should fall back to legacy discovery).
        """
        return self.get("/api/me/projects")

    def me_upsert_machine(
        self, fingerprint: str, *, hostname: str = "", agent: str = ""
    ) -> dict:
        """Get-or-mint machine_id for (user, fingerprint).

        Returns ``{"machine_id": ..., "minted": bool, "fingerprint": ...}``.
        Caller caches machine_id in ``~/.beacon/machine.json``.
        """
        body = {"fingerprint": fingerprint}
        if hostname:
            body["hostname"] = hostname
        if agent:
            body["agent"] = agent
        return self.post("/api/me/machine", body)

    def me_heartbeat(
        self,
        project_id: str,
        machine_id: str,
        parent_pid: int,
        *,
        cwd: str = "",
        branch: str = "",
        focus_milestone: str = "",
        agent: dict | None = None,
    ) -> dict:
        """Get-or-mint session_id for the identity tuple.

        Returns ``{"session_id": ..., "minted": bool, "last_heartbeat_at":
        ..., "created_at": ...}``. Tuple = (project_id, machine_id,
        parent_pid).
        """
        body: dict = {
            "project_id": project_id,
            "machine_id": machine_id,
            "parent_pid": parent_pid,
        }
        if cwd:
            body["cwd"] = cwd
        if branch:
            body["branch"] = branch
        if focus_milestone:
            body["focus_milestone"] = focus_milestone
        if agent:
            body["agent"] = agent
        return self.post("/api/me/heartbeat", body)

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

    def upload_document_image(self, project_id: str, local_path: str) -> dict:
        """Upload an image to be embedded in document markdown (ms-43).

        Multipart POST against ``/api/projects/<id>/documents/images``.
        Returns ``{url, markdown, size, content_type}`` on success.
        Server-side validation gates content-type (image/* only) and size
        (10 MiB cap) — see ``server/doc_images.py``.
        """
        import os as _os
        import mimetypes as _mt
        import uuid as _uuid

        with open(local_path, "rb") as f:
            file_bytes = f.read()
        filename = _os.path.basename(local_path)
        content_type = _mt.guess_type(filename)[0] or "application/octet-stream"

        boundary = f"----BeaconCli{_uuid.uuid4().hex}"
        body_parts: list[bytes] = []
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            (
                f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        body_parts.append(file_bytes)
        body_parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(body_parts)

        url = (
            f"{self._base_url}/api/projects/"
            f"{urllib.parse.quote(project_id, safe='')}/documents/images"
        )
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        token = self._get_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
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

    # Operation T2 envelopes (ms-60 / e-1339).
    # The SPEC author lists ``approved_actions`` in YAML frontmatter; the
    # server parses + validates + signs an envelope, stored in the
    # ``operation_envelopes`` subcollection. ``ttl_seconds`` defaults to
    # ~30 years per ms-60 SPEC "SPEC 更新まで無期限".

    def operation_approve(self, project_id: str, op_id: str, *,
                          spec_doc_id: str,
                          ttl_seconds: int | None = None) -> dict:
        body: dict = {"spec_doc_id": spec_doc_id}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return self.post(
            f"/api/projects/{project_id}/operations/"
            f"{urllib.parse.quote(op_id, safe='')}/envelopes",
            body,
        )

    def operation_revoke(self, project_id: str, op_id: str,
                         envelope_id: str, *, reason: str) -> dict:
        return self.post(
            f"/api/projects/{project_id}/operations/"
            f"{urllib.parse.quote(op_id, safe='')}/envelopes/"
            f"{urllib.parse.quote(envelope_id, safe='')}/revoke",
            {"reason": reason},
        )

    def list_operation_envelopes(self, project_id: str, op_id: str, *,
                                 status: str | None = None) -> list:
        suffix = f"?status={urllib.parse.quote(status)}" if status else ""
        return self.get(
            f"/api/projects/{project_id}/operations/"
            f"{urllib.parse.quote(op_id, safe='')}/envelopes{suffix}"
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

    def list_sessions(self, project_id: str, *, user_id: str = "",
                      machine: str = "", agent: str = "",
                      live_only: bool = False, since_minutes: int = 5,
                      healthy_only: bool = False) -> list:
        """List sessions for a project. Empty filters return everything (the
        original ms-57 behavior); set filters to do a directory query per
        ms-54 / e-1134.

        ``healthy_only`` (ms-54 / e-1318 Option C): drop sessions whose bridge
        poll loop is stale or has gracefully shut down. Each returned row
        carries a ``poll_health`` block regardless of this flag, so callers
        that want to display "stale (45s)" rather than filter can keep
        ``healthy_only=False`` and inspect the field client-side.
        """
        qs = []
        if user_id:
            qs.append(f"user_id={urllib.parse.quote(user_id)}")
        if machine:
            qs.append(f"machine={urllib.parse.quote(machine)}")
        if agent:
            qs.append(f"agent={urllib.parse.quote(agent)}")
        if live_only:
            qs.append("live_only=true")
            qs.append(f"since_minutes={since_minutes}")
        if healthy_only:
            qs.append("healthy_only=true")
        suffix = "?" + "&".join(qs) if qs else ""
        return self.get(f"/api/projects/{project_id}/sessions{suffix}")

    def list_user_sessions(self, *, live_only: bool = False,
                           since_minutes: int = 5, healthy_only: bool = False,
                           machine: str = "", agent: str = "") -> list:
        """List the calling user's sessions across all projects (ms-54 / e-1587).

        Use ``list_sessions(project_id)`` when you know which project to query;
        use this method when you need the cross-project view — e.g. the
        /beacon-dm-send picker that spans projects the caller is not currently
        cd'd into, or cross-project heartbeat watchdogs (op-6).

        Each returned row carries ``project_id`` and ``project_name`` so the
        consumer can route follow-up calls (``bus send --project <pid>``)
        without an extra lookup.
        """
        qs = []
        if live_only:
            qs.append("live_only=true")
            qs.append(f"since_minutes={since_minutes}")
        if healthy_only:
            qs.append("healthy_only=true")
        if machine:
            qs.append(f"machine={urllib.parse.quote(machine)}")
        if agent:
            qs.append(f"agent={urllib.parse.quote(agent)}")
        suffix = "?" + "&".join(qs) if qs else ""
        return self.get(f"/api/me/sessions{suffix}")

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
                       sender_session_id: str = "", payload: dict | None = None,
                       delivery: str = "propose-to-ai",
                       envelope: dict | None = None,
                       requested_action: str | None = None) -> dict:
        body = {
            "channel": channel,
            "sender_session_id": sender_session_id,
            "payload": payload or {},
            "delivery": delivery,
        }
        # e-1155 Phase 1: only include envelope keys when the caller passes
        # them, so older clients that don't know about envelopes keep working
        # (their posts will land on the T5-equivalent legacy path).
        if envelope is not None:
            body["envelope"] = envelope
        if requested_action is not None:
            body["requested_action"] = requested_action
        return self.post(f"/api/projects/{project_id}/bus", body)

    def issue_bus_envelope(self, project_id: str, *, tier: str,
                           actions_authorized: list[str] | None = None,
                           scope: str | None = None,
                           data_class: str = "free",
                           conversation_id: str | None = None,
                           in_reply_to: str | None = None,
                           chain_depth: int = 0,
                           ttl_seconds: int = 3600) -> dict:
        """Mint a server-signed bus envelope (ms-54 / e-1155 Phase 1).

        The returned dict is the full envelope (including ``signature``);
        callers embed it as the ``envelope`` field on a subsequent
        ``post_bus_event`` call.
        """
        body = {
            "tier": tier,
            "actions_authorized": actions_authorized or [],
            "data_class": data_class,
            "chain_depth": chain_depth,
            "ttl_seconds": ttl_seconds,
        }
        if scope is not None:
            body["scope"] = scope
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if in_reply_to is not None:
            body["in_reply_to"] = in_reply_to
        return self.post(f"/api/projects/{project_id}/bus/envelope/issue", body)

    def list_bus_audit(self, project_id: str, *, since: str = "",
                       limit: int = 100) -> list:
        qs = []
        if since:
            qs.append(f"since={urllib.parse.quote(since)}")
        if limit:
            qs.append(f"limit={limit}")
        suffix = "?" + "&".join(qs) if qs else ""
        return self.get(f"/api/projects/{project_id}/bus/audit{suffix}")

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

    # Per-event receipts (ms-54 / e-1348). Two callsites:
    #   * channel/bus.mjs uses these to stamp delivered/opened on receive.
    #   * `beacon bus status <event_id>` uses get_bus_event to render the
    #     3-stage view to the sender.

    def get_bus_event(self, project_id: str, event_id: str) -> dict:
        """Fetch one bus event with its receipt fields. 404 → ApiClientError."""
        return self.get(
            f"/api/projects/{project_id}/bus/{urllib.parse.quote(event_id)}"
        )

    def ack_bus_event_receipt(self, project_id: str, event_id: str, *,
                              stage: str, recipient_session_id: str) -> dict:
        """Stamp a receipt stage (delivered|opened). First-write-wins per stage."""
        return self.post(
            f"/api/projects/{project_id}/bus/{urllib.parse.quote(event_id)}/ack",
            {"stage": stage, "recipient_session_id": recipient_session_id},
        )

    # ms-54 / e-1369 Layer 4: AI-authored intent. Set via `beacon session
    # focus "<text>"` / `beacon session attention --set true`. Read by the
    # /beacon-dm-send picker so a sender sees "what is each session doing".

    def upsert_session_intent(self, project_id: str, session_id: str, *,
                              text: Optional[str] = None,
                              attention_required: Optional[bool] = None) -> dict:
        body: dict = {}
        if text is not None:
            body["text"] = text
        if attention_required is not None:
            body["attention_required"] = attention_required
        return self.post(
            f"/api/projects/{project_id}/sessions/{urllib.parse.quote(session_id)}/intent",
            body,
        )

    def get_session(self, project_id: str, session_id: str) -> dict:
        """Fetch a single session by id (used by `beacon session focus --show`).

        The server exposes the full /sessions list endpoint; we filter
        client-side to keep the helper simple. Adds a dedicated /sessions/{sid}
        GET when the cost becomes meaningful.
        """
        listed = self.list_sessions(project_id)
        for s in listed:
            if s.get("session_id") == session_id:
                return s
        return {}

    # Trek operations (ms-69 / e-1681)
    #
    # Top-level resource (= not under /api/projects/) because treks bridge
    # projects. Caller does not pass a project_id — trek membership lives at
    # the user grain (user_id + email), so the auth token alone identifies
    # the caller. See server/app.py /api/treks/* endpoints (e-1656).

    def list_treks(self, *, status: str = "", include_archived: bool = False,
                   all_actors: bool = False) -> list:
        """List treks visible to the caller. Default scope = creator OR member.

        ``status``: filter by lifecycle (planning|active|archived).
        ``include_archived``: also surface archived treks (default hides).
        ``all_actors``: admin view (= every trek). Non-admin caller gets 403.
        """
        qs = []
        if status:
            qs.append(f"status={urllib.parse.quote(status)}")
        if include_archived:
            qs.append("include_archived=true")
        if all_actors:
            qs.append("all_actors=true")
        suffix = "?" + "&".join(qs) if qs else ""
        return self.get(f"/api/treks{suffix}")

    def create_trek(self, *, title: str, creator_session_id: str,
                    description: str = "", type_: str = "persistent") -> dict:
        """Create a trek. Caller becomes creator + initial leader.

        ``creator_session_id`` is recorded as ``leader_session_id`` per
        SPEC 設計方針 9 (= leader is at session grain).
        """
        return self.post("/api/treks", {
            "title": title,
            "description": description,
            "type": type_,
            "creator_session_id": creator_session_id,
        })

    def get_trek(self, trek_id: str) -> dict:
        """Fetch a single trek by id. 403 if caller is neither creator nor member."""
        return self.get(f"/api/treks/{urllib.parse.quote(trek_id, safe='')}")

    def patch_trek(self, trek_id: str, *, title: str | None = None,
                   description: str | None = None, type_: str | None = None) -> dict:
        """Update title / description / type. Leader-only."""
        body: dict = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if type_ is not None:
            body["type"] = type_
        return self.patch(f"/api/treks/{urllib.parse.quote(trek_id, safe='')}", body)

    def archive_trek(self, trek_id: str) -> dict:
        """Archive (= status → archived, terminal). Leader-only."""
        return self.delete(f"/api/treks/{urllib.parse.quote(trek_id, safe='')}")

    def start_trek(self, trek_id: str) -> dict:
        """Transition planning → active. Leader-only."""
        return self.post(f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/start")

    def invite_trek_member(self, trek_id: str, email: str) -> dict:
        """Invite a user (by email) to the trek. Any joined member may invite."""
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/members",
            {"email": email},
        )

    def join_trek(self, trek_id: str) -> dict:
        """Caller accepts their own invitation. Non-invited → 403."""
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/members/join"
        )

    def leave_trek(self, trek_id: str) -> dict:
        """Caller removes themselves. Leader must transfer first; last member
        cannot leave (= archive instead)."""
        return self.delete(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/members/me"
        )

    def add_trek_scope(self, trek_id: str, *, project: str,
                       milestone: str = "", operation: str = "",
                       task: str = "") -> dict:
        """Append a scope entry (cross-project ref). Any joined member."""
        body: dict = {"project": project}
        if milestone:
            body["milestone"] = milestone
        if operation:
            body["operation"] = operation
        if task:
            body["task"] = task
        return self.put(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/scope", body,
        )

    def remove_trek_scope(self, trek_id: str, *, project: str,
                          milestone: str = "", operation: str = "",
                          task: str = "") -> dict:
        """Remove a scope entry. Any joined member."""
        body: dict = {"project": project}
        if milestone:
            body["milestone"] = milestone
        if operation:
            body["operation"] = operation
        if task:
            body["task"] = task
        return self.delete(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/scope", body,
        )

    def set_trek_halt(self, trek_id: str, *, issued_by_session_id: str,
                      reason: str = "") -> dict:
        """Pull the Andon cord. Any joined member may halt an active trek."""
        return self.put(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/halt",
            {"issued_by_session_id": issued_by_session_id, "reason": reason},
        )

    def clear_trek_halt(self, trek_id: str) -> dict:
        """Release the Andon cord. Any joined member."""
        return self.delete(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/halt"
        )

    def transfer_trek_leader(self, trek_id: str, *, from_session_id: str,
                             to_session_id: str) -> dict:
        """Hand off leadership. Caller's session must equal the trek's current
        ``leader_session_id`` AND the calling user must hold the leader role."""
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/transfer-leader",
            {"from_session_id": from_session_id,
             "to_session_id": to_session_id},
        )

    def get_trek_summary(self, trek_id: str) -> dict:
        """Compact status snapshot (counts + status + halt) for dashboards."""
        return self.get(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/summary"
        )

    def list_trek_documents(self, trek_id: str) -> list:
        """List documents associated with this trek (= trek_id field set)."""
        return self.get(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/documents"
        )

    # Reverse lookup: project work item → related treks (ms-69 / e-1663)
    # Used by the Related Treks widget on milestone / operation / task
    # detail pages (e-1664). Includes archived treks by default.

    def list_related_treks_for_milestone(self, project_id: str, ms_id: str) -> list:
        return self.get(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/milestones/{urllib.parse.quote(ms_id, safe='')}/related-treks"
        )

    def list_related_treks_for_operation(self, project_id: str, op_id: str) -> list:
        return self.get(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/operations/{urllib.parse.quote(op_id, safe='')}/related-treks"
        )

    def list_related_treks_for_entry(self, project_id: str, entry_id: str) -> list:
        return self.get(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/entries/{urllib.parse.quote(entry_id, safe='')}/related-treks"
        )

    # ms-55 e-1730: active claims subcollection (= lib/claims.py mirror).
    # The wire payload is the dict lib/claims.py:build_claim_payload returns.

    def list_active_claims(self, project_id: str) -> list:
        """Return all active claims on a project, sorted by issued_at."""
        return self.get(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/active_claims"
        )

    def get_active_claim(self, project_id: str, claim_id: str) -> dict:
        return self.get(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/active_claims/{urllib.parse.quote(claim_id, safe='')}"
        )

    def save_active_claim(self, project_id: str, claim_id: str, payload: dict) -> dict:
        """Upsert a claim; returns ``{claim_id, status}``."""
        return self.post(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/active_claims/{urllib.parse.quote(claim_id, safe='')}",
            {"payload": payload},
        )

    def delete_active_claim(self, project_id: str, claim_id: str) -> dict:
        """Release a claim; returns ``{claim_id, deleted: bool}``."""
        return self.delete(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/active_claims/{urllib.parse.quote(claim_id, safe='')}"
        )
