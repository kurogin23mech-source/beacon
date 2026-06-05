"""TrailNode OrgService — server-side organization management.

Mounted under `/api/trailnode/orgs/*` on the Beacon Cloud Run service.

This module is the **server side** of the OrgService interface (ms-6 e-20 /
e-21). It is the **only** place that touches the `orgs` / `memberships`
Firestore collections — endpoints from `trailnode.py` (push/pull/artifact)
authorize through `is_member()` so they never reach Firestore for org data
directly. That isolation is the lever we'll pull when extracting OrgService
into a stand-alone Identity Service.

## Firestore schema (canonical definition)

Mirrored in `trailnode/src/trailnode/orgs.py` docstring; if you change one,
change both.

  Collection: `orgs/{slug}`  (slug = lowercase, [a-z0-9-]{2,32})
    {
      "slug": str,
      "name": str,
      "owner_sub": str,         # google subject id of the original creator
      "created_at": Timestamp,
    }

  Collection: `memberships/{slug}__{user_sub}`
    {
      "org_slug": str,
      "user_sub": str,
      "user_email": str,
      "role": "owner" | "member",
      "invited_by_sub": str,
      "invited_at": Timestamp,
    }

Composite key `{slug}__{user_sub}` is the membership document id so
`is_member()` is an O(1) `.document(...).get()` without composite indexes.
`list_members()` / `list_orgs_for_user()` use single-field `where` queries
which Firestore satisfies with its default automatic indexes.

## Authorization model

  - create_org      : anyone authenticated (caller becomes owner)
  - list_orgs       : anyone authenticated (sees only their own memberships)
  - get_org         : member only (others get 404 — don't leak existence)
  - invite_member   : owner only
  - list_members    : member only
  - remove_member   : owner only; cannot remove the last owner
  - leave_org       : member only; owners can leave iff another owner exists
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from google.cloud import firestore as fs

import firestore_client as db

logger = logging.getLogger(__name__)

_ENV = os.environ.get("BEACON_ENV", "dev")
_ORGS_COLLECTION = "orgs" if _ENV == "prod" else "orgs-dev"
_MEMBERSHIPS_COLLECTION = "memberships" if _ENV == "prod" else "memberships-dev"

# Same rule as the client; lockstep validation prevents drift.
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_MAX_NAME_LEN = 64


# ---------------------------------------------------------------------------
# OrgService — pure module (no class needed for MVP, easier to extract later)
# ---------------------------------------------------------------------------


def _membership_doc_id(slug: str, user_sub: str) -> str:
    # `user_sub` is a Google subject id (digits) — no '/' or '__' to escape.
    return f"{slug}__{user_sub}"


def _validate_slug(slug: str) -> None:
    if not _SLUG_PATTERN.match(slug):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid org slug '{slug}': must be lowercase letters, "
                "digits, or hyphens (2-32 chars, must start with letter or digit)"
            ),
        )


def _derive_slug_from_name(name: str) -> str:
    """Greedy default: lowercase, non [a-z0-9] → '-', collapse repeats, trim."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:32]


def _get_org_doc(slug: str) -> dict | None:
    doc = db.get_db().collection(_ORGS_COLLECTION).document(slug).get()
    return doc.to_dict() if doc.exists else None


def _get_membership(slug: str, user_sub: str) -> dict | None:
    doc = (
        db.get_db()
        .collection(_MEMBERSHIPS_COLLECTION)
        .document(_membership_doc_id(slug, user_sub))
        .get()
    )
    return doc.to_dict() if doc.exists else None


def _serialize_org(data: dict) -> dict:
    out = dict(data)
    if hasattr(out.get("created_at"), "isoformat"):
        out["created_at"] = out["created_at"].isoformat()
    return out


def _serialize_membership(data: dict) -> dict:
    out = dict(data)
    if hasattr(out.get("invited_at"), "isoformat"):
        out["invited_at"] = out["invited_at"].isoformat()
    return out


# ---------------------------------------------------------------------------
# Public service functions (called from trailnode.py for push/pull authz)
# ---------------------------------------------------------------------------


def is_member(slug: str, user_sub: str) -> bool:
    """O(1) membership check. Used by push/pull authz."""
    if not slug or not user_sub:
        return False
    return _get_membership(slug, user_sub) is not None


def get_role(slug: str, user_sub: str) -> str | None:
    """Return 'owner' / 'member' / None. Used for owner-only operations."""
    m = _get_membership(slug, user_sub)
    return m.get("role") if m else None


