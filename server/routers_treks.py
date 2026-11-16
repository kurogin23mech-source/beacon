"""Trek ("/api/treks/*") の **読み出しだけ** を提供するルータ (ms-185 e-7070)。

この機能は畳まれた。e-7069 が両フロントの `beacon trek` と Skill 4 本を落とし、
e-7079 が作成経路 (POST /api/treks) と画面の入口を落とし、e-7070 が本体 ——
``lib/trek*.py`` / ``lib/cmd_trek.py`` / サーバの定期集約 (``_run_trek_tick``) /
この module の書き込み 41 ルート —— を落とした。

**残っているのは読み出し 3 ルートだけで、これは意図である** (ms-185 SPEC 方針5 =
記録を後から読めなくしない)。過去に作られた Trek の記録は「当時そう判断した事実」
なので、機能を畳んでも監査のために引けなければならない。消す/残すの判断と理由は
e-7081 の判断記録にあり、``tests/test_trek_record_read_surface_e7081.py`` が
この 3 ルートの存在を 1 つずつ名指しで固定している (落とすと赤くなる)。

同じ作法の先例は ``lib/dm_consent.RETIRED_CONSENT_REASON_EXPLANATIONS`` ——
規則は消したが、過去の監査記録に残る判定理由の**説明だけ**は読めるまま残した。
読む側だけ残す、という同じ形をここでも採る。

**新しい機能をここに足さないこと。** 書き込みは戻さない: 作成経路が無いので
新しい記録は生まれず、既存 14 件 + ローカル 3 件を読む面としてのみ生かしている。
POST /api/treks が戻っていないことは
``tests/test_trek_removal_pinned_e7079.py::test_no_creation_route_on_the_server``
が固定している。

Injected dependencies (owned by app.py, passed in to avoid an import cycle):

- ``require_auth``     — the host app's identity dependency.
- ``_require_admin``   — ``app._require_admin`` (instance-admin gate、
                         ``?all_actors=true`` の管理者ビューで使う)。
- ``is_auth_enabled``  — zero-arg callable returning app.py's current
                         ``_auth_enabled`` bool (getter, not snapshot —
                         実行中にフラグを倒すテストに追随するため)。

``db`` mirrors app.py's binding — ``store_router as db`` (e-1544 backend routing)。
読み取り関数 ``get_trek`` / ``list_treks`` / ``list_trek_logs`` が両 backend
(firestore_client / dynamodb_client) に在ることも e-7081 のテストが固定している
(片方だけ残すと backend を切り替えた瞬間に読めなくなる)。
"""
from __future__ import annotations

import json as _json
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

import store_router as db  # e-1544: same backend-routing binding app.py uses

# ---------------------------------------------------------------------------
# members[] の読み取り粒度 (ms-97 / e-2658 Phase 1 AC6 由来)
#
# e-7070 以前はこの 3 つを ``lib/trek.py`` (5,146 行) から import していた。
# 読み出しに要るのはこれだけなので、本体を落とすにあたって**ここへ写した**
# (読み取り 3 ルートのために 5,146 行を残すのは、畳む目的と逆行する)。
#
# なぜ粒度が 2 つあるか: phase A 以降の trek は ``members[]`` が session_id
# keyed で、pre-A の trek は user_id keyed のまま残っている。1 user が N
# session を持つので user_id は phase A 以降では曖昧であり、session_id が
# member identity の真値。両方の世代の記録が現に在るため、読み出しは
# どちらの粒度でも引けなければならない。
# ms-185 e-7326: **members の世代判定を宣言している唯一の場所がここ。**
# e-7070 で正本 (``lib/trek.py``) を落としたとき、この判定は ``lib/cmd_bus.py`` と
# この file の 2 つに写され、一致を測る検査が無い状態になっていた (e-7069 が
# 「二重実装は黙ってズレる」として足した突き合わせを、削除の過程で失った形)。
# cmd_bus 側の写しは e-7080 / e-7321 で送信予算の迂回判定ごと落ちたので、残るのは
# ここ 1 つである。
#
# **phase を増やすとき (例 'D') は、ここだけを直せばよい。** 他に写しが無いことは
# ``tests/test_member_generation_single_owner_e7326.py`` が固定しており、2 つ目の
# 宣言が現れた時点で赤くなる。共有する必要が出たら、写すのではなく一方が他方を
# import すること。
# ---------------------------------------------------------------------------
_DEFAULT_MIGRATION_PHASE = "pre-A"
_VALID_MIGRATION_PHASES = ("pre-A", "A", "B", "C")
_MIGRATION_PHASE_META_KEY = "migration_phase"


def _get_migration_phase(trek_doc: dict) -> str:
    """Return the trek's members migration phase (= "pre-A" if absent).

    ``trek.meta.migration_phase`` が無い trek は migration 未着手として扱う
    (= 旧 user_id keyed のまま)。
    """
    meta = trek_doc.get("meta") or {}
    phase = meta.get(_MIGRATION_PHASE_META_KEY) or _DEFAULT_MIGRATION_PHASE
    if phase not in _VALID_MIGRATION_PHASES:
        return _DEFAULT_MIGRATION_PHASE
    return phase


def _is_session_id_keyed(trek_doc: dict) -> bool:
    """True if this trek's ``members[]`` is session_id keyed (phase A/B/C)."""
    return _get_migration_phase(trek_doc) in ("A", "B", "C")


def _find_member(trek_doc: dict, *, user_id: str = "",
                 session_id: str = "") -> Optional[dict]:
    """Return the member dict matching the given grain, or None if absent.

    session_id を与えられたらそちらを優先する (phase A 以降の真値)。
    どちらも空なら None (呼び出し側が 403 を出す)。原典 ``trek.find_member``
    は両方空のとき ValueError を上げたが、ここは読み出し専用で、匿名の
    caller は既に上位の auth で弾かれているため None に畳んである。
    """
    members = trek_doc.get("members") or []
    if session_id:
        for m in members:
            if m.get("session_id") == session_id:
                return m
        return None
    if not user_id:
        return None
    for m in members:
        if m.get("user_id") == user_id:
            return m
    return None


