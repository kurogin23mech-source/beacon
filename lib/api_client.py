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

    def _guard_base_url(self) -> str:
        """The base_url the prod-test-write guards read, tolerant of a client
        built via ``ApiClient.__new__(...)`` (some tests bypass ``__init__`` and
        stub ``.post``, so ``_base_url`` is unset — they never reach the network
        and aren't targeting prod, so the guard must no-op, not AttributeError).
        Single accessor so every guard call site uses one idiom (ms-108 e-5194
        maint finding)."""
        return getattr(self, "_base_url", "")

    def _guard_bus_write(self) -> None:
        """Refuse a bus WRITE from a test context targeting prod. The ONE
        embodiment of "guard before a bus write" (ms-108 e-5216 / e-5300 maint
        review): every bus-mutating verb — post_bus_event / issue_bus_envelope /
        advance_bus_cursor / ack_bus_event_receipt / respond_dm_approval — calls
        THIS, so the decision and its deferred import live in one place instead of
        a 2-line preamble copied into five method bodies (which imposed a hidden
        sync obligation on the next author). No-op off test context / non-prod."""
        import cloud_write_guard
        cloud_write_guard.guard_prod_bus_write(self._guard_base_url())

    def _guard_read(self) -> None:
        """Refuse a READ from a test context targeting prod (ms-166 e-6819).

        Same shape as the sibling write helpers — the decision and its deferred
        import live in one place. Called from the GET leg of :meth:`_request`,
        the single hallway all ~36 read verbs pass through, so no reader can be
        forgotten. No-op off test context / non-prod.
        """
        import cloud_write_guard
        cloud_write_guard.guard_prod_read(self._guard_base_url())

    def _guard_decision_write(self) -> None:
        """Refuse a decision-stream WRITE from a test context targeting prod.

        ms-166 e-6637. Same shape as :meth:`_guard_bus_write` — the decision is
        made in ONE place so the next author cannot forget a copied preamble.
        ``record_decision`` is the only door into the append-only stream, so
        guarding it here covers every ``lib/`` call site and every future one.
        No-op off test context / non-prod.
        """
        import cloud_write_guard
        cloud_write_guard.guard_prod_decision_write(self._guard_base_url())

    def _guard_write(self, method: str = "", path: str = "") -> None:
        """ANY write to prod from a test context is refused (ms-166 e-6854).

        Same shape as the sibling ``_guard_*`` helpers: the判定 lives in one place
        so a call site is a single line. This is the **backstop** — the specific
        guards (project / bus / decision) fire earlier inside their own methods,
        so their richer wording is what the author sees for those doors.
        """
        import cloud_write_guard
        cloud_write_guard.guard_prod_write(
            self._guard_base_url(), method=method, path=path)

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, extra_headers: dict | None = None) -> dict:
        # ms-98 / e-2777: fail fast when a recent 429 storm has opened
        # the circuit. Kept ahead of URL / body assembly so an already-
        # tripped breaker never even builds a request object.
        _circuit_check_and_raise()
        # ms-166 e-6819: 読み取りの唯一の廊下。テスト文脈から本番を読むと、結果が
        # そのときの本番の状態に左右されて再現しないので断る。書き込みは各扉で既に
        # 守られているので、ここで問うのは GET だけ (廊下で全部を断つと書き込みの
        # 診断文言が読み取りのものに置き換わってしまう)。
        if (method or "").upper() == "GET":
            self._guard_read()
        else:
            # ms-166 e-6854: 書き込みの受け止め。以前ここには「書き込みは各扉で既に
            # 守られているので GET だけ問う」と書いてあったが、**実測では 61 の書き込み
            # メソッドのうち守られていたのは 8 件だった** — コメントが前提として
            # 述べていたことが成り立っていなかった。扉を 1 つずつ塞ぐのを 4 回
            # (e-4029 / e-5194 / e-5216 / e-6637) やったので、5 回目は廊下で閉じる。
            # 固有のガードは各扉の中で先に発火するので、診断文言はそちらが勝つ。
            self._guard_write(method, path)

        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        # ms-123 / e-4028: caller-supplied headers (e.g. X-Beacon-Envelope for
        # envelope-gated destructive endpoints like project.archive). Added
        # before auth below so a caller can never clobber Authorization.
        for _hk, _hv in (extra_headers or {}).items():
            req.add_header(_hk, _hv)
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

    def post(self, path: str, body: dict | None = None,
             *, extra_headers: dict | None = None) -> dict:
        # ms-123: only thread extra_headers through when a caller actually
        # supplies them. Forwarding extra_headers=None unconditionally broke
        # every test that stubs `_request` with a double lacking the kwarg
        # (test_bus_transport / test_trek_cli_cloud / …). Normal callers keep
        # the pre-existing 3-arg call shape; only the envelope-gated archive
        # path (the sole extra_headers user) takes the 4-arg branch.
        if extra_headers is not None:
            return self._request("POST", path, body, extra_headers=extra_headers)
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

    def me_recap(self, *, from_date: str = "", to_date: str = "",
                 limit: int = 0) -> dict:
        """Cross-project period query (ms-184 e-7017 / SPEC 方針 3).

        Returns the ``progress_window.aggregate`` shape — only the projects
        that moved, plus ``unavailable`` for the ones that could not be read.
        Raises ``RuntimeError`` on 404 / 501 (= a server that predates this
        endpoint, or one where it is not wired), which the caller handles by
        reading each project itself; both paths run the same derivation, so
        they cannot disagree about what counts as progress.
        """
        q = []
        if from_date:
            q.append(f"from={urllib.parse.quote(from_date, safe='')}")
        if to_date:
            q.append(f"to={urllib.parse.quote(to_date, safe='')}")
        if limit:
            q.append(f"limit={int(limit)}")
        qs = ("?" + "&".join(q)) if q else ""
        return self.get(f"/api/me/recap{qs}")

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
        # ms-123 / e-4029: PUT upserts to /api/projects/{id}, so it can
        # materialize a new project just like create_project — guard the same
        # leak vector (e.g. `beacon cloud upload-initial` → cmd_cloud_push).
        import cloud_write_guard
        cloud_write_guard.guard_prod_project_write(self._guard_base_url())
        return self.put(f"/api/projects/{project_id}", data)

    def create_project(self, project_id: str, name: str, objective: str = "") -> dict:
        # ms-123 / e-4029: structural leak guard. A test context creating a
        # project on the production cloud is the exact path that left 48
        # phase4-test residue projects behind — block it at the choke point.
        import cloud_write_guard
        cloud_write_guard.guard_prod_project_write(self._guard_base_url())
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
        # ms-166 e-6854: この扉は multipart を自分で組むので共有の輸送路 (_request) を
        # 通らない。廊下に受け止めを置いても here は素通りするため個別に通す。
        # **ファイルを読む前に** 断る (断る予定の仕事はしない)。
        # (この扉は PR #785 の独立 AX レビュー AX-1 が「verb 名だけ見る検出器が
        # 形を変えた扉を一度も見ていなかった」と指摘した当のメソッド。)
        self._guard_write("POST", "/documents/images")

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

    def record_decision(self, project_id: str, decision: dict) -> dict:
        """Append a decision-arm event to the unified stream (ms-154 e-5593).

        ``decision`` is the record body (kind / decision / rationale /
        decided_by / evidence / options / related). The server stamps ``who``
        from the token + session header, so callers do NOT pass identity here.

        ms-166 e-6637: guarded. The stream is append-only, so a row written from
        a test context can never be removed — the guard refuses instead.
        """
        self._guard_decision_write()
        return self.post(f"/api/projects/{project_id}/decisions", decision)

    def list_decisions(self, project_id: str, *, kind: str = "",
                       limit: int = 100, since: str = "", session: str = "",
                       target: str = "") -> dict:
        """Read the unified decision-arm stream (ms-154 e-5595 / ms-164 e-6030).

        Returns ``{"decisions": [...], "count": N}``. ``kind`` filters to one
        decision family; ``session`` (= ``who.session_id``) / ``target`` (=
        ``related.target_id``) filter to one session's / one worked-Target's
        decisions; ``since`` / ``limit`` page the append-only stream. All filters
        are applied server-side BEFORE the ``limit`` window.
        """
        q = []
        if kind:
            q.append(f"kind={urllib.parse.quote(kind, safe='')}")
        if limit:
            q.append(f"limit={int(limit)}")
        if since:
            q.append(f"since={urllib.parse.quote(since, safe='')}")
        if session:
            q.append(f"session={urllib.parse.quote(session, safe='')}")
        if target:
            q.append(f"target={urllib.parse.quote(target, safe='')}")
        qs = ("?" + "&".join(q)) if q else ""
        return self.get(f"/api/projects/{project_id}/decisions{qs}")

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
                      cwd: str = "", agent_kind: str = "",
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
        if cwd:
            qs.append(f"cwd={urllib.parse.quote(cwd)}")
        if agent_kind:
            qs.append(f"agent_kind={urllib.parse.quote(agent_kind)}")
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
                       requested_action: str | None = None,
                       context: str = "", rationale: str = "",
                       client_event_id: str = "",
                       is_retry: bool = False,
                       max_retries: int = 2) -> dict:
        # ms-108 e-5194 follow-up: structural leak guard. A test context posting
        # a bus event to the production cloud is what let a non-hermetic
        # operation-trigger unit test spray op-1 "test" events onto the live bus
        # every suite run. Routed through the shared _guard_bus_write helper
        # (e-5300) so all bus-write verbs share one guard call. No-op off test/prod.
        self._guard_bus_write()
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
        # ms-90 / e-3246: decision-event の背景・判断理由。空なら送らない
        # (= 古いサーバは無視、新しいサーバのみ decision_events に記録する)。
        if context:
            body["context"] = context
        if rationale:
            body["rationale"] = rationale
        # ms-110 / e-4001: idempotent send. Generate a stable key for this
        # logical send (once) and, on an *ambiguous* failure (timeout /
        # disconnect — we don't know whether the server processed it), retry
        # with the SAME key + is_retry so the server dedups instead of double-
        # delivering. HTTPError (a definite server response like 403/500) is
        # NOT ambiguous, so it is not retried. Older servers ignore the extra
        # body fields, so this degrades to a plain single POST.
        import secrets as _secrets
        import time as _time
        key = client_event_id or f"ce-{int(_time.time())}-{_secrets.token_hex(4)}"
        body["client_event_id"] = key
        # ms-128 / e-4289: a caller that re-runs `beacon bus send` after an
        # *ambiguous* failure (the CLI process died / was killed after the POST
        # may have landed) can pass the SAME client_event_id + is_retry so the
        # server dedups (returns the stored event as idempotent_replay) instead
        # of creating a true duplicate. Within a single process the transport
        # retry below sets is_retry itself; this seeds it for the cross-
        # invocation case where the first attempt left no parseable output.
        if is_retry:
            body["is_retry"] = True
        path = f"/api/projects/{project_id}/bus"
        attempt = 0
        while True:
            try:
                resp = self.post(path, body)
                # ms-128 / e-4289 (AX review PR#545): guarantee the response
                # carries the client-minted key so a caller can ALWAYS read it
                # back (from `--json`) to drive a later idempotent --retry. Do
                # not depend on the server echoing it — stamp it client-side.
                if isinstance(resp, dict):
                    resp.setdefault("client_event_id", key)
                return resp
            except (ConnectionError, TimeoutError):
                # Ambiguous transport failure: the request may or may not have
                # landed. ``post`` wraps most url/socket errors as
                # ConnectionError, but a bare socket timeout during read can
                # surface as TimeoutError — both are ambiguous, so retry the
                # same key with is_retry so a landed-but-unacked send is deduped.
                # (A definite server response raises RuntimeError, which is NOT
                # caught here — it is not ambiguous and must not be resent.)
                attempt += 1
                if attempt > max_retries:
                    raise
                body["is_retry"] = True
                _time.sleep(min(2 ** attempt, 4))

    def issue_bus_envelope(self, project_id: str, *, tier: str,
                           actions_authorized: list[str] | None = None,
                           scope: str | None = None,
                           data_class: str = "free",
                           conversation_id: str | None = None,
                           in_reply_to: str | None = None,
                           chain_depth: int = 0,
                           ttl_seconds: int = 3600,
                           consent_claim: dict | None = None) -> dict:
        """Mint a server-signed bus envelope (ms-54 / e-1155 Phase 1).

        The returned dict is the full envelope (including ``signature``);
        callers embed it as the ``envelope`` field on a subsequent
        ``post_bus_event`` call.

        ms-110 / e-3443: ``consent_claim`` (the ``recipient_confirmed``
        cross-user consent claim) is passed to the mint endpoint so the
        server signs the envelope *with* the claim included. This keeps the
        claim authentic (covered by the server HMAC) and lets it survive the
        receive-time verify pipeline. It must NOT be grafted onto the returned
        envelope afterward — that invalidates the signature (the original bug).
        """
        # ms-108 e-5216: a bus WRITE choke point, same as post_bus_event — minting an
        # envelope against prod from a test context is a prod write. Shared helper
        # (e-5300) so guarding does not leave sibling bus-write verbs as bypass doors.
        self._guard_bus_write()
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
        if consent_claim is not None:
            body["recipient_confirmed"] = consent_claim
        return self.post(f"/api/projects/{project_id}/bus/envelope/issue", body)

    def archive_project(self, project_id: str, envelope: dict) -> dict:
        """Archive (soft-delete) a project, presenting a signed envelope.

        ms-123 / e-4028. ``POST /api/projects/{id}/archive`` is gated by
        ``require_envelope_for_action("project.archive")`` — owner-only PLUS a
        verified envelope whose ``actions_authorized`` includes
        ``project.archive``. The envelope must be minted for THIS project
        (via ``issue_bus_envelope``) because the server verify pipeline binds
        it to ``project_id``. It travels in the ``X-Beacon-Envelope`` header
        as base64-encoded JSON (the canonical header transport).
        """
        import base64 as _b64
        env_b64 = _b64.b64encode(
            json.dumps(envelope).encode("utf-8")
        ).decode("ascii")
        return self.post(
            f"/api/projects/{project_id}/archive",
            None,
            extra_headers={"X-Beacon-Envelope": env_b64},
        )

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
        # ms-108 e-5216: advancing a cursor is a prod bus WRITE — a mis-mocked test
        # could move a real recipient's cursor and make a live session silently skip
        # events. Shared guard helper (e-5300); no-op off test/prod.
        self._guard_bus_write()
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
        # ms-108 e-5216: stamping a receipt is a prod bus WRITE — shared guard helper
        # (e-5300); no-op off test context / non-prod targets.
        self._guard_bus_write()
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
        # ms-108 e-5216: recording an approve/deny decision is a prod bus WRITE —
        # shared guard helper (e-5300); no-op off test/prod.
        self._guard_bus_write()
        return self.post(
            f"/api/projects/{project_id}/dm/approval/"
            f"{urllib.parse.quote(event_id)}",
            {"decision": decision},
        )

    # ms-151 / e-5474: machine API key (headless machine 認証の鍵) の管理。
    # owner だけが叩ける project-scoped endpoint を薄くラップする。issue の返り
    # ``key`` は発行時のみ得られる raw token (以後サーバーは hash のみ)。

    def issue_machine_key(self, project_id: str, *, label: str = "") -> dict:
        """発行 (owner 限定)。返り ``{"key": raw, "machine_key": redacted}``。"""
        return self.post(
            f"/api/projects/{project_id}/machine-keys",
            {"label": label},
        )

    def list_machine_keys(self, project_id: str) -> dict:
        """一覧 (owner 限定)。返り ``{"machine_keys": [redacted, ...]}``。"""
        return self.get(f"/api/projects/{project_id}/machine-keys")

    def revoke_machine_key(self, project_id: str, key_id: str) -> dict:
        """失効 (owner 限定)。未存在は 404。返り ``{"machine_key": redacted}``。"""
        return self.delete(
            f"/api/projects/{project_id}/machine-keys/"
            f"{urllib.parse.quote(key_id)}"
        )

    # ms-54 / e-1369 Layer 4: AI-authored intent. Set via `beacon session
    # focus "<text>"` / `beacon session attention --set true`. Read by the
    # /beacon-dm-send picker so a sender sees "what is each session doing".

    def upsert_session_intent(self, project_id: str, session_id: str, *,
                              text: Optional[str] = None,
                              attention_required: Optional[bool] = None,
                              working_target: Optional[dict] = None) -> dict:
        body: dict = {}
        if text is not None:
            body["text"] = text
        if attention_required is not None:
            body["attention_required"] = attention_required
        # ms-159 / e-6291 — the session's declaration of which target it works on
        # ({root, target:{kind,id,title}}), riding the same intent doc as text.
        if working_target is not None:
            body["working_target"] = working_target
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
    # ms-185 e-7322 / e-7327: ここに Trek の HTTP クライアント読み出し 2 つ
    # (``list_treks`` / ``get_trek``) が在った。e-7071 の注記は「残したのは読み出し
    # 2 つだけ」と書いていたが、``get_trek`` は同じコミットで唯一の呼び出し元
    # (``store_api.StoreApi.get_trek``) も消しており呼び出し元ゼロ、``list_treks`` の
    # 唯一の呼び出し元 ``StoreApi.list_treks`` も e-7322 で落ちた。32 メソッドに適用
    # した「呼び出し元ゼロなら落とす」をこの 2 つにも適用する。サーバ側の HTTP
    # ルートは残っており (tests/test_trek_record_read_surface_e7081.py が固定)、
    # 読みたければ curl / ブラウザから引ける。
    def create_org(self, *, name: str) -> dict:
        """Create a team org. Caller (= auth token's user) becomes owner.

        Creator identity is resolved server-side from the Bearer token, so
        only ``name`` is sent (= client can't spoof the owner)。
        """
        return self.post("/api/orgs", {"name": name})

    def list_orgs(self) -> list:
        """List orgs the caller is a member of (= server filters by token)."""
        return self.get("/api/orgs")

    def get_org(self, org_id: str) -> dict:
        """Fetch a single org by id. 404 if caller is not a member (= 存在を漏らさない)."""
        return self.get(f"/api/orgs/{urllib.parse.quote(org_id, safe='')}")

    def add_org_member(self, org_id: str, *, email: str,
                       role: str = "member") -> dict:
        """Add a member into an org (participation-only). Caller must be a member."""
        return self.post(
            f"/api/orgs/{urllib.parse.quote(org_id, safe='')}/members",
            {"email": email, "role": role})

    def remove_org_member(self, org_id: str, *, target: str) -> dict:
        """Remove a member (user_id or email) from an org. Owner/admin only server-side."""
        return self.delete(
            f"/api/orgs/{urllib.parse.quote(org_id, safe='')}/members/"
            f"{urllib.parse.quote(target, safe='')}")

    def delete_org(self, org_id: str) -> dict:
        """Delete a team org (ms-118 / e-4234). Owner-only server-side.

        personal org は削除不可 (400)、owner でない caller は 403。
        """
        return self.delete(
            f"/api/orgs/{urllib.parse.quote(org_id, safe='')}")

    def rehome_project(self, project_id: str, *, target_org_id: str) -> dict:
        """Re-home a project into an org (ms-118 / e-4233). Project-owner-only server-side.

        Only the project's ``org_id`` link is rewritten; identity and history
        are preserved and disclosure re-evaluates live against the new org.
        """
        return self.post(
            f"/api/projects/{urllib.parse.quote(project_id, safe='')}/rehome",
            {"org_id": target_org_id})

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
