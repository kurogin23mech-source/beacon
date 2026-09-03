"""Firestore client wrapper for Beacon API."""

from __future__ import annotations

import logging
import os

from google.cloud import firestore

logger = logging.getLogger(__name__)

_db: firestore.Client | None = None

# ms-64 (beacon-vs-beacon-cloud-separation 原則): 私たちの特定 Beacon Cloud 運用
# 環境の GCP project ID をハードコードしない。BEACON_GCP_PROJECT_ID で明示的に
# 渡すか、google-cloud-firestore の auto-detect (GOOGLE_CLOUD_PROJECT env が
# 設定された Cloud Run / GAE / GKE runtime、または gcloud config / ADC) に
# 任せる。後方互換: 既存の Cloud Run 環境は GOOGLE_CLOUD_PROJECT が runtime
# で自動 inject されるので、移行時に挙動が変わらない。
PROJECT_ID = os.environ.get("BEACON_GCP_PROJECT_ID") or None

# Environment-based collection prefix: dev uses "projects-dev", prod uses "projects"
_ENV = os.environ.get("BEACON_ENV", "dev")
COLLECTION = "projects" if _ENV == "prod" else "projects-dev"
USERS_COLLECTION = "users" if _ENV == "prod" else "users-dev"
# ms-113 / e-3731: Organization (組織) = user → org → project → target の
# テナンシー階層で project の所有境界を束ねる top-level エンティティ。
ORGS_COLLECTION = "organizations" if _ENV == "prod" else "organizations-dev"


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=PROJECT_ID) if PROJECT_ID else firestore.Client()
    return _db


def get_project(project_id: str) -> dict | None:
    """Load a project document. Returns None if not found."""
    doc = get_db().collection(COLLECTION).document(project_id).get()
    if not doc.exists:
        return None
    return doc.to_dict()


def save_project(project_id: str, data: dict) -> None:
    """Save a project document (full replace)."""
    get_db().collection(COLLECTION).document(project_id).set(data)


def list_projects(user_id: str | None = None, include_archived: bool = False) -> list[dict]:
    """List projects. If user_id is given, only return projects owned by or shared with that user.

    ms-95 / e-2411 — also returns ``owner_email`` per row so ``beacon cloud
    list`` can render "who owns this project" without N+1 round-trips. The
    email is resolved via ``get_user(owner_user_id)`` and silently omitted
    when lookup fails (= migration-era projects without an ``owner`` field,
    or stale user_id pointing at a deleted user). Existing consumers see
    new field as additive — same shape plus the new key.
    """
    query = get_db().collection(COLLECTION)
    docs = query.stream()
    # Per-call cache so we resolve each owner_user_id at most once even if
    # they own multiple projects in the response.
    _email_cache: dict[str, str] = {}

    def _resolve_owner_email(owner_uid: str) -> str:
        if not owner_uid:
            return ""
        if owner_uid in _email_cache:
            return _email_cache[owner_uid]
        try:
            owner_doc = get_user(owner_uid) or {}
            email = owner_doc.get("email") or ""
        except Exception:
            email = ""
        _email_cache[owner_uid] = email
        return email

    result = []
    for doc in docs:
        data = doc.to_dict()
        if not include_archived and data.get("archived"):
            continue
        if user_id:
            owner = data.get("owner")
            # ms-95 / e-2794 (2026-07-03): 旧来の「owner 未設定 project は全ユーザーに
            # 見せる」migration-period fallthrough を撤廃。ms-6 マルチユーザー対応の
            # 初期に、owner を持たない legacy project を延命する一時措置として入って
            # いたが、そのまま放置され「別 Google ID で login すると他人の project
            # が全部見える」情報漏洩の原因になっていた (dogfood で観測)。
            # deny by default: owner が無ければ誰にも見せない。復旧が必要なら
            # admin が /api/admin/projects/ownerless で対象を洗い出し、owner を
            # 手動補填する運用に切り替える。
            if not owner:
                logger.warning(
                    "list_projects: skipping ownerless project %s "
                    "(user %s asked, deny-by-default)",
                    doc.id, user_id,
                )
                continue
            members = [m.get("user_id") for m in data.get("members", [])]
            if owner != user_id and user_id not in members:
                continue
        owner_uid = data.get("owner") or ""
        row = {
            "project_id": doc.id,
            "name": data.get("name", ""),
            "objective": data.get("objective", ""),
            "archived": data.get("archived", False),
            "owner": owner_uid,
            "owner_email": _resolve_owner_email(owner_uid),
        }
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# Users (collection: users/{user_id})
# ---------------------------------------------------------------------------


def get_user(user_id: str) -> dict | None:
    """Get a user document. Returns None if not found."""
    doc = get_db().collection(USERS_COLLECTION).document(user_id).get()
    return doc.to_dict() if doc.exists else None


def get_or_create_user(user_id: str, email: str) -> dict:
    """Get or create a user document. Returns user data."""
    import datetime

    doc_ref = get_db().collection(USERS_COLLECTION).document(user_id)
    doc = doc_ref.get()
    if doc.exists:
        user_data = doc.to_dict()
        # Update email if changed
        if user_data.get("email") != email:
            doc_ref.update({"email": email})
            user_data["email"] = email
        return user_data

    user_data = {
        "email": email,
        "role": "user",
        "created_at": datetime.datetime.now().isoformat(),
    }
    doc_ref.set(user_data)
    return user_data


def list_users() -> list[dict]:
    """List all users."""
    docs = get_db().collection(USERS_COLLECTION).stream()
    return [{"user_id": doc.id, **doc.to_dict()} for doc in docs]


