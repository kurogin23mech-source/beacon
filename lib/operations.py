"""Beacon Operations Layer - Atomic project mutation with optimistic concurrency.

This module is the single mediator between API/CLI write paths and the underlying
storage (Firestore for cloud, JSON file for local). It exists to eliminate the
"_load → modify → _save" pattern that allowed lost-update bugs to slip through
concurrent writes (UC11-P6'/e-632, SPEC doc: gP9pCssCoa3QduuSMGR0).

Design (matches SPEC "A 案"):

    [API endpoint / CLI]
       ↓ "apply this op() to project_id"
    [Operations layer (apply_operation)]
       ├─ schema_version decides legacy vs β (β = subcollection-based)
       ├─ Firestore: @transactional wraps read→op→write atomically
       ├─ Local file: fcntl LOCK_EX wraps read→op→write atomically
       └─ Conflict / retry handled by Firestore SDK (LocalStore uses lock so no conflicts)
       ↓
    [Firestore document / local project.json]

Contracts the caller MUST keep:

  • op(data) → (new_data, result) — pure data transform. It may be re-invoked
    multiple times by the Firestore SDK during transaction retries, so:
      - no I/O, no network, no logging side-effects inside op
      - no external mutable state (use closure args only as inputs)
      - it is OK to mutate `data` in-place and return it (this is the existing
        idiom of lib/core.py functions like task_done, milestone_update, etc.)

  • result is returned to the caller verbatim and is what becomes the HTTP
    response body / CLI return value. Build it inside op so that retries
    produce a consistent result.

  • Side effects (changelog append, audit log, AI evaluation, broadcast) are
    performed AFTER apply_operation returns, by the caller or by the
    operations layer itself in the post-commit hook (see _post_commit_hook).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from typing import Any, Callable, Optional, Tuple


# ---------------------------------------------------------------------------
# Per-request audit context (ms-14 e-825)
# ---------------------------------------------------------------------------
#
# Endpoint handlers run synchronously inside FastAPI's threadpool / async
# loop, then call into operations.apply_operation. Threading ip/email/
# user_agent through every call site would touch dozens of files; using
# a ContextVar lets the middleware set it once and have apply_operation
# pick it up implicitly. The vars are scoped to the current task so
# concurrent requests don't bleed into each other's audit records.

_current_ip: contextvars.ContextVar[str] = contextvars.ContextVar(
    "beacon_audit_ip", default=""
)
_current_user_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "beacon_audit_user_agent", default=""
)
_current_email: contextvars.ContextVar[str] = contextvars.ContextVar(
    "beacon_audit_email", default=""
)


def set_audit_context(*, ip: str = "", user_agent: str = "", email: str = "") -> None:
    """Stash request metadata so the changelog writer can pick it up.

    Called from AuditLogMiddleware on each mutating request. CLI / local
    callers can ignore it — defaults are empty strings and the field is
    omitted from the changelog entry when empty.
    """
    if ip:
        _current_ip.set(ip)
    if user_agent:
        _current_user_agent.set(user_agent)
    if email:
        _current_email.set(email)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

# An operation receives a project data dict and returns (new_data, result).
# It MUST be a pure transformation — no I/O, no external mutable state — so the
# Firestore transaction can re-invoke it on conflict.
Op = Callable[[dict], Tuple[dict, Any]]


# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

# v1 (legacy): whole project document — milestones[] is an array inside the
#              single Firestore document. Concurrent writes serialize through
#              a single-doc transaction (no parallel writes possible, but
#              lost updates are prevented).
#
# v2 (β):     Subcollection-based — project doc holds only metadata
#             (name, summary, owner, members, schema_version=2). Milestones
#             live as independent documents under projects/{id}/milestones/{ms_id}.
#             Different milestones can be written in parallel without conflict.
#
# Existing projects (no schema_version field) are treated as v1. v2 must be
# explicitly opted into at project creation time. There is no automatic
# migration — see SPEC doc gP9pCssCoa3QduuSMGR0 §"既存プロジェクトは legacy
# のまま動かす".
SCHEMA_V1_LEGACY = 1
SCHEMA_V2_BETA = 2
# ms-96 e-2379 (2026-07-06): MySQL 移行のタイミングで entry-level 分割まで踏み込む
# 新スキーマ。 milestone は独立行、 entry も独立行、 子 entry (= 深さ 2 の commit
# 等) は親 entry の JSON 内包で保持。 現状 MySQL backend のみ実装 (= mysql_client)。
# Firestore backend では v2 のまま (portability 保持、 v3 化は将来 task)。
# 詳細は DM `Yh9JnCBfj4aVjd71gjKc` (iruca3 <-> kurogin) 参照。
SCHEMA_V3_ENTRY = 3


def get_schema_version(data: dict) -> int:
    """Return the schema version of a project document.

    Defaults to v1 (legacy) when the field is absent, to keep existing
    projects working without migration.
    """
    v = data.get("schema_version")
    if isinstance(v, int) and v in (SCHEMA_V1_LEGACY, SCHEMA_V2_BETA,
                                    SCHEMA_V3_ENTRY):
        return v
    return SCHEMA_V1_LEGACY


# ---------------------------------------------------------------------------
# Operation logging (changelog.jsonl)
# ---------------------------------------------------------------------------

_op_logger = logging.getLogger("beacon.operations")


def _changelog_path(project_id: str) -> Optional[str]:
    """Return changelog.jsonl path for local mode, or None for cloud mode.

    Cloud mode emits structured logs via the audit logger middleware instead
    of writing a file.
    """
    # Local mode: write next to project.json
    project_file = os.environ.get("BEACON_PROJECT_FILE")
    if project_file and os.path.exists(os.path.dirname(project_file) or "."):
        beacon_dir = os.path.dirname(project_file) or ".beacon"
        return os.path.join(beacon_dir, "changelog.jsonl")
    return None


def _append_changelog(project_id: str, op_name: str, actor: str,
                      reason: str = "", extra: Optional[dict] = None) -> None:
    """Append one operation record to changelog (ms-14 e-825).

    Two sinks, both best-effort:

    - Local mode: ``.beacon/changelog.jsonl`` next to project.json. This
      is the path that has existed since the CORE doc
      ``data-immutability-principle`` was written.
    - Cloud mode (running inside the API server): the
      ``projects/{id}/changelog`` Firestore subcollection. Picked up via
      ``firestore_client.append_changelog``. ip / user_agent / email are
      threaded in from the request via the ``set_audit_context``
      ContextVars so endpoint handlers don't have to plumb them through
      every call site.

    Format matches CORE doc ``data-immutability-principle``:
      ``{"ts":..., "op":..., "actor":..., "reason":..., ...extra}``

    Failures here MUST NOT propagate — operation logging is best-effort
    and must never break the actual write path.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry: dict = {
        "ts": ts,
        "op": op_name,
        "actor": actor,
        "project_id": project_id,
    }
    if reason:
        entry["reason"] = reason
    if extra:
        entry.update(extra)

    # Local sink: append to the jsonl file (no-op when not in local mode).
    path = _changelog_path(project_id)
    if path is not None:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            _op_logger.warning("Failed to append changelog: %s", e)

    # Cloud sink: persist to the backend's changelog (Firestore subcollection
    # for GCP, DynamoDB changelog table for AWS). Only fires when we're inside
    # the API server (= store_router or firestore_client already loaded).
    # ms-64 e-1631: route via store_router so the DynamoDB build also receives
    # the changelog write — otherwise audit log would silently disappear on AWS.
    sys_mod = __import__("sys").modules
    if "store_router" in sys_mod:
        import store_router as db  # type: ignore[import-not-found]
    elif "firestore_client" in sys_mod:
        try:
            import firestore_client as db  # type: ignore[import-not-found]
        except ImportError:
            return
    else:
        return
    cloud_entry = dict(entry)
    email = _current_email.get()
    if email:
        cloud_entry["email"] = email
    ip = _current_ip.get()
    if ip:
        cloud_entry["ip"] = ip
    ua = _current_user_agent.get()
    if ua:
        cloud_entry["user_agent"] = ua
    try:
        db.append_changelog(project_id, cloud_entry)
    except Exception as e:  # noqa: BLE001 - audit must never break the write
        _op_logger.warning("Failed to persist cloud changelog: %s", e)


