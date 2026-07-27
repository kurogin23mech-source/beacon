"""Unit tests for org membership (invite / remove) — ms-118 / e-4232.

Covers the local-mode path behind `beacon org invite` / `org remove-member`:

  - org.find_org_member       : resolve a member by user_id OR email
  - LocalStore.invite_org_member : add a member (participation-only, no project)
  - LocalStore.remove_org_member : remove by user_id or email, last-owner guarded

The cloud path (StoreApi → /api/orgs/{id}/members) + the participation-not-granted
regression live in test_org_invite_endpoints_e4232.py.
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
    org = local_store.invite_org_member(oid, email="bob@x")
    roles = {m["email"]: m["role"] for m in org["members"]}
    assert roles == {"u1@x": "owner", "bob@x": "member"}
    # 永続化されている
    assert org_mod.is_org_member(local_store.get_org(oid), "bob@x")


def test_invite_admin_role(local_store):
    oid = _new_org(local_store)["org_id"]
    org = local_store.invite_org_member(oid, email="carol@x", role="admin")
    assert org_mod.find_org_member(org, "carol@x")["role"] == "admin"


def test_invite_unknown_org_raises(local_store):
    with pytest.raises(ValueError):
        local_store.invite_org_member("org-t-nope", email="bob@x")


def test_remove_member_by_email(local_store):
    oid = _new_org(local_store)["org_id"]
    local_store.invite_org_member(oid, email="bob@x")
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
