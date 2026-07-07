"""Beacon API Client - HTTP client for cloud API access.

Used by CLI cloud commands to communicate with the Beacon API server
instead of directly accessing Firestore.
"""

from __future__ import annotations

import json
import os as _os
import ssl as _ssl
import time as _time
import urllib.parse
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# TLS trust store (ms-95 bug-aggregation / observed 2026-07-07 dogfood)
#
# On some hosts (seen on Windows) Python's default SSL context validates
# against a system trust store that still carries an expired root CA
# (Let's Encrypt's old cross-signed chain), so every HTTPS call to the
# API fails with ``CERTIFICATE_VERIFY_FAILED: certificate has expired``
# even though the server presents a valid, freshly-renewed certificate
# (curl / browsers accept it). ``certifi`` ships Mozilla's up-to-date CA
# bundle; when it is importable we build the context from that so trust
# does not depend on the host store being current. Falls back to the
# stdlib default when certifi is absent (local-only installs), and a
# custom bundle can be pinned via ``BEACON_API_CAFILE``.
#
# Cached module-level (same pattern as the circuit breaker state): the CA
# bundle does not change within a process, and building a context is not
# free.
# ---------------------------------------------------------------------------

_CAFILE_ENV = "BEACON_API_CAFILE"
_SSL_CONTEXT = None