def update_user(user_id: str, updates: dict) -> bool:
    """Update user fields. Returns True if user existed."""
    doc_ref = get_db().collection(USERS_COLLECTION).document(user_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.update(updates)
    return True


def delete_user(user_id: str) -> bool:
    """Delete a user. Returns True if existed."""
    doc_ref = get_db().collection(USERS_COLLECTION).document(user_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True


def list_all_projects() -> list[dict]:
    """List all projects (admin). Returns summary only, no entries."""
    import datetime
    docs = get_db().collection(COLLECTION).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        result.append({
            "project_id": doc.id,
            "name": data.get("name", ""),
            "owner": data.get("owner", ""),
            "member_count": len(data.get("members", [])),
            "milestone_count": len(data.get("milestones", [])),
            "updated_at": data.get("updated_at", ""),
        })
    return result


def _delete_subcollection(col_ref) -> None:
    """Delete all documents in a subcollection (Firestore doesn't auto-delete these)."""
    for doc in col_ref.stream():
        doc.reference.delete()


def delete_project(project_id: str) -> bool:
    """Delete a project and ALL its subcollections. Returns True if existed."""
    doc_ref = get_db().collection(COLLECTION).document(project_id)
    if not doc_ref.get().exists:
        return False
    # Delete known subcollections first (Firestore does not cascade).
    # Use literal names here to avoid forward-reference issues with the constants.
    # Changelog is project-scoped: when the project is deleted, the audit
    # trail goes with it (no orphan access path).
    for subcol_name in ("documents", "retros", "members", "triggers", "notes",
                        "changelog", "operation_envelopes", "machine_keys"):
        _delete_subcollection(doc_ref.collection(subcol_name))
    doc_ref.delete()
    return True


def find_user_by_email(email: str) -> tuple[str, dict] | None:
    """Find a user by email. Returns (user_id, user_data) or None."""
    docs = (
        get_db()
        .collection(USERS_COLLECTION)
        .where("email", "==", email)
        .limit(1)
        .stream()
    )
    for doc in docs:
        return doc.id, doc.to_dict()
    return None


# ---------------------------------------------------------------------------
# Retros (subcollection: projects/{project_id}/retros/{week})
# ---------------------------------------------------------------------------

RETRO_SUBCOLLECTION = "retros"


def list_retros(project_id: str) -> list[dict]:
    """List all retro documents for a project (week + updated_at only)."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(RETRO_SUBCOLLECTION)
        .order_by("week", direction=firestore.Query.DESCENDING)
        .stream()
    )
    return [{"week": doc.id, **doc.to_dict()} for doc in docs]


def get_retro(project_id: str, week: str) -> dict | None:
    """Get a single retro document."""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(RETRO_SUBCOLLECTION)
        .document(week)
        .get()
    )
    if not doc.exists:
        return None
    return {"week": doc.id, **doc.to_dict()}


# ---------------------------------------------------------------------------
# Documents (subcollection: projects/{project_id}/documents/{doc_id})
# ---------------------------------------------------------------------------

DOCS_SUBCOLLECTION = "documents"
DOC_REVISIONS_SUBCOLLECTION = "document_revisions"


def list_documents(project_id: str) -> list[dict]:
    """List all documents for a project. Soft-deleted docs are excluded."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .order_by("updated_at", direction=firestore.Query.DESCENDING)
        .stream()
    )
    result = []
    for doc in docs:
        data = doc.to_dict()
        if data.get("deleted"):
            continue
        # milestone: Firestore field first, fallback to frontmatter
        milestone = data.get("milestone") or _extract_frontmatter_field(
            data.get("content", ""), "milestone"
        )
        # e-1859: operation field, same row-first / frontmatter-fallback
        # pattern as milestone. Without this, op-scoped docs were invisible
        # to `beacon doc list --op <op-id>` and to /beacon-operation-review
        # Step 2 discovery (= "beacon doc list --scope spec --op X").
        operation = data.get("operation") or _extract_frontmatter_field(
            data.get("content", ""), "operation"
        )
        # ms-109 e-3754: canonical target-class-agnostic linkage (+ trek_id,
        # previously omitted) so `doc list --account`/`--target` work in cloud.
        target = data.get("target") or _extract_frontmatter_field(
            data.get("content", ""), "target"
        )
        trek_id = data.get("trek_id") or _extract_frontmatter_field(
            data.get("content", ""), "trek_id"
        )
        entry = {
            "doc_id": doc.id,
            "title": data.get("title", ""),
            "scope": data.get("scope", "memo"),
            "updated_at": data.get("updated_at", ""),
        }
        if milestone:
            entry["milestone"] = milestone
        if operation:
            entry["operation"] = operation
        if target:
            entry["target"] = target
        if trek_id:
            entry["trek_id"] = trek_id
        result.append(entry)
    return result


def get_document(project_id: str, doc_id: str) -> dict | None:
    """Get a single document."""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .document(doc_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"doc_id": doc.id, **doc.to_dict()}


def _extract_frontmatter_field(content: str, field: str, default: str = "") -> str:
    """Extract a field from YAML frontmatter in content."""
    if not content.startswith("---"):
        return default
    end = content.find("\n---", 3)
    if end == -1:
        return default
    for line in content[4:end].split("\n"):
        line = line.strip()
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    return default


def _extract_scope(content: str) -> str:
    """Extract scope from YAML frontmatter in content. Default: memo."""
    val = _extract_frontmatter_field(content, "scope", "memo")
    return val if val in ("core", "spec", "memo") else "memo"


def save_document(project_id: str, doc_id: str, title: str, content: str,
                  scope: str | None = None, updated_by: str = "unknown") -> str:
    """Save a document. If doc_id is empty, auto-generate one.
    When updating an existing document, the current content is saved as a revision first.
    """
    import datetime
    resolved_scope = scope if scope in ("core", "spec", "memo") else _extract_scope(content)
    milestone = _extract_frontmatter_field(content, "milestone")
    # ms-69 / e-1663: trek_id is optional; lets a doc be associated with a
    # cross-project trek (= 別 project / 別 session を巻き込んだ協奏作業領域).
    # Existing docs without trek_id are unaffected (= migration 不要).
    trek_id = _extract_frontmatter_field(content, "trek_id")

    # e-1859: operation field mirrors milestone — extract it from the body's
    # frontmatter so the document row keeps an indexable column alongside
    # the raw content. list_documents reads this back; without it, op-scoped
    # docs only existed in the content blob and `--op <op-id>` filtering
    # silently returned [].
    operation = _extract_frontmatter_field(content, "operation")

    col = get_db().collection(COLLECTION).document(project_id).collection(DOCS_SUBCOLLECTION)
    data = {
        "title": title,
        "content": content,
        "scope": resolved_scope,
        "updated_at": datetime.datetime.now().isoformat(),
        "updated_by": updated_by,
    }
    if milestone:
        data["milestone"] = milestone
    if operation:
        data["operation"] = operation
    if trek_id:
        data["trek_id"] = trek_id

    if doc_id:
        doc_ref = col.document(doc_id)
        existing = doc_ref.get()
        if existing.exists:
            # Save current content as a revision before overwriting
            rev_col = doc_ref.collection(DOC_REVISIONS_SUBCOLLECTION)
            existing_data = existing.to_dict()
            rev_docs = rev_col.order_by("rev", direction="DESCENDING").limit(1).stream()
            last_rev = 0
            for r in rev_docs:
                last_rev = r.to_dict().get("rev", 0)
            rev_col.add({
                "rev": last_rev + 1,
                "content": existing_data.get("content", ""),
                "title": existing_data.get("title", ""),
                "ts": existing_data.get("updated_at", ""),
                "saved_by": existing_data.get("updated_by", "unknown"),
            })
        doc_ref.set(data)
        return doc_id
    else:
        ref = col.add(data)
        return ref[1].id


def list_document_revisions(project_id: str, doc_id: str) -> list:
    """List all revisions of a document (newest first)."""
    revs = (
        get_db()
        .collection(COLLECTION).document(project_id)
        .collection(DOCS_SUBCOLLECTION).document(doc_id)
        .collection(DOC_REVISIONS_SUBCOLLECTION)
        .order_by("rev", direction="DESCENDING")
        .stream()
    )
    return [{"rev": r.to_dict().get("rev"), "ts": r.to_dict().get("ts"), "saved_by": r.to_dict().get("saved_by")} for r in revs]


def get_document_revision(project_id: str, doc_id: str, rev: int) -> dict | None:
    """Get a specific revision of a document."""
    revs = (
        get_db()
        .collection(COLLECTION).document(project_id)
        .collection(DOCS_SUBCOLLECTION).document(doc_id)
        .collection(DOC_REVISIONS_SUBCOLLECTION)
        .where("rev", "==", rev)
        .limit(1)
        .stream()
    )
    for r in revs:
        d = r.to_dict()
        return {"rev": d.get("rev"), "content": d.get("content", ""), "title": d.get("title", ""), "ts": d.get("ts"), "saved_by": d.get("saved_by")}
    return None


def delete_document(project_id: str, doc_id: str, deleted_by: str = "unknown",
                    reason: str = "") -> bool:
    """Soft-delete a document (sets deleted flag, never physically removes).

    Optional ``reason`` is stored as ``trash_reason`` for audit symmetry
    with local mode's frontmatter (ms-14 e-991). Returns True if existed.
    """
    import datetime
    doc_ref = (
        get_db()
        .collection(COLLECTION).document(project_id)
        .collection(DOCS_SUBCOLLECTION)
        .document(doc_id)
    )
    doc = doc_ref.get()
    if not doc.exists:
        return False
    update = {
        "deleted": True,
        "deleted_at": datetime.datetime.now().isoformat(),
        "deleted_by": deleted_by,
        # Clear any prior restore stamps so audit fields reflect the
        # current trash event.
        "restored_at": firestore.DELETE_FIELD,
        "restored_by": firestore.DELETE_FIELD,
        "restore_reason": firestore.DELETE_FIELD,
    }
    if reason:
        update["trash_reason"] = reason
    else:
        update["trash_reason"] = firestore.DELETE_FIELD
    doc_ref.update(update)
    return True


def sweep_trashed_documents(project_id: str, *, days: int = 30,
                            dry_run: bool = False) -> list[str]:
    """Hard-delete soft-deleted docs older than ``days`` (ms-14 e-991).

    Companion to ``core.sweep_trashed_in_project`` for the milestone /
    task case — docs live in this subcollection rather than in the
    project json blob so they need their own sweep path.

    Returns the list of ``doc_id`` that were (or would be) deleted.
    ``dry_run=true`` returns ids without removing.

    Docs with ``deleted=true`` but missing ``deleted_at`` are NOT swept
    (mirrors the MS / task rule — no timestamp, no proof the window has
    passed). Operator can purge them manually.
    """
    import datetime
    cutoff_iso = (datetime.datetime.now()
                  - datetime.timedelta(days=max(1, days))).isoformat()
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DOCS_SUBCOLLECTION)
    )
    snaps = col.where("deleted", "==", True).stream()
    purged: list[str] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        deleted_at = data.get("deleted_at", "")
        if not deleted_at or deleted_at >= cutoff_iso:
            continue
        purged.append(snap.id)
        if not dry_run:
            # Also remove the doc's revisions subcollection so we don't
            # orphan storage. Firestore has no cascade.
            _delete_subcollection(
                snap.reference.collection(DOC_REVISIONS_SUBCOLLECTION)
            )
            snap.reference.delete()
    return purged


# ---------------------------------------------------------------------------
# Active claims (subcollection: projects/{project_id}/active_claims/{claim_id})
# ms-55 e-1730
# ---------------------------------------------------------------------------
#
# Server-side mirror of lib/claims.py's local `.beacon/active_claims.json`
# store. Each session (= one bclaude process) records the claims it has
# issued so that:
#
#   * the issuer can recover "what was I in the middle of?" after a
#     restart (= `beacon claim list --mine`)
#   * the Web UI Active Claims tab shows the project-wide picture
#     without scanning the bus event stream
#   * cross-machine coordination (= Mac + Win running side by side) sees
#     the same authoritative claim set instead of two local files that
#     don't know about each other
#
# The wire payload (= what gets stored here) is the same dict
# lib/claims.py:build_claim_payload returns. We don't validate the
# schema server-side; the client builds + validates locally, then this
# layer is a pure mirror. That keeps the schema evolution surface in
# one place (= the builder).

ACTIVE_CLAIMS_SUBCOLLECTION = "active_claims"


