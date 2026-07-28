"""cross-instance 検証: 同 org の 2 インスタンスが同一 Account を共有する (ms-111 / e-3623).

SPEC AC4: 同じ org に属す 2 つの project が同じマスターに束縛し、同一 Account を見られる。
e-3622 の全 slice (chunk1 org 境界 / chunk2a outbound write-through + authz / chunk2b inbound
fan-out) を real endpoint 経路で束ねた end-to-end。1 インスタンスでの rename が master 経由で
もう 1 インスタンスの投影へ伝播し、別 org インスタンスには伝播しないことを固定する。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import firestore_client  # noqa: E402
import disclosure  # noqa: E402
import master_projection as mp  # noqa: E402
import master_identity as mi  # noqa: E402

ORG_A = "org-A"
ORG_B = "org-B"
POSTER_PID = "P1"

_projects: dict[str, dict] = {}
_master_store: dict[tuple, dict] = {}
_user_orgs: dict[str, list[dict]] = {}
_bus_seq = [0]


def _linked_account(name, owner_org, master_id="macc-1"):
    acc = {"id": "a1", "name": name, "label": name, "contacts": [],
           disclosure.OWNER_ORG_FIELD: owner_org}   # 投影の所有 org (resolve の org 照合基準)
    mp.link_account_to_master(acc, master_id)
    return acc


def _mk_project(org, name):
    return {"org_id": org, "owner": "uid-alice",
            "members": [{"user_id": "uid-alice", "role": "owner"},
                        {"user_id": "uid-bob", "role": "editor"}],
            "milestones": [], "accounts": [_linked_account(name, org)]}


def _append_bus_event(pid, data):
    _bus_seq[0] += 1
    return f"e-{_bus_seq[0]:06d}"


def _master_get(entity, pk):
    r = _master_store.get((entity, pk))
    return copy.deepcopy(r) if r is not None else None


def _master_put(entity, pk, data):
    _master_store[(entity, pk)] = copy.deepcopy(data)


def _master_scan(entity):
    return [copy.deepcopy(v) for k, v in _master_store.items() if k[0] == entity]


# patch firestore_client BEFORE import app (store_router snapshots at import).
firestore_client.append_bus_event = _append_bus_event
firestore_client.append_bus_audit = lambda pid, rec: "a1"
firestore_client.list_bus_events = lambda pid, since="", channel="", limit=100: []
firestore_client.find_bus_event = lambda pid, eid: None
firestore_client.list_sessions = lambda pid: []
firestore_client.get_bus_cursor = lambda pid, rid: {}
firestore_client.check_and_record_bus_nonce = lambda *a, **k: True
firestore_client.set_bus_event_receipt = lambda *a, **k: None
firestore_client.get_or_create_user = lambda *a, **k: None
firestore_client.get_project = lambda pid: copy.deepcopy(_projects.get(pid))
firestore_client.save_project = lambda pid, data: _projects.__setitem__(pid, copy.deepcopy(data))
firestore_client.list_projects = lambda *a, **k: [{"project_id": p} for p in _projects]
firestore_client.list_orgs_for_user = lambda uid=None: copy.deepcopy(_user_orgs.get(uid or "", []))
firestore_client.master_get = _master_get
firestore_client.master_put = _master_put
firestore_client.master_scan = _master_scan

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import store_router as db_module  # noqa: E402
import master_adapter  # noqa: E402

app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None
client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for name, ref in (
        ("append_bus_event", _append_bus_event),
        ("append_bus_audit", lambda pid, rec: "a1"),
        ("list_bus_events", lambda pid, since="", channel="", limit=100: []),
        ("find_bus_event", lambda pid, eid: None),
        ("list_sessions", lambda pid: []),
        ("get_bus_cursor", lambda pid, rid: {}),
        ("check_and_record_bus_nonce", lambda *a, **k: True),
        ("set_bus_event_receipt", lambda *a, **k: None),
        ("get_or_create_user", lambda *a, **k: None),
        ("get_project", lambda pid: copy.deepcopy(_projects.get(pid))),
        ("save_project", lambda pid, data: _projects.__setitem__(pid, copy.deepcopy(data))),
        ("list_projects", lambda *a, **k: [{"project_id": p} for p in _projects]),
        ("list_orgs_for_user", lambda uid=None: copy.deepcopy(_user_orgs.get(uid or "", []))),
        ("master_get", _master_get), ("master_put", _master_put), ("master_scan", _master_scan),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)
    _projects.clear()
    _master_store.clear()
    _user_orgs.clear()
    _bus_seq[0] = 0
    master_adapter.reset_master_adapter()
    # 同 org (org-A) の 2 インスタンス P1, P2 + 別 org (org-B) の P3、全て macc-1 を投影。
    _projects["P1"] = _mk_project(ORG_A, "Acme")
    _projects["P2"] = _mk_project(ORG_A, "Acme")
    _projects["P3"] = _mk_project(ORG_B, "Acme")
    _master_store[("master_accounts", "macc-1")] = mi.new_master_account(
        "Acme", org_id=ORG_A, now="2026-07-27T01:00:00+00:00", master_account_id="macc-1")
    _user_orgs["uid-alice"] = [{"org_id": ORG_A}]
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    yield
    master_adapter.reset_master_adapter()


def _name(pid):
    return _projects[pid]["accounts"][0]["name"]


def test_rename_in_one_instance_propagates_to_same_org_instance(monkeypatch):
    """P1 由来の rename(master-sync)が master 経由で P2(同 org)へ伝播、P3(別 org)は不変。"""
    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda tok: {"sub": "uid-alice", "email": "a@x"})
    body = {
        "channel": "master-sync", "sender_session_id": "sess-a",
        "payload": {"kind": "master_sync", "entity": "account",
                    "master_account_id": "macc-1", "new_name": "Acme Global", "org_id": ORG_A},
        "delivery": "propose-to-ai",
    }
    resp = client.post(f"/api/projects/{POSTER_PID}/bus", json=body,
                       headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200, resp.text
    # master が真値を更新。
    assert _master_store[("master_accounts", "macc-1")]["name"] == "Acme Global"
    # 同 org の 2 インスタンスが同一 Account の新名を共有 (cross-instance sharing)。
    assert _name("P1") == "Acme Global"
    assert _name("P2") == "Acme Global"
    # 別 org インスタンスへは伝播しない (org 境界)。
    assert _name("P3") == "Acme"


def test_resolve_reads_shared_master_identity_for_both_instances(monkeypatch):
    """read-through: 同 org の両インスタンスの投影が master 経由で同一 identity を解決する。"""
    # master を直接更新 (別経路で master が変わった状況)。
    rec = _master_store[("master_accounts", "macc-1")]
    rec["name"] = "Acme Worldwide"
    _master_store[("master_accounts", "macc-1")] = rec
    adapter = master_adapter.get_master_adapter()
    # P1, P2 の投影 (org-A) は master live 読みで同一の canonical name を返す。
    for pid in ("P1", "P2"):
        acc = _projects[pid]["accounts"][0]
        assert mp.resolve_account_identity(acc, adapter) == "Acme Worldwide"
    # P3 (org-B) は org 不一致で master を採らず投影 fallback (cross-org leak なし)。
    acc3 = _projects["P3"]["accounts"][0]
    assert mp.resolve_account_identity(acc3, adapter) == "Acme"
