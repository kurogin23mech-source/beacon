"""User self-service ("/api/me/*") router.

ms-127 e-4869 (B フェーズ): the second resource extracted from the
server/app.py god-module and the FIRST auth-requiring one. It establishes the
``make_router(require_auth, ...)`` injection pattern (mirrors
``trailnode.make_router``) that the remaining resource routers (auth / orgs /
admin, then treks / projects) follow.

Pure move: every route body below is verbatim from app.py's ``me_*`` handlers —
same paths, same request/response shapes, no behavior change. The E2E proof is
tests/test_app_router_me_e4869.py (in-process TestClient through full routing).

Injected dependencies (owned by app.py, passed in to avoid an import cycle —
app.py imports this module, so this module must not import app.py back):

- ``require_auth``             — the host app's HTTPBearer + HMAC CLI-token
                                  identity dependency.
- ``stamp_session_liveness``   — ``app._stamp_session_liveness``. Stamps
                                  poll_health / ws_live / live onto a session
                                  row (ms-101 / e-3010). Still owned by app.py
                                  because the not-yet-extracted
                                  ``/api/projects/{pid}/sessions`` consumes the
                                  same helper — the liveness rule stays
                                  single-sourced.
- ``session_is_live``          — ``app._session_is_live``. The shutdown-aware
                                  ``--live`` predicate, shared with the same
                                  per-project endpoint (e-3220 requires BOTH
                                  directory endpoints to agree, so the rule
                                  lives in ONE place).

``db`` is bound the same way app.py binds it — ``store_router as db`` (e-1544:
``BEACON_STORE_BACKEND`` routes firestore / dynamodb). Importing the wrong
module here would silently swap the storage backend, so this must mirror app.py.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import store_router as db  # e-1544: same backend-routing binding app.py uses

# Same logger name as app.py's ``_server_logger`` (= logging.getLogger(
# "beacon.server")), so log records keep landing on the identical logger.
logger = logging.getLogger("beacon.server")


class MeMachineUpsert(BaseModel):
    """Body for POST /api/me/machine (e-1509).

    fingerprint is the client-supplied identifier that buckets "is this the
    same machine I saw before?". Typically the OS hostname. The server uses
    it as the lookup key and returns a fresh opaque machine_id on first
    sight; subsequent calls with the same fingerprint return the same
    machine_id.
    """
    fingerprint: str
    hostname: Optional[str] = None
    agent: Optional[str] = None


class MeHeartbeat(BaseModel):
    """Body for POST /api/me/heartbeat (e-1509).

    Identity tuple = (project_id, machine_id, parent_pid). Carries cwd and
    other heartbeat metadata as observational payload — server stores them
    on the session record but does not use them for identity lookup.
    """
    project_id: str
    machine_id: str
    parent_pid: int
    cwd: Optional[str] = None
    branch: Optional[str] = None
    focus_milestone: Optional[str] = None
    agent: Optional[dict] = None


class MeProfileUpdate(BaseModel):
    """Body for PATCH /api/me/profile (ms-78 / e-1909).

    Only ``display_name`` is mutable through this endpoint — email is the
    sign-in identity (managed by the OAuth provider) and ``user_id`` is
    immutable. Empty string clears the display name (= fall back to email
    in the UI).
    """
    display_name: str = ""


def make_router(
    require_auth: Callable,
    *,
    stamp_session_liveness: Callable[..., None],
    session_is_live: Callable[..., bool],
) -> APIRouter:
    """Build the /api/me/* router with the host app's auth + liveness helpers.

    Called once from app.py and mounted via::

        app.include_router(make_router(
            require_auth,
            stamp_session_liveness=_stamp_session_liveness,
            session_is_live=_session_is_live,
        ))

    The two liveness helpers are keyword-only (``*``) and ``Callable``-typed on
    purpose: this is the first multi-dependency router factory (``trailnode``
    takes only ``require_auth``), so the signature it sets is the型 the
    remaining resource routers (auth / orgs / admin, treks, projects) copy.
    Both helpers have the shape ``Callable[[dict, ...], ...]`` and would
    silently transpose if passed positionally; keyword-only makes a mis-wire a
    construction-time ``TypeError`` instead of a request-time crash, and the
    annotations stop a caller reading only the signature from mistaking
    ``session_is_live`` (a predicate callable) for a bool flag.
    """
    router = APIRouter()

    @router.get("/api/me/profile")
    def me_get_profile(user: dict = Depends(require_auth)):
        """Return the caller's own profile (display_name + email + user_id).

        ms-78 / e-1909 — the Web UI's Settings > Profile tab and the retroactive
        "you haven't set a display name yet" prompt both read this endpoint to
        discover the current state. We don't leak any field outside the
        user's own record (= same identity gate as every other /api/me/* route:
        ``require_auth`` resolves the JWT to a ``sub``).
        """
        uid = user.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="user has no sub claim")
        udata = db.get_user(uid) or {}
        email = (udata.get("email") or user.get("email") or "").strip()
        display_name = (udata.get("display_name") or "").strip()
        return {
            "user_id": uid,
            "email": email,
            "display_name": display_name,
        }

    @router.patch("/api/me/profile")
    def me_update_profile(body: MeProfileUpdate, user: dict = Depends(require_auth)):
        """Update the caller's own display_name (ms-78 / e-1909).

        Trimmed empty string explicitly clears the field — the UI then falls
        back to the email label. ``db.update_user`` is symmetric across the
        Firestore and DynamoDB backends (= store_router routes by
        ``BEACON_STORE_BACKEND``).
        """
        uid = user.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="user has no sub claim")
        display_name = (body.display_name or "").strip()
        # Mint the user record if absent (= first-time profile edit for an
        # auto-created identity that never went through invite-accept).
        udata = db.get_user(uid)
        if not udata:
            email = (user.get("email") or "").strip()
            try:
                db.get_or_create_user(uid, email)
            except Exception:  # noqa: BLE001 - best-effort mint, update still proceeds
                pass
        try:
            ok = db.update_user(uid, {"display_name": display_name})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"profile update failed: {e}")
        if not ok:
            raise HTTPException(status_code=404, detail="user record not found")
        return {
            "status": "ok",
            "user_id": uid,
            "display_name": display_name,
        }

    @router.get("/api/me/projects")
    def me_list_projects(user: dict = Depends(require_auth)):
        """List the calling user's project memberships with role (ms-62 / e-1509).

        Mirrors the filter logic in list_projects but emits a per-project role so
        callers (= /beacon-dm-send picker, dm_discover) get membership without
        scraping the broader project listing.
        """
        uid = user.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="user has no sub claim")
        items = db.list_projects(user_id=uid, include_archived=False)
        result = []
        for item in items:
            pid = item.get("project_id", "")
            if not pid:
                continue
            # Reach into the project doc once to discover the role. list_projects
            # does the membership filter but doesn't return role; this second
            # read is cheap because Firestore single-doc reads are fast and the
            # user typically has <50 projects.
            project = db.get_project(pid) or {}
            if project.get("owner") == uid:
                role = "owner"
            else:
                members = project.get("members", []) or []
                role = ""
                for m in members:
                    if m.get("user_id") == uid:
                        role = m.get("role", "member") or "member"
                        break
                # ms-95 / e-2794 (2026-07-03): 旧「migration-period 対応で無 owner
                # project を member 扱い」フォールバックを削除。上流の list_projects
                # が既に ownerless project を deny by default で除外しているため、
                # ここに到達する時点で role が決まらないケース (= owner でも member
                # でもない) は認可データの不整合であり、silent に member 化しない。
                if not role:
                    logger.warning(
                        "me_list_projects: project %s reached me-endpoint without "
                        "resolvable role for user %s; skipping",
                        pid, uid,
                    )
                    continue
            result.append({
                "id": pid,
                "name": item.get("name", ""),
                "role": role,
            })
        return result

    @router.get("/api/me/sessions")
    def me_list_sessions(
        live_only: bool = False,
        since_minutes: int = 5,
        healthy_only: bool = False,
        machine: str = "",
        agent: str = "",
        user: dict = Depends(require_auth),
    ):
        """Cross-project session directory for the calling user (ms-54 / e-1587).

        The per-project endpoint /api/projects/{pid}/sessions answers "who in
        *this* project is live"; what was missing was the cross-project view
        answering "what bclaude sessions of mine are alive *anywhere* right now".
        Without it, /beacon-dm-send had to cd into each candidate project to list
        DM recipients, and incident diagnosis (e.g. the e-1579 heartbeat-stop
        re-occurrence check) could not see live sessions outside the diagnostician's
        cwd.

        Same filter contract as the per-project endpoint (live_only, since_minutes,
        healthy_only, machine, agent). Each returned row carries the project_id +
        project_name it belongs to, so the dm picker can route the subsequent
        `bus send --project <pid>` without an extra lookup.

        Membership is enforced via db.list_projects(user_id=uid) — projects the
        user is neither owner nor member of are excluded. Archived projects are
        also excluded; resurrecting them is an explicit user action.
        """
        uid = user.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="user has no sub claim")

        import datetime
        now_dt = datetime.datetime.now(datetime.timezone.utc)

        items = db.list_projects(user_id=uid, include_archived=False)

        all_sessions: list[dict] = []
        for item in items:
            pid = item.get("project_id", "")
            if not pid:
                continue
            sessions = db.list_sessions(pid)
            # Stamp project context + poll_health on every row before filtering so
            # the consumer can disambiguate by project_id and read health without
            # a second round-trip. Same shape as the per-project endpoint plus
            # the new project_id / project_name fields.
            pname = item.get("name", "")
            for s in sessions:
                s["project_id"] = pid
                s["project_name"] = pname
                stamp_session_liveness(s, pid, now_dt)
            all_sessions.extend(sessions)

        def _matches(s: dict) -> bool:
            actor = s.get("actor") or {}
            if machine and actor.get("machine", "") != machine:
                return False
            if agent and actor.get("agent", "") != agent:
                return False
            return True

        filtered = [s for s in all_sessions if _matches(s)] if (machine or agent) else all_sessions

        if live_only:
            cutoff = now_dt - datetime.timedelta(minutes=since_minutes)
            cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            # e-3220: cross-project directory (the default `bus directory` path)
            # must drop shut-down daemons too — shared with the per-project filter.
            filtered = [s for s in filtered if session_is_live(s, cutoff_iso)]

        if healthy_only:
            # ms-101 / e-3010 — per-project endpoint と同じ union 判定。接続ベースの
            # ws_live か poll_healthy のどちらかで live なら healthy 受信者とみなす。
            filtered = [s for s in filtered if s.get("live") is True]

        filtered.sort(key=lambda s: s.get("last_active", ""), reverse=True)
        return filtered

    @router.post("/api/me/machine")
    def me_upsert_machine(
        body: MeMachineUpsert, user: dict = Depends(require_auth)
    ):
        """Get or mint a machine_id for (user, fingerprint) (ms-62 / e-1509).

        Returns ``{machine_id, minted, fingerprint}``. ``minted`` is True iff
        this call created the document (= the client should cache the returned
        machine_id in ~/.beacon/machine.json).
        """
        uid = user.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="user has no sub claim")
        if not body.fingerprint:
            raise HTTPException(
                status_code=400, detail="fingerprint is required"
            )
        machine_id, minted = db.get_or_mint_machine(
            uid, body.fingerprint,
            hostname=body.hostname or body.fingerprint,
            agent=body.agent or "",
        )
        return {
            "machine_id": machine_id,
            "minted": minted,
            "fingerprint": body.fingerprint,
        }

    @router.post("/api/me/heartbeat")
    def me_heartbeat(body: MeHeartbeat, user: dict = Depends(require_auth)):
        """Get or mint a session_id for the identity tuple (ms-62 / e-1509).

        Tuple = (project_id, machine_id, parent_pid). Returns
        ``{session_id, minted, last_heartbeat_at, created_at}``.

        The caller must be a member of project_id, otherwise 403 — this prevents
        a user from materialising session records in projects they don't
        belong to (= same membership boundary as the rest of /api/projects).
        """
        uid = user.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="user has no sub claim")
        if not body.project_id or not body.machine_id:
            raise HTTPException(
                status_code=400,
                detail="project_id and machine_id are required",
            )
        if not isinstance(body.parent_pid, int) or body.parent_pid <= 0:
            raise HTTPException(
                status_code=400, detail="parent_pid must be a positive integer",
            )
        # Reuse the existing project-load + membership check from
        # /api/projects/{p}/* so we don't drift on the membership rule.
        project = db.get_project(body.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        # Membership boundary. ms-158 / e-5773: this used to read
        # ``if owner and owner != uid and uid not in members`` — the leading
        # ``owner and`` made it fall OPEN for ownerless projects (owner falsy →
        # short-circuit → any signed-in user could mint session records into an
        # ownerless project). That is the same fail-open migration relic e-5757
        # closed in ``_get_role``. Deny by default: an ownerless project has no
        # members, so a caller who is neither owner nor in ``members`` is
        # rejected. (Kept inline rather than routed through ``_get_role`` on
        # purpose — ``_get_role`` bypasses to "owner" when auth is disabled, but
        # this endpoint enforces the membership boundary unconditionally so a
        # session record is never materialised in a project the caller doesn't
        # belong to. See test_me_heartbeat_rejects_* .)
        owner = project.get("owner")
        members = [m.get("user_id") for m in project.get("members", []) or []]
        if owner != uid and uid not in members:
            raise HTTPException(
                status_code=403,
                detail="not a member of this project",
            )

        metadata = {}
        if body.branch:
            metadata["git"] = {"branch": body.branch}
        if body.focus_milestone:
            metadata["focus"] = {"milestone": {"id": body.focus_milestone}}
        if body.agent:
            metadata["agent"] = body.agent
        result = db.get_or_mint_session_by_tuple(
            body.project_id,
            body.machine_id,
            body.parent_pid,
            user_id=uid,
            cwd=body.cwd or "",
            metadata=metadata,
        )
        return result

    return router