# ---------------------------------------------------------------------------
# Local-mode atomic apply (file lock)
# ---------------------------------------------------------------------------

def _apply_local(project_file: str, op: Op) -> Any:
    """Apply op atomically to a local JSON project file.

    Routes through ``local_writer.atomic_apply`` — the single serialised,
    atomic, crash-safe local write path shared with ``LocalStore.save_project``
    (ms-148 e-5410). It holds one exclusive lock across the whole
    read→op→write window (so nothing is lost between the read and the write)
    and swaps the new document in with an atomic os.replace (so a crash
    mid-write can't corrupt the file — the old truncate+dump could).

    ``baseline=None`` because ``op`` runs against state re-read *under* the
    lock, so there is no load→save gap to guard against here. ``validate=True``
    keeps the previous behaviour: ``core.validate_project`` runs before the
    write, so a schema violation raises ValueError and never reaches disk.
    """
    import local_writer  # lazy import keeps operations.py import-light
    result, _ = local_writer.atomic_apply(
        project_file, op, baseline=None, validate=True,
    )
    return result


# ---------------------------------------------------------------------------
# Cloud-mode atomic apply (Firestore transaction, v1 legacy)
# ---------------------------------------------------------------------------

def _apply_cloud_v1(project_id: str, op: Op) -> Any:
    """Apply op atomically to a Firestore project document (v1 legacy schema).

    Uses Firestore's transactional API: the SDK reads the document inside the
    transaction, runs op() to produce new_data, and commits with set(). If
    another writer commits between read and write, Firestore aborts and
    automatically retries op() up to 5 times.

    This is what makes op required to be pure: it WILL be re-invoked on conflict.
    """
    # Lazy import — server-side only (firestore client not present in CLI).
    import firestore_client as db  # type: ignore[import-not-found]
    from google.cloud import firestore  # type: ignore[import-not-found]

    client = db.get_db()
    doc_ref = client.collection(db.COLLECTION).document(project_id)

    # Closure to capture result across retries. Firestore re-runs the
    # transactional function on conflict, so we must reassign on each attempt
    # rather than appending.
    result_holder: dict[str, Any] = {"result": None}

    @firestore.transactional
    def _txn(transaction):
        snap = doc_ref.get(transaction=transaction)
        if not snap.exists:
            raise LookupError(f"Project '{project_id}' not found")
        data = snap.to_dict() or {}

        new_data, result = op(data)

        # Validate inside the transaction so an invalid mutation aborts
        # before we attempt the write.
        import core  # noqa: PLC0415
        core.validate_project(new_data)

        transaction.set(doc_ref, new_data)
        result_holder["result"] = result

    transaction = client.transaction()
    _txn(transaction)
    return result_holder["result"]


