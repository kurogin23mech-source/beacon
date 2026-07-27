"""Unit tests for org delete + owner-only destructive guard — ms-118 / e-4234.

Covers the shared destructive-op guard and the local-mode delete path:

  - org.is_destructive_allowed : owner のみ True (admin/member/非member は False)
  - org.assert_org_deletable   : personal org は削除不可、team org は可
  - org_store.delete_org       : file 物理削除 (低レベル I/O)
  - LocalStore.delete_org      : team 削除 OK / personal・unknown は ValueError

The server endpoint contract (owner-only DELETE, admin removal now 403) lives in
test_org_delete_endpoints_e4234.py.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import org as org_mod  # noqa: E402
import org_store  # noqa: E402
from store_local import LocalStore  # noqa: E402


NOW = "2026-07-27T00:00:00+00:00"


# ---------------------------------------------------------------------------
# org.is_destructive_allowed — owner-only ガードの単一真実源
# ---------------------------------------------------------------------------

def _org_with(roles):
    return {"org_id": "org-t-1", "members": [
        {"user_id": uid, "role": role} for uid, role in roles.items()]}


def test_destructive_allowed_owner_only():
    org = _org_with({"o": "owner", "a": "admin", "m": "member"})
    assert org_mod.is_destructive_allowed(org, "o") is True
    assert org_mod.is_destructive_allowed(org, "a") is False  # admin 不可
    assert org_mod.is_destructive_allowed(org, "m") is False
    assert org_mod.is_destructive_allowed(org, "stranger") is False


# ---------------------------------------------------------------------------
# org.assert_org_deletable — personal org は消せない (構造ガード)
# ---------------------------------------------------------------------------

def test_assert_deletable_team_ok():
    org = org_mod.new_org("Acme", creator_user_id="u1", now=NOW,
                          org_id="org-t-abcd")
    org_mod.assert_org_deletable(org)  # raises しなければ OK


def test_assert_deletable_personal_rejected():
    org = org_mod.build_personal_org("u1", "u1@x", now=NOW)
    with pytest.raises(ValueError, match="personal"):
        org_mod.assert_org_deletable(org)


def test_assert_deletable_personal_by_id_prefix():
    # personal フラグが無くても id prefix で判定する (= 防御的)。
    org = {"org_id": org_mod.personal_org_id("u1"), "members": []}
    with pytest.raises(ValueError):
        org_mod.assert_org_deletable(org)


# ---------------------------------------------------------------------------
# org_store.delete_org — low-level file I/O
# ---------------------------------------------------------------------------

def test_org_store_delete_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_ORGS_DIR", str(tmp_path / "orgs"))
    org = org_mod.new_org("Acme", creator_user_id="u1", now=NOW,
                          org_id="org-t-abcd")
    org_store.save_org(org)
    assert org_store.load_org("org-t-abcd") is not None
    assert org_store.delete_org("org-t-abcd") is True
    assert org_store.load_org("org-t-abcd") is None
    # 二度目は False (= 冪等: 既に無い)。
    assert org_store.delete_org("org-t-abcd") is False


# ---------------------------------------------------------------------------
# LocalStore.delete_org
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_ORGS_DIR", str(tmp_path / "orgs"))
    return LocalStore(str(tmp_path / "project.json"))


def test_local_delete_team_org(local_store):
    oid = local_store.create_org(name="Acme", creator_user_id="u1")["org_id"]
    result = local_store.delete_org(oid)
    assert result == {"org_id": oid, "deleted": True}
    with pytest.raises(ValueError, match="not found"):
        local_store.get_org(oid)


def test_local_delete_unknown_org(local_store):
    with pytest.raises(ValueError, match="not found"):
        local_store.delete_org("org-t-ghost")


def test_local_delete_personal_org_rejected(local_store, tmp_path):
    import org_store as os_mod
    personal = org_mod.build_personal_org("u1", "u1@x", now=NOW)
    os_mod.save_org(personal)
    with pytest.raises(ValueError, match="personal"):
        local_store.delete_org(personal["org_id"])
    # 拒否時は消えていない。
    assert local_store.get_org(personal["org_id"]) is not None


# ---------------------------------------------------------------------------
# StoreApi: HTTP-404 → ValueError, others → RuntimeError
# ---------------------------------------------------------------------------

def test_store_api_delete_maps_404_to_valueerror():
    from store_api import StoreApi

    class _Client:
        def delete_org(self, org_id):
            raise RuntimeError("API error 404: org not found")

    api = StoreApi.__new__(StoreApi)
    api._client = _Client()
    with pytest.raises(ValueError):
        api.delete_org("org-t-x")


def test_store_api_delete_propagates_403_as_runtimeerror():
    from store_api import StoreApi

    class _Client:
        def delete_org(self, org_id):
            raise RuntimeError("API error 403: deleting an org requires owner")

    api = StoreApi.__new__(StoreApi)
    api._client = _Client()
    with pytest.raises(RuntimeError):
        api.delete_org("org-t-x")
