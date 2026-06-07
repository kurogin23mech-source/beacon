"""TrailNode capability registry endpoints.

Mounted under /api/trailnode/* on the Beacon Cloud Run service.

Design summary (see trailnode/docs/manifest-schema.md, ms-2 SPEC, ms-6 SPEC):
- Capabilities (manifests) are stored in Firestore collection `capabilities`
  (prod) or `capabilities-dev` (other envs), keyed by slug
  `<org>__<name>__<version>` (`/` and `@` replaced with `__`).
- Artifacts (tar.gz) are stored in GCS bucket `trailnode-artifacts` under
  `{env}/{org}/{name}/{version}.tar.gz`.
- Pull goes through Cloud Run (proxy stream) instead of signed URLs to
  avoid signing-key/IAM complexity in MVP. Revisit in ms-3 if traffic
  shape requires direct GCS reads.
- Authorization piggybacks on Beacon's existing HTTPBearer + HMAC CLI
  token via the host app's `require_auth` dependency, injected through
  the `make_router` factory to avoid an import cycle with `app.py`.
- **ms-6 change**: Capability ID first segment is the org slug (was: author
  user). Push/pull authz now goes through `trailnode_orgs.is_member()`
  and `trailnode_orgs.require_org_member()` — endpoints never touch the
  `orgs` / `memberships` collections directly.
- **ms-3 change (e-30)**: Manifest docs carry `updated_at` (last write
  timestamp, used as diff-sync frontier by the list endpoint) and
  `deleted_at` (soft-delete sentinel; `None` = active). Soft-delete is the
  convention going forward — the list endpoint will filter by
  `deleted_at == None` for active docs and read `deleted_at >= since`
  for the deleted-ids tail. Push always resets `deleted_at` to `None`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from google.cloud import storage as gcs_storage

import firestore_client as db
import trailnode_orgs as orgs

logger = logging.getLogger(__name__)


class _VersionRaceError(Exception):
    """Raised inside the push transaction when another concurrent push has
    already committed the version slot this push was targeting.

    Caught at the HTTP layer and surfaced as 409 so the client can retry
    (which naturally picks the next version slot — see ms-53 e-1163).
    """

_BUCKET_NAME = os.environ.get("TRAILNODE_BUCKET", "trailnode-artifacts")
_ENV = os.environ.get("BEACON_ENV", "dev")
_CAPABILITIES_COLLECTION = "capabilities" if _ENV == "prod" else "capabilities-dev"
_NAMESPACES_COLLECTION = (
    "capability-namespaces" if _ENV == "prod" else "capability-namespaces-dev"
)
_GCS_PREFIX = f"{_ENV}/"  # keep dev/prod artifacts isolated

# ms-8: capability refs accepted by the registry.
#   <org>/<name>                          push only — server auto-assigns version
#   <org>/<name>@<version>                pull / pin (or legacy push, version ignored)
#   <org>/<name>:<branch>                 pull a branch's latest
#   <org>/<name>:<branch>@<version>       pull a branch pin
# Branch names follow the same charset as org/name segments. Version is
# either the new YYYY-MM-DD.N format (ms-8) or legacy SemVer (read-compat).
_ORG_NAME_PATTERN = re.compile(r"^([a-z0-9-]+)/([a-z0-9-]+)$")
_BRANCH_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")
_VERSION_PATTERN_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
_VERSION_PATTERN_LEGACY = re.compile(r"^\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?$")

# ms-6 legacy: <org>/<name>@<semver>. Kept for backward-compat with older
# clients and for tests that still pin legacy ids.
_ID_PATTERN = re.compile(
    r"^([a-z0-9-]+)/([a-z0-9-]+)@(\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?)$"
)

# `artifact_url` is intentionally NOT required from clients — the server
# fills it in after the GCS upload. `author` stays required as a free-form
# label of the human author (decoupled from the org since ms-6).
# ms-8: `version` is no longer required from clients — server auto-assigns
# it at push time. Legacy clients that still send `version` have the field
# silently overridden with the assigned YYYY-MM-DD.N value.
_REQUIRED_MANIFEST_FIELDS = (
    "schema_version", "id", "name", "type", "description", "author",
)
_SUPPORTED_TYPES = ("skill", "app", "program", "ai-workflow")  # ms-5: 4-type gate (client mirror: trailnode src/trailnode/push.py)

_gcs_client: gcs_storage.Client | None = None


def _get_gcs_client() -> gcs_storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = gcs_storage.Client()
    return _gcs_client


def _parse_capability_id(capability_id: str) -> tuple[str, str, str]:
    """Return (org_slug, name, version) on success (legacy ms-6 format)."""
    m = _ID_PATTERN.match(capability_id)
    if not m:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid capability_id '{capability_id}': "
                "expected <org>/<name>@<semver>"
            ),
        )
    return m.group(1), m.group(2), m.group(3)


def _parse_capability_ref(ref: str) -> dict:
    """Parse a capability ref into its components (ms-8).

    Accepted forms:
    - `<org>/<name>`                       → push (server auto-assigns version)
    - `<org>/<name>@<version>`             → pull / pin (or legacy push)
    - `<org>/<name>:<branch>`              → pull a branch's latest
    - `<org>/<name>:<branch>@<version>`    → pull a branch pin

    Returns a dict with keys:
      org_slug, name, branch (str | None), version (str | None).
    `branch` is None for trunk. `version` is None when omitted.

    Raises HTTPException(400) on shape mismatch.
    """
    # Split off the @version suffix first (if any).
    if "@" in ref:
        head, version = ref.rsplit("@", 1)
        if not (
            _VERSION_PATTERN_DATE.match(version)
            or _VERSION_PATTERN_LEGACY.match(version)
        ):
            raise HTTPException(
                400,
                detail=(
                    f"invalid version '{version}' in ref '{ref}': "
                    "expected YYYY-MM-DD.N (e.g. 2026-06-06.1) or legacy SemVer"
                ),
            )
    else:
        head, version = ref, None

    # Then split off the :branch suffix (if any).
    if ":" in head:
        org_name, branch = head.rsplit(":", 1)
        if not _BRANCH_NAME_PATTERN.match(branch):
            raise HTTPException(
                400,
                detail=(
                    f"invalid branch name ':{branch}' in ref '{ref}': "
                    "expected lowercase letters, digits, hyphens"
                ),
            )
    else:
        org_name, branch = head, None

    m = _ORG_NAME_PATTERN.match(org_name)
    if not m:
        raise HTTPException(
            400,
            detail=(
                f"invalid ref '{ref}': "
                "expected <org>/<name>[:<branch>][@<version>]"
            ),
        )
    return {
        "org_slug": m.group(1),
        "name": m.group(2),
        "branch": branch,
        "version": version,
    }


def _slug(capability_id: str) -> str:
    """Legacy slug builder (ms-6 format: <org>/<name>@<ver>)."""
    return capability_id.replace("/", "__").replace("@", "__")


def _slug_for(org_slug: str, name: str, branch: Optional[str], version: str) -> str:
    """Build Firestore document id (ms-8).

    Trunk:  <org>__<name>__<version>
    Branch: <org>__<name>__b__<branch>__<version>

    `__b__` is the branch marker — it's never a legal char sequence inside
    org_slug / name / branch (which match `[a-z0-9-]+`), so the parse is
    unambiguous.
    """
    if branch is None:
        return f"{org_slug}__{name}__{version}"
    return f"{org_slug}__{name}__b__{branch}__{version}"


def _gcs_object_key(org_slug: str, name: str, version: str) -> str:
    """Legacy GCS path (ms-6 trunk-only)."""
    return f"{_GCS_PREFIX}{org_slug}/{name}/{version}.tar.gz"


def _gcs_object_key_for(
    org_slug: str, name: str, branch: Optional[str], version: str
) -> str:
    """Build GCS object key (ms-8). Branches live under `branches/<branch>/`."""
    if branch is None:
        return f"{_GCS_PREFIX}{org_slug}/{name}/{version}.tar.gz"
    return f"{_GCS_PREFIX}{org_slug}/{name}/branches/{branch}/{version}.tar.gz"


def _sanitize_branch_identity(email: str) -> str:
    """Derive a branch name from a pusher's email.

    Takes the local part of the email, lowercases, and replaces any character
    outside `[a-z0-9-]` with `-`. Trims leading/trailing hyphens and collapses
    runs. Returns the sanitized string, or "anon" if the result is empty.

    This is a one-way derivation — the branch_identity is stored on the
    manifest so the original pusher can still be looked up via uploaded_by /
    uploaded_by_email.
    """
    if not email:
        return "anon"
    local = email.split("@", 1)[0].lower()
    out_chars: list[str] = []
    for ch in local:
        if ch.isalnum() or ch == "-":
            out_chars.append(ch)
        else:
            out_chars.append("-")
    # collapse repeated hyphens
    collapsed = re.sub(r"-+", "-", "".join(out_chars)).strip("-")
    return collapsed or "anon"


def _compute_next_version_from_existing(
    today_str: str, existing_versions: list[str]
) -> str:
    """Pure helper: return `today_str.N` where N is one more than the max N
    seen in `existing_versions` for the same date prefix (else 1).

    Versions not matching `today_str.N` (different dates, legacy SemVer, etc.)
    are ignored — they don't affect today's counter.
    """
    max_n = 0
    prefix = today_str + "."
    for v in existing_versions:
        if not v.startswith(prefix):
            continue
        tail = v[len(prefix):]
        try:
            n = int(tail)
        except ValueError:
            continue
        if n > max_n:
            max_n = n
    return f"{today_str}.{max_n + 1}"


def _today_utc_str() -> str:
    """Return UTC date in YYYY-MM-DD form (ms-8 version date component)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _validate_manifest_for_push(data: dict, org_slug: str, name: str) -> None:
    """Validate a manifest at push time (ms-8 model).

    Differences from the ms-6 validator:
    - `version` is NOT required in the manifest — the server assigns it.
      Any client-provided `version` is silently overridden later.
    - `id` is not cross-checked against the URL's full capability_id,
      because the canonical id (with branch + version) is server-decided.
      Only `name` is verified (and `org_slug` indirectly via the URL parse).
    """
    missing = [f for f in _REQUIRED_MANIFEST_FIELDS if f not in data]
    if missing:
        raise HTTPException(400, detail=f"manifest missing required fields: {missing}")
    if data["name"] != name:
        raise HTTPException(400, detail=f"manifest.name '{data['name']}' != URL name '{name}'")
    if data["type"] not in _SUPPORTED_TYPES:
        raise HTTPException(
            400,
            detail=(
                f"v0 only accepts type in {_SUPPORTED_TYPES}, got '{data['type']}'"
            ),
        )
    # `app` type requires a non-empty install.recipe (ms-5 SPEC §設計方針4).
    if data["type"] == "app":
        recipe = (data.get("install") or {}).get("recipe")
        if not isinstance(recipe, list) or not recipe:
            raise HTTPException(
                400,
                detail=(
                    "type='app' requires a non-empty install.recipe (ordered "
                    "list of shell commands run at `trailnode use` time)."
                ),
            )
    # `artifact_url` is filled in server-side; tolerate absent/empty.
    au = data.get("artifact_url")
    if au is not None and not isinstance(au, list):
        raise HTTPException(400, detail="artifact_url must be an array if provided")