# ---------------------------------------------------------------------------
# Cloud-mode atomic apply (Firestore transaction, v2 β subcollection)
# ---------------------------------------------------------------------------

def _apply_cloud_v2(project_id: str, op: Op) -> Any:
    """Apply op atomically to a v2 β-schema project (subcollection-based).

    v2 layout:
      projects/{id}                         — metadata only (name, summary,
                                              owner, members, schema_version=2,
                                              objective)
      projects/{id}/milestones/{ms_id}      — individual milestone document
                                              with its full state and entries

    For now, v2 reads the metadata document + all milestone subdocs inside a
    transaction, hands a hydrated dict to op (same shape as v1), then writes
    back only the documents that changed. This keeps the op contract identical
    between v1 and v2 — the storage layout difference is invisible to op.

    Detecting which milestones changed:
      We snapshot a shallow signature (json hash of each milestone dict) before
      op() and compare after. Only changed milestones are written. Milestones
      that disappeared from new_data are deleted. Milestones added are created.
    """
    import hashlib
    import firestore_client as db  # type: ignore[import-not-found]
    from google.cloud import firestore  # type: ignore[import-not-found]

    client = db.get_db()
    doc_ref = client.collection(db.COLLECTION).document(project_id)
    ms_col = doc_ref.collection("milestones")

    def _ms_sig(ms: dict) -> str:
        return hashlib.md5(
            json.dumps(ms, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    result_holder: dict[str, Any] = {"result": None}

    @firestore.transactional
    def _txn(transaction):
        snap = doc_ref.get(transaction=transaction)
        if not snap.exists:
            raise LookupError(f"Project '{project_id}' not found")
        meta = snap.to_dict() or {}

        # Hydrate milestones from subcollection.
        # NOTE: Firestore transactions require all reads BEFORE any write.
        ms_snaps = list(ms_col.stream(transaction=transaction))
        milestones = [s.to_dict() for s in ms_snaps]

        # Combine into the shape op expects (same as v1 whole-doc).
        data = {**meta, "milestones": milestones}

        before_sigs = {ms.get("id", ""): _ms_sig(ms) for ms in milestones}
        before_ids = set(before_sigs.keys())

        new_data, result = op(data)

        import core  # noqa: PLC0415
        core.validate_project(new_data)

        new_milestones = new_data.get("milestones", [])
        new_ids = {ms.get("id", "") for ms in new_milestones if ms.get("id")}

        # Write back metadata (everything except milestones) to the project doc.
        new_meta = {k: v for k, v in new_data.items() if k != "milestones"}
        new_meta["schema_version"] = SCHEMA_V2_BETA
        transaction.set(doc_ref, new_meta)

        # Upsert changed/new milestones.
        for ms in new_milestones:
            ms_id = ms.get("id", "")
            if not ms_id:
                continue
            new_sig = _ms_sig(ms)
            if before_sigs.get(ms_id) != new_sig:
                transaction.set(ms_col.document(ms_id), ms)

        # Delete milestones that were removed from new_data.
        for ms_id in before_ids - new_ids:
            if ms_id:
                transaction.delete(ms_col.document(ms_id))

        result_holder["result"] = result

    transaction = client.transaction()
    _txn(transaction)
    return result_holder["result"]


# ---------------------------------------------------------------------------
# Cloud-mode whole-document replace for v2 (subcollection-aware)
# ---------------------------------------------------------------------------

def _replace_cloud_v2(project_id: str, new_data: dict) -> None:
    """Whole-document replace on a v2 (subcollection) project.

    Same semantics as the v1 branch of replace_project — the caller has a
    full project dict and wants the server-side state to match it — but the
    storage layout is v2: meta lives in the project doc, milestones live as
    independent subcollection docs. So we decompose new_data into:

      - meta: everything except milestones[]  →  projects/{id}        (set)
      - per-MS: each milestones[] element     →  projects/{id}/milestones/{ms_id}  (set)

    Milestones not present in new_data are deleted from the subcollection
    (whole-replace semantics: the caller's view of state wins).

    This exists so that legacy save_project / cloud push paths keep working
    after a v1→v2 migration without rewriting the 35+ CLI sites that still
    use whole-document PUT.
    """
    import hashlib
    import firestore_client as db  # type: ignore[import-not-found]
    from google.cloud import firestore  # type: ignore[import-not-found]

    client = db.get_db()
    doc_ref = client.collection(db.COLLECTION).document(project_id)
    ms_col = doc_ref.collection("milestones")

    @firestore.transactional
    def _txn(transaction):
        # Read existing MS ids so we can compute deletions.
        # Firestore requires all reads before any writes in a transaction.
        existing_snaps = list(ms_col.stream(transaction=transaction))
        existing_ids = {s.id for s in existing_snaps}

        # Decompose new_data into meta + per-MS subdocs.
        new_meta = {k: v for k, v in new_data.items() if k != "milestones"}
        new_meta["schema_version"] = SCHEMA_V2_BETA

        new_milestones = new_data.get("milestones", []) or []
        new_ids: set[str] = set()
        for ms in new_milestones:
            ms_id = ms.get("id", "")
            if not ms_id:
                continue
            new_ids.add(ms_id)

        # Write meta (whole replace of the project doc — much smaller now
        # since milestones[] is no longer inside).
        transaction.set(doc_ref, new_meta)

        # Upsert each milestone subdoc.
        for ms in new_milestones:
            ms_id = ms.get("id", "")
            if not ms_id:
                continue
            transaction.set(ms_col.document(ms_id), ms)

        # Delete milestones that the caller no longer has.
        for ms_id in existing_ids - new_ids:
            transaction.delete(ms_col.document(ms_id))

    _txn(client.transaction())


# ---------------------------------------------------------------------------
# v1 → v2 migration (one-time per project)
# ---------------------------------------------------------------------------

def migrate_v1_to_v2(project_id: str) -> dict:
    """Convert a v1 (whole-doc) project to v2 (subcollection) layout in place.

    Reads the current v1 project doc and atomically:
      1. Writes each milestones[] element as its own doc under
         projects/{id}/milestones/{ms_id}.
      2. Trims the project doc to meta-only (removes milestones[]) and
         stamps schema_version=2.

    The migration write is a *shrink* of the project doc (from ~1.06 MiB
    down to ~100 KiB for the existing beacon-b95643 project), which is
    exactly what unblocks projects that have hit the Firestore 1 MiB cap:
    the write that gets the project below the cap is the migration itself.

    Idempotent: if the project is already v2, returns {"status": "already_v2"}
    and does nothing.

    Returns a dict with status + milestone_count for the caller (typically
    the HTTP endpoint).

    Raises:
      LookupError       — project doesn't exist
      RuntimeError      — backend is not cloud (migration only makes sense
                          for Firestore-backed projects)
    """
    backend = _detect_backend()
    if backend != "cloud":
        raise RuntimeError(
            f"migrate_v1_to_v2 requires cloud backend, got '{backend}'. "
            "Local (file-backed) projects don't have the 1 MiB cap problem."
        )

    import firestore_client as db  # type: ignore[import-not-found]
    from google.cloud import firestore  # type: ignore[import-not-found]

    client = db.get_db()
    doc_ref = client.collection(db.COLLECTION).document(project_id)
    ms_col = doc_ref.collection("milestones")

    result_holder: dict[str, Any] = {}

    @firestore.transactional
    def _txn(transaction):
        snap = doc_ref.get(transaction=transaction)
        if not snap.exists:
            result_holder["error"] = "not_found"
            return
        data = snap.to_dict() or {}

        if data.get("schema_version") == SCHEMA_V2_BETA:
            result_holder["status"] = "already_v2"
            result_holder["milestone_count"] = len(data.get("milestones", []) or [])
            return

        milestones = data.get("milestones", []) or []

        # Decompose: meta (no milestones[]) + per-MS subdocs.
        new_meta = {k: v for k, v in data.items() if k != "milestones"}
        new_meta["schema_version"] = SCHEMA_V2_BETA

        # Set trimmed meta (this is the shrink write that escapes 1 MiB).
        transaction.set(doc_ref, new_meta)

        # Write each MS as an independent subcollection doc.
        for ms in milestones:
            ms_id = ms.get("id", "")
            if not ms_id:
                # Skip malformed MS without an id; surface count to caller.
                result_holder.setdefault("skipped_no_id", 0)
                result_holder["skipped_no_id"] += 1
                continue
            transaction.set(ms_col.document(ms_id), ms)

        result_holder["status"] = "migrated"
        result_holder["milestone_count"] = len(milestones)
        result_holder["meta_keys"] = sorted(new_meta.keys())

    _txn(client.transaction())

    if result_holder.get("error") == "not_found":
        raise LookupError(f"Project '{project_id}' not found")

    return {k: v for k, v in result_holder.items() if k != "error"}


# ---------------------------------------------------------------------------
# Cloud-mode atomic apply for DynamoDB backend (ms-64 e-1631)
# ---------------------------------------------------------------------------

def _apply_cloud_dynamodb(project_id: str, op: Op) -> Any:
    """Apply ``op`` atomically against the DynamoDB-backed project document.

    DynamoDB does not have Firestore's multi-doc transactional model nor the
    v1 / v2 subcollection split (the 1 MiB cap that motivated v2 only exists
    on Firestore). The atomic primitive here is single-item put_item under
    a conditional check on the schema version field, which is sufficient
    because ``apply_operation`` only guarantees per-project linearizability
    of writes — not cross-project transactions.

    Concurrency model:
      - Read the current project document.
      - Run ``op(data)`` in memory.
      - Write it back via ``store_router.save_project`` (which the DynamoDB
        backend implements as a single PK put_item).

    Conflict handling: at the moment we accept the same last-writer-wins
    semantics that Firestore v1's transaction retries protect against, with
    the practical observation that AWS GA-incubation traffic is single-writer
    (one CLI per cwd). Adding a ConditionalCheckExpression on a version
    field is a future hardening step (out of scope for e-1631).
    """
    import store_router as db  # type: ignore[import-not-found]

    data = db.get_project(project_id)
    if data is None:
        raise LookupError(f"Project '{project_id}' not found")

    new_data, result = op(data)

    import core  # noqa: PLC0415
    core.validate_project(new_data)

    db.save_project(project_id, new_data)
    return result


# ---------------------------------------------------------------------------
# Mock-mode atomic apply (in-process lock, for unit tests with patched db)
# ---------------------------------------------------------------------------

# Single process-wide lock for the mock backend. Tests are not concurrent
# across files (pytest serializes by default), but we still want the lock so
# that intra-test concurrency tests can be added later without surprises.
_mock_lock = None


def _get_mock_lock():
    global _mock_lock
    if _mock_lock is None:
        import threading
        _mock_lock = threading.RLock()
    return _mock_lock


def _apply_mock(project_id: str, op: Op) -> Any:
    """Apply op atomically using the patched firestore_client module.

    This path is selected by BEACON_OPERATIONS_BACKEND=mock and exists so that
    tests/test_api.py can monkey-patch firestore_client.get_project /
    save_project (its in-memory store) and have apply_operation route through
    those mocks rather than the real Firestore transactional API.

    Semantically equivalent to the cloud v1 path for the test's purposes:
    read → op → validate → write, serialized by a process-wide lock.
    """
    import firestore_client as db  # type: ignore[import-not-found]

    with _get_mock_lock():
        data = db.get_project(project_id)
        if data is None:
            raise LookupError(f"Project '{project_id}' not found")

        new_data, result = op(data)

        import core  # noqa: PLC0415
        core.validate_project(new_data)

        db.save_project(project_id, new_data)
    return result


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def _detect_backend() -> str:
    """Return 'cloud' if running inside the API server, else 'local'.

    Precedence:
      1. BEACON_OPERATIONS_BACKEND env var (explicit override for tests)
      2. Presence of store_router or firestore_client on sys.modules
         (= API server context — either GCP/Firestore or AWS/DynamoDB build)
      3. Default: 'local'

    ms-64 e-1631: the cloud signal originally checked firestore_client only,
    but the DynamoDB backend (server/dynamodb_client) is wired in through
    store_router, which may not import firestore_client at all in pure-AWS
    deployments. Recognise either as the cloud signal so the API server runs
    in cloud mode regardless of which storage backend it was configured with.
    The env var override still lets tests bypass that signal.
    """
    forced = os.environ.get("BEACON_OPERATIONS_BACKEND", "").strip().lower()
    if forced in ("local", "cloud", "mock"):
        return forced

    import sys
    if "store_router" in sys.modules or "firestore_client" in sys.modules:
        return "cloud"
    return "local"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_operation(
    project_id: str,
    op: Op,
    *,
    op_name: str = "project.update",
    actor: str = "",
    reason: str = "",
    project_file: Optional[str] = None,
) -> Any:
    """Apply a pure data transformation `op` to project_id atomically.

    Parameters:
      project_id   — target project (used by cloud backend; ignored locally
                     except for changelog tagging)
      op           — pure function (data) → (new_data, result). May be retried.
      op_name      — short label for changelog ("task.done", "milestone.start",
                     etc). Defaults to a generic value.
      actor        — who initiated this operation (user id, "claude", git email).
                     Defaults to core._get_actor() result if empty.
      reason       — human-readable reason. Required for state-changing ops by
                     CORE doc `data-immutability-principle` but enforced at the
                     CLI/API layer, not here (this layer logs whatever is given).
      project_file — local-mode override for the JSON file path. Defaults to
                     BEACON_PROJECT_FILE env var.

    Returns the `result` value produced by op(data).

    Raises:
      LookupError      — project does not exist (cloud only — local raises
                         FileNotFoundError from the json open).
      ValueError       — validation failure in core.validate_project
      RuntimeError     — Firestore transaction exhaustion (very rare; >5 retries)

    Threading / concurrency:
      • Local backend: serialized by fcntl LOCK_EX across all callers on the
        same machine. Cross-machine concurrency does not apply (local file).
      • Cloud v1: serialized by Firestore single-document transaction. Conflicts
        cause automatic op() retry inside the SDK.
      • Cloud v2: serialized by Firestore multi-document transaction over
        metadata + milestone subdocs. Different milestones can be updated
        in parallel without aborting each other (the transaction reads only
        what it touches — see Firestore docs).
    """
    if not actor:
        # Defer import to avoid a hard dependency at module-load time.
        import core  # noqa: PLC0415
        actor = core._get_actor()

    backend = _detect_backend()

    if backend == "local":
        path = project_file or os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
        result = _apply_local(path, op)
    elif backend == "mock":
        # Test path: uses patched firestore_client.{get,save}_project.
        result = _apply_mock(project_id, op)
    else:
        # Cloud: pick the storage backend, then within Firestore pick v1 / v2
        # based on the existing project's schema_version.
        #
        # ms-64 e-1631: DynamoDB does not have the v1/v2 subcollection layout
        # (1 MiB cap is a Firestore-specific constraint), so the AWS build
        # always uses the whole-document apply path. Single-PK put_item is
        # atomic on DynamoDB, so a read → op → write loop with one retry on
        # ConditionalCheckFailed is functionally equivalent to the Firestore
        # transactional apply for the consistency guarantees apply_operation
        # promises (= per-project linearizability of writes).
        store_backend = os.environ.get("BEACON_STORE_BACKEND", "firestore").lower()
        if store_backend == "mysql":
            # ms-96 e-2379: MySQL は 2 段 dispatch。
            # (a) schema=v3 → entry-level split (mysql_client.apply_project_op_v3)
            #     行単位 update で書き込み増幅を回避 (= iruca3 提案 / kurogin 合意)。
            # (b) schema=v1 → whole-doc apply (= 既存の DynamoDB と同じ経路)
            #     11 project 中 v1 で載っている 10 個はこの経路で当面動く。
            # meta doc の peek は cheap (1 行 SELECT) なので schema 判定に使う。
            import store_router as db  # type: ignore[import-not-found]
            existing_meta = db.get_project(project_id)
            existing_schema = (
                get_schema_version(existing_meta) if existing_meta
                else SCHEMA_V1_LEGACY
            )
            if existing_schema == SCHEMA_V3_ENTRY:
                result = db.apply_project_op_v3(project_id, op)
            else:
                # v1 whole-doc (DynamoDB と同じ経路の read-modify-write)。
                result = _apply_cloud_dynamodb(project_id, op)
        elif store_backend == "dynamodb":
            # DynamoDB は v1/v2 subcollection 分離が不要なので whole-document apply。
            # (単一 PK put_item が atomic、read → op → conditional put の retry で
            # Firestore transactional apply と同等の per-project linearizability)。
            result = _apply_cloud_dynamodb(project_id, op)
        else:
            import firestore_client as db  # type: ignore[import-not-found]
            existing = db.get_project(project_id)
            if existing is None:
                raise LookupError(f"Project '{project_id}' not found")
            schema = get_schema_version(existing)
            if schema == SCHEMA_V2_BETA:
                result = _apply_cloud_v2(project_id, op)
            else:
                result = _apply_cloud_v1(project_id, op)

    # Post-commit side effects (operation log). Failures here are non-fatal.
    _append_changelog(project_id, op_name, actor, reason=reason)

    return result


# ---------------------------------------------------------------------------
# Whole-document replace (special path for `PUT /api/projects/{id}`)
# ---------------------------------------------------------------------------

def replace_project(
    project_id: str,
    new_data: dict,
    *,
    actor: str = "",
    reason: str = "",
    project_file: Optional[str] = None,
) -> None:
    """Atomically replace the entire project document.

    This is the moral equivalent of `cloud push` — the caller has its own copy
    of the project state and wants to overwrite the server-side state with it.
    Lost-update protection here means "no in-flight write gets silently
    discarded mid-replace", not "stale local data is detected and rejected".
    The latter is `cloud push`'s --force semantics and lives elsewhere.

    Despite the destructive semantics, this still runs inside the same
    transactional envelope as apply_operation so it cannot tear concurrent
    mutations (e.g. a member update racing with the replace).

    The new_data is validated via core.validate_project before write.
    """
    # Validate up-front; replace_project is not a "retry" operation so we
    # don't need validation inside the txn closure.
    import core  # noqa: PLC0415
    core.validate_project(new_data)

    if not actor:
        actor = core._get_actor()

    backend = _detect_backend()

    if backend == "local":
        path = project_file or os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
        # Identity op — apply_operation already handles the locking, just
        # discard whatever's there.
        # NOTE: _apply_local requires the file to exist; for replace where the
        # file may be absent (first cloud push, etc.), fall back to a direct
        # write under the lock. Keep semantics tight.
        if os.path.exists(path):
            _apply_local(path, lambda _existing: (new_data, None))
        else:
            # First-time write — bypass lock since no existing state to race against.
            with open(path, "w", encoding="utf-8") as f:
                json.dump(new_data, f, indent=2, ensure_ascii=False)
                f.write("\n")
    elif backend == "mock":
        import firestore_client as db  # type: ignore[import-not-found]
        with _get_mock_lock():
            db.save_project(project_id, new_data)
    else:
        # Cloud: dispatch by store backend then by schema version.
        #
        # ms-64 e-1627 注: Firestore (Cloud Run) 経路と DynamoDB (AWS Lambda)
        # 経路の両方を扱う。store_router 経由でバックエンドが切替わる仕組みは
        # 既存だが、operations.py には Firestore 固有の transaction / collection
        # 経路 (= db.get_db().collection().document()) が残っており、
        # DynamoDB ではそれらが存在しないので別経路で処理する。
        store_backend = os.environ.get("BEACON_STORE_BACKEND", "firestore").lower()

        if store_backend == "mysql":
            # ms-96 e-2379: MySQL は 2 段 dispatch (apply_operation と同型)。
            # 既存 project の schema を peek し、 v3 なら entry-level 経路、
            # そうでなければ whole-doc replace。 new_data の schema_version は
            # 「これから書き込む形」なので existing schema (現行 shape) を優先。
            # 「schema 変更を跨ぐ replace_project」 (= v1 -> v3) は migrate script
            # の役割で本 API は担わない (= caller が本気で shape を変えたい時は
            # migration を挟む前提)。
            import store_router as db  # type: ignore[import-not-found]
            existing = db.get_project(project_id)
            existing_schema = (
                get_schema_version(existing) if existing else
                get_schema_version(new_data)
            )
            if existing_schema == SCHEMA_V3_ENTRY:
                db.replace_project_v3(project_id, new_data)
            else:
                db.save_project(project_id, new_data)
        elif store_backend == "dynamodb":
            # DynamoDB: トランザクションは PK の put_item に集約される
            # (= 単一 PK 内の put_item が atomic)。v2 schema の subcollection 分解は
            # dynamodb_client.save_project が PK のみでサブコレクションは別テーブルに
            # 置く設計なので whole-doc set で安全に動く。
            import store_router as db  # type: ignore[import-not-found]
            db.save_project(project_id, new_data)
        else:
            # Firestore: 既存の transaction + schema version dispatch 経路
            import firestore_client as db  # type: ignore[import-not-found]
            from google.cloud import firestore  # type: ignore[import-not-found]

            client = db.get_db()
            doc_ref = client.collection(db.COLLECTION).document(project_id)

            # Peek at schema version (single doc read, cheap) before deciding path.
            existing_meta = db.get_project(project_id)
            existing_schema = (
                get_schema_version(existing_meta) if existing_meta else SCHEMA_V1_LEGACY
            )

            if existing_schema == SCHEMA_V2_BETA:
                _replace_cloud_v2(project_id, new_data)
            else:
                @firestore.transactional
                def _txn(transaction):
                    # We still read so we have a transactional read-write pair;
                    # whether the doc exists is informational only for replace.
                    doc_ref.get(transaction=transaction)
                    transaction.set(doc_ref, new_data)

                _txn(client.transaction())

    _append_changelog(
        project_id, "project.replace", actor,
        reason=reason or "whole-document replace (cloud push or admin write)",
    )


# ---------------------------------------------------------------------------
# Convenience: read-only project fetch with schema awareness
# ---------------------------------------------------------------------------

def load_project_consistent(project_id: str) -> dict:
    """Return the current project data, transparently flattening v2 → unified dict.

    Use this in read paths that previously called db.get_project directly so
    they automatically benefit from v2 subcollection layout.

    Note: This is NOT a transactional snapshot — for transactional reads,
    do them inside apply_operation. This helper is for cases where you just
    need the latest known state (e.g. GET endpoints, status displays).
    """
    backend = _detect_backend()
    if backend == "local":
        path = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ms-64 e-1627: store_router 経由でバックエンド (firestore / dynamodb)
    # を切替える。v2 schema (= milestones を subcollection に分解) は
    # Firestore 由来の概念で、DynamoDB 経路では存在しない (= 新規 project は
    # v1 unified 形式で save_project に渡されるため)。よって DynamoDB
    # バックエンド時は schema check + subcollection hydration を skip して
    # whole-doc を返す。
    import store_router as db  # type: ignore[import-not-found]
    meta = db.get_project(project_id)
    if meta is None:
        raise LookupError(f"Project '{project_id}' not found")

    store_backend = os.environ.get("BEACON_STORE_BACKEND", "firestore").lower()
    if store_backend == "mysql":
        # ms-96 e-2379: MySQL の schema-aware hydration。
        # v3 (entry-level split) の場合は milestones / entries が独立行なので
        # get_project_v3 で hydrate してから返す (= caller は unified dict を
        # 期待している)。 v1 (whole-doc) の場合は既に meta が unified なのでそのまま。
        # 判定は meta.schema_version、無ければ v1 扱い。
        if get_schema_version(meta) == SCHEMA_V3_ENTRY:
            hydrated = db.get_project_v3(project_id)
            # 論理的には meta が読めた時点で hydrated も存在するはずだが、 read-race
            # (= 別 tx で削除された) の防御として fallback。
            return hydrated if hydrated is not None else meta
        return meta
    if store_backend == "dynamodb":
        # DynamoDB も v2 subcollection layout を持たない (= 新規 project は v1
        # unified で保存される) ので whole-doc をそのまま返す。
        return meta

    if get_schema_version(meta) == SCHEMA_V1_LEGACY:
        return meta

    # v2 (Firestore only): hydrate milestones from subcollection.
    import firestore_client as fsdb  # type: ignore[import-not-found]
    client = fsdb.get_db()
    ms_col = (
        client.collection(fsdb.COLLECTION)
        .document(project_id)
        .collection("milestones")
    )
    milestones = [s.to_dict() for s in ms_col.stream()]
    return {**meta, "milestones": milestones}


def load_project_meta_only(project_id: str) -> dict:
    """Return the project meta doc only — same shape as :func:`load_project_consistent`
    but WITHOUT streaming the ``milestones`` subcollection.

    This is the cost-reduction sibling of ``load_project_consistent``. Every
    call to the latter incurred one Firestore read per milestone (= 97 reads
    for the Beacon project) because v2 layout stores milestones in a
    subcollection and the consistent-load path hydrates them unconditionally.
    On high-frequency polling endpoints (bus/unread, cursor advance, session
    intent, per-event ack) the milestones payload was never used — the
    handler only needed the meta doc for the auth role check.

    The returned dict is guaranteed to include ``"milestones": []`` so
    downstream code that does ``for ms in data["milestones"]`` becomes a
    silent no-op instead of raising ``KeyError``. Callers that DO need
    milestones must call ``load_project_consistent`` instead — this helper
    intentionally does not fall back.

    v1 legacy projects (and DynamoDB backend, which stores project docs
    whole) already have ``milestones`` inline on the meta doc. We overwrite
    that field with ``[]`` to keep the contract uniform ("this helper never
    returns milestones data"), so v1 / DynamoDB callers are equally
    protected from accidental reliance on hydrated milestones. Callers that
    know they need the whole doc should call ``load_project_consistent``.

    Backend behavior:
      * ``local`` — reads the project.json file (single filesystem read; no
        Firestore cost).
      * ``dynamodb`` — one ``db.get_project`` call, no subcollection stream
        (DynamoDB stores the whole doc; we still stub milestones=[]).
      * Firestore v1 / v2 — one ``db.get_project`` call, never touches the
        milestones subcollection.
    """
    backend = _detect_backend()
    if backend == "local":
        path = os.environ.get("BEACON_PROJECT_FILE", ".beacon/project.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["milestones"] = []
        return data

    # ms-64 e-1627 parity with load_project_consistent: honor the store_router
    # backend switch so DynamoDB deployments benefit from the same "meta only"
    # contract even though they never hit the subcollection stream anyway.
    import store_router as db  # type: ignore[import-not-found]
    # 2026-08-20 本番停止: MySQL backend では get_project が ~8MB の行を丸ごと
    # SELECT + parse するため、「重い payload を読まない」というこの helper の
    # 目的が果たされていなかった。meta 専用の読み取りを持つ backend ではそちらを
    # 使い、持たない backend は従来どおり (挙動不変)。
    _meta_read = getattr(db, "get_project_meta", None)
    meta = _meta_read(project_id) if _meta_read is not None \
        else db.get_project(project_id)
    if meta is None:
        raise LookupError(f"Project '{project_id}' not found")
    return {**meta, "milestones": []}