def make_router(
    require_auth: Callable,
    *,
    _require_admin: Callable,
    is_auth_enabled: Callable[[], bool],
) -> APIRouter:
    """Build the read-only /api/treks/* router (ms-185 e-7070).

    Keyword-only + Callable-typed injected helpers with a construction-time
    callability check (same rationale as the sibling routers): a mis-wire fails
    at mount rather than at request time.
    """
    for _name, _dep in (
        ("require_auth", require_auth),
        ("_require_admin", _require_admin),
        ("is_auth_enabled", is_auth_enabled),
    ):
        if not callable(_dep):
            raise TypeError(
                f"routers_treks.make_router: {_name} must be callable, "
                f"got {type(_dep).__name__} — pass a function, not a value."
            )

    router = APIRouter()

    def _trek_find_member_dual(
        t: dict, *, user_id: str, session_id: str = "",
    ) -> Optional[dict]:
        """Phase-gated member lookup (= ms-97 / e-2658 Phase 1 AC6 dual-mode).

        session_id が分かっていて trek が phase A+ なら session 粒度で引く。
        caller の session_id が空 (= 旧 bridge / curl など header 未送) か
        pre-A trek なら user_id 粒度で引く。どちらの世代の記録も読めるように
        するための二重経路で、書き込みが無くなった今も読み出しには要る。
        """
        if session_id and _is_session_id_keyed(t):
            m = _find_member(t, session_id=session_id)
            if m is not None:
                return m
            # 移行期の正直な caller を締め出さないための fallback
            # (header は在るが session が未登録、という窓があった)。
        return _find_member(t, user_id=user_id)

    def _load_trek_for_read(
        trek_id: str, user: dict, request: "Request | None" = None,
    ) -> dict:
        """Load a trek doc. 404 if missing, 403 if caller is neither creator
        nor a member (per SPEC visibility = creator OR members).

        ``request`` から ``X-Beacon-Session`` を取り、phase A+ trek の時は
        session_id 粒度で membership を見る。``request`` 省略時は user_id 粒度のみ。
        """
        t = db.get_trek(trek_id)
        if t is None:
            raise HTTPException(status_code=404,
                                detail=f"Trek '{trek_id}' not found")
        if not is_auth_enabled():
            return t
        uid = user.get("sub", "")
        creator_uid = (t.get("creator_actor") or {}).get("user_id", "")
        if creator_uid == uid:
            return t
        caller_sid = ""
        if request is not None:
            caller_sid = request.headers.get("X-Beacon-Session", "") or ""
        if _trek_find_member_dual(t, user_id=uid,
                                  session_id=caller_sid) is not None:
            return t
        raise HTTPException(status_code=403,
                            detail="Not a member of this trek")

    # ---- 読み出し 3 ルート (e-7081 のテストが 1 つずつ固定している) ----
    #
    # ここに POST / PATCH / PUT / DELETE を足さないこと。作成経路が無い状態を
    # 前提に、この repo の各所 (lib/cmd_pr.py / CLI の削除告知 / RETIRING の
    # 注記) が「新しい Trek は作れない」と述べている。書き込みを 1 つ戻すと
    # それらが一斉に嘘になる。

    @router.get("/api/treks")
    def list_treks_endpoint(
        status: Optional[str] = None,
        include_archived: bool = False,
        all_actors: bool = False,
        user: dict = Depends(require_auth),
    ):
        """List treks visible to the caller.

        Default: treks where caller is creator OR member.
        ``?all_actors=true`` returns every trek (admin only; non-admin sees 403).
        ``?status=`` narrows to a specific lifecycle state.
        ``?include_archived=true`` includes archived treks (default hides them).
        """
        if all_actors:
            _require_admin(user)
            actor_filter = None
        else:
            actor_filter = user.get("sub") if is_auth_enabled() else None
        return db.list_treks(
            actor_id=actor_filter,
            status=status,
            include_archived=include_archived,
        )

    @router.get("/api/treks/{trek_id}")
    def get_trek_endpoint(trek_id: str, user: dict = Depends(require_auth)):
        """Get a single trek by id. Caller must be creator or member.

        request を取らないのは原典どおり (= user_id 粒度のみで membership
        を見る)。縮小にあたって session 粒度を足せば通る caller が増えるが、
        それは挙動の変更なので、削除の変更に混ぜない。
        """
        return _load_trek_for_read(trek_id, user)

    @router.get("/api/treks/{trek_id}/logs.jsonl")
    def trek_logs_jsonl_endpoint(trek_id: str, request: Request,
                                 since: str = "",
                                 user: dict = Depends(require_auth)):
        """Stream the trek's structured logs as NDJSON (AC27).

        One JSON object per line, ascending by ``created_at``. RBAC: any
        joined member (= same surface as ``_load_trek_for_read``). Optional
        ``?since=<ISO8601>`` filters older rows so callers can resume.
        """
        _load_trek_for_read(trek_id, user, request)
        # Cap at a generous limit to keep streaming bounded; clients that
        # need everything can page via ``since``.
        rows = db.list_trek_logs(trek_id, limit=10000, since=since or "")

        def _generator():
            for row in rows:
                try:
                    yield _json.dumps(row, ensure_ascii=False) + "\n"
                except Exception:
                    # Skip un-serialisable rows defensively (= one bad row
                    # must not break the stream).
                    continue

        return StreamingResponse(_generator(),
                                 media_type="application/x-ndjson")

    return router
