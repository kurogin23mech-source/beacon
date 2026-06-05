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

_BUCKET_NAME = os.environ.get("TRAILNODE_BUCKET", "trailnode-artifacts")
_ENV = os.environ.get("BEACON_ENV", "dev")
_CAPABILITIES_COLLECTION = "capabilities" if _ENV == "prod" else "capabilities-dev"
_GCS_PREFIX = f"{_ENV}/"  # keep dev/prod artifacts isolated

# ms-6: <org>/<name>@<semver>. The first segment used to be the uploader's
# username (`author`); now it is the organization slug. Same character class
# (lowercase letters, digits, hyphens) so the regex shape is unchanged —
# only the semantic name shifts.
_ID_PATTERN = re.compile(
    r"^([a-z0-9-]+)/([a-z0-9-]+)@(\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?)$"
)

# `artifact_url` is intentionally NOT required from clients — the server
# fills it in after the GCS upload. `author` stays required as a free-form
# label of the human author (decoupled from the org since ms-6).
_REQUIRED_MANIFEST_FIELDS = (
    "schema_version", "id", "name", "type", "version", "description", "author",
)
_SUPPORTED_TYPES = ("skill", "app", "program", "ai-workflow")  # ms-5: 4-type gate (client mirror: trailnode src/trailnode/push.py)

_gcs_client: gcs_storage.Client | None = None


def _get_gcs_client() -> gcs_storage.Client:
    global _gcs_client
    if _gcs_client is None:
        _gcs_client = gcs_storage.Client()
    return _gcs_client


def _parse_capability_id(capability_id: str) -> tuple[str, str, str]:
    """Return (org_slug, name, version) on success."""
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


def _slug(capability_id: str) -> str:
    return capability_id.replace("/", "__").replace("@", "__")


def _gcs_object_key(org_slug: str, name: str, version: str) -> str:
    return f"{_GCS_PREFIX}{org_slug}/{name}/{version}.tar.gz"


def _validate_manifest(
    data: dict, capability_id: str, org_slug: str, name: str, version: str
) -> None:
    missing = [f for f in _REQUIRED_MANIFEST_FIELDS if f not in data]
    if missing:
        raise HTTPException(400, detail=f"manifest missing required fields: {missing}")
    if data["id"] != capability_id:
        raise HTTPException(
            400, detail=f"manifest.id '{data['id']}' != URL capability_id '{capability_id}'"
        )
    # ms-6: the first id segment is the org, not the user's username. We
    # still re-derive it from the id (single source of truth) and verify
    # `name`/`version`. `author` is now a free-form human label and does
    # not need to match the id; it documents the human author distinct
    # from the org and from `uploaded_by` (the actual user who ran push).
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
    # `artifact_url` is filled in server-side; tolerate absent/empty.
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

    @router.put("/capabilities/{capability_id:path}")
    async def push_capability(
        capability_id: str,
        manifest: str = Form(...),
        artifact: UploadFile = File(...),
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        org_slug, name, version = _parse_capability_id(capability_id)

        # ms-6: caller must be a member of `org_slug`. 403 (not 404) is
        # safe here — they already had to authenticate, and they get a
        # clearer signal when their `--org` was wrong vs. when the org
        # doesn't exist.
        user_sub = user.get("sub", "")
        orgs.require_org_member(org_slug, user_sub)

        try:
            manifest_data = json.loads(manifest)
        except json.JSONDecodeError as e:
            raise HTTPException(400, detail=f"manifest is not valid JSON: {e}")

        _validate_manifest(manifest_data, capability_id, org_slug, name, version)

        object_key = _gcs_object_key(org_slug, name, version)
        bucket = _get_gcs_client().bucket(_BUCKET_NAME)
        blob = bucket.blob(object_key)
        content = await artifact.read()
        blob.upload_from_string(content, content_type="application/gzip")

        artifact_url_canonical = f"gs://{_BUCKET_NAME}/{object_key}"
        manifest_data["artifact_url"] = [artifact_url_canonical]
        # ms-6: split the org (auth scope) from the user who ran push.
        manifest_data["org_slug"] = org_slug
        manifest_data["uploaded_by"] = user_sub
        manifest_data["uploaded_by_email"] = user.get("email", "")

        from google.cloud import firestore as fs
        manifest_data["uploaded_at"] = fs.SERVER_TIMESTAMP
        # ms-3 e-30: updated_at is the diff-sync frontier (server returns
        # docs where updated_at >= since). Re-push always bumps it, so a
        # soft-deleted manifest re-pushed under the same id is implicitly
        # un-deleted: deleted_at goes back to None.
        manifest_data["updated_at"] = fs.SERVER_TIMESTAMP
        manifest_data["deleted_at"] = None

        slug = _slug(capability_id)
        db.get_db().collection(_CAPABILITIES_COLLECTION).document(slug).set(manifest_data)

        logger.info(
            "trailnode push: %s by %s (size=%d)",
            capability_id, user.get("email", "?"), len(content),
        )
        return JSONResponse({"id": capability_id, "artifact_url": [artifact_url_canonical]})

    @router.get("/capabilities/{capability_id:path}")
    async def pull_capability(
        capability_id: str,
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        org_slug, _name, _version = _parse_capability_id(capability_id)

        # ms-6: 403 (not 404) on org-mismatch so users get a clearer
        # diagnostic when their token is fine but the org is wrong. We
        # still 404 on capability-not-found below.
        orgs.require_org_member(org_slug, user.get("sub", ""))

        slug = _slug(capability_id)
        doc = db.get_db().collection(_CAPABILITIES_COLLECTION).document(slug).get()
        if not doc.exists:
            raise HTTPException(404, detail=f"capability '{capability_id}' not found")
        manifest_data = doc.to_dict()
        # Firestore returns Timestamp fields as DatetimeWithNanoseconds which
        # JSONResponse can't serialize. Stringify any timestamp field; skip
        # None (deleted_at is None for active docs).
        for ts_field in ("uploaded_at", "updated_at", "deleted_at"):
            val = manifest_data.get(ts_field)
            if val is not None and hasattr(val, "isoformat"):
                manifest_data[ts_field] = val.isoformat()
        return JSONResponse(manifest_data)

    @router.get("/artifacts/{capability_id:path}")
    async def get_artifact(
        capability_id: str,
        user: dict = Depends(require_auth),
    ) -> StreamingResponse:
        org_slug, name, version = _parse_capability_id(capability_id)

        # ms-6: same authz as manifest GET; artifact bytes must not leak
        # cross-org even if a URL is shared.
        orgs.require_org_member(org_slug, user.get("sub", ""))

        slug = _slug(capability_id)
        doc = db.get_db().collection(_CAPABILITIES_COLLECTION).document(slug).get()
        if not doc.exists:
            raise HTTPException(404, detail=f"capability '{capability_id}' not found")

        object_key = _gcs_object_key(org_slug, name, version)
        blob = _get_gcs_client().bucket(_BUCKET_NAME).blob(object_key)
        if not blob.exists():
            raise HTTPException(404, detail=f"artifact missing for '{capability_id}'")

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
                "Content-Disposition": f'attachment; filename="{name}-{version}.tar.gz"'
            },
        )

    return router