def _validate_manifest(
    data: dict, capability_id: str, org_slug: str, name: str, version: str
) -> None:
    """Legacy ms-6 validator. Kept for any callers still going through the
    old strict path; the ms-8 PUT endpoint uses `_validate_manifest_for_push`.
    """
    missing_legacy = [f for f in _REQUIRED_MANIFEST_FIELDS if f not in data]
    if "version" not in data:
        missing_legacy.append("version")
    if missing_legacy:
        raise HTTPException(400, detail=f"manifest missing required fields: {missing_legacy}")
    if data["id"] != capability_id:
        raise HTTPException(
            400, detail=f"manifest.id '{data['id']}' != URL capability_id '{capability_id}'"
        )
    if data["name"] != name:
        raise HTTPException(400, detail=f"manifest.name '{data['name']}' != id name '{name}'")
    if data["version"] != version:
        raise HTTPException(
            400, detail=f"manifest.version '{data['version']}' != id version '{version}'"
        )
    if data["type"] not in _SUPPORTED_TYPES:
        raise HTTPException(
            400,
            detail=(
                f"v0 only accepts type in {_SUPPORTED_TYPES}, got '{data['type']}'"
            ),
        )
    au = data.get("artifact_url")
    if au is not None and not isinstance(au, list):
        raise HTTPException(400, detail="artifact_url must be an array if provided")


