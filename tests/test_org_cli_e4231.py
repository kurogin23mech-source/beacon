"""Unit tests for the org CLI foundation (ms-118 / e-4231).

Covers the local-mode path that backs `beacon org create/list/show`:

  - org.new_org             : pure team-org doc builder (creator = owner)
  - org.mint_org_id         : team org id (org-t-*) distinct from personal (org-p-*)
  - org_store               : local file store (~/.beacon/orgs/, BEACON_ORGS_DIR override)
  - LocalStore.create_org / list_orgs / get_org : the Store abstraction the CLI calls

The cloud path (StoreApi → /api/orgs) is pinned by test_org_endpoints_e4231.py.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import org as org_mod  # noqa: E402
import org_store  # noqa: E402
from store_local import LocalStore  # noqa: E402


NOW = "2026-07-27T00:00:00+00:00"


# ---------------------------------------------------------------------------
# org.new_org / mint_org_id (pure)
# ---------------------------------------------------------------------------

def test_new_org_creator_becomes_owner():
    doc = org_mod.new_org("Acme 法人", creator_user_id="u1",
                          creator_email="u1@x", now=NOW, org_id="org-t-fixed")
    assert doc["org_id"] == "org-t-fixed"
    assert doc["name"] == "Acme 法人"
    assert doc["personal"] is False
    assert doc["created_at"] == NOW
    assert doc["members"] == [
        {"user_id": "u1", "email": "u1@x", "role": "owner",
         "added_at": NOW, "added_by": "u1"}
    ]


def test_new_org_trims_name_and_rejects_empty():
    assert org_mod.new_org("  Trimmed  ", creator_user_id="u1",
                           now=NOW, org_id="o")["name"] == "Trimmed"
    with pytest.raises(ValueError):
        org_mod.new_org("   ", creator_user_id="u1", now=NOW)
    with pytest.raises(ValueError):
        org_mod.new_org("Acme", creator_user_id="", now=NOW)


def test_mint_org_id_is_team_prefixed_and_distinct_from_personal():
    oid = org_mod.mint_org_id()
    assert oid.startswith("org-t-")
    assert org_mod.is_team_org_id(oid)
    # personal orgs use org-p-<user_id>; team ids must not collide with that
    assert not org_mod.is_personal_org_id(oid)
    assert org_mod.mint_org_id() != org_mod.mint_org_id()  # unique per call


# ---------------------------------------------------------------------------
# org_store (local file store)
# ---------------------------------------------------------------------------

@pytest.fixture()
def orgs_dir(tmp_path, monkeypatch):
    d = tmp_path / "orgs"
    monkeypatch.setenv("BEACON_ORGS_DIR", str(d))
    return d


def test_org_store_save_load_roundtrip(orgs_dir):
    doc = org_mod.new_org("Acme", creator_user_id="u1", now=NOW, org_id="org-t-a")
    org_store.save_org(doc)
    assert (orgs_dir / "org-t-a.json").exists()
    assert org_store.load_org("org-t-a") == doc
    assert org_store.load_org("org-t-missing") is None


def test_org_store_save_requires_org_id(orgs_dir):
    with pytest.raises(ValueError):
        org_store.save_org({"name": "no id"})


def test_org_store_list_filters_by_membership(orgs_dir):
    org_store.save_org(org_mod.new_org("A", creator_user_id="u1", now=NOW, org_id="org-t-a"))
    org_store.save_org(org_mod.new_org("B", creator_user_id="u2", now=NOW, org_id="org-t-b"))
    # u1 only sees org A; None (admin) sees both
    ids_u1 = {o["org_id"] for o in org_store.list_orgs(user_id="u1")}
    assert ids_u1 == {"org-t-a"}
    ids_all = {o["org_id"] for o in org_store.list_orgs()}
    assert ids_all == {"org-t-a", "org-t-b"}


# ---------------------------------------------------------------------------
# LocalStore org methods (= what the CLI calls via get_store())
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_ORGS_DIR", str(tmp_path / "orgs"))
    # project_file is unused by org ops but required by the constructor.
    return LocalStore(str(tmp_path / "project.json"))


def test_localstore_create_list_show_roundtrip(local_store):
    doc = local_store.create_org(name="Acme", creator_user_id="u1", creator_email="u1@x")
    assert doc["name"] == "Acme"
    assert doc["members"][0]["role"] == "owner"
    assert org_mod.is_team_org_id(doc["org_id"])

    listed = local_store.list_orgs(user_id="u1")
    assert [o["org_id"] for o in listed] == [doc["org_id"]]

    fetched = local_store.get_org(doc["org_id"])
    assert fetched == doc


def test_localstore_get_org_unknown_raises_valueerror(local_store):
    with pytest.raises(ValueError):
        local_store.get_org("org-t-nope")


def test_localstore_create_org_requires_creator_in_local_mode(local_store):
    # local mode has no auth token, so the caller identity is mandatory
    # (= we must not fabricate an owner silently).
    with pytest.raises(ValueError):
        local_store.create_org(name="Acme", creator_user_id="")