def _get_ssl_context() -> "_ssl.SSLContext":
    """Return a shared SSL context that trusts an up-to-date CA bundle.

    Prefers ``$BEACON_API_CAFILE`` if set, then ``certifi``'s bundle, then
    the stdlib default. Built once and cached for the process.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    cafile = _os.environ.get(_CAFILE_ENV, "").strip() or None
    if cafile is None:
        try:
            import certifi

            cafile = certifi.where()
        except Exception:
            cafile = None
    try:
        _SSL_CONTEXT = _ssl.create_default_context(cafile=cafile)
    except Exception:
        # A bad/unreadable cafile should not make the client unusable;
        # fall back to the host default rather than raising here.
        _SSL_CONTEXT = _ssl.create_default_context()
    return _SSL_CONTEXT


# ---------------------------------------------------------------------------
# Client-side circuit breaker (ms-98 / e-2777)
#
# Rationale: on 2026-07-02 the client kept retrying against a Cloud Run
# instance pool that was already returning 429 "no available instance",
# adding to the server's load and amplifying the storm. This breaker
# short-circuits ``_request`` when the recent 429 rate crosses a
# threshold. e-2764 (trigger-check split) and e-2769 (session cache)
# reduce the number of times the client picks up the phone; this closes
# the fallback path where an already-picked-up call retries into a
# degraded server.
#
# State is module-level so every ``ApiClient`` inside a single Python
# process shares one view. Cross-process coordination is a future
# concern (an OS lock file is overkill for a per-invocation CLI). The
# wall-clock TTL from e-2773 already caps how long any single process
# can retry.
# ---------------------------------------------------------------------------

_CIRCUIT_LOCK_ENV = "BEACON_API_CIRCUIT_BREAKER_DISABLE"
_CIRCUIT_WINDOW_ENV = "BEACON_API_CIRCUIT_WINDOW_SECONDS"
_CIRCUIT_THRESHOLD_ENV = "BEACON_API_CIRCUIT_THRESHOLD"
_CIRCUIT_COOLDOWN_ENV = "BEACON_API_CIRCUIT_COOLDOWN_SECONDS"

_DEFAULT_CIRCUIT_WINDOW = 60.0
_DEFAULT_CIRCUIT_THRESHOLD = 3
_DEFAULT_CIRCUIT_COOLDOWN = 60.0

_recent_429_timestamps: list[float] = []
_circuit_open_until: float = 0.0


def _circuit_enabled() -> bool:
    return _os.environ.get(_CIRCUIT_LOCK_ENV, "").strip() != "1"


def _circuit_window() -> float:
    raw = _os.environ.get(_CIRCUIT_WINDOW_ENV, "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_CIRCUIT_WINDOW


def _circuit_threshold() -> int:
    raw = _os.environ.get(_CIRCUIT_THRESHOLD_ENV, "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_CIRCUIT_THRESHOLD


def _circuit_cooldown() -> float:
    raw = _os.environ.get(_CIRCUIT_COOLDOWN_ENV, "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _DEFAULT_CIRCUIT_COOLDOWN


def _circuit_check_and_raise() -> None:
    """Raise ``RuntimeError`` if the circuit is currently open.

    Called before the network hop. When the breaker has opened due to a
    recent 429 streak, this turns "another retry attempt" into
    "immediate fail with a diagnostic" — the client stops piling
    requests onto a server that is already refusing them.
    """
    if not _circuit_enabled():
        return
    now = _time.monotonic()
    if now < _circuit_open_until:
        remaining = _circuit_open_until - now
        raise RuntimeError(
            "API circuit open (recent 429 storm); cooling down for "
            f"{remaining:.1f}s. Set "
            "BEACON_API_CIRCUIT_BREAKER_DISABLE=1 to override."
        )


def _circuit_record_429() -> None:
    """Track a 429 hit; open the circuit if the recent window is saturated."""
    if not _circuit_enabled():
        return
    global _circuit_open_until
    now = _time.monotonic()
    window = _circuit_window()
    _recent_429_timestamps.append(now)
    # Prune anything older than the window so ``len`` reflects only the
    # burst that matters right now.
    cutoff = now - window
    while _recent_429_timestamps and _recent_429_timestamps[0] < cutoff:
        _recent_429_timestamps.pop(0)
    if len(_recent_429_timestamps) >= _circuit_threshold():
        _circuit_open_until = now + _circuit_cooldown()


def _circuit_reset_for_tests() -> None:
    """Clear circuit state — hook for tests to isolate cases."""
    global _circuit_open_until
    _recent_429_timestamps.clear()
    _circuit_open_until = 0.0


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
        # ms-98 / e-2777: fail fast when a recent 429 storm has opened
        # the circuit. Kept ahead of URL / body assembly so an already-
        # tripped breaker never even builds a request object.
        _circuit_check_and_raise()

        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        token = self._get_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        # ms-86 / e-2225 — forward the local session id to the server so
        # endpoints that record session-grained provenance (= trek join,
        # task-state stamps, etc.) can attribute the call to a session
        # without inventing a new auth path. Empty / unset is fine — the
        # server treats missing header as anonymous-session.
        #
        # ms-97 / e-2694 dogfood fix: when ``BEACON_SESSION_ID`` is empty
        # (= unset in the user's interactive shell, the common case for
        # CLI direct invocation), fall back to the active session resolver
        # which reads ``.beacon/session.json`` / bridge claim. This makes
        # ALL cloud-mode API calls (= not just trek join) carry an
        # ``X-Beacon-Session`` header so server-side endpoints that key
        # off phase A+ session_id (= trek join, task-state stamps,
        # session-grained audit) work from a bare shell without requiring
        # the user to set the env var manually. Best-effort: any failure
        # in the resolver (= no .beacon/ dir, corrupt JSON) leaves the
        # header empty — the existing "anonymous-session" fallback path
        # continues to apply.
        import os as _os
        sid = _os.environ.get("BEACON_SESSION_ID", "").strip()
        if not sid:
            try:
                # Resolve from .beacon/session.json / bridge claim. Same
                # source used by ``commands._resolve_session_id`` so the
                # CLI and the api_client agree on session identity.
                import session as _session
                sid = (_session.resolve_active_session_id() or "").strip()
            except Exception:
                sid = ""
        if sid:
            req.add_header("X-Beacon-Session", sid)

        try:
            with urllib.request.urlopen(
                req, timeout=30, context=_get_ssl_context()
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(error_body).get("detail", error_body)
            except (json.JSONDecodeError, AttributeError):
                detail = error_body
            # ms-98 / e-2777: track 429s to feed the circuit breaker.
            # Recorded AFTER the response body is drained so we don't
            # leak the socket if the state machine opens here.
            if e.code == 429:
                _circuit_record_429()
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
        # ms-98 / e-2777: honor the shared circuit state so image
        # uploads don't sneak past the fail-fast during a 429 storm.
        _circuit_check_and_raise()

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
            with urllib.request.urlopen(
                req, timeout=60, context=_get_ssl_context()
            ) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(error_body).get("detail", error_body)
            except (json.JSONDecodeError, AttributeError):
                detail = error_body
            # ms-98 / e-2777: image upload path shares the circuit state
            # with plain _request so a 429 here also opens the breaker.
            if e.code == 429:
                _circuit_record_429()
            raise RuntimeError(f"API error {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Cannot connect to API ({self._base_url}): {e.reason}"
            ) from e

    # Milestone operations (ms-84 Phase 1 — fine-grained Store wiring)

    def get_milestone(self, project_id: str, ms_id: str) -> dict:
        """Fetch a single milestone (with task counts) from the cloud project.

        Mirrors ``GET /api/projects/{project_id}/milestones/{ms_id}`` which
        returns the milestone dict augmented with ``total_tasks`` / ``done_tasks``
        / ``entries`` (JSON-serialised). Raises ``RuntimeError`` (HTTP 404) when
        the milestone is unknown so callers can map to a CLI-friendly error.
        """
        return self.get(
            f"/api/projects/{project_id}/milestones/"
            f"{urllib.parse.quote(ms_id, safe='')}"
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

    # Operation fire claim (ms-95 / e-1668 + e-2350)

    def claim_operation_fire(self, project_id: str, op_id: str,
                             session_id: str = "") -> dict:
        """Claim the right to fire ``op_id`` today for ``project_id``.

        Returns ``{claimed, claimed_by, claimed_at}``. If ``claimed`` is
        False, another bclaude session in the same project already fired
        today — the caller (= ``_auto_fire_operation_triggers``) should
        skip its local trigger write + bus push.

        Server-side Firestore transaction serializes concurrent claims so
        only one of N parallel sessions wins per (project, op, date).
        See SPEC ``7JO6uTMTGs0ehGhGk3KX`` (ms-95) § 設計方針 1.
        """
        return self.post(
            f"/api/projects/{project_id}/operation-fires/{op_id}/claim",
            {"session_id": session_id},
        )

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

    # ms-70 / e-1923 (e-1718 AC 4): audit history listing for decided
    # (approved | denied) DM-action sidecars. Mirrors the Web UI's
    # Settings > Audit table but renders 6 columns in the terminal.
    def list_dm_approval_history(self, project_id: str, *,
                                  limit: int = 50) -> list[dict]:
        """List decided DM approval sidecars (audit-only, read-only).

        Wraps ``GET /api/projects/{pid}/dm/approval/history?limit=N``.
        The server filters out ``pending`` and ``auto`` rows by design
        (SPEC 設計方針 3: approve/deny lives only in the terminal), so
        every returned row carries ``approval_status`` in {approved,
        denied} with non-empty ``decision_by`` / ``decision_at``.

        ``limit`` is capped server-side at 500 to bound audit-scroll
        surface. Default 50 matches the Web UI.
        """
        suffix = f"?limit={int(limit)}" if limit else ""
        return self.get(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/dm/approval/history{suffix}"
        )

    # ms-70 / e-1716: receiver-side decision on a pending DM-action sidecar.
    # The CLI carries the human's Bearer token; the server stamps decision_by
    # from that token's sub claim — the CLI cannot spoof a different user.
    def respond_dm_approval(self, project_id: str, event_id: str, *,
                            decision: str) -> dict:
        """POST a receiver-side approve / deny decision on a pending sidecar.

        ``decision`` must be ``"approve"`` or ``"deny"``. Returns the resulting
        7-field sidecar row. Server enforces:
          * 400 — bad decision verb
          * 403 — caller is not the addressed receiver
          * 404 — no sidecar exists for this event_id (= legacy / auto / typo)
          * 409 — already decided by someone else, or attempt to flip decision
        Idempotent for "same caller resubmits same decision" (= no-op return).
        """
        return self.post(
            f"/api/projects/{project_id}/dm/approval/"
            f"{urllib.parse.quote(event_id)}",
            {"decision": decision},
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

    # ms-99 / e-2830 — Trek slot schema v2 client methods.
    def add_trek_slot(self, trek_id: str, *, project: str,
                      milestone: str = "", operation: str = "",
                      task: str = "",
                      included_task_ids: list[str] | None = None) -> dict:
        """Stage a slot-add with a fresh sl-<8 hex> id. Any joined member."""
        body: dict = {"project": project}
        if milestone:
            body["milestone"] = milestone
        if operation:
            body["operation"] = operation
        if task:
            body["task"] = task
        if included_task_ids is not None:
            body["included_task_ids"] = list(included_task_ids)
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/slots", body,
        )

    def amend_trek_slot(self, trek_id: str, slot_id: str, *,
                        add_children: list[str] | None = None,
                        remove_children: list[str] | None = None) -> dict:
        """Stage a slot-amend (edit included_task_ids). Any joined member."""
        body = {
            "add_children": list(add_children or []),
            "remove_children": list(remove_children or []),
        }
        return self.patch(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}"
            f"/slots/{urllib.parse.quote(slot_id, safe='')}",
            body,
        )

    def claim_trek_slot(self, trek_id: str, slot_id: str, *,
                        session_id: str = "") -> dict:
        """Stage a slot-claim (stamp claim_session_id). Empty = unclaim."""
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}"
            f"/slots/{urllib.parse.quote(slot_id, safe='')}/claim",
            {"session_id": session_id or ""},
        )

    def list_trek_slots(self, trek_id: str) -> dict:
        """Return the materialize slot view of the trek."""
        return self.get(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/slots",
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

    def trek_summary_sent(self, trek_id: str) -> dict:
        """Stamp ``meta.summary_sent_at`` after the leader sent the user
        summary DM (ms-97 / Phase 7-A / AC21).

        Leader-only on the server side; the CLI dispatches via the
        scheduler's ``X-Beacon-Session`` header so the AC13 hard-check
        resolves correctly. Returns the updated trek doc.
        """
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/summary-sent",
            {},
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

    def pulse_ack_trek(self, trek_id: str, *, session_id: str,
                       picked_choice: str = "", note: str = "",
                       state_summary: str = "",
                       blockers=None,
                       needs_leader_judgment: bool = False,
                       time_on_task_seconds: int = 0) -> dict:
        """Self-report /beacon-trek-pulse Skill invocation (ms-88 / e-2106).

        Layer 2 observability: the Skill calls this as Step 1 so the server
        knows the Skill actually fired in response to a scheduler tick.

        ms-92 / e-2165 — the structured fields (``state_summary`` /
        ``blockers`` / ``needs_leader_judgment`` / ``time_on_task_seconds``)
        are sent alongside the legacy ``picked_choice`` + ``note`` so
        the leader-digest (e-2164) can mechanically aggregate counts
        without parsing natural-language notes. All are optional —
        pre-e-2165 servers ignore the unknown keys.

        Returns the updated per-session pulse_acks entry so the Skill can
        echo the recorded state back to the user.
        """
        payload = {
            "session_id": session_id,
            "picked_choice": picked_choice,
            "note": note,
            "state_summary": state_summary,
            "blockers": list(blockers or []),
            "needs_leader_judgment": bool(needs_leader_judgment),
            "time_on_task_seconds": int(time_on_task_seconds or 0),
        }
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/pulse-ack",
            payload,
        )

    def list_trek_pulse_acks(self, trek_id: str) -> dict:
        """Per-session pulse-ack summary for dashboards (ms-88 / e-2108)."""
        return self.get(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/pulse-acks"
        )

    def kickoff_trek(self, trek_id: str, *, session_id: str,
                     kickoff_dm_event_id: str = "") -> dict:
        """Mark a session's Kickoff Ritual DM as sent (ms-88 / e-2138).

        Called by ``/beacon-trek-pulse`` Skill's Step 0.4 after the kickoff
        DM has been generated and posted to the trek leader. Until this is
        called, ``pulse-ack`` rejects further progress with HTTP 400
        ``kickoff_required`` — so this stamp is the structural gate that
        forces every fresh session to declare its plan to peers first.

        Returns the per-session ``kickoff_status`` entry so the caller can
        echo "stamped" back to the user.
        """
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/kickoff",
            {"session_id": session_id,
             "kickoff_dm_event_id": kickoff_dm_event_id},
        )

    def take_over_trek(self, trek_id: str, *, session_id: str) -> dict:
        """Re-bind leader_session_id to the caller's fresh session (ms-88 / e-2089).

        Unlike ``transfer-leader``, this does **not** require the prior
        ``leader_session_id`` to authorize — the caller proves leader role
        at user-grain (= joined member with ``role == 'leader'``), and the
        server overwrites ``leader_session_id`` with ``session_id``. This is
        the fresh-session recovery path for the case where the original
        leader session is dead (= Mac restart, terminal closed, bclaude
        relaunched from clean env) and ``transfer-leader`` cannot recover
        because it needs the dead session as origin.
        """
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/take-over",
            {"session_id": session_id},
        )

    def reconcile_trek(self, trek_id: str, *, apply: bool = False) -> dict:
        """ms-88 / e-2167 — reconcile Trek task_states with task pool.

        Scans the Trek's task_states and compares each entry's pool status
        across the Trek's scope projects. Returns a diff of "pool done but
        Trek stamp non-terminal" stuck entries. If ``apply=True``, the
        server mirrors them to ``done`` with ``task-pool-mirror`` actor.
        """
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/reconcile",
            {"apply": apply},
        )

    def set_trek_task_state(self, trek_id: str, *, task_id: str,
                             state: str, note: str = "") -> dict:
        """ms-75 / e-2048 — Stamp Trek-internal task state.

        Member-only (= server-side check). The server validates the
        state transition and emits a one-time trek-task-review DM to
        the leader when the new state is terminal (= done /
        waiting-review). Returns the updated trek doc.
        """
        return self.patch(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/task-state",
            {"task_id": task_id, "state": state, "note": note},
        )

    def extend_trek_task_ttl(self, trek_id: str, *, task_id: str,
                             minutes: int, reason: str = "") -> dict:
        """ms-95 / e-2308 — Postpone TTL safety net deadline on a task.

        Leader-side primitive for the Agent-tool subagent dispatch path
        where the subagent cannot stamp ``last_activity_at`` itself.
        ``minutes <= 0`` clears the extension and lets normal TTL
        semantics resume on the next scheduler tick.
        """
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/extend-ttl",
            {
                "task_id": task_id,
                "minutes": int(minutes),
                "reason": reason or "",
            },
        )

    def add_trek_task(self, trek_id: str, *,
                      target_project: str,
                      target_milestone: str,
                      description: str,
                      entry_type: str = "task",
                      priority: str = "",
                      motivation: str = "",
                      acceptance_criteria: str = "") -> dict:
        """ms-92 / e-2141 — cross-project task add via Trek scope.

        Server walks ``trek.check_trek_task_add_allowed`` and either:
          * 200 with {"entry_id": "e-XXX", "project_id": ..., "milestone_id": ...}
            on success (task is written under target project's MS),
          * 403 ``scope-reject`` (project / milestone not in scope, or
            scope-only-task-narrowing — same 4 reason codes the CLI knows),
          * 404 when trek does not exist,
          * 400 when ``target`` is malformed.

        ``meta.trek_id`` is stamped on the created entry for audit trail
        — that lets ``/beacon-retrospect`` query "which tasks were
        sprouted via Trek X" later.
        """
        return self.post(
            f"/api/treks/{urllib.parse.quote(trek_id, safe='')}/task-add",
            {
                "target_project": target_project,
                "target_milestone": target_milestone,
                "description": description,
                "type": entry_type,
                "priority": priority,
                "motivation": motivation,
                "acceptance_criteria": acceptance_criteria,
            },
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