def _filter_manifests_by_membership(
    manifests: list[dict], member_slugs: set
) -> list[dict]:
    """Drop manifests whose `org_slug` is not in the caller's `member_slugs`.

    ms-3 e-32: pure function so the cross-org leak guard is unit-testable
    without a Firestore stub. Manifests without an `org_slug` field are
    excluded conservatively (legacy docs predate ms-6's org concept and
    should never be served on the new endpoint).
    """
    return [m for m in manifests if m.get("org_slug") in member_slugs]


# ──────────────────────────────────────────────────────────────────────────
# ms-8 trunk/branch routing (Firestore-touching helpers)
# ──────────────────────────────────────────────────────────────────────────


def _ns_slug(org_slug: str, name: str) -> str:
    return f"{org_slug}__{name}"


def _get_or_establish_owner(
    org_slug: str, name: str, user_sub: str, user_email: str
) -> tuple[str, bool]:
    """Look up the owner (uploaded_by) of `<org>/<name>`. If no namespace
    record exists yet, establish the caller as owner.

    Returns (owner_sub, is_first_push).

    Idempotent under sequential calls. Concurrent first-push races accept
    last-write-wins on the namespace doc — acceptable for MVP (extremely
    rare in single-org cloud-runs).
    """
    from google.cloud import firestore as fs

    ns_doc = db.get_db().collection(_NAMESPACES_COLLECTION).document(
        _ns_slug(org_slug, name)
    )
    snapshot = ns_doc.get()
    if snapshot.exists:
        data = snapshot.to_dict() or {}
        existing_owner = data.get("owner_sub", "")
        return existing_owner, False
    # First push — establish caller as owner.
    ns_doc.set(
        {
            "org_slug": org_slug,
            "name": name,
            "owner_sub": user_sub,
            "owner_email": user_email,
            "established_at": fs.SERVER_TIMESTAMP,
        }
    )
    return user_sub, True


