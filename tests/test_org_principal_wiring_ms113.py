"""Server-side wiring tests for ms-113 principal / disclosure (e-3731〜e-3733).

pure エンジン (test_principal_disclosure_ms113 / test_org_tenancy_e3731) とは別に、
server 側 (app.py) の配線を pin する:

  - e-3731 _save が org_id を決定的に stamp する (既存挙動は不変)
  - e-3732 principal を claims から cheap に組み立て request に伝播する
  - e-3733 **org に居るだけでは project にアクセスできない** (_get_role が org を
           access に漏らさない) / participation は query 時に list_projects で解決
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app as app_module  # noqa: E402
import principal as principal_mod  # noqa: E402


# ---------------------------------------------------------------------------
# e-3731: _save stamps org_id deterministically, without changing auth behavior
# ---------------------------------------------------------------------------

def test_save_stamps_org_id_from_owner(monkeypatch):
    saved = {}
    monkeypatch.setattr(app_module.db, "save_project",
                        lambda pid, data: saved.update({pid: data}))
    monkeypatch.setattr(app_module.core, "validate_project", lambda data: None)
    data = {"name": "p", "milestones": [], "owner": "u1"}
    app_module._save("proj-1", data)
    assert saved["proj-1"]["org_id"] == "org-p-u1"


def test_save_does_not_override_explicit_org(monkeypatch):
    saved = {}
    monkeypatch.setattr(app_module.db, "save_project",
                        lambda pid, data: saved.update({pid: data}))
    monkeypatch.setattr(app_module.core, "validate_project", lambda data: None)
    data = {"name": "p", "milestones": [], "owner": "u1", "org_id": "org-team-9"}
    app_module._save("proj-1", data)
    assert saved["proj-1"]["org_id"] == "org-team-9"


# ---------------------------------------------------------------------------
# e-3732: principal build + propagation (cheap, no store I/O)
# ---------------------------------------------------------------------------

def test_build_request_principal_is_client_with_personal_org():
    p = app_module._build_request_principal({"sub": "u1"})
    assert p["user_id"] == "u1"
    assert p["org_id"] == "org-p-u1"
    assert p["agent_kind"] == principal_mod.AGENT_CLIENT


def test_user_participation_resolves_from_list_projects(monkeypatch):
    monkeypatch.setattr(app_module.db, "list_projects",
                        lambda uid: [{"project_id": "p1"}, {"project_id": "p2"}])
    assert app_module._user_participation("u1") == {"p1", "p2"}


def test_user_participation_fail_safe_on_error(monkeypatch):
    def boom(uid):
        raise RuntimeError("store down")
    monkeypatch.setattr(app_module.db, "list_projects", boom)
    # fail-closed: 解決失敗は空集合 = 何も開示しない安全側。
    assert app_module._user_participation("u1") == set()


def test_effective_scope_for_composes_participation_and_focus(monkeypatch):
    monkeypatch.setattr(app_module, "_user_participation",
                        lambda uid: {"p1", "p2", "p3"})
    p = app_module._build_request_principal({"sub": "u1"}, focus="p2")
    assert app_module._effective_scope_for(p) == {"p2"}


# ---------------------------------------------------------------------------
# e-3733: being in an org grants NO project access (the disclosure invariant)
# ---------------------------------------------------------------------------

def test_org_membership_does_not_grant_project_access(monkeypatch):
    # 認可有効下で、project owner でも member でもない user は access 不可。
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    proj = {"owner": "u1", "members": [], "org_id": "org-p-u1"}
    assert app_module._get_role(proj, {"sub": "u1"}) == "owner"
    # u2 は同じ世界の別 user。org に居ようが project 非参加なら "" (= no access)。
    # org_id が付いていても _get_role は org を access に漏らさない。
    assert app_module._get_role(proj, {"sub": "u2"}) == ""


def test_project_member_still_has_access_unchanged(monkeypatch):
    # e-3731 AC3: 既存の project member 認可は org 導入後も不変。
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    proj = {"owner": "u1", "members": [{"user_id": "u2", "role": "editor"}],
            "org_id": "org-p-u1"}
    assert app_module._get_role(proj, {"sub": "u2"}) == "editor"