def require_org_member(slug: str, user_sub: str) -> None:
    """Raise 403 if the user is not a member of `slug`.

    Used by trailnode.py push/pull handlers so the authz lives in one
    place. We return 403 (not 404) once the URL parses to a known org
    shape — but for capability fetches the caller may instead surface
    404 to avoid leaking existence (decide at the call site).
    """
    if not is_member(slug, user_sub):
        raise HTTPException(
            status_code=403,
            detail=f"user is not a member of org '{slug}'",
        )


def list_orgs_for_user(user_sub: str) -> list[dict]:
    """Every org `user_sub` is a member of. Used by GET /api/trailnode/orgs."""
    membership_docs = (
        db.get_db()
        .collection(_MEMBERSHIPS_COLLECTION)
        .where("user_sub", "==", user_sub)
        .stream()
    )
    org_slugs = [m.to_dict().get("org_slug") for m in membership_docs]
    org_slugs = [s for s in org_slugs if s]

    out: list[dict] = []
    for slug in org_slugs:
        data = _get_org_doc(slug)
        if data:
            out.append(_serialize_org(data))
    return out


def _count_owners(slug: str) -> int:
    docs = (
        db.get_db()
        .collection(_MEMBERSHIPS_COLLECTION)
        .where("org_slug", "==", slug)
        .where("role", "==", "owner")
        .stream()
    )
    return sum(1 for _ in docs)


# ---------------------------------------------------------------------------
# Router (exposed via make_router(require_auth) from app.py, like trailnode.py)
# ---------------------------------------------------------------------------


