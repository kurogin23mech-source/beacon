"""Unit tests for Organization tenancy primitives (ms-113 / e-3731).

ms-113 は `user → org → project → target` のテナンシー階層を、既存の
project-scoped 単一ユーザー認可を壊さず後付けする MS。e-3731 はその土台 =
org エンティティ + membership + 既存 project の personal org (個人組織) 自動収容
(lazy) を担う。

ここで pin する不変条件 (= lazy retrofit の安全性そのもの):
  - personal org id が決定的 (= ensure が冪等、二重生成しない)
  - org_id 未設定の既存 project も owner から所属先が一意に導出できる
    (= 全 project を一括 backfill する migration が不要 = 無停止)
  - membership 操作 (add / remove) と最後の owner 保護
  - stamp_project_org が O(1) 純粋導出で、既に org_id があれば no-op
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import org  # noqa: E402

NOW = "2026-07-21T00:00:00+00:00"


# ---------------------------------------------------------------------------
# personal org id: 決定的 = ensure 冪等の根拠
# ---------------------------------------------------------------------------

def test_personal_org_id_is_deterministic():
    assert org.personal_org_id("u1") == "org-p-u1"
    # 同じ user で何度呼んでも同じ id (= ensure が二重生成しない)。
    assert org.personal_org_id("u1") == org.personal_org_id("u1")
    assert org.personal_org_id("u2") != org.personal_org_id("u1")


def test_personal_org_id_requires_user():
    with pytest.raises(ValueError):
        org.personal_org_id("")


def test_is_personal_org_id():
    assert org.is_personal_org_id("org-p-abc") is True
    assert org.is_personal_org_id("org-team-xyz") is False
    assert org.is_personal_org_id(None) is False


# ---------------------------------------------------------------------------
# build_personal_org: owner membership を持つ器
# ---------------------------------------------------------------------------

def test_build_personal_org_shape():
    o = org.build_personal_org("u1", "u1@example.com", now=NOW)
    assert o["org_id"] == "org-p-u1"
    assert o["personal"] is True
    assert o["personal_for"] == "u1"
    assert o["name"] == "u1@example.com"
    assert len(o["members"]) == 1
    m = o["members"][0]
    assert m["user_id"] == "u1"
    assert m["role"] == org.ORG_ROLE_OWNER
    assert o["created_at"] == NOW


def test_build_personal_org_name_falls_back_to_user_id():
    o = org.build_personal_org("u1", "", now=NOW)
    assert o["name"] == "u1"


def test_build_personal_org_requires_user():
    with pytest.raises(ValueError):
        org.build_personal_org("", now=NOW)


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------

def test_org_member_role_and_is_member():
    o = org.build_personal_org("u1", now=NOW)
    assert org.org_member_role(o, "u1") == org.ORG_ROLE_OWNER
    assert org.org_member_role(o, "stranger") == ""
    assert org.is_org_member(o, "u1") is True
    assert org.is_org_member(o, "stranger") is False


def test_add_org_member_new_and_update():
    o = org.build_personal_org("u1", now=NOW)
    org.add_org_member(o, "u2", role=org.ORG_ROLE_MEMBER, email="u2@x", now=NOW)
    assert org.org_member_role(o, "u2") == org.ORG_ROLE_MEMBER
    # 既存 user の再追加は role を更新する (= 重複 member を作らない)。
    org.add_org_member(o, "u2", role=org.ORG_ROLE_ADMIN, now=NOW)
    assert org.org_member_role(o, "u2") == org.ORG_ROLE_ADMIN
    assert len([m for m in o["members"] if m["user_id"] == "u2"]) == 1


def test_add_org_member_rejects_invalid_role():
    o = org.build_personal_org("u1", now=NOW)
    with pytest.raises(ValueError):
        org.add_org_member(o, "u2", role="superuser", now=NOW)


def test_remove_org_member():
    o = org.build_personal_org("u1", now=NOW)
    org.add_org_member(o, "u2", role=org.ORG_ROLE_MEMBER, now=NOW)
    assert org.remove_org_member(o, "u2") is True
    assert org.is_org_member(o, "u2") is False
    # 非メンバーの除去は False。
    assert org.remove_org_member(o, "ghost") is False


def test_remove_last_owner_is_blocked():
    o = org.build_personal_org("u1", now=NOW)
    # 最後の owner は除去できない (= org を owner 不在にしない)。
    with pytest.raises(ValueError):
        org.remove_org_member(o, "u1")


def test_remove_owner_allowed_when_multiple_owners():
    o = org.build_personal_org("u1", now=NOW)
    org.add_org_member(o, "u2", role=org.ORG_ROLE_OWNER, now=NOW)
    assert org.remove_org_member(o, "u1") is True
    assert org.org_member_role(o, "u2") == org.ORG_ROLE_OWNER


# ---------------------------------------------------------------------------
# project_org_id / stamp_project_org: lazy retrofit の純粋導出
# ---------------------------------------------------------------------------

def test_project_org_id_explicit_wins():
    p = {"owner": "u1", "org_id": "org-team-7"}
    assert org.project_org_id(p) == "org-team-7"


def test_project_org_id_derives_from_owner_when_absent():
    # org_id 未 write の既存 project も、owner から所属先が一意に決まる
    # (= 全 project を backfill する migration 不要 = 無停止 retrofit の核)。
    p = {"owner": "u1"}
    assert org.project_org_id(p) == "org-p-u1"


def test_project_org_id_ownerless_returns_empty():
    assert org.project_org_id({}) == ""


def test_stamp_project_org_sets_when_absent():
    p = {"owner": "u1", "name": "proj"}
    changed = org.stamp_project_org(p)
    assert changed is True
    assert p["org_id"] == "org-p-u1"


def test_stamp_project_org_is_idempotent_noop():
    p = {"owner": "u1", "org_id": "org-team-7"}
    # 既に org_id があれば触らない (= 明示 org 所属を上書きしない)。
    changed = org.stamp_project_org(p)
    assert changed is False
    assert p["org_id"] == "org-team-7"
    # owner が無ければ no-op (= migration 期の owner-less project を壊さない)。
    p2 = {"name": "legacy"}
    assert org.stamp_project_org(p2) is False
    assert "org_id" not in p2