def list_active_claims(project_id: str) -> list[dict]:
    """Return all active claims for a project, ordered by issued_at."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(ACTIVE_CLAIMS_SUBCOLLECTION)
        .stream()
    )
    out = []
    for doc in docs:
        data = doc.to_dict() or {}
        # Surface the doc id as claim_id even if the stored payload lost
        # it; the subcollection layout uses claim_id as the document id.
        data.setdefault("claim_id", doc.id)
        out.append(data)
    out.sort(key=lambda r: r.get("issued_at") or "")
    return out


def get_active_claim(project_id: str, claim_id: str) -> dict | None:
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(ACTIVE_CLAIMS_SUBCOLLECTION)
        .document(claim_id)
        .get()
    )
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data.setdefault("claim_id", doc.id)
    return data


def save_active_claim(project_id: str, claim_id: str, payload: dict) -> str:
    """Upsert a claim. Returns the claim_id stored.

    Idempotent: writing the same claim_id overwrites. Matches "the
    latest issue is the canonical record" semantics from local mode.
    """
    if not claim_id:
        raise ValueError("claim_id is required")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    body = dict(payload)
    body["claim_id"] = claim_id  # ensure stored payload has the id
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(ACTIVE_CLAIMS_SUBCOLLECTION)
        .document(claim_id)
        .set(body)
    )
    return claim_id


def delete_active_claim(project_id: str, claim_id: str) -> bool:
    """Hard-delete a claim. Returns True iff it existed.

    Unlike documents, claims have no soft-delete / restore concept —
    a release IS the deletion. The bus event stream retains the
    full audit trail.
    """
    doc_ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(ACTIVE_CLAIMS_SUBCOLLECTION)
        .document(claim_id)
    )
    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True


# ---------------------------------------------------------------------------
# Changelog (subcollection: projects/{project_id}/changelog/{doc_id})
# ---------------------------------------------------------------------------
#
# Append-only audit trail of mutating operations (ms-14 e-825). Every
# write that crosses the API boundary should also land an entry here so
# that:
#
#   - data-destructive operations (purge, delete, sweep) are traceable
#     beyond the lifetime of the data they removed,
#   - multi-user projects can show "who did what" in the Web UI, and
#   - the 30-day soft-delete window (e-826 / e-991) has a permanent
#     companion record after the trashed item itself ages out.
#
# Document IDs are millisecond-prefixed iso8601 timestamps with a random
# suffix so:
#   - they sort naturally in Firestore ASC ordering, and
#   - two writes in the same millisecond don't collide.
#
# No DELETE function exists. Removal must go through Firestore Admin SDK
# (maintainer-only, leaves an admin audit trail of its own).

CHANGELOG_SUBCOLLECTION = "changelog"


def append_changelog(project_id: str, entry: dict) -> str:
    """Append one structured entry to the project's changelog subcollection.

    Best-effort: returns the document id on success, empty string on
    failure. Callers MUST NOT raise on failure — the audit trail is a
    non-functional concern that must never break the actual write.

    The caller controls the entry's contents. Recommended fields (none
    are enforced here so the schema can evolve without breaking older
    readers):
        ts          ISO8601 UTC timestamp
        op          short action key (\"milestone.delete\", \"trash.sweep\", ...)
        actor       user sub (or \"system\" for cron-driven sweeps)
        email       resolved email of the actor
        reason      human-readable explanation, optional
        target      {\"type\": ..., \"id\": ...}, optional
        ip          requester IP if available
        user_agent  HTTP UA if available
        payload     free-form dict for extra context (small)
    """
    import datetime
    import secrets

    # Millisecond-resolution ISO timestamp + 4 hex chars to break ties
    # between concurrent writes. The Firestore SDK orders string IDs
    # lexicographically so this gives us natural time-ordering.
    now = datetime.datetime.now(datetime.timezone.utc)
    doc_id = now.strftime("%Y%m%dT%H%M%S.%f") + "-" + secrets.token_hex(2)

    # Ensure ts is always present, even if the caller forgot.
    payload = dict(entry)
    payload.setdefault("ts", now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    try:
        (
            get_db()
            .collection(COLLECTION)
            .document(project_id)
            .collection(CHANGELOG_SUBCOLLECTION)
            .document(doc_id)
            .set(payload)
        )
        return doc_id
    except Exception:  # noqa: BLE001 - best-effort write; never propagate.
        return ""


def list_changelog(project_id: str, *, since: str | None = None,
                   limit: int = 100) -> list[dict]:
    """Return changelog entries for a project, newest first.

    ``since`` (ISO8601 string) filters to entries with ``ts > since`` —
    useful for incremental polling. ``limit`` is capped at 500 to keep
    pagination payloads bounded.
    """
    limit = max(1, min(500, int(limit)))
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(CHANGELOG_SUBCOLLECTION)
    )
    query = col.order_by("ts", direction=firestore.Query.DESCENDING).limit(limit)
    if since:
        # Use string comparison since `ts` is ISO8601 and lexicographically
        # ordered. The composite query with order_by + where on different
        # fields would require a Firestore index; staying single-field.
        query = (
            col.where("ts", ">", since)
            .order_by("ts", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
    out = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        data["id"] = doc.id
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Session Notes (subcollection: projects/{project_id}/notes/{note_id})
# ---------------------------------------------------------------------------

NOTES_SUBCOLLECTION = "notes"


def add_note(project_id: str, note: dict) -> str:
    """Add a session note. Returns the generated note ID."""
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(NOTES_SUBCOLLECTION)
    )
    ref = col.add(note)
    return ref[1].id


def list_notes(project_id: str) -> list[dict]:
    """List session notes ordered by timestamp."""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(NOTES_SUBCOLLECTION)
        .order_by("ts")
        .stream()
    )
    return [doc.to_dict() for doc in docs]


def clear_notes(project_id: str) -> None:
    """Delete all session notes for a project."""
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(NOTES_SUBCOLLECTION)
    )
    _delete_subcollection(col)


# ---------------------------------------------------------------------------
# Bus events (subcollection: projects/{project_id}/bus_events/{auto-id})
# ms-54 / e-996: append-only event log for the agent-to-agent / session-to-
# session real-time bus. Read API is polling-friendly via ``since`` cursor;
# WS push is added on top in e-997 (separate task). Schema is intentionally
# minimal at this slice:
#   - channel: routing tag (e.g. "session-dm", "operation-due")
#   - sender_session_id: who emitted (typically from ms-57 session registry)
#   - payload: arbitrary JSON dict, event-specific
#   - created_at: server-assigned ISO8601 UTC (cursor key)
# The recipient/delivery-target fields land in e-1135 (delivery policy task).
# ---------------------------------------------------------------------------

BUS_EVENTS_SUBCOLLECTION = "bus_events"


def append_bus_event(project_id: str, data: dict) -> str:
    """Append a bus event (auto-id). Returns the generated event_id.

    The Firestore auto-id is lexically sortable but for chronological
    polling we order by ``created_at`` in :func:`list_bus_events` so the
    sender can pass any ISO8601 timestamp it last saw as the ``since``
    cursor without needing to track Firestore's id format.
    """
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
    )
    ref = col.add(data)
    return ref[1].id


def get_bus_event(project_id: str, event_id: str) -> dict | None:
    """Return the bus_event doc for ``event_id`` or None if absent.

    ms-70 / e-1717: the denied-reply chain needs to read the original
    envelope's ``sender_session_id`` (= where the AI that emitted the
    action request is listening) to address the "denied by receiver"
    reply back to it. The append-only bus_events collection is keyed
    by auto-id, but Firestore lets us address a single doc by id without
    a query — this helper is the small wrapper around that direct read.

    Returned dict shape mirrors :func:`list_bus_events` rows: the
    Firestore doc fields plus a top-level ``event_id`` set to the doc id.
    Returns None if no doc exists for that id.
    """
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
        .document(event_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"event_id": doc.id, **(doc.to_dict() or {})}


# ---------------------------------------------------------------------------
# Bus cursors (subcollection: projects/{project_id}/bus_cursors/{recipient_id})
# ms-54 / e-998: per-recipient read cursor. Together with the append-only event
# log this gives "受信者ごとに既読位置が保持され、重複配信が起きない" — see ms-39 lost
# update教訓: cursors are advance-only so a stale client can never rewind another
# client's progress. recipient_id is opaque (session_id is the typical value).
# ---------------------------------------------------------------------------

BUS_CURSORS_SUBCOLLECTION = "bus_cursors"


def get_bus_cursor(project_id: str, recipient_id: str) -> dict:
    """Return the cursor doc for ``recipient_id`` or an empty dict when unset.

    Schema (when set): ``{"last_seen_at": "<ISO8601>", "updated_at": "<ISO8601>"}``.
    Empty dict means "never seen anything" — callers should treat that as
    cursor=epoch (i.e. return all events).
    """
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_CURSORS_SUBCOLLECTION)
        .document(recipient_id)
        .get()
    )
    return doc.to_dict() or {} if doc.exists else {}


def advance_bus_cursor(project_id: str, recipient_id: str,
                       last_seen_at: str) -> dict:
    """Forward-only cursor advance. Returns the resulting cursor.

    If the request's ``last_seen_at`` is *not* strictly greater than the
    existing cursor, the existing one is preserved (no rewind, no overwrite).
    This is the structural defense against "stale client commits older
    cursor and replays events for live clients" — same lost-update class as
    ms-39's project.json overwrite bug.
    """
    import datetime
    ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_CURSORS_SUBCOLLECTION)
        .document(recipient_id)
    )
    snap = ref.get()
    existing = snap.to_dict() if snap.exists else None
    existing_seen = (existing or {}).get("last_seen_at", "")
    if existing_seen and last_seen_at <= existing_seen:
        # Idempotent no-op; return what's already stored.
        return dict(existing or {})
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    data = {"last_seen_at": last_seen_at, "updated_at": now}
    ref.set(data, merge=True)
    return data


# ---------------------------------------------------------------------------
# Bus envelope nonce store + audit (ms-54 / e-1155 Phase 1)
#
# ``bus_nonces``: project-scoped replay-protection store. Each envelope nonce
# may be consumed at most once per project. Documents are keyed by nonce and
# carry ``expires_at`` so a sweeper (cron / TTL policy) can GC them; the
# verify path itself only writes new nonces and reads to check existence.
#
# ``bus_audit``: subcollection storing every receive-time verify outcome —
# pass, fail, or degrade. This is the e-1168 audit log target co-located
# with the bus events it documents, so a single query yields the full
# "who signed → what was requested → what we let through" trail.
# ---------------------------------------------------------------------------

BUS_NONCES_SUBCOLLECTION = "bus_nonces"
BUS_AUDIT_SUBCOLLECTION = "bus_audit"


def check_and_record_bus_nonce(project_id: str, nonce: str,
                               expires_at: str) -> bool:
    """Return True iff ``nonce`` is fresh; atomically record it on first use.

    Uses a Firestore transaction so concurrent verify paths on the same
    nonce can't both see "fresh". On a write collision, the transaction
    retries and the second caller sees the prior document → returns False.

    ``expires_at`` is informational (used by an external sweeper). The
    in-flight verify does not GC; production cleanup is operational work
    (Firestore TTL policy or a scheduled function).
    """
    import datetime
    db = get_db()
    ref = (
        db.collection(COLLECTION)
        .document(project_id)
        .collection(BUS_NONCES_SUBCOLLECTION)
        .document(nonce)
    )

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if snap.exists:
            return False
        tx.set(ref, {
            "nonce": nonce,
            "expires_at": expires_at,
            "recorded_at": datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
        })
        return True

    return _txn(db.transaction())


def set_bus_event_receipt(project_id: str, event_id: str, stage: str,
                          recipient_session_id: str) -> dict | None:
    """Stamp a per-event receipt timestamp (ms-54 / e-1348 DM read receipt).

    ``stage`` ∈ {"delivered", "opened"}. Sets the ``<stage>_at`` (server clock,
    same ISO8601 µs-precision format as ``created_at``) and ``<stage>_by``
    (the recipient session that observed the event) fields on the bus event
    document.

    **First-write-wins per stage**: the receipt is *idempotent*. A second ack
    of the same stage returns the existing timestamp with ``already_set=True``
    rather than overwriting. The transaction makes that race-safe so two
    receivers of the same broadcast event do not produce a flip-flop.

    Returns ``None`` if the event does not exist. Otherwise returns
    ``{event_id, stage, timestamp, by, already_set}``.

    Why per-event fields instead of reusing the cursor (advance_bus_cursor)?
    The cursor is per-recipient and tracks the "processed up to here" frontier
    — it can only express delivery in aggregate. Read-receipt asks the
    different question "did *this specific event* surface to anyone?". Two
    fields on the event itself answer that without forcing every recipient to
    maintain a parallel per-event ack journal.
    """
    import datetime
    if stage not in ("delivered", "opened"):
        raise ValueError(f"set_bus_event_receipt: invalid stage {stage!r}")
    ts_field = f"{stage}_at"
    by_field = f"{stage}_by"
    db = get_db()
    ref = (
        db.collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
        .document(event_id)
    )

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        existing_ts = data.get(ts_field)
        if existing_ts:
            return {
                "event_id": event_id,
                "stage": stage,
                "timestamp": existing_ts,
                "by": data.get(by_field, ""),
                "already_set": True,
            }
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        tx.update(ref, {ts_field: now, by_field: recipient_session_id})
        return {
            "event_id": event_id,
            "stage": stage,
            "timestamp": now,
            "by": recipient_session_id,
            "already_set": False,
        }

    return _txn(db.transaction())


def find_bus_event(project_id: str, event_id: str) -> dict | None:
    """Look up a single bus event by id. Returns None if missing.

    Used by the envelope verify step 6 (in_reply_to parent existence +
    recipient-of-parent check). Keeps the heavy ``list_bus_events`` query
    off the verify hot path.
    """
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
        .document(event_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"event_id": doc.id, **doc.to_dict()}


def append_bus_audit(project_id: str, record: dict) -> str:
    """Append an audit record. Returns the auto-id.

    Schema (Phase 1):
      {
        "received_at": ISO8601,
        "envelope": {tier, issuer, scope, conversation_id, chain_depth, ...},
        "verify": {passed, effective_tier, steps, rejection_reason},
        "requested_action": str | None,
        "requested_delivery": str,
        "effective_delivery": str,
        "sender_session_id": str,
        "channel": str,
        "event_id": str | None,   # set if the event was persisted
      }

    Audit records are written for both pass and fail receives so the trail
    is complete (CORE doc § "監査ログとの統合").
    """
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_AUDIT_SUBCOLLECTION)
    )
    ref = col.add(record)
    return ref[1].id


def list_bus_audit(project_id: str, *, since: str = "",
                   limit: int = 100) -> list[dict]:
    """List audit records ordered by received_at (most recent at end).

    Used by the Web UI (Phase 2) and by ops queries — "what envelopes have
    we accepted / rejected in the last hour for this project?".
    """
    q = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_AUDIT_SUBCOLLECTION)
        .order_by("received_at")
    )
    if since:
        q = q.where("received_at", ">", since)
    if limit:
        q = q.limit(limit)
    return [{"audit_id": doc.id, **doc.to_dict()} for doc in q.stream()]


def list_bus_events(project_id: str, since: str = "", channel: str = "",
                    limit: int = 100) -> list[dict]:
    """List bus events ordered by created_at.

    ``since``: ISO8601 timestamp; only events with ``created_at > since`` are
    returned. Empty string returns from the beginning.
    ``channel``: optional equality filter for routing.
    ``limit``: cap to keep wire payload bounded. Default 100 — callers that
    fall behind will iterate by advancing ``since`` to the last seen
    ``created_at`` until they catch up.

    Firestore needs a composite index for ``where(channel ==) + where(created_at >)
    + order_by(created_at)``, which is operational work to provision per
    project. Until the bus reaches a scale that justifies the index, we keep
    the channel filter **in-memory** post-fetch and only push ``since`` +
    ``order_by`` to Firestore (single-field, no composite index needed). The
    ``limit`` is applied after the in-memory filter so a heavy unrelated-channel
    burst doesn't starve the requested channel — at the cost of fetching
    slightly more rows than necessary in the worst case.
    """
    q = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENTS_SUBCOLLECTION)
        .order_by("created_at")
    )
    if since:
        q = q.where("created_at", ">", since)
    # Fetch a bit more than `limit` so the post-filter still satisfies the
    # caller's bound when most rows belong to other channels. 5x is a soft
    # guard; if a single project ever exceeds that ratio we should provision
    # the composite index and push the channel filter to Firestore.
    fetch_cap = (limit * 5) if (limit and channel) else limit
    if fetch_cap:
        q = q.limit(fetch_cap)
    rows = [{"event_id": doc.id, **doc.to_dict()} for doc in q.stream()]
    if channel:
        rows = [r for r in rows if r.get("channel") == channel]
    if limit:
        rows = rows[:limit]
    return rows


# ---------------------------------------------------------------------------
# Bus event approvals (subcollection: projects/{project_id}/bus_event_approvals/{event_id})
# ms-70 / e-1712: receiver-side decision_stamp sidecar for cross-user DM action
# envelopes. The envelope itself (in bus_events) is HMAC-signed and therefore
# immutable — flipping a field on it to record "approved by user X at time T"
# would break signature verification on every subsequent read.
#
# Instead, we keep the envelope intact in bus_events and store the variable
# decision state in this parallel subcollection, keyed by the same event_id.
# That gives CLI / Skill / Web UI / another session's AI a single place to
# ask "is this envelope pending, approved, denied, or auto-allowed right now?"
# without touching the signed payload.
#
# Schema (7 fields, mirrored byte-for-byte in DynamoDB):
#   event_id           : str           (= parent bus_event auto-id, also doc id here)
#   approval_status    : str           (= "pending" | "approved" | "denied" | "auto")
#   decision_by        : str | None    (= user_id of the decider, None while pending)
#   decision_at        : str | None    (= ISO8601 timestamp of decision, None while pending)
#   created_at         : str           (= ISO8601 timestamp the sidecar row was first written)
#   sender_user_id     : str           (= who issued the envelope; carried for cheap UI filtering)
#   receiver_user_id   : str           (= who needs to approve; carried for cheap "my pending" query)
#
# Migration semantics: a bus_event without a matching sidecar row is treated
# as approval_status="auto" by callers (= existing behaviour, e.g. same-user
# DMs or Trek-scoped blanket-allow events that never produced a stamp).
# get_bus_event_approval returns None for that case; the caller is expected
# to interpret None as "auto" rather than "missing".
# ---------------------------------------------------------------------------

BUS_EVENT_APPROVALS_SUBCOLLECTION = "bus_event_approvals"

# Allowed approval_status values. "auto" is materialised only when a caller
# explicitly stamps a sidecar row to record that an auto-allow decision was
# made (e.g. shared-Trek blanket-allow). Bare absence of a sidecar row is
# also semantically "auto" but no document is written for it.
_BUS_EVENT_APPROVAL_STATUSES = ("pending", "approved", "denied", "auto")


def get_bus_event_approval(project_id: str, event_id: str) -> dict | None:
    """Return the sidecar approval doc for ``event_id`` or None if absent.

    None is the **legacy / auto-allow** signal — callers should treat None
    as ``approval_status="auto"`` for backwards compatibility with bus_events
    written before this sidecar landed (= ms-70 / e-1712).

    Schema returned (7 fields) — see module-level doc above:
      event_id / approval_status / decision_by / decision_at /
      created_at / sender_user_id / receiver_user_id
    """
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENT_APPROVALS_SUBCOLLECTION)
        .document(event_id)
        .get()
    )
    if not doc.exists:
        return None
    return {"event_id": doc.id, **(doc.to_dict() or {})}


def put_bus_event_approval(project_id: str, event_id: str, *,
                           approval_status: str,
                           sender_user_id: str,
                           receiver_user_id: str,
                           decision_by: str | None = None,
                           decision_at: str | None = None) -> dict:
    """Write or update the sidecar approval row for ``event_id``.

    First call on a given event_id creates the row with ``created_at`` set to
    the server clock; subsequent calls update ``approval_status`` /
    ``decision_by`` / ``decision_at`` but preserve the original ``created_at``
    (= ms-70 design: the lifecycle "pending → approved | denied" should not
    rewrite when the request came in).

    Returns the resulting row as a dict (7 fields), in the same shape as
    :func:`get_bus_event_approval`.

    Raises ``ValueError`` if ``approval_status`` is not in the allowed set.
    """
    import datetime
    if approval_status not in _BUS_EVENT_APPROVAL_STATUSES:
        raise ValueError(
            f"put_bus_event_approval: invalid approval_status "
            f"{approval_status!r} (allowed: {_BUS_EVENT_APPROVAL_STATUSES})"
        )
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENT_APPROVALS_SUBCOLLECTION)
        .document(event_id)
    )
    snap = ref.get()
    existing = snap.to_dict() if snap.exists else None
    if existing:
        created_at = existing.get("created_at") or now
    else:
        created_at = now
    data = {
        "approval_status": approval_status,
        "decision_by": decision_by,
        "decision_at": decision_at,
        "created_at": created_at,
        "sender_user_id": sender_user_id,
        "receiver_user_id": receiver_user_id,
    }
    ref.set(data)
    return {"event_id": event_id, **data}


def list_pending_approvals(project_id: str, *,
                           receiver_user_id: str | None = None,
                           limit: int = 100) -> list[dict]:
    """List sidecar rows in ``approval_status="pending"`` ordered by created_at.

    ``receiver_user_id`` (optional): restrict to rows where the receiver
    matches — the common UI query is "what is *my* inbox waiting on me to
    decide". Applied in-memory after the equality query on
    ``approval_status`` so no composite index is required (= same trade-off
    as :func:`list_bus_events`'s channel filter).

    ``limit``: cap returned rows; defaults to 100. Applied after the
    in-memory receiver filter so a heavy other-receiver burst does not
    starve the requested caller.
    """
    q = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(BUS_EVENT_APPROVALS_SUBCOLLECTION)
        .where("approval_status", "==", "pending")
        .order_by("created_at")
    )
    # Fetch slightly more than `limit` when we will post-filter by receiver
    # so the final result still satisfies the bound.
    fetch_cap = (limit * 5) if (limit and receiver_user_id) else limit
    if fetch_cap:
        q = q.limit(fetch_cap)
    rows = [{"event_id": doc.id, **(doc.to_dict() or {})} for doc in q.stream()]
    if receiver_user_id:
        rows = [r for r in rows if r.get("receiver_user_id") == receiver_user_id]
    if limit:
        rows = rows[:limit]
    return rows


def list_decided_approvals(project_id: str, *, limit: int = 50) -> list[dict]:
    """List sidecar rows in approval_status in {"approved","denied"} (= ms-70 / e-1718).

    Audit-trail read used by the Web UI's "DM 承認履歴" (DM approval history)
    section. Returns rows newest-first (= ordered by ``decision_at`` DESC where
    available, falling back to ``created_at`` for any malformed row), which
    matches the human expectation of "show me the most recent decisions".

    Pending rows are **deliberately excluded** — per SPEC 設計方針 3 the
    terminal Claude Code is the only surface that decides approvals. The Web
    UI is read-only audit; surfacing pending rows here would tempt a future
    contributor to wire approve/deny buttons into the audit table, breaking
    the "terminal-only decision" invariant.

    ``auto`` rows are also excluded — they carry no human decision and would
    drown out the interesting human-decided rows.

    No GSI / composite index is required: Firestore enforces a single
    equality + order_by pair without one, so we do **two** equality queries
    (approved / denied) and merge in memory. This is fine at dev scale; if
    decisions ever exceed a few hundred per project we can swap to an "in"
    query with a composite index.
    """
    out: list[dict] = []
    for status in ("approved", "denied"):
        q = (
            get_db()
            .collection(COLLECTION)
            .document(project_id)
            .collection(BUS_EVENT_APPROVALS_SUBCOLLECTION)
            .where("approval_status", "==", status)
        )
        # No order_by on decision_at — that field may be None for legacy rows
        # and Firestore would skip them. Sort in memory after merge.
        #
        # We do NOT apply `.limit(limit)` per-status here. Doing so would
        # truncate each status independently in created_at order, which is
        # **not** the same as truncating the merged-and-sorted-by-decision_at
        # result: e.g. if status=approved has 10 old rows and status=denied
        # has 1 new row, a per-status limit=3 would surface evt-000..002
        # plus the denied row, but the actually-newest 3 by decision_at are
        # evt-007..009. So we fetch everything and truncate after the merge
        # sort. At dev scale this is fine; if decisions grow large we should
        # add a GSI on (project_id, decision_at) and switch to a server-side
        # ORDER BY decision_at DESC LIMIT.
        for doc in q.stream():
            out.append({"event_id": doc.id, **(doc.to_dict() or {})})
    # newest-first by decision_at (fall back to created_at). Rows missing
    # both sort to the end so they don't displace real decisions.
    def _sort_key(r: dict) -> str:
        return r.get("decision_at") or r.get("created_at") or ""
    out.sort(key=_sort_key, reverse=True)
    if limit:
        out = out[:limit]
    return out


# ---------------------------------------------------------------------------
# Machine API keys (subcollection: projects/{project_id}/machine_keys/{key_id})
# ms-151 / e-5474: headless machine 認証の鍵。record は machine_key.build_record が
# 組み立てた secret_hash-only 形 (平文 secret を保存しない)。verify は token 由来の
# (project_id, key_id) で get_machine_key を直接引く (scan 不要 = 全 backend O(1))。
# delete_project の cascade 対象 (project を消したら鍵も消える)。
# ---------------------------------------------------------------------------

MACHINE_KEYS_SUBCOLLECTION = "machine_keys"


def save_machine_key(project_id: str, record: dict) -> dict:
    """発行済み machine key レコードを保存する (key_id で upsert)。"""
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(MACHINE_KEYS_SUBCOLLECTION)
        .document(record["key_id"])
        .set(record)
    )
    return record


def get_machine_key(project_id: str, key_id: str) -> dict | None:
    """(project_id, key_id) の key レコードを返す。無ければ None。"""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(MACHINE_KEYS_SUBCOLLECTION)
        .document(key_id)
        .get()
    )
    if not doc.exists:
        return None
    return doc.to_dict()


def list_machine_keys(project_id: str) -> list[dict]:
    """project の全 machine key を新しい順 (created_at 降順) で返す。"""
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(MACHINE_KEYS_SUBCOLLECTION)
        .stream()
    )
    rows = [doc.to_dict() for doc in docs]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def revoke_machine_key(project_id: str, key_id: str,
                       revoked_at: str) -> dict | None:
    """key を失効させる (revoked_at を刻む)。無ければ None を返す。"""
    ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(MACHINE_KEYS_SUBCOLLECTION)
        .document(key_id)
    )
    snap = ref.get()
    if not snap.exists:
        return None
    existing = snap.to_dict() or {}
    # e-5502 AX review A: 冪等。既に失効済みなら最初の revoked_at を保持し
    # 上書きしない (再 revoke で監査証跡=最初の失効時刻を消さない)。
    if existing.get("revoked_at"):
        return existing
    ref.update({"revoked_at": revoked_at})
    return {**existing, "revoked_at": revoked_at}


# ---------------------------------------------------------------------------
# Session registry (subcollection: projects/{project_id}/sessions/{session_id})
# ms-57 / e-1063: cloud-visible per-session state, used by Web UI for "who is
# active right now" and by session-start for cross-machine rescue lookups.
# Each session document is keyed by session_id (not auto-id) so CLI clients
# can upsert idempotently from any machine.
# ---------------------------------------------------------------------------

SESSIONS_SUBCOLLECTION = "sessions"


def upsert_session(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session document by session_id (merge=True).

    merge=True is critical so that a heartbeat-only update (just last_active)
    does not wipe other fields written by a previous create call.
    """
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSIONS_SUBCOLLECTION)
        .document(session_id)
        .set(data, merge=True)
    )


def stamp_session_actor_email(project_id: str, session_id: str,
                              email: str) -> None:
    """Stamp ``actor.email`` on a session document without touching other
    ``actor.*`` subfields (ms-54 / e-1349).

    ``set(data, merge=True)`` on a nested map *replaces* the entire map — so
    if a heartbeat call wrote ``{actor: {email: x}}`` after a mint wrote
    ``{actor: {machine: m, agent: a}}``, the heartbeat would silently wipe
    machine/agent. Firestore's ``update`` with a dotted field path
    (``actor.email``) is the canonical fix: only the named leaf participates
    in the write, every other ``actor.*`` subfield is left untouched.

    Used by the server-side upsert_session endpoint to attribute the
    authenticated user's email to the bridge's session, so the directory
    picker (``/beacon-dm-send``) can show member names alongside machine /
    agent identity without trusting client-supplied attribution.

    ``update`` raises if the document does not exist. Callers MUST run the
    main upsert_session(...) write first so the parent doc is materialised
    before this stamp lands. We fall back to ``set`` with the nested map on
    NotFound for paranoid robustness — should not normally happen.
    """
    if not email:
        return
    ref = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSIONS_SUBCOLLECTION)
        .document(session_id)
    )
    try:
        # Dotted path → server-side merge at the leaf, preserves
        # actor.machine / actor.agent if a prior mint wrote them.
        ref.update({"actor.email": email})
    except Exception:
        # Parent doc missing (race with upsert) — degrade to a nested set
        # that creates {actor: {email: ...}} on top of whatever is there.
        # Loses machine/agent only in the unlikely race window.
        ref.set({"actor": {"email": email}}, merge=True)