def make_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/api/trailnode/orgs", tags=["trailnode-orgs"])

    # -----------------------------------------------------------------------
    # POST / — create org, caller becomes first owner.
    # -----------------------------------------------------------------------
    @router.post("")
    async def create_org(
        body: dict = Body(...),
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        name = (body.get("name") or "").strip()
        if not name or len(name) > _MAX_NAME_LEN:
            raise HTTPException(
                400, detail=f"name must be 1..{_MAX_NAME_LEN} chars"
            )
        slug = (body.get("slug") or _derive_slug_from_name(name)).strip()
        _validate_slug(slug)

        user_sub = user.get("sub", "")
        user_email = user.get("email", "")
        if not user_sub:
            raise HTTPException(401, detail="caller has no sub claim")

        db_client = db.get_db()
        org_ref = db_client.collection(_ORGS_COLLECTION).document(slug)
        if org_ref.get().exists:
            raise HTTPException(
                409, detail=f"org slug '{slug}' already exists"
            )

        org_data = {
            "slug": slug,
            "name": name,
            "owner_sub": user_sub,
            "created_at": fs.SERVER_TIMESTAMP,
        }
        org_ref.set(org_data)

        membership_data = {
            "org_slug": slug,
            "user_sub": user_sub,
            "user_email": user_email,
            "role": "owner",
            "invited_by_sub": user_sub,  # self-invite
            "invited_at": fs.SERVER_TIMESTAMP,
        }
        db_client.collection(_MEMBERSHIPS_COLLECTION).document(
            _membership_doc_id(slug, user_sub)
        ).set(membership_data)

        logger.info("trailnode orgs: created %s by %s", slug, user_email or user_sub)

        created = _get_org_doc(slug) or org_data
        return JSONResponse(_serialize_org(created))

    # -----------------------------------------------------------------------
    # GET / — list orgs the caller is a member of.
    # -----------------------------------------------------------------------
    @router.get("")
    async def list_orgs(user: dict = Depends(require_auth)) -> JSONResponse:
        return JSONResponse(list_orgs_for_user(user.get("sub", "")))

    # -----------------------------------------------------------------------
    # GET /{slug} — fetch a single org (member only).
    # -----------------------------------------------------------------------
    @router.get("/{slug}")
    async def get_org(
        slug: str, user: dict = Depends(require_auth)
    ) -> JSONResponse:
        _validate_slug(slug)
        if not is_member(slug, user.get("sub", "")):
            # 404 on purpose so non-members can't probe org existence.
            raise HTTPException(404, detail=f"org '{slug}' not found")
        data = _get_org_doc(slug)
        if data is None:
            raise HTTPException(404, detail=f"org '{slug}' not found")
        return JSONResponse(_serialize_org(data))

    # -----------------------------------------------------------------------
    # POST /{slug}/invite — owner adds a member by email.
    # -----------------------------------------------------------------------
    @router.post("/{slug}/invite")
    async def invite_member(
        slug: str,
        body: dict = Body(...),
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        _validate_slug(slug)
        user_sub = user.get("sub", "")
        if get_role(slug, user_sub) != "owner":
            raise HTTPException(403, detail="only owners can invite members")

        email = (body.get("email") or "").strip().lower()
        if not email:
            raise HTTPException(400, detail="email is required")
        role = (body.get("role") or "member").strip()
        if role not in ("owner", "member"):
            raise HTTPException(400, detail="role must be 'owner' or 'member'")

        target = db.find_user_by_email(email)
        if target is None:
            raise HTTPException(
                404,
                detail=(
                    f"no user found with email '{email}'. "
                    "They need to `beacon auth login` once before being invited."
                ),
            )
        target_sub, target_data = target

        membership_id = _membership_doc_id(slug, target_sub)
        member_ref = (
            db.get_db().collection(_MEMBERSHIPS_COLLECTION).document(membership_id)
        )
        if member_ref.get().exists:
            raise HTTPException(
                409, detail=f"user '{email}' is already a member of '{slug}'"
            )

        membership_data = {
            "org_slug": slug,
            "user_sub": target_sub,
            "user_email": target_data.get("email", email),
            "role": role,
            "invited_by_sub": user_sub,
            "invited_at": fs.SERVER_TIMESTAMP,
        }
        member_ref.set(membership_data)
        logger.info(
            "trailnode orgs: invited %s into %s by %s",
            email, slug, user.get("email", user_sub),
        )

        # Refetch to serialize the server-resolved timestamp.
        saved = member_ref.get().to_dict() or membership_data
        return JSONResponse(_serialize_membership(saved))

    # -----------------------------------------------------------------------
    # GET /{slug}/members — list memberships (member only).
    # -----------------------------------------------------------------------
    @router.get("/{slug}/members")
    async def list_members(
        slug: str, user: dict = Depends(require_auth)
    ) -> JSONResponse:
        _validate_slug(slug)
        require_org_member(slug, user.get("sub", ""))

        docs = (
            db.get_db()
            .collection(_MEMBERSHIPS_COLLECTION)
            .where("org_slug", "==", slug)
            .stream()
        )
        return JSONResponse([_serialize_membership(d.to_dict()) for d in docs])

    # -----------------------------------------------------------------------
    # DELETE /{slug}/members/{user_sub} — owner removes a member.
    # -----------------------------------------------------------------------
    @router.delete("/{slug}/members/{target_sub}")
    async def remove_member(
        slug: str,
        target_sub: str,
        user: dict = Depends(require_auth),
    ) -> JSONResponse:
        _validate_slug(slug)
        caller_sub = user.get("sub", "")
        if get_role(slug, caller_sub) != "owner":
            raise HTTPException(403, detail="only owners can remove members")

        target = _get_membership(slug, target_sub)
        if target is None:
            raise HTTPException(
                404, detail=f"user '{target_sub}' is not a member of '{slug}'"
            )

        # Refuse to drop the last owner (the org would become un-administer-able).
        if target.get("role") == "owner" and _count_owners(slug) <= 1:
            raise HTTPException(
                400,
                detail=(
                    "cannot remove the last owner; promote another member to "
                    "owner first"
                ),
            )

        db.get_db().collection(_MEMBERSHIPS_COLLECTION).document(
            _membership_doc_id(slug, target_sub)
        ).delete()
        logger.info(
            "trailnode orgs: removed %s from %s by %s",
            target_sub, slug, user.get("email", caller_sub),
        )
        return JSONResponse({"removed": target_sub, "org_slug": slug})

    # -----------------------------------------------------------------------
    # POST /{slug}/leave — current user leaves the org.
    # -----------------------------------------------------------------------
    @router.post("/{slug}/leave")
    async def leave_org(
        slug: str, user: dict = Depends(require_auth)
    ) -> JSONResponse:
        _validate_slug(slug)
        caller_sub = user.get("sub", "")
        membership = _get_membership(slug, caller_sub)
        if membership is None:
            raise HTTPException(
                404, detail=f"you are not a member of '{slug}'"
            )
        if membership.get("role") == "owner" and _count_owners(slug) <= 1:
            raise HTTPException(
                400,
                detail=(
                    "you are the last owner; promote another member to owner "
                    "before leaving (or use `trailnode org` admin commands)"
                ),
            )
        db.get_db().collection(_MEMBERSHIPS_COLLECTION).document(
            _membership_doc_id(slug, caller_sub)
        ).delete()
        logger.info(
            "trailnode orgs: %s left %s", user.get("email", caller_sub), slug
        )
        return JSONResponse({"left": slug})

    return router
