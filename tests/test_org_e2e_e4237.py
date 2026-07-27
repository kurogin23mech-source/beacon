"""End-to-end acceptance test for ms-118 — org を実際に立てて使う一本の物語 (e-4237).

各 slice (e-4231 create/list/show, e-4232 invite/remove, e-4233 rehome, e-4234
owner ガード, e-4235 外部ゲスト, e-4236 俯瞰) は個別 test で pin 済み。本 test は
それらを **1 本の narrative に合成** し、SPEC 受入条件 (§受入条件 1–8) が組み合わさ
った時に破れないことを確認する:

    法人 org を作る → 社員を org に招く (所属だけ、まだ何も見えない)
    → 社員を必要な project にだけ参加させる (= その project だけ見える)
    → org 外の user を 1 project にだけ招く (外部ゲスト)
    → 破壊的操作 (member 削除 / org 削除) は owner だけができる

実際の FastAPI endpoint を通す (= org create/list/overview/member-add/member-remove
/delete)。db は overview endpoint test (e-4236) と同じ in-memory mock で差し替える。
participation (= 参加 = アクセス) は ``list_projects`` が返す集合で表現し、org 所属
(members[]) とは別レイヤーであること (受入条件8: org 所属が access に漏れない) を、
overview の projects が participation 無しでは空になることで証明する。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ["BEACON_OPERATIONS_BACKEND"] = "mock"

import firestore_client  # noqa: E402,F401
from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import org as org_mod  # noqa: E402
import principal as principal_mod  # noqa: E402


# ---------------------------------------------------------------------------
# in-memory stores (org / project / participation / user directory)
# ---------------------------------------------------------------------------
_orgs: dict[str, dict] = {}
_projects: dict[str, dict] = {}
_visible: dict[str, list] = {}   # uid -> [project summaries]  (= participation)
_users: dict[str, str] = {}      # email -> uid


def _mock_get_org(org_id):
    d = _orgs.get(org_id)
    return copy.deepcopy(d) if d else None


def _mock_save_org(org_id, data):
    _orgs[org_id] = {**copy.deepcopy(data), "org_id": org_id}


def _mock_delete_org(org_id):
    return _orgs.pop(org_id, None) is not None


def _mock_list_orgs_for_user(user_id=None):
    out = []
    for org in _orgs.values():
        if user_id and not org_mod.is_org_member(org, user_id):
            continue
        out.append(copy.deepcopy(org))
    return out


def _mock_list_projects(uid=None):
    return copy.deepcopy(_visible.get(uid, []))


def _mock_get_project(pid):
    d = _projects.get(pid)
    return copy.deepcopy(d) if d else None


def _mock_find_user_by_email(email):
    uid = _users.get(email)
    return (uid, {"email": email}) if uid else None


@pytest.fixture(autouse=True)
def _bind(monkeypatch):
    db = app_module.db
    prior = {}
    for name, mock in [
        ("get_org", _mock_get_org),
        ("save_org", _mock_save_org),
        ("delete_org", _mock_delete_org),
        ("list_orgs_for_user", _mock_list_orgs_for_user),
        ("list_projects", _mock_list_projects),
        ("get_project", _mock_get_project),
        ("find_user_by_email", _mock_find_user_by_email),
    ]:
        prior[name] = getattr(db, name, None)
        setattr(db, name, mock)
    prior_auth = app_module._auth_enabled
    app_module._auth_enabled = True
    _orgs.clear(); _projects.clear(); _visible.clear(); _users.clear()
    yield
    app_module._auth_enabled = prior_auth
    for k, v in prior.items():
        if v is None:
            hasattr(db, k) and delattr(db, k)
        else:
            setattr(db, k, v)
    app_module.app.dependency_overrides.pop(app_module.require_auth, None)


client = TestClient(app_module.app)


def _as(uid, email=None):
    app_module.app.dependency_overrides[app_module.require_auth] = \
        lambda: {"sub": uid, "email": email or f"{uid}@x"}


def _overview_projects(org_id):
    """helper: GET overview の projects (project_id 集合) を返す。"""
    r = client.get(f"/api/orgs/{org_id}/overview")
    assert r.status_code == 200, r.text
    return {p["project_id"] for p in r.json()["projects"]}


def test_org_end_to_end_admin_creates_invites_and_participation_gates_access():
    # 登場人物: boss = 法人 owner / emp = 社員 / guest = org 外の外部 user
    _users.update({"boss@x": "boss", "emp@x": "emp", "guest@x": "guest"})

    # --- AC1: 法人 org を作る。作成者が owner、list に出る -----------------
    _as("boss", "boss@x")
    r = client.post("/api/orgs", json={"name": "Acme 法人"})
    assert r.status_code == 200, r.text
    org = r.json()
    oid = org["org_id"]
    assert org_mod.is_team_org_id(oid)
    assert org_mod.org_member_role(org, "boss") == "owner"
    assert oid in {o["org_id"] for o in client.get("/api/orgs").json()}

    # 2 つの project を org 配下に置く (p1/p2 とも Acme 所属)。boss は両方 owner。
    _projects["p1"] = {"project_id": "p1", "name": "Proj1", "owner": "boss",
                       "org_id": oid, "members": []}
    _projects["p2"] = {"project_id": "p2", "name": "Proj2", "owner": "boss",
                       "org_id": oid, "members": []}
    _visible["boss"] = [{"project_id": "p1"}, {"project_id": "p2"}]

    # --- AC2: 社員を org に招く。所属はできるが、どの project も見えない -----
    r = client.post(f"/api/orgs/{oid}/members", json={"email": "emp@x"})
    assert r.status_code == 200, r.text
    assert org_mod.is_org_member(r.json(), "emp")

    # emp は org member だが participation ゼロ → overview の projects は空。
    # (受入条件8: org 所属は access を与えない — 唯一の判定源は participation)
    _as("emp", "emp@x")
    _visible["emp"] = []
    assert _overview_projects(oid) == set()

    # 判定源そのもの (client_effective_scope) でも、org 所属だけでは実効ゼロ。
    emp_principal = principal_mod.make_principal("emp", org_mod.personal_org_id("emp"))
    assert principal_mod.client_effective_scope(emp_principal, set()) == set()

    # --- AC3: 必要な project にだけ参加させると、その project だけ見える -----
    _projects["p1"]["members"] = [{"user_id": "emp", "role": "editor"}]
    _visible["emp"] = [{"project_id": "p1"}]          # p1 にだけ参加
    assert _overview_projects(oid) == {"p1"}          # p2 は参加してないので出ない
    assert principal_mod.client_effective_scope(emp_principal, {"p1"}) == {"p1"}

    # --- AC5: org 外の guest を p1 にだけ招く (外部ゲスト) ------------------
    _projects["p1"]["members"].append({"user_id": "guest", "role": "viewer"})
    _visible["guest"] = [{"project_id": "p1"}]        # guest は p1 にだけ参加
    # boss (owner, org member) から見た p1 の member 行に guest が external_guest で載る。
    _as("boss", "boss@x")
    r = client.get(f"/api/orgs/{oid}/overview")
    p1_members = {m["user_id"]: m for m in
                  next(p for p in r.json()["projects"] if p["project_id"] == "p1")["members"]}
    assert p1_members["guest"]["external_guest"] is True    # org 非所属
    assert p1_members["emp"]["external_guest"] is False     # org member
    assert p1_members["boss"]["external_guest"] is False    # owner
    # guest は org member ではないので org 俯瞰自体は 404 (存在を漏らさない)、
    # だが招かれた p1 は participation で見える (= 招かれた project 以外は見えない)。
    _as("guest", "guest@x")
    assert client.get(f"/api/orgs/{oid}/overview").status_code == 404
    assert principal_mod.client_effective_scope(
        principal_mod.make_principal("guest", org_mod.personal_org_id("guest")),
        {"p1"}) == {"p1"}

    # --- AC6: 破壊的操作 (member 削除 / org 削除) は owner のみ --------------
    # emp (org member だが非 owner) は member 削除も org 削除もできない。
    _as("emp", "emp@x")
    assert client.delete(f"/api/orgs/{oid}/members/guest").status_code == 403
    assert client.delete(f"/api/orgs/{oid}").status_code == 403
    # boss (owner) は member を外せる。
    _as("boss", "boss@x")
    assert client.delete(f"/api/orgs/{oid}/members/emp@x").status_code == 200
    assert not org_mod.is_org_member(_orgs[oid], "emp")
    # boss (owner) は team org を削除できる。以後 list に出ない。
    assert client.delete(f"/api/orgs/{oid}").status_code == 200
    assert oid not in _orgs
    assert oid not in {o["org_id"] for o in client.get("/api/orgs").json()}