def list_sessions(project_id: str) -> list[dict]:
    """List all session documents for a project, ordered by last_active desc.

    Used by session-start rescue, by the Web UI "active sessions" view, and
    by ms-54 / e-1134 directory query. The Firestore doc ID **is** the
    session_id, so we merge ``doc.id`` into each returned dict — otherwise
    the directory picker has no way to address a chosen row in
    ``beacon bus send --sender <session_id>``. Existing keys win, so this
    is purely additive for callers that already had session_id from another
    source.
    """
    docs = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSIONS_SUBCOLLECTION)
        .order_by("last_active", direction=firestore.Query.DESCENDING)
        .stream()
    )
    # doc.id is the authoritative session_id; put it last so it wins over
    # any stray same-named field that might appear in older docs.
    return [{**doc.to_dict(), "session_id": doc.id} for doc in docs]


# ---------------------------------------------------------------------------
# Session log (subcollection: projects/{project_id}/session_logs/{session_id})
# ms-57 / e-1037: per-session aggregated entry. summary is the durable
# content (decision trail surviving past entry GC); *_ids are best-effort
# back-references that work only while the linked entries remain. Doc id
# == session_id so session-end and rescue (e-1038/1039) can upsert
# idempotently without an ID lookup step.
# ---------------------------------------------------------------------------