def _query_existing_versions(
    org_slug: str, name: str, branch: Optional[str]
) -> list[str]:
    """Return the list of stored `version` strings for the given
    trunk-or-branch stream of `<org>/<name>`.

    Used by `_compute_next_version_from_existing` to decide today's next N.
    """
    collection = db.get_db().collection(_CAPABILITIES_COLLECTION)
    query = (
        collection.where("org_slug", "==", org_slug)
        .where("name", "==", name)
        .where("branch", "==", branch)
    )
    out: list[str] = []
    for doc in query.stream():
        v = (doc.to_dict() or {}).get("version")
        if isinstance(v, str):
            out.append(v)
    return out


def _query_latest_trunk_version(org_slug: str, name: str) -> Optional[str]:
    """Return the most recently updated trunk version for `<org>/<name>`,
    or None if no trunk version (in the ms-8 format) exists yet.

    Used as the `base_trunk` reference when a non-owner pushes a branch.
    """
    from google.cloud import firestore as fs

    collection = db.get_db().collection(_CAPABILITIES_COLLECTION)
    query = (
        collection.where("org_slug", "==", org_slug)
        .where("name", "==", name)
        .where("branch", "==", None)
        .where("deleted_at", "==", None)
        .order_by("updated_at", direction=fs.Query.DESCENDING)
        .limit(1)
    )
    for doc in query.stream():
        v = (doc.to_dict() or {}).get("version")
        if isinstance(v, str):
            return v
    return None


