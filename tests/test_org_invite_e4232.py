"""Unit tests for org membership (invite / remove) — ms-118 / e-4232.

Covers the local-mode path behind `beacon org add-member` / `org remove-member`:

  - org.find_org_member        : resolve a member by user_id OR email
  - org.validate_invitable_role: role 値域 (member/admin) の単一真実源
  - LocalStore.add_org_member  : add-only member add (participation-only, no project)
  - LocalStore.remove_org_member : remove by user_id or email, last-owner guarded

The server endpoint contract + the participation-not-granted regression live in
test_org_invite_endpoints_e4232.py. StoreApi's HTTP-404 → ValueError mapping is
pinned here directly (see test_store_api_maps_404_to_valueerror) since the
endpoint tests hit FastAPI, not the client-side StoreApi.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import org as org_mod  # noqa: E402
from store_local import LocalStore  # noqa: E402


NOW = "2026-07-27T00:00:00+00:00"


def test_find_org_member_by_userid_or_email():
    org = {"members": [
        {"user_id": "u1", "email": "u1@x", "role": "owner"},
        {"user_id": "u2", "email": "bob@x", "role": "member"},
    ]}
    assert org_mod.find_org_member(org, "u2")["email"] == "bob@x"
    assert org_mod.find_org_member(org, "bob@x")["user_id"] == "u2"
    assert org_mod.find_org_member(org, "nobody") == {}
    assert org_mod.find_org_member(org, "") == {}


@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_ORGS_DIR", str(tmp_path / "orgs"))
    return LocalStore(str(tmp_path / "project.json"))


def _new_org(local_store):
    return local_store.create_org(name="Acme", creator_user_id="u1",
                                  creator_email="u1@x")


def test_invite_adds_member_default_role(local_store):
    oid = _new_org(local_store)["org_id"]
    org = local_store.add_org_member(oid, email="bob@x")
    roles = {m["email"]: m["role"] for m in org["members"]}
    assert roles == {"u1@x": "owner", "bob@x": "member"}
    # 永続化されている
    assert org_mod.is_org_member(local_store.get_org(oid), "bob@x")


def test_invite_admin_role(local_store):
    oid = _new_org(local_store)["org_id"]
    org = local_store.add_org_member(oid, email="carol@x", role="admin")
    assert org_mod.find_org_member(org, "carol@x")["role"] == "admin"


def test_invite_unknown_org_raises(local_store):
    with pytest.raises(ValueError):
        local_store.add_org_member("org-t-nope", email="bob@x")


def test_add_member_rejects_userid_input(local_store):
    # add-member は email を受ける。user-id (no '@') は誤診を避けて拒否。
    oid = _new_org(local_store)["org_id"]
    with pytest.raises(ValueError):
        local_store.add_org_member(oid, email="uid-bob")


def test_add_member_rejects_invalid_role(local_store):
    # role の値域 (member/admin) は local でも強制される (owner は add で作らない)。
    oid = _new_org(local_store)["org_id"]
    with pytest.raises(ValueError):
        local_store.add_org_member(oid, email="bob@x", role="owner")


def test_add_member_is_add_only_not_upsert(local_store):
    # 既存 member の再追加は role を silent 上書きせず拒否 (= 降格事故を防ぐ)。
    oid = _new_org(local_store)["org_id"]
    local_store.add_org_member(oid, email="bob@x", role="admin")
    with pytest.raises(ValueError):
        local_store.add_org_member(oid, email="bob@x")  # would demote to member
    # 元の role は保たれる
    assert org_mod.find_org_member(local_store.get_org(oid), "bob@x")["role"] == "admin"


def test_remove_member_by_email(local_store):
    oid = _new_org(local_store)["org_id"]
    local_store.add_org_member(oid, email="bob@x")
    org = local_store.remove_org_member(oid, target="bob@x")
    assert not org_mod.is_org_member(org, "bob@x")
    assert org_mod.is_org_member(org, "u1")  # owner remains


def test_remove_unknown_member_raises(local_store):
    oid = _new_org(local_store)["org_id"]
    with pytest.raises(ValueError):
        local_store.remove_org_member(oid, target="ghost@x")


def test_remove_last_owner_is_blocked(local_store):
    oid = _new_org(local_store)["org_id"]
    # u1 is the sole owner — removing them would leave the org owner-less.
    with pytest.raises(ValueError):
        local_store.remove_org_member(oid, target="u1")


# ---------------------------------------------------------------------------
# StoreApi HTTP-404 → ValueError mapping (cloud path, F: 直接カバレッジ)
# ---------------------------------------------------------------------------

class _FakeClient:
    """api_client stub that raises a 404-shaped RuntimeError for org member ops."""
    def add_org_member(self, org_id, *, email, role="member"):
        raise RuntimeError("HTTP 404: no Beacon user for email")

    def remove_org_member(self, org_id, *, target):
        raise RuntimeError("HTTP 404: org member not found")


def test_store_api_maps_404_to_valueerror():
    from store_api import StoreApi
    api = StoreApi.__new__(StoreApi)   # bypass __init__ (no real cloud config)
    api._client = _FakeClient()
    with pytest.raises(ValueError):
        api.add_org_member("org-t-x", email="bob@x")
    with pytest.raises(ValueError):
        api.remove_org_member("org-t-x", target="bob@x")