SESSION_LOGS_SUBCOLLECTION = "session_logs"


# ---------------------------------------------------------------------------
# Cloud-first identity (ms-62 / e-1509)
#
# Three layers of server-managed identity introduced by ms-62 SPEC:
#
#   project_id  — already server-issued at project creation (existing)
#   machine_id  — server-issued at first heartbeat for (user, machine
#                 fingerprint). Stored under users/{uid}/machines/{machine_id}
#                 with the fingerprint as the lookup key. Client caches the
#                 returned machine_id in ~/.beacon/machine.json so subsequent
#                 heartbeats can claim "I am machine X" without re-minting.
#   session_id  — server-issued at first heartbeat for the tuple
#                 (project_id, machine_id, parent_pid). Stored in the existing
#                 projects/{p}/sessions/{sid} collection PLUS a small lookup
#                 doc projects/{p}/session_lookup/{machine_id}_{parent_pid}
#                 that maps the tuple to the sid. The lookup doc lets us
#                 answer "what sid did we already mint for this tuple?"
#                 without compound-where Firestore queries (= no composite
#                 index to deploy).
#
# Session continuity across heartbeat gaps:
#   1. bclaude starts in terminal T1 → bus.mjs heartbeat carries
#      (project_id, machine_id, parent_pid=T1.pid). Server mints sid_A and
#      writes the lookup doc.
#   2. bclaude restarts in same terminal T1 → same parent_pid → lookup
#      finds sid_A → server returns it. Continuity preserved.
#   3. Terminal closes, new terminal T3 opens → parent_pid=T3.pid is new
#      → lookup miss → server mints sid_B. Semantically correct: a new
#      terminal session is a new work session.
#
# Multi-bclaude in same cwd:
#   Different terminals → different parent_pids → distinct sids. No
#   client-side disambiguation needed (= ms-57 e-1460 bridges/<sid>.json
#   layout is superseded by this server-side tuple lookup).
# ---------------------------------------------------------------------------