def _compute_route(
    org_slug: str, name: str, user_sub: str, user_email: str
) -> dict:
    """Decide whether this push goes to trunk or a branch (ms-8 §設計方針3).

    Returns:
      {
        "is_trunk": bool,
        "branch": str | None,           # branch name (== branch_identity), None for trunk
        "base_trunk": str | None,       # latest trunk version at push time, None for trunk
        "is_first_push": bool,          # True only if this push establishes the owner
      }
    """
    owner_sub, is_first = _get_or_establish_owner(
        org_slug, name, user_sub, user_email
    )
    if user_sub == owner_sub:
        return {
            "is_trunk": True,
            "branch": None,
            "base_trunk": None,
            "is_first_push": is_first,
        }
    # Non-owner → branch named after the pusher.
    branch = _sanitize_branch_identity(user_email)
    base_trunk = _query_latest_trunk_version(org_slug, name)
    return {
        "is_trunk": False,
        "branch": branch,
        "base_trunk": base_trunk,
        "is_first_push": False,
    }


def make_router(require_auth) -> APIRouter:
    """Wire the TrailNode router with the host app's auth dependency."""
    router = APIRouter(prefix="/api/trailnode", tags=["trailnode"])

    # ms-3 e-31: list endpoint. Must be declared BEFORE the path catch-all
    # GET below — otherwise `/capabilities/manifests` would be swallowed by
    # `/capabilities/{capability_id:path}` and fail id validation.
    @router.get("/capabilities/manifests")
    async def list_manifests(
        since: Optional[str] = None,
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        # Diff-sync endpoint. AI agents call this at session-start (via
        # SessionStart hook) to refresh the local manifest cache.
        #
        # since=None → return every visible active manifest (first sync).
        # since=ISO8601 → return manifests where updated_at >= since
        #   (composite index `deleted_at == None AND updated_at`) plus
        #   `deleted_ids` for the soft-delete frontier since the same
        #   point in time.
        #
        # ms-3 e-32: results are filtered to org slugs the caller is a
        # member of (`orgs.list_org_slugs_for_user`). A non-member can
        # never see another org's manifest — neither in `manifests` nor
        # as a bare id in `deleted_ids`.
        from datetime import datetime, timezone

        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                raise HTTPException(
                    400, detail=f"invalid since (expected ISO8601): {since!r}"
                )
            # Firestore Timestamps are UTC-aware; naive `since` is treated as UTC.
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)

        member_slugs = orgs.list_org_slugs_for_user(user.get("sub", ""))

        collection = db.get_db().collection(_CAPABILITIES_COLLECTION)

        active_query = collection.where("deleted_at", "==", None)
        if since_dt is not None:
            active_query = active_query.where("updated_at", ">=", since_dt)

        active_raw: list[dict] = []
        for doc in active_query.stream():
            data = doc.to_dict()
            for ts_field in ("uploaded_at", "updated_at", "deleted_at"):
                val = data.get(ts_field)
                if val is not None and hasattr(val, "isoformat"):
                    data[ts_field] = val.isoformat()
            active_raw.append(data)

        manifests = _filter_manifests_by_membership(active_raw, member_slugs)

        deleted_ids: list[str] = []
        if since_dt is not None:
            deleted_raw = [
                doc.to_dict()
                for doc in collection.where("deleted_at", ">=", since_dt).stream()
            ]
            for data in _filter_manifests_by_membership(deleted_raw, member_slugs):
                cap_id = data.get("id")
                if cap_id:
                    deleted_ids.append(cap_id)

        return JSONResponse(
            {
                "manifests": manifests,
                "deleted_ids": deleted_ids,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    @router.put("/capabilities/{capability_ref:path}")
    async def push_capability(
        capability_ref: str,
        manifest: str = Form(...),
        artifact: UploadFile = File(...),
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        """Push a capability (ms-8 trunk/branch model).

        URL accepts:
          /capabilities/<org>/<name>            ← new (server auto-assigns version)
          /capabilities/<org>/<name>@<version>  ← legacy (version ignored, server still auto-assigns)

        Push is NEVER allowed to specify `:branch` in the URL — branch routing
        is determined by the authenticated user's identity (§設計方針3).
        Attempts to do so are rejected with 409, including
        "branch-of-branch" attempts dressed up as `<org>/<name>:foo`.
        """
        parsed = _parse_capability_ref(capability_ref)
        org_slug = parsed["org_slug"]
        name = parsed["name"]
        client_branch = parsed["branch"]
        client_version = parsed["version"]  # ignored if present (legacy)

        if client_branch is not None:
            # The pusher tried to target a specific branch in the URL.
            # In the trunk/branch model, branch routing is server-side only
            # (uploaded_by determines branch). Allowing client-driven branch
            # selection would let anyone push to anyone's branch — and would
            # also let a branch owner attempt a "branch-of-branch".
            raise HTTPException(
                409,
                detail=(
                    f"push does not accept ':{client_branch}' in the URL — "
                    "branch routing is determined by the authenticated "
                    "user's identity. Re-push to "
                    f"/capabilities/{org_slug}/{name} (no :branch); the "
                    "server will route you to your own branch if you are "
                    "not the owner."
                ),
            )

        # ms-6: caller must be a member of `org_slug`. 403 (not 404) is
        # safe here — they already had to authenticate.
        user_sub = user.get("sub", "")
        user_email = user.get("email", "")
        orgs.require_org_member(org_slug, user_sub)

        try:
            manifest_data = json.loads(manifest)
        except json.JSONDecodeError as e:
            raise HTTPException(400, detail=f"manifest is not valid JSON: {e}")

        _validate_manifest_for_push(manifest_data, org_slug, name)

        # ms-8: decide trunk vs branch based on pusher identity, then assign
        # the next YYYY-MM-DD.N version. This happens BEFORE the GCS upload
        # so a doomed push (e.g. unauthenticated) doesn't burn bytes.
        route = _compute_route(org_slug, name, user_sub, user_email)
        branch = route["branch"]
        base_trunk = route["base_trunk"]

        today_str = _today_utc_str()
        existing_versions = _query_existing_versions(org_slug, name, branch)
        version = _compute_next_version_from_existing(today_str, existing_versions)

        # Canonical id and slug now depend on trunk vs branch.
        if branch is None:
            capability_id = f"{org_slug}/{name}@{version}"
        else:
            capability_id = f"{org_slug}/{name}:{branch}@{version}"
        object_key = _gcs_object_key_for(org_slug, name, branch, version)
        doc_slug = _slug_for(org_slug, name, branch, version)

        # GCS upload.
        bucket = _get_gcs_client().bucket(_BUCKET_NAME)
        blob = bucket.blob(object_key)
        content = await artifact.read()
        blob.upload_from_string(content, content_type="application/gzip")

        # Build the stored manifest. Server-decided fields override any
        # client-supplied values (id / version / branch / base_trunk).
        artifact_url_canonical = f"gs://{_BUCKET_NAME}/{object_key}"
        manifest_data["id"] = capability_id
        manifest_data["version"] = version
        manifest_data["artifact_url"] = [artifact_url_canonical]
        manifest_data["org_slug"] = org_slug
        manifest_data["uploaded_by"] = user_sub
        manifest_data["uploaded_by_email"] = user_email
        manifest_data["branch"] = branch  # None for trunk
        manifest_data["base_trunk"] = base_trunk  # None for trunk
        manifest_data["branch_identity"] = branch  # alias for clarity

        from google.cloud import firestore as fs
        manifest_data["uploaded_at"] = fs.SERVER_TIMESTAMP
        manifest_data["updated_at"] = fs.SERVER_TIMESTAMP
        manifest_data["deleted_at"] = None

        # ms-53 e-1163 concurrency hardening: wrap the doc write in a
        # Firestore transaction so two concurrent pushes for the same
        # (org, name, branch, today) can't both compute the same version
        # and silently overwrite each other.
        #
        # The window we're closing:
        #   T1: query existing_versions (sees []) → compute v=2026-06-08.1
        #   T2: query existing_versions (sees []) → compute v=2026-06-08.1
        #   T1: upload to GCS @ key for v=2026-06-08.1
        #   T2: upload to GCS @ key for v=2026-06-08.1 (different content)
        #   T1: doc.set(doc_slug, ...) ← writes
        #   T2: doc.set(doc_slug, ...) ← silently overwrites T1's manifest
        #
        # Inside the transaction we (a) re-read the target doc — registering
        # it on the transaction's read set so Firestore will Abort+retry on
        # commit conflict — and (b) refuse to write if it already exists.
        # The loser of the race gets 409 and is expected to retry the push
        # from the client side (which re-computes a higher version number
        # because the winner's doc is now visible in `_query_existing_versions`).
        # The GCS object uploaded by the loser is orphaned; cleaning that up
        # is a follow-up (cheap because doc-of-truth never points at it).
        client = db.get_db()
        doc_ref = client.collection(_CAPABILITIES_COLLECTION).document(doc_slug)

        @fs.transactional
        def _commit_doc(transaction):
            snap = doc_ref.get(transaction=transaction)
            if snap.exists:
                # Another push committed first with the same doc_slug.
                # We deliberately raise here (instead of overwriting) so
                # the caller can retry and pick the next version slot.
                raise _VersionRaceError(
                    f"concurrent push committed '{capability_id}' first "
                    "while this push was in flight — please retry"
                )
            transaction.set(doc_ref, manifest_data)

        try:
            _commit_doc(client.transaction())
        except _VersionRaceError as e:
            raise HTTPException(status_code=409, detail=str(e))

        logger.info(
            "trailnode push: %s by %s (branch=%s, base_trunk=%s, size=%d)",
            capability_id,
            user.get("email", "?"),
            branch or "(trunk)",
            base_trunk or "-",
            len(content),
        )
        return JSONResponse(
            {
                "id": capability_id,
                "version": version,
                "branch": branch,
                "base_trunk": base_trunk,
                "artifact_url": [artifact_url_canonical],
            }
        )

    @router.get("/capabilities/{capability_ref:path}")
    async def pull_capability(
        capability_ref: str,
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        """Pull a capability manifest.

        Accepts pinned refs in ms-8 (`@<version>`, optionally with `:<branch>`)
        and legacy ms-6 SemVer ids. The "latest" resolver (no `@<version>`)
        lands in ms-8 e-86; for now, omitting the version returns 400 with a
        pointer at the pin syntax.
        """
        parsed = _parse_capability_ref(capability_ref)
        if parsed["version"] is None:
            # ms-8 e-86 will add latest-resolver semantics here. Until then,
            # require an explicit pin so callers don't silently 404.
            raise HTTPException(
                400,
                detail=(
                    f"version is required when pulling '{capability_ref}' — "
                    "the latest resolver (no `@<version>`) lands in ms-8 e-86. "
                    "Pin the version explicitly for now."
                ),
            )

        org_slug = parsed["org_slug"]
        name = parsed["name"]
        branch = parsed["branch"]
        version = parsed["version"]

        # ms-6: 403 (not 404) on org-mismatch so users get a clearer
        # diagnostic when their token is fine but the org is wrong.
        orgs.require_org_member(org_slug, user.get("sub", ""))

        # ms-8: prefer the new branch-aware slug; fall back to the legacy
        # ms-6 slug so reads of pre-ms-8 docs keep working.
        ms8_slug = _slug_for(org_slug, name, branch, version)
        doc = (
            db.get_db()
            .collection(_CAPABILITIES_COLLECTION)
            .document(ms8_slug)
            .get()
        )
        if not doc.exists and branch is None:
            legacy_id = f"{org_slug}/{name}@{version}"
            doc = (
                db.get_db()
                .collection(_CAPABILITIES_COLLECTION)
                .document(_slug(legacy_id))
                .get()
            )
        if not doc.exists:
            label = (
                f"{org_slug}/{name}:{branch}@{version}"
                if branch
                else f"{org_slug}/{name}@{version}"
            )
            raise HTTPException(404, detail=f"capability '{label}' not found")
        manifest_data = doc.to_dict()
        # Firestore returns Timestamp fields as DatetimeWithNanoseconds which
        # JSONResponse can't serialize. Stringify any timestamp field; skip
        # None (deleted_at is None for active docs).
        for ts_field in ("uploaded_at", "updated_at", "deleted_at"):
            val = manifest_data.get(ts_field)
            if val is not None and hasattr(val, "isoformat"):
                manifest_data[ts_field] = val.isoformat()
        return JSONResponse(manifest_data)

    @router.get("/artifacts/{capability_ref:path}")
    async def get_artifact(
        capability_ref: str,
        user: dict = Depends(require_auth),
    ) -> StreamingResponse:
        parsed = _parse_capability_ref(capability_ref)
        if parsed["version"] is None:
            raise HTTPException(
                400,
                detail=(
                    f"version is required for artifacts; got '{capability_ref}'. "
                    "Pin the version explicitly."
                ),
            )
        org_slug = parsed["org_slug"]
        name = parsed["name"]
        branch = parsed["branch"]
        version = parsed["version"]

        # ms-6: same authz as manifest GET; artifact bytes must not leak
        # cross-org even if a URL is shared.
        orgs.require_org_member(org_slug, user.get("sub", ""))

        # Same legacy-fallback as pull_capability above.
        ms8_slug = _slug_for(org_slug, name, branch, version)
        doc = (
            db.get_db()
            .collection(_CAPABILITIES_COLLECTION)
            .document(ms8_slug)
            .get()
        )
        if not doc.exists and branch is None:
            legacy_id = f"{org_slug}/{name}@{version}"
            doc = (
                db.get_db()
                .collection(_CAPABILITIES_COLLECTION)
                .document(_slug(legacy_id))
                .get()
            )
        if not doc.exists:
            label = (
                f"{org_slug}/{name}:{branch}@{version}"
                if branch
                else f"{org_slug}/{name}@{version}"
            )
            raise HTTPException(404, detail=f"capability '{label}' not found")

        object_key = _gcs_object_key_for(org_slug, name, branch, version)
        blob = _get_gcs_client().bucket(_BUCKET_NAME).blob(object_key)
        if not blob.exists() and branch is None:
            # Legacy artifacts predate the branches/ sub-path.
            blob = _get_gcs_client().bucket(_BUCKET_NAME).blob(
                _gcs_object_key(org_slug, name, version)
            )
        if not blob.exists():
            label = (
                f"{org_slug}/{name}:{branch}@{version}"
                if branch
                else f"{org_slug}/{name}@{version}"
            )
            raise HTTPException(404, detail=f"artifact missing for '{label}'")

        filename_stem = f"{name}-{branch}" if branch else name

        def stream():
            with blob.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            stream(),
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename_stem}-{version}.tar.gz"'
            },
        )

    return router
