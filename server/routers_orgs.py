"""Organization ("/api/orgs/*") router — Beacon team orgs (ms-113 / ms-118).

ms-127 e-4869 (B フェーズ): the Beacon org REST endpoints extracted from the
server/app.py god-module, following the auth-router型 established by
server/routers_me.py (make_router(require_auth, *, ...injected helpers)).

NOTE: these are the **Beacon** org endpoints (org tenancy, ms-113). They are
distinct from server/trailnode_orgs.py, which serves /api/trailnode/orgs for the
TrailNode sister product. Do not conflate the two.

Pure move: every route body is verbatim from app.py's ``*_org_endpoint``
handlers — same paths, same shapes, no behavior change. Owner/membership rules
live in lib/org.py (imported here as ``org_mod``) and are not reimplemented.

Injected dependencies (owned by app.py, passed in to avoid an import cycle —
app.py imports this module, so this module must not import app.py back):

- ``require_auth``          — the host app's identity dependency.
- ``is_auth_enabled``       — a zero-arg callable returning app.py's current
                              ``_auth_enabled``. Passed as a callable, NOT a
                              bool, because the value is read per-request and the
                              test suite flips ``app._auth_enabled`` at runtime;
                              a frozen bool captured at mount time would go stale
                              (and silently skip the membership guards in dev
                              mode inconsistently). Route bodies call
                              ``is_auth_enabled()`` where app.py read
                              ``_auth_enabled``.
- ``load_org_for_member``   — ``app._load_org_for_member``. Loads an org and
                              enforces caller membership (404 for non-members,
                              hiding existence). Still owned by app.py because
                              the not-yet-extracted ``/api/projects/{id}/rehome``
                              endpoint consumes the same helper — kept
                              single-sourced (e-4871 will revisit).

``db`` mirrors app.py's binding — ``store_router as db`` (e-1544 backend
routing). Importing the wrong module would silently swap the storage backend.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import org as org_mod  # ms-113 / e-3731: Organization tenancy primitives
import store_router as db  # e-1544: same backend-routing binding app.py uses


class OrgCreate(BaseModel):
    name: str


class OrgMemberAdd(BaseModel):
    email: str
    role: str = "member"


def make_router(
    require_auth: Callable,
    *,
    is_auth_enabled: Callable[[], bool],
    load_org_for_member: Callable[[str, dict], dict],
) -> APIRouter:
    """Build the /api/orgs/* router with the host app's auth + org helpers.

    Called once from app.py and mounted via::

        app.include_router(make_router(
            require_auth,
            is_auth_enabled=lambda: _auth_enabled,
            load_org_for_member=_load_org_for_member,
        ))

    Keyword-only + ``Callable``-typed injected helpers (same rationale as
    routers_me.make_router): a mis-wire fails at construction rather than at
    request time, and the signature self-describes.
    """
    # Construction-time type guard. ``is_auth_enabled`` in particular reads as a
    # bool flag (its name mirrors app.py's ``_auth_enabled``), so a caller may
    # pass the bool itself instead of ``lambda: _auth_enabled``. Keyword-only
    # arguments stop positional transposition but NOT a wrong-type value, so we
    # check callability here — turning that mis-wire into a mount-time TypeError
    # rather than a ``'bool' object is not callable`` crash on the first request.
    for _name, _dep in (
        ("require_auth", require_auth),
        ("is_auth_enabled", is_auth_enabled),
        ("load_org_for_member", load_org_for_member),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_orgs.make_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function "
                f"(e.g. is_auth_enabled=lambda: _auth_enabled), not a value."
            )

    router = APIRouter()

    @router.post("/api/orgs")
    def create_org_endpoint(body: OrgCreate, user: dict = Depends(require_auth)):
        """Create a team org. Caller becomes owner.

        作成者 identity は auth token から解決する (= client が owner を詐称できない、
        trek の create と同型)。org 所属はアクセスを与えない — participation-only なので
        社員は別途 project に参加させて初めてその project が見える (SPEC 方針2)。
        """
        import datetime
        if not body.name or not body.name.strip():
            raise HTTPException(status_code=400, detail="org name is required")
        creator = user.get("sub", "")
        if not creator:
            # auth 無効モード等で sub が空だと new_org が ValueError を投げ、捕捉が
            # 無いと unhandled 500 になる。org 作成は認証された user を要するので、
            # ここで明示的に 400 にして回復経路を示す (list の _auth_enabled 分岐と対称)。
            raise HTTPException(
                status_code=400,
                detail="creating an org requires an authenticated user "
                       "(auth 無効モードでは org を作成できません)",
            )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        doc = org_mod.new_org(
            body.name,
            creator_user_id=creator,
            creator_email=user.get("email", ""),
            now=now,
        )
        db.save_org(doc["org_id"], doc)
        return doc

    @router.get("/api/orgs")
    def list_orgs_endpoint(user: dict = Depends(require_auth)):
        """List orgs the caller is a member of.

        可視性は org membership で絞る (= 自分が所属する org だけ)。org を跨いだ
        相互可視は participation-only の外なので、ここでは出さない。
        """
        user_filter = user.get("sub") if is_auth_enabled() else None
        return db.list_orgs_for_user(user_filter)

    @router.get("/api/orgs/{org_id}")
    def get_org_endpoint(org_id: str, user: dict = Depends(require_auth)):
        """Get a single org by id. Member only — 非 member は 404 で存在を漏らさない。"""
        doc = db.get_org(org_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="org not found")
        if is_auth_enabled() and not org_mod.is_org_member(doc, user.get("sub", "")):
            # 存在を漏らさない (= trailnode get_org / 高リスク endpoint と同方針)
            raise HTTPException(status_code=404, detail="org not found")
        return doc

    @router.post("/api/orgs/{org_id}/members")
    def add_org_member_endpoint(org_id: str, body: OrgMemberAdd,
                                user: dict = Depends(require_auth)):
        """Add a member into an org — 所属だけを与え、アクセスは付けない (CLI: org add-member)。

        participation-only (ms-113 / SPEC 方針2): この endpoint は org doc の members[]
        にしか書かず、どの project の participation (= 参加 = アクセス) も変えない。追加された
        社員は org の member になるが、必要な project に別途参加させるまで何も見えない。
        承諾フローは無い即時追加 (= project 側の token+accept 招待とは別物)。
        """
        import datetime
        org = load_org_for_member(org_id, user)
        email = (body.email or "").strip()
        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        if "@" not in email:
            # user-id を渡された誤診を防ぐ (add-member は email を受ける)。
            raise HTTPException(
                status_code=400,
                detail="add-member takes an email address, not a user-id")
        # mint できる role は member / admin のみ。値域検証は lib/org.py に一本化し、
        # local (LocalStore.add_org_member) と物理的に一致させる。
        try:
            org_mod.validate_invitable_role(body.role)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # email を実 user に解決する。存在しなければ 404 (= まだ Beacon アカウントが無い)。
        found = db.find_user_by_email(email)
        if not found:
            raise HTTPException(
                status_code=404,
                detail=f"no Beacon user for email '{email}' "
                       "(先に相手がサインアップする必要があります)")
        invitee_uid, _ = found
        # add-only: 既に member なら role を silent 上書きせず 409 で弾く (= 冪等のつもりの
        # 再追加で admin が member に降格する事故を防ぐ)。role 変更は別操作。
        if org_mod.is_org_member(org, invitee_uid):
            raise HTTPException(
                status_code=409,
                detail=f"{email} is already a member "
                       "(role の変更は add-member ではなく別操作で行います)")
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        org_mod.add_org_member(org, invitee_uid, role=body.role, email=email,
                               added_by=user.get("sub", ""), now=now)
        db.save_org(org["org_id"], org)  # ← project は一切触らない (participation-only)
        return org

    @router.delete("/api/orgs/{org_id}/members/{target}")
    def remove_org_member_endpoint(org_id: str, target: str,
                                   user: dict = Depends(require_auth)):
        """Remove a member (user_id or email) from an org — 破壊的操作なので owner のみ (e-4234).

        ms-118 SPEC 受入条件6: 「org 削除 / member 削除は owner のみ」。e-4232 は暫定で
        owner / admin に緩めていたが、e-4234 で org 削除と同じ ``is_destructive_allowed``
        (= owner のみ) に統一する。admin は member の **追加** (非破壊的な add-member) は
        できるが、削除はできない (= 破壊的操作は owner に集約)。
        """
        org = load_org_for_member(org_id, user)
        if is_auth_enabled() and not org_mod.is_destructive_allowed(org, user.get("sub", "")):
            raise HTTPException(
                status_code=403,
                detail="removing an org member requires owner")
        member = org_mod.find_org_member(org, target)
        if not member:
            raise HTTPException(status_code=404,
                                detail=f"org member '{target}' not found")
        try:
            org_mod.remove_org_member(org, member.get("user_id"))
        except ValueError as e:
            # last-owner 保護 (= org を owner 不在にしない)
            raise HTTPException(status_code=400, detail=str(e))
        db.save_org(org["org_id"], org)
        return org

    @router.delete("/api/orgs/{org_id}")
    def delete_org_endpoint(org_id: str, user: dict = Depends(require_auth)):
        """Delete a team org — 破壊的操作なので owner のみ (ms-118 / e-4234, SPEC 受入条件6).

        member 削除と同じ ``is_destructive_allowed`` (= owner only) ガードを共有する。
        personal org (= 個人組織) は構造的に削除不可 (= 自動生成の器を消させない、400)。
        非 member には org の存在を漏らさない (404、``load_org_for_member`` 経由)。
        """
        org = load_org_for_member(org_id, user)
        if is_auth_enabled() and not org_mod.is_destructive_allowed(org, user.get("sub", "")):
            raise HTTPException(status_code=403,
                                detail="deleting an org requires owner")
        try:
            org_mod.assert_org_deletable(org)  # personal org は消せない
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        db.delete_org(org_id)  # project は触らない (org 所属リンクの後始末は re-home の担当)
        return {"org_id": org_id, "deleted": True}

    @router.get("/api/orgs/{org_id}/overview")
    def org_overview_endpoint(org_id: str, user: dict = Depends(require_auth)):
        """org 俯瞰: この org に属する project と各 project の member を返す (ms-118 / e-4236).

        最小 UI (= read-only の俯瞰、SPEC 受入条件7「どの org に どの project と member が
        あり、誰がどの project に参加しているか」) が消費するデータ。caller が member の
        org について、caller が参加している project のうち org_id 所属のものだけを列挙し、
        各 project の member を role + external_guest (= org 非所属の外部ゲスト、e-4235)
        付きで返す。

        participation-only 準拠: caller が参加していない project は列挙しない (= 開示は
        現在の参加でのみ与えられる)。非 member の org は 404 で存在を漏らさない
        (``load_org_for_member`` 経由)。project は一切書き換えない read-only。
        """
        org = load_org_for_member(org_id, user)
        uid = user.get("sub", "")
        projects_out = []
        for summ in (db.list_projects(uid) or []):
            qid = summ.get("project_id") or summ.get("id")
            if not qid:
                continue
            q = db.get_project(qid)
            if not q or org_mod.project_org_id(q) != org_id:
                continue
            # owner (top-level) + members[] を 1 リストに畳む (owner 重複は除く)。
            rows, seen = [], set()
            owner_id = q.get("owner")
            if owner_id:
                rows.append({"user_id": owner_id, "role": "owner"})
                seen.add(owner_id)
            for m in (q.get("members") or []):
                mid = m.get("user_id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                rows.append({"user_id": mid, "role": m.get("role", "viewer")})
            # 外部ゲスト判定は e-4235 の helper に一本化 (team org 限定)。
            guest_ids = org_mod.external_guest_user_ids(org, [r["user_id"] for r in rows])
            for r in rows:
                r["external_guest"] = r["user_id"] in guest_ids
            projects_out.append({"project_id": qid, "name": q.get("name", ""),
                                 "members": rows})
        return {"org_id": org_id, "name": org.get("name", ""),
                "projects": projects_out}

    return router