MACHINES_SUBCOLLECTION = "machines"
SESSION_LOOKUP_SUBCOLLECTION = "session_lookup"


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _safe_doc_id(s: str) -> str:
    """Sanitise a string for use as a Firestore document ID.

    Firestore doc IDs cannot contain "/" and must be non-empty. Hostnames
    can contain dots and dashes (both OK), but worst-case we hash anything
    suspicious. Empty input gets a deterministic placeholder so the caller
    never lands at the collection root.
    """
    import hashlib
    if not s:
        return "_empty"
    # Length cap (Firestore allows 1500 bytes but be polite).
    if "/" in s or len(s) > 200:
        return "h-" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]
    return s


def get_or_mint_machine(
    user_id: str, fingerprint: str, *, hostname: str = "", agent: str = ""
) -> tuple[str, bool]:
    """Get or mint a machine_id for (user_id, fingerprint).

    The fingerprint is used as the lookup key under
    ``users/{user_id}/machines/`` — same fingerprint always resolves to
    the same machine_id. Returns ``(machine_id, minted)`` where
    ``minted`` is True iff this call created the document.

    The mint is deterministic only in the bucket sense (= same
    fingerprint maps to same doc), but the machine_id value itself is a
    fresh nonce so the value never leaks the fingerprint.
    """
    import secrets

    if not user_id or not fingerprint:
        raise ValueError("user_id and fingerprint are required")
    fingerprint_id = _safe_doc_id(fingerprint)
    ref = (
        get_db()
        .collection(USERS_COLLECTION)
        .document(user_id)
        .collection(MACHINES_SUBCOLLECTION)
        .document(fingerprint_id)
    )
    doc = ref.get()
    now = _now_iso()
    if doc.exists:
        data = doc.to_dict() or {}
        ref.update({"last_seen_at": now})
        return data.get("machine_id", ""), False
    machine_id = f"mc-{secrets.token_hex(8)}"
    ref.set({
        "machine_id": machine_id,
        "fingerprint": fingerprint,
        "hostname": hostname or fingerprint,
        "agent": agent,
        "created_at": now,
        "last_seen_at": now,
    })
    return machine_id, True


def get_or_mint_session_by_tuple(
    project_id: str,
    machine_id: str,
    parent_pid: int,
    *,
    user_id: str,
    cwd: str = "",
    metadata: dict | None = None,
) -> dict:
    """Server-side identity tuple lookup (ms-62 e-1509).

    Resolve ``(project_id, machine_id, parent_pid)`` to a session_id.
    Existing tuple → return the previously minted sid + refresh
    last_heartbeat_at. New tuple → mint a fresh sid, register the lookup
    doc, create the session record, return.

    Returns ``{session_id, minted, last_heartbeat_at, created_at}``.

    The "lookup miss after a long idle" case (= the bclaude was killed and
    a NEW one happened to inherit the recycled OS pid) collapses naturally:
    the lookup doc still points at the old sid, but the old sid's
    last_heartbeat_at is stale. Callers (= the heartbeat endpoint) decide
    whether to honour the stale lookup or force a re-mint by passing a
    stale-threshold check at the API layer.
    """
    import secrets

    if not project_id or not machine_id or not isinstance(parent_pid, int):
        raise ValueError("project_id, machine_id, parent_pid are required")

    db = get_db()
    project_ref = db.collection(COLLECTION).document(project_id)
    lookup_key = f"{machine_id}_{parent_pid}"
    lookup_ref = (
        project_ref
        .collection(SESSION_LOOKUP_SUBCOLLECTION)
        .document(_safe_doc_id(lookup_key))
    )
    lookup_doc = lookup_ref.get()
    now = _now_iso()

    if lookup_doc.exists:
        sid = (lookup_doc.to_dict() or {}).get("session_id", "")
        if sid:
            session_ref = (
                project_ref
                .collection(SESSIONS_SUBCOLLECTION)
                .document(sid)
            )
            # Refresh last_heartbeat_at on the session doc itself. We use
            # update() with a dotted path so other heartbeat-tracked fields
            # (last_active, last_poll_at from the existing bridge writer)
            # are not clobbered.
            session_ref.set({
                "last_heartbeat_at": now,
                "machine_id": machine_id,
                "parent_pid": parent_pid,
                "cwd": cwd,
                **(metadata or {}),
            }, merge=True)
            existing = session_ref.get().to_dict() or {}
            return {
                "session_id": sid,
                "minted": False,
                "last_heartbeat_at": now,
                "created_at": existing.get("created_at", ""),
            }

    # Mint new
    epoch_ms = int(__import__("time").time() * 1000)
    new_sid = f"sv-{machine_id[3:11] if machine_id.startswith('mc-') else machine_id[:8]}-{epoch_ms}-{secrets.token_hex(4)}"

    session_ref = (
        project_ref
        .collection(SESSIONS_SUBCOLLECTION)
        .document(new_sid)
    )
    session_data = {
        "session_id": new_sid,
        "user_id": user_id,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "cwd": cwd,
        "created_at": now,
        "last_heartbeat_at": now,
        "last_active": now,
        "source": "server_minted",
        **(metadata or {}),
    }
    session_ref.set(session_data, merge=True)
    lookup_ref.set({
        "session_id": new_sid,
        "machine_id": machine_id,
        "parent_pid": parent_pid,
        "created_at": now,
    })
    return {
        "session_id": new_sid,
        "minted": True,
        "last_heartbeat_at": now,
        "created_at": now,
    }


def list_user_machines(user_id: str) -> list[dict]:
    """List all machines for a user. Used by the Web UI machine management view."""
    docs = (
        get_db()
        .collection(USERS_COLLECTION)
        .document(user_id)
        .collection(MACHINES_SUBCOLLECTION)
        .stream()
    )
    return [doc.to_dict() for doc in docs]


def upsert_session_log(project_id: str, session_id: str, data: dict) -> None:
    """Upsert a session log entry by session_id.

    Uses merge=True so a rescue-path partial upsert (just summary + recovered
    flag) does not wipe note_ids / commit_ids / pr_ids written by a richer
    session-end aggregation that happened earlier — important when both
    paths race on the same session.
    """
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSION_LOGS_SUBCOLLECTION)
        .document(session_id)
        .set(data, merge=True)
    )


def list_session_logs(project_id: str, limit: int | None = None) -> list[dict]:
    """List session log entries ordered by last_aggregated_at desc.

    session-start reads the top N (typically 3 per SPEC §10) so the most
    recent prior sessions surface first. ``limit`` is applied server-side
    to keep the wire payload small.
    """
    q = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSION_LOGS_SUBCOLLECTION)
        .order_by("last_aggregated_at", direction=firestore.Query.DESCENDING)
    )
    if limit:
        q = q.limit(limit)
    return [doc.to_dict() for doc in q.stream()]


def get_session_log(project_id: str, session_id: str) -> dict | None:
    """Fetch a single session log entry, or None if absent."""
    doc = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(SESSION_LOGS_SUBCOLLECTION)
        .document(session_id)
        .get()
    )
    return doc.to_dict() if doc.exists else None


