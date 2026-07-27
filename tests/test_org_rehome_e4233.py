"""Unit tests for project re-home — ms-118 / e-4233.

Covers the local-mode path behind `beacon org rehome <project> --to <org>`:

  - org.rehome_project        : pure org_id relink (identity/history untouched)
  - LocalStore.rehome_project : target-org existence + project-id match guards

The server endpoint contract (owner-only + target-org-member, disclosure
re-evaluation) lives in test_org_rehome_endpoints_e4233.py. StoreApi's
HTTP-404 → ValueError mapping is pinned here directly since the endpoint tests
hit FastAPI, not the client-side StoreApi.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import org as org_mod  # noqa: E402
from store_local import LocalStore  # noqa: E402


NOW = "2026-07-27T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Pure helper: org.rehome_project
# ---------------------------------------------------------------------------

def test_rehome_relinks_org_id_and_returns_previous():
    # owner だけで org_id 未設定 → previous は personal org の導出値。
    project = {"project_id": "p1", "owner": "u1", "milestones": [{"id": "ms-1"}]}
    prev = org_mod.rehome_project(project, "org-t-abcd1234")
    assert project["org_id"] == "org-t-abcd1234"
    assert prev == org_mod.personal_org_id("u1")


def test_rehome_preserves_identity_and_history():
    project = {
        "project_id": "p1",
        "owner": "u1",
        "org_id": "org-t-old00000",
        "milestones": [{"id": "ms-1"}],
        "members": [{"user_id": "u1", "role": "owner"}],
    }
    prev = org_mod.rehome_project(project, "org-t-new11111")
    assert prev == "org-t-old00000"
    assert project["project_id"] == "p1"           # identity 不変
    assert project["milestones"] == [{"id": "ms-1"}]  # 履歴不変
    assert project["members"] == [{"user_id": "u1", "role": "owner"}]
    assert project["org_id"] == "org-t-new11111"    # org_id だけ変わる


def test_rehome_accepts_personal_org_target():
    project = {"project_id": "p1", "owner": "u1"}
    org_mod.rehome_project(project, org_mod.personal_org_id("u2"))
    assert project["org_id"] == org_mod.personal_org_id("u2")


@pytest.mark.parametrize("bad", ["", "   ", "not-an-org", "team-xyz", "orgt-1"])
def test_rehome_rejects_invalid_target(bad):
    project = {"project_id": "p1", "owner": "u1"}
    with pytest.raises(ValueError):
        org_mod.rehome_project(project, bad)
    # 失敗時は org_id を変えない (= 部分適用しない)。
    assert "org_id" not in project


# ---------------------------------------------------------------------------
# LocalStore.rehome_project
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv("BEACON_ORGS_DIR", str(tmp_path / "orgs"))
    pf = tmp_path / "project.json"
    pf.write_text(json.dumps({
        "project_id": "p1",
        "owner": "u1",
        "milestones": [{"id": "ms-1", "title": "keep me"}],
    }), encoding="utf-8")
    return LocalStore(str(pf))


def _team_org(local_store, name="Acme"):
    return local_store.create_org(name=name, creator_user_id="u1",
                                  creator_email="u1@x")


def test_local_rehome_relinks_and_persists(local_store):
    oid = _team_org(local_store)["org_id"]
    result = local_store.rehome_project("p1", target_org_id=oid)
    assert result["project_id"] == "p1"
    assert result["org_id"] == oid
    # personal org (owner 導出) からの移動として previous が出る。
    assert result["previous_org_id"] == org_mod.personal_org_id("u1")
    # 永続化 + 履歴不変。
    data = local_store.load_project()
    assert data["org_id"] == oid
    assert data["milestones"] == [{"id": "ms-1", "title": "keep me"}]


def test_local_rehome_rejects_unknown_org(local_store):
    with pytest.raises(ValueError, match="not found"):
        local_store.rehome_project("p1", target_org_id="org-t-doesnotexist")
    # project は書き換わっていない。
    assert "org_id" not in local_store.load_project()


def test_local_rehome_rejects_project_id_mismatch(local_store):
    oid = _team_org(local_store)["org_id"]
    with pytest.raises(ValueError, match="not found in this workspace"):
        local_store.rehome_project("some-other-project", target_org_id=oid)
    assert "org_id" not in local_store.load_project()


# ---------------------------------------------------------------------------
# StoreApi: HTTP-404 → ValueError (offline pin, no FastAPI)
# ---------------------------------------------------------------------------

def test_store_api_maps_404_to_valueerror():
    from store_api import StoreApi

    class _Client:
        def rehome_project(self, project_id, *, target_org_id):
            raise RuntimeError("API error 404: org not found")

    api = StoreApi.__new__(StoreApi)
    api._client = _Client()
    with pytest.raises(ValueError):
        api.rehome_project("p1", target_org_id="org-t-x")


def test_store_api_propagates_non_404_as_runtimeerror():
    from store_api import StoreApi

    class _Client:
        def rehome_project(self, project_id, *, target_org_id):
            raise RuntimeError("API error 403: forbidden")

    api = StoreApi.__new__(StoreApi)
    api._client = _Client()
    with pytest.raises(RuntimeError):
        api.rehome_project("p1", target_org_id="org-t-x")
