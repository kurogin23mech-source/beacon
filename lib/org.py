"""lib/org.py — Organization (組織) tenancy primitives (ms-113 / e-3731).

Organization は `user → org → project → target` のテナンシー階層で project の
所有境界を束ねる top-level エンティティ。既存の project-scoped 単一ユーザー認可を
壊さず後付け (retrofit) するため、本モジュールは「純関数 (dict を受けて dict を
返す)」に徹し、store I/O は server 側 (app.py / *_client.py) が担う (= tool/skill
分離と同型の lib/server 分離)。

スコープは e-3731 = org エンティティ + membership + 既存 project の personal org
(= 個人組織) 自動収容 (lazy) に限定する。認可の合成 (org membership = baseline
access + project role = override) は e-3732 / e-3733 に委ねる — 本モジュールは
authz 判定そのものを持たない (= role を「読む」helper は提供するが「アクセス可否を
決める」責務は server の認可チョークポイントに残す)。

決定的 (deterministic) な personal org id を採用する理由 (SPEC 設計方針2):
  - ensure が冪等になる (= 同じ user で何度呼んでも同じ org を指す、二重生成しない)
  - 既存 project の所属 org を owner から純粋導出できる (= org_id 未 write の
    project でも所属先が一意に定まる ⇒ 全 project を一括 backfill する migration
    が不要 = 無停止 retrofit)
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# org member role 語彙 (SPEC 方針3 / 受入条件7)
#   owner  : org の所有者。破壊的操作 (member 削除 / org 削除) を行える
#   admin  : member 管理はできるが org 自体は消せない
#   member : org に所属する一般メンバー (baseline access の対象、認可合成は e-3733)
# ---------------------------------------------------------------------------
ORG_ROLE_OWNER = "owner"
ORG_ROLE_ADMIN = "admin"
ORG_ROLE_MEMBER = "member"
VALID_ORG_ROLES = {ORG_ROLE_OWNER, ORG_ROLE_ADMIN, ORG_ROLE_MEMBER}

# 破壊的操作 (member 削除 / org 削除) を許すのは owner のみ (受入条件7)。
ORG_DESTRUCTIVE_ROLES = {ORG_ROLE_OWNER}

_PERSONAL_ORG_PREFIX = "org-p-"


def personal_org_id(user_id: str) -> str:
    """user の personal org (個人組織) の決定的 id を返す。

    `org-p-{user_id}` 形式。決定的ゆえ ensure は冪等、既存 project の org_id も
    owner から純粋導出できる (= 未 write の project も所属先が一意)。
    """
    if not user_id:
        raise ValueError("user_id is required to derive a personal org id")
    return f"{_PERSONAL_ORG_PREFIX}{user_id}"


def is_personal_org_id(org_id: str) -> bool:
    """org_id が personal org (自動生成の個人組織) の id かどうか。"""
    return isinstance(org_id, str) and org_id.startswith(_PERSONAL_ORG_PREFIX)


def build_personal_org(user_id: str, email: str = "", *, now: str) -> dict:
    """user の personal org doc を組み立てる (owner membership を 1 件持たせる)。

    `now` は ISO8601 timestamp を呼び出し側から渡す (= argless datetime を lib 内で
    呼ばず、決定的にテスト可能にするため)。
    """
    if not user_id:
        raise ValueError("user_id is required to build a personal org")
    return {
        "org_id": personal_org_id(user_id),
        # 表示名は email を優先 (無ければ user_id)。personal org は本人だけの器。
        "name": email or user_id,
        "personal": True,
        "personal_for": user_id,
        "members": [
            {
                "user_id": user_id,
                "email": email,
                "role": ORG_ROLE_OWNER,
                "added_at": now,
                "added_by": user_id,
            }
        ],
        "created_at": now,
    }


def org_member_role(org: dict, user_id: str) -> str:
    """org 内の user の role を返す。非メンバー / 引数不足なら "" (= no membership)。"""
    if not org or not user_id:
        return ""
    for m in org.get("members", []) or []:
        if m.get("user_id") == user_id:
            return m.get("role", ORG_ROLE_MEMBER)
    return ""


def is_org_member(org: dict, user_id: str) -> bool:
    """user が org の member かどうか (role の有無で判定)。"""
    return bool(org_member_role(org, user_id))


def add_org_member(org: dict, user_id: str, *, role: str = ORG_ROLE_MEMBER,
                   email: str = "", added_by: str = "", now: str) -> dict:
    """org に member を追加する (既存 user なら role / email を更新)。

    org を in-place で変更し、同じ org を返す。role は VALID_ORG_ROLES のみ許可。
    """
    if role not in VALID_ORG_ROLES:
        raise ValueError(
            f"invalid org role: {role!r} (valid: {sorted(VALID_ORG_ROLES)})"
        )
    if not user_id:
        raise ValueError("user_id is required to add an org member")
    members = org.setdefault("members", [])
    for m in members:
        if m.get("user_id") == user_id:
            m["role"] = role
            if email:
                m["email"] = email
            return org
    members.append({
        "user_id": user_id,
        "email": email,
        "role": role,
        "added_at": now,
        "added_by": added_by or user_id,
    })
    return org


def remove_org_member(org: dict, user_id: str) -> bool:
    """org から member を除く。除いたら True、非メンバーなら False。

    最後の owner は除去できない (= org を owner 不在にしない安全弁)。owner が複数
    いる場合はそのうちの 1 人を除ける。
    """
    members = org.get("members", []) or []
    target = next((m for m in members if m.get("user_id") == user_id), None)
    if target is None:
        return False
    if target.get("role") == ORG_ROLE_OWNER:
        owners = [m for m in members if m.get("role") == ORG_ROLE_OWNER]
        if len(owners) <= 1:
            raise ValueError("cannot remove the last owner of an org")
    org["members"] = [m for m in members if m.get("user_id") != user_id]
    return True


def is_external_guest(org: dict, user_id: str, *, participates_in_project: bool) -> bool:
    """user が「外部ゲスト」か = 或る project に参加しているが org の member ではない
    (ms-113 / e-3735, Notion guest 相当)。

    開示は project 参加でのみ与えられる (e-3733) ので、org member にしなくても特定
    project にだけ招待すれば、その guest は招待された project の情報だけが見える
    (= 他 project は参加していないので開示されない)。本 helper はその状態を分類
    するだけで、可否判定そのものは participation-gated な開示プリミティブが行う。
    """
    return bool(participates_in_project) and not is_org_member(org, user_id)


def project_org_id(project: dict) -> str:
    """project の所属 org_id を返す (= lazy retrofit の純粋導出)。

    明示 org_id があればそれを、無ければ owner から personal org を決定的に導出する
    (= org_id 未 write の既存 project も所属先が一意に決まる)。owner も無い
    (= migration 期の owner-less project) なら "" を返す。
    """
    explicit = project.get("org_id")
    if explicit:
        return explicit
    owner = project.get("owner")
    if owner:
        return personal_org_id(owner)
    return ""


def stamp_project_org(project: dict) -> bool:
    """project に org_id 未設定 かつ owner ありなら、決定的に org_id を stamp する。

    純粋な文字列導出のみ (store I/O 無し / O(1)) なので、write chokepoint
    (server の `_save`) から毎回呼んでも安全。変更したら True、既に org_id が
    あるか owner が無ければ False (no-op)。
    """
    if project.get("org_id"):
        return False
    owner = project.get("owner")
    if not owner:
        return False
    project["org_id"] = personal_org_id(owner)
    return True