def save_retro(project_id: str, week: str, content: str) -> None:
    """Save a retro document."""
    import datetime

    get_db().collection(COLLECTION).document(project_id).collection(
        RETRO_SUBCOLLECTION
    ).document(week).set(
        {
            "week": week,
            "content": content,
            "updated_at": datetime.datetime.now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Operation envelopes (ms-60 / e-1339)
#
# T2 envelope = Operation scope envelope. The SPEC author lists
# ``approved_actions``; ``beacon operation approve`` mints a server-signed
# envelope from that list. Stored in a subcollection so the audit trail
# (active + revoked records) doesn't bloat the parent project document
# and stays under the Firestore 1 MiB cap (ms-59 lesson).
#
# Doc id = envelope nonce (already unique per server-issued envelope).
# Status discipline: at most one ``active`` envelope per ``op_id``. Re-approve
# auto-revokes the prior active one with ``reason="superseded"``.
# ---------------------------------------------------------------------------

OPERATION_ENVELOPES_SUBCOLLECTION = "operation_envelopes"


def _operation_envelopes_col(project_id: str):
    return (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(OPERATION_ENVELOPES_SUBCOLLECTION)
    )


def get_active_operation_envelope(
    project_id: str, op_id: str
) -> dict | None:
    """Return the single active T2 envelope for ``op_id``, or None.

    There must be at most one — the issue path auto-revokes any prior
    active record before writing the new one. If multiple actives are ever
    observed, returns the most recently created and logs a warning (the
    issue path's transaction makes the race impossible, but operational
    repair could leave artifacts).
    """
    q = (
        _operation_envelopes_col(project_id)
        .where("op_id", "==", op_id)
        .where("status", "==", "active")
    )
    docs = list(q.stream())
    if not docs:
        return None
    if len(docs) > 1:
        # Pick newest by created_at; this should never happen under the
        # transactional issue path but we don't want a phantom record to
        # mask the intended scope.
        docs.sort(key=lambda d: (d.to_dict() or {}).get("created_at", ""),
                  reverse=True)
    return docs[0].to_dict()


def issue_operation_envelope(
    project_id: str,
    op_id: str,
    spec_doc_id: str,
    spec_revision_id: str,
    envelope_dict: dict,
    approved_actions: list[str],
    created_by: str,
) -> dict:
    """Store a freshly minted T2 envelope. Auto-revokes any prior active.

    ``envelope_dict`` is the already-signed envelope from
    ``envelope.issue_envelope``; the doc id is its ``nonce`` (unique by
    construction). Returns the stored record.

    The auto-revoke + write happens in a single transaction so a concurrent
    second approve can't leave two actives.
    """
    import datetime

    nonce = envelope_dict.get("nonce")
    if not nonce:
        raise ValueError("envelope_dict missing nonce")
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    new_doc = {
        "envelope_id": nonce,
        "op_id": op_id,
        "spec_doc_id": spec_doc_id,
        "spec_revision_id": spec_revision_id,
        "envelope": envelope_dict,
        "approved_actions": list(approved_actions),
        "status": "active",
        "created_at": now,
        "created_by": created_by,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }
    col = _operation_envelopes_col(project_id)
    db = get_db()

    @firestore.transactional
    def _txn(tx):
        # Find any active for this op_id and mark revoked first.
        prior = (
            col.where("op_id", "==", op_id)
            .where("status", "==", "active")
        )
        for snap in prior.stream(transaction=tx):
            tx.update(snap.reference, {
                "status": "revoked",
                "revoked_at": now,
                "revoked_by": created_by,
                "revoke_reason": "superseded by new approve",
            })
        tx.set(col.document(nonce), new_doc)
        return new_doc

    return _txn(db.transaction())


def revoke_operation_envelope(
    project_id: str,
    envelope_id: str,
    revoked_by: str,
    reason: str,
) -> dict | None:
    """Mark an envelope as revoked. Returns the updated record, or None.

    Idempotent: revoking an already-revoked envelope is a no-op that returns
    the existing record unchanged (the auditor cares about *who first*
    revoked it, not the latest no-op).
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    ref = _operation_envelopes_col(project_id).document(envelope_id)
    db = get_db()

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if not snap.exists:
            return None
        cur = snap.to_dict() or {}
        if cur.get("status") == "revoked":
            return cur
        update = {
            "status": "revoked",
            "revoked_at": now,
            "revoked_by": revoked_by,
            "revoke_reason": reason,
        }
        tx.update(ref, update)
        cur.update(update)
        return cur

    return _txn(db.transaction())


def list_operation_envelopes(
    project_id: str,
    op_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List envelopes, optionally filtered by op_id and/or status.

    Ordered by ``created_at`` descending (newest first) — both auditors and
    the CLI ``operation show`` extension want the current state at top.
    """
    q = _operation_envelopes_col(project_id)
    if op_id:
        q = q.where("op_id", "==", op_id)
    if status:
        q = q.where("status", "==", status)
    docs = [d.to_dict() for d in q.stream()]
    docs.sort(key=lambda d: (d or {}).get("created_at", ""), reverse=True)
    return docs


def get_operation_envelope(
    project_id: str, envelope_id: str
) -> dict | None:
    """Fetch a single envelope record by id, or None if absent."""
    snap = _operation_envelopes_col(project_id).document(envelope_id).get()
    return snap.to_dict() if snap.exists else None


# ---------------------------------------------------------------------------
# Operation fire gate (ms-95 / e-1668 + e-2350)
#
# ``operation_fires``: per-project per-op per-day "I already fired today"
# ledger. Subcollection keyed by ``<op_id>_<YYYY-MM-DD>``. First-write-wins
# inside a Firestore transaction.
#
# Why this exists: the CLI scheduler (``_auto_fire_operation_triggers`` in
# lib/commands.py) is invoked by every bclaude session's bridge poll loop
# (channel/bus.mjs runSchedulerTick). With N parallel sessions in the same
# project, the local "today already ran?" check (= scan ``op.entries`` for a
# run_record dated today) races against the cloud-synced view: each session
# can independently see "no run_record yet" before the first run_record
# round-trips through Firestore, producing N-multiplied fires (e-1668) and
# 4-6 minute retrigger storms (e-2350).
#
# This subcollection serializes the decision via transaction so only one
# session per (project, op, date) wins the claim — the rest skip and the
# local file gate keeps same-cwd sessions consistent as a second line.
# ---------------------------------------------------------------------------

OPERATION_FIRES_SUBCOLLECTION = "operation_fires"


def claim_operation_fire_if_new(project_id: str, op_id: str, date: str,
                                session_id: str) -> dict:
    """Atomically claim "first to fire ``op_id`` on ``date`` for project".

    First caller's transaction creates the document → returns
    ``{"claimed": True, "claimed_by": session_id, "claimed_at": <now>}``.
    Subsequent callers see the existing document and return
    ``{"claimed": False, "claimed_by": <first session>, "claimed_at": <first ts>}``.

    ``date`` is an ISO date string (``YYYY-MM-DD``). The doc id is
    ``<op_id>_<date>`` so the same op can fire again on a later day
    without prior gating.

    ``session_id`` is recorded for tracing (= who won the race today).
    Empty string is acceptable for callers without bridge mint
    (e.g. non-bridged CLI on a fresh machine).

    Used by the operation fire claim endpoint
    (``POST /api/projects/<pid>/operation-fires/<op>/claim``); the CLI
    scheduler hits the endpoint before posting the operation-trigger bus
    event so cross-cwd / cross-machine duplicate fires are suppressed.
    """
    import datetime
    db = get_db()
    doc_id = f"{op_id}_{date}"
    ref = (
        db.collection(COLLECTION)
        .document(project_id)
        .collection(OPERATION_FIRES_SUBCOLLECTION)
        .document(doc_id)
    )

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if snap.exists:
            data = snap.to_dict() or {}
            return {
                "claimed": False,
                "claimed_by": data.get("session_id", ""),
                "claimed_at": data.get("claimed_at", ""),
            }
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        tx.set(ref, {
            "op_id": op_id,
            "date": date,
            "session_id": session_id,
            "claimed_at": now,
        })
        return {
            "claimed": True,
            "claimed_by": session_id,
            "claimed_at": now,
        }

    return _txn(db.transaction())


# ---------------------------------------------------------------------------
# Treks (ms-69 / e-1652)
#
# Top-level collection ``treks/{trek_id}`` — independent of any single
# project. SPEC 設計方針 1: trek is its own authorisation surface
# (= creator + invited members), cross-project by construction. Storing
# under a project subcollection would formally bias ownership.
#
# Schema (Phase 1):
#   trek_id        string   # "tk-<8 hex>"
#   title          string
#   description    string
#   type           string   # "temporary" | "persistent"
#   status         string   # "planning" | "active" | "paused" | "archived"
#   creator_actor  {user_id, email}
#   leader_actor   {user_id, email}
#   members        [{user_id, email, role, invited_at, joined_at, invited_by}]
#   scope          [{project, milestone?, operation?, task?}]
#   created_at     ISO8601
#   updated_at     ISO8601
#   archived_at    ISO8601 | null
#
# Activity log / summary docs / claim records are separate subcollections
# under ``treks/{trek_id}/...`` added in e-1663 / e-1657 — out of scope
# for this Phase 1 schema slice.
# ---------------------------------------------------------------------------

TREKS_COLLECTION = "treks" if _ENV == "prod" else "treks-dev"


def get_trek(trek_id: str) -> dict | None:
    """Load a trek document. Returns None if not found.

    The Firestore doc id is the authoritative trek_id; it is merged in
    last so it always wins over any stale field of the same name.
    """
    doc = get_db().collection(TREKS_COLLECTION).document(trek_id).get()
    if not doc.exists:
        return None
    return {**(doc.to_dict() or {}), "trek_id": doc.id}


def save_trek(trek_id: str, data: dict) -> None:
    """Save a trek document (full replace).

    The caller is responsible for assembling the full doc; this mirrors
    ``save_project`` which is also a full set (no merge). Mutators that
    need merge semantics should compose get → modify → save.

    ``trek_id`` field in ``data`` is ignored — the doc id is the truth.
    """
    payload = {k: v for k, v in data.items() if k != "trek_id"}
    get_db().collection(TREKS_COLLECTION).document(trek_id).set(payload)


def list_treks(actor_id: str | None = None, *,
               status: str | None = None,
               include_archived: bool = False) -> list[dict]:
    """List treks. Optionally filter by visibility / status.

    ``actor_id=None`` returns all treks (= admin view). When set, only
    treks where the actor is creator OR appears in the members list
    are returned (= same authorisation model as ``list_projects``).

    ``include_archived=False`` (default) hides archived treks; setting
    True surfaces them for "show me everything we have ever worked on"
    auditor queries.
    """
    docs = get_db().collection(TREKS_COLLECTION).stream()
    result: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        if not include_archived and data.get("status") == "archived":
            continue
        if status and data.get("status") != status:
            continue
        if actor_id:
            creator = (data.get("creator_actor") or {}).get("user_id")
            members = [
                m.get("user_id") for m in data.get("members", []) or []
            ]
            if creator != actor_id and actor_id not in members:
                continue
        # Merge doc.id last so it always wins (= mirrors list_sessions).
        result.append({**data, "trek_id": doc.id})
    # Newest first by created_at; ties fall back to trek_id for stability.
    result.sort(key=lambda t: (t.get("created_at", ""), t.get("trek_id", "")),
                reverse=True)
    return result


def delete_trek(trek_id: str) -> bool:
    """Delete a trek. Returns True if it existed.

    Phase 1: hard delete of the top-level doc only. Subcollections
    (activity log, summaries, claims — e-1663) are introduced later;
    when they land this function will cascade like ``delete_project``.
    """
    doc_ref = get_db().collection(TREKS_COLLECTION).document(trek_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True


# ---------------------------------------------------------------------------
# Organizations (ms-113 / e-3731) — top-level entity, doc id = org_id.
# Mirrors the trek store shape (get / save full-replace / list / delete).
# The doc id is authoritative and merged in last so it wins over any stale
# ``org_id`` field, exactly like ``get_trek`` / ``list_treks``.
# ---------------------------------------------------------------------------

def get_org(org_id: str) -> dict | None:
    """Load an organization document. Returns None if not found."""
    doc = get_db().collection(ORGS_COLLECTION).document(org_id).get()
    if not doc.exists:
        return None
    return {**(doc.to_dict() or {}), "org_id": doc.id}


def save_org(org_id: str, data: dict) -> None:
    """Save an organization document (full replace, mirrors ``save_trek``).

    ``org_id`` in ``data`` is ignored — the doc id is the truth.
    """
    payload = {k: v for k, v in data.items() if k != "org_id"}
    get_db().collection(ORGS_COLLECTION).document(org_id).set(payload)


def list_orgs_for_user(user_id: str | None = None) -> list[dict]:
    """List organizations. ``user_id=None`` returns all (= admin view).

    When set, only orgs where the user appears in ``members`` are returned
    (= same membership-visibility model as ``list_treks`` / ``list_projects``).
    """
    docs = get_db().collection(ORGS_COLLECTION).stream()
    result: list[dict] = []
    for doc in docs:
        data = doc.to_dict() or {}
        if user_id:
            members = [m.get("user_id") for m in data.get("members", []) or []]
            if user_id not in members:
                continue
        result.append({**data, "org_id": doc.id})
    result.sort(key=lambda o: (o.get("created_at", ""), o.get("org_id", "")),
                reverse=True)
    return result


def delete_org(org_id: str) -> bool:
    """Delete an organization. Returns True if it existed."""
    doc_ref = get_db().collection(ORGS_COLLECTION).document(org_id)
    if not doc_ref.get().exists:
        return False
    doc_ref.delete()
    return True


# ---------------------------------------------------------------------------
# Master identity store (ms-111 / e-3620) — 汎用プリミティブ。
# entity (master_accounts / master_contacts) をそれぞれ top-level collection に
# 対応させ (env で -dev 接尾)、record を pk (= canonical id) の doc に丸ごと保存する。
# 問い合わせ・書き込み意味論は lib/master_store.BeaconDefaultAdapter が持つので、
# ここは raw get/put/scan のみ。record は id を内包するので doc.id 再構成は不要。
# ---------------------------------------------------------------------------

def _master_collection(entity: str) -> str:
    return entity if _ENV == "prod" else f"{entity}-dev"


def master_get(entity: str, pk: str) -> dict | None:
    doc = get_db().collection(_master_collection(entity)).document(pk).get()
    if not doc.exists:
        return None
    return dict(doc.to_dict() or {})


def master_put(entity: str, pk: str, data: dict) -> None:
    get_db().collection(_master_collection(entity)).document(pk).set(dict(data))


def master_scan(entity: str) -> list[dict]:
    return [dict(d.to_dict() or {})
            for d in get_db().collection(_master_collection(entity)).stream()]


# ---------------------------------------------------------------------------
# Trek structured logs (ms-97 Phase 7-C, AC26 / AC27, e-2603)
# Subcollection: treks/{trek_id}/logs/{log_id}
# Retention: indefinite (MVP).
# ---------------------------------------------------------------------------

TREK_LOGS_SUBCOLLECTION = "logs"


def _mint_trek_log_id() -> str:
    import secrets as _secrets
    return f"log-{_secrets.token_hex(8)}"


def append_trek_log(trek_id: str, log_entry: dict) -> str:
    """Append a trek log entry. Returns the assigned log_id.

    ``log_entry`` is the caller-supplied record (= ``kind`` / ``session_id``
    / ``payload`` / ``created_at`` / ...). This helper stamps ``log_id`` and
    ``trek_id`` so callers don't have to (= safe defaults: ``log_id`` is
    minted if absent, ``trek_id`` is forced to the path arg).
    """
    payload = dict(log_entry or {})
    log_id = payload.get("log_id") or _mint_trek_log_id()
    payload["log_id"] = log_id
    payload["trek_id"] = trek_id
    (
        get_db()
        .collection(TREKS_COLLECTION)
        .document(trek_id)
        .collection(TREK_LOGS_SUBCOLLECTION)
        .document(log_id)
        .set(payload)
    )
    return log_id


def list_trek_logs(trek_id: str, *, limit: int = 100,
                   since: str = "") -> list[dict]:
    """Return ``treks/{trek_id}/logs`` ordered by ``created_at`` ascending.

    ``since`` is an optional ISO8601 lower bound (= entries with
    ``created_at > since`` only). ``limit`` caps the number of rows
    returned to keep the read O(1) for dashboard use cases.
    """
    col = (
        get_db()
        .collection(TREKS_COLLECTION)
        .document(trek_id)
        .collection(TREK_LOGS_SUBCOLLECTION)
    )
    docs = col.stream()
    out: list[dict] = []
    for d in docs:
        rec = d.to_dict() or {}
        if since and (rec.get("created_at") or "") <= since:
            continue
        rec["log_id"] = d.id
        out.append(rec)
    out.sort(key=lambda r: (r.get("created_at", ""), r.get("log_id", "")))
    if limit and limit > 0:
        out = out[:limit]
    return out


# ---------------------------------------------------------------------------
# Decision events (ms-90 e-3242): projects/{pid}/decision_events に永続化する
# 意思決定の統一 append-only ストリーム。schema builder は
# server/decision_event.py。approvals sidecar (bus_event_approvals) を 4 経路
# (dm-send / trek-review / scope-approval / halt-resume) に一般化したもの。
# ---------------------------------------------------------------------------

DECISION_EVENTS_SUBCOLLECTION = "decision_events"


def _mint_decision_event_id() -> str:
    import secrets as _secrets
    return f"dec-{_secrets.token_hex(8)}"


def append_decision_event(project_id: str, data: dict) -> str:
    """Append a decision event (= minted decision_id). Returns the decision_id.

    Stamps ``decision_id`` / ``created_at`` if absent and rejects
    outcome-like fields before the write (= SPEC §設計方針2 invariant).
    """
    try:
        from decision_event import assert_no_outcome
        assert_no_outcome(data or {})
    except ImportError:
        pass
    payload = dict(data or {})
    decision_id = payload.get("decision_id") or _mint_decision_event_id()
    payload["decision_id"] = decision_id
    if not payload.get("created_at"):
        payload["created_at"] = _now_iso()
    (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DECISION_EVENTS_SUBCOLLECTION)
        .document(decision_id)
        .set(payload)
    )
    return decision_id


def list_decision_events(project_id: str, *, kind: str = "", limit: int = 100,
                         since: str = "", session: str = "",
                         target: str = "") -> list[dict]:
    """Fetch ``projects/{pid}/decision_events`` and window them (ms-166 e-5970 /
    ms-164 e-6030).

    This backend only FETCHES the rows; the read-window semantics (kind / session /
    target filter → since filter → newest ``limit``) live in the single source
    ``decision_event.window_decision_events`` so the three store backends can
    never drift from each other. See that helper for the full rationale (why the
    newest window, not the oldest — an oldest-``limit`` slice hid every recent
    decision once the append-only backlog exceeded ``limit``).
    """
    from decision_event import window_decision_events
    col = (
        get_db()
        .collection(COLLECTION)
        .document(project_id)
        .collection(DECISION_EVENTS_SUBCOLLECTION)
    )
    rows: list[dict] = []
    for d in col.stream():
        rec = d.to_dict() or {}
        rec["decision_id"] = d.id
        rows.append(rec)
    return window_decision_events(rows, kind=kind, limit=limit, since=since,
                                  session=session, target=target)
