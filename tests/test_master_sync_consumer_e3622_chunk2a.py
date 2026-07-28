"""master-sync consumer の server 統合 test (ms-111 / e-3622 chunk2a).

POST /api/projects/{id}/bus に kind=master_sync の event を投げたとき、server の
post_bus_event hook が **認証済み送信者 (JWT sub) の org membership** を authz の真値源に
して master を更新する / 弾くことを、in-memory harness (test_bus_visibility_e2275 と同型)
で固定する。security の核 = payload の org_id を詐称しても、送信者が master の実 org の
member でなければ apply されないこと (rename injection 防御)。

pin する invariant:
  1. 送信者が master の実 org の member → master name が更新される。
  2. 送信者が別 org の member (payload org を詐称) → master は不変 (drop)。
  3. 未知 master_account_id → 不変。
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

PROJECT_ID = "test-master-sync"
ORG_A = "org-A"
ORG_B = "org-B"

_bus_store: dict[str, list[dict]] = {}
_audit_store: dict[str, list[dict]] = {}
_event_seq = [0]
# master store: (entity, pk) -> record。server の store_router.master_* が触る in-memory。
_master_store: dict[tuple, dict] = {}
# user_id -> org dict のリスト (list_orgs_for_user のモック)。
_user_orgs: dict[str, list[dict]] = {}


def _mock_append_bus_event(project_id: str, data: dict) -> str:
    _event_seq[0] += 1
    eid = f"ms-{_event_seq[0]:06d}"
    _bus_store.setdefault(project_id, []).append({"event_id": eid, **copy.deepcopy(data)})
    return eid


def _mock_append_bus_audit(project_id: str, record: dict) -> str:
    _audit_store.setdefault(project_id, []).append(copy.deepcopy(record))
    return f"audit-{len(_audit_store[project_id])}"


def _mock_master_get(entity, pk):
    r = _master_store.get((entity, pk))
    return copy.deepcopy(r) if r is not None else None


def _mock_master_put(entity, pk, data):
    _master_store[(entity, pk)] = copy.deepcopy(data)


def _mock_master_scan(entity):
    return [copy.deepcopy(v) for k, v in _master_store.items() if k[0] == entity]


def _mock_list_orgs_for_user(user_id=None):
    return copy.deepcopy(_user_orgs.get(user_id or "", []))


_PROJECT = {
    "name": "test", "milestones": [], "owner": "uid-alice",
    "members": [
        {"user_id": "uid-alice", "role": "owner"},
        {"user_id": "uid-bob", "role": "editor"},
    ],
}

# store_router snapshots `from firestore_client import ...` at app-import time,
# so patch firestore_client BEFORE importing app (mirror test_bus_visibility).
firestore_client.append_bus_event = _mock_append_bus_event
firestore_client.append_bus_audit = _mock_append_bus_audit
firestore_client.list_bus_events = lambda pid, since="", channel="", limit=100: []
firestore_client.find_bus_event = lambda pid, eid: None
firestore_client.list_sessions = lambda pid: []
firestore_client.get_bus_cursor = lambda pid, rid: {}
firestore_client.check_and_record_bus_nonce = lambda *a, **k: True
firestore_client.set_bus_event_receipt = lambda *a, **k: None
firestore_client.get_or_create_user = lambda *a, **k: None
firestore_client.get_project = lambda pid: copy.deepcopy(_PROJECT)
firestore_client.save_project = lambda pid, data: None
firestore_client.list_projects = lambda: []
firestore_client.list_orgs_for_user = _mock_list_orgs_for_user
firestore_client.master_get = _mock_master_get
firestore_client.master_put = _mock_master_put
firestore_client.master_scan = _mock_master_scan

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import store_router as db_module  # noqa: E402
import master_adapter  # noqa: E402
import master_identity as mi  # noqa: E402

app_module._start_watcher = lambda project_id: None
app_module._stop_watcher = lambda project_id: None

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for name, ref in (
        ("append_bus_event", _mock_append_bus_event),
        ("append_bus_audit", _mock_append_bus_audit),
        ("list_bus_events", lambda pid, since="", channel="", limit=100: []),
        ("find_bus_event", lambda pid, eid: None),
        ("list_sessions", lambda pid: []),
        ("get_bus_cursor", lambda pid, rid: {}),
        ("check_and_record_bus_nonce", lambda *a, **k: True),
        ("set_bus_event_receipt", lambda *a, **k: None),
        ("get_or_create_user", lambda *a, **k: None),
        ("get_project", lambda pid: copy.deepcopy(_PROJECT)),
        ("save_project", lambda pid, data: None),
        ("list_projects", lambda: []),
        ("list_orgs_for_user", _mock_list_orgs_for_user),
        ("master_get", _mock_master_get),
        ("master_put", _mock_master_put),
        ("master_scan", _mock_master_scan),
    ):
        monkeypatch.setattr(firestore_client, name, ref, raising=False)
        monkeypatch.setattr(db_module, name, ref, raising=False)
    _bus_store.clear()
    _audit_store.clear()
    _master_store.clear()
    _user_orgs.clear()
    _event_seq[0] = 0
    master_adapter.reset_master_adapter()
    # org membership: alice ∈ org-A、bob ∈ org-B。
    _user_orgs["uid-alice"] = [{"org_id": ORG_A}]
    _user_orgs["uid-bob"] = [{"org_id": ORG_B}]
    # master account (macc-1) は org-A 所有、name=Acme。
    _master_store[("master_accounts", "macc-1")] = mi.new_master_account(
        "Acme", org_id=ORG_A, now="2026-07-27T01:00:00+00:00", master_account_id="macc-1")
    monkeypatch.setattr(app_module, "_auth_enabled", True)
    yield
    master_adapter.reset_master_adapter()


def _as(monkeypatch, user_id: str):
    monkeypatch.setattr(app_module, "_verify_id_token",
                        lambda tok: {"sub": user_id, "email": f"{user_id}@x.example"})


def _post_master_sync(spoof_org: str, new_name: str = "Acme Inc.") -> None:
    body = {
        "channel": "master-sync",
        "sender_session_id": "sess-x",
        "payload": {
            "kind": "master_sync", "entity": "account",
            "master_account_id": "macc-1", "new_name": new_name, "org_id": spoof_org,
        },
        "delivery": "propose-to-ai",
    }
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json=body,
                       headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200, resp.text


def _master_name() -> str:
    return _master_store[("master_accounts", "macc-1")]["name"]


def test_member_sender_applies_master_rename(monkeypatch):
    _as(monkeypatch, "uid-alice")            # alice ∈ org-A = master の org
    _post_master_sync(spoof_org=ORG_A)
    assert _master_name() == "Acme Inc."      # 適用された


def test_non_member_sender_with_spoofed_payload_org_is_rejected(monkeypatch):
    """bob は org-B の member。payload に org_id=org-A を詐称しても弾かれる。"""
    _as(monkeypatch, "uid-bob")
    _post_master_sync(spoof_org=ORG_A)        # payload org を master の org に詐称
    assert _master_name() == "Acme"           # 認可されず不変 (rename injection 防御)


def test_non_member_even_with_own_org_in_payload_is_rejected(monkeypatch):
    _as(monkeypatch, "uid-bob")
    _post_master_sync(spoof_org=ORG_B)        # bob の org を payload に入れても master は org-A
    assert _master_name() == "Acme"


def test_unknown_master_id_is_noop(monkeypatch):
    _as(monkeypatch, "uid-alice")
    body = {
        "channel": "master-sync", "sender_session_id": "sess-x",
        "payload": {"kind": "master_sync", "entity": "account",
                    "master_account_id": "nope", "new_name": "X", "org_id": ORG_A},
        "delivery": "propose-to-ai",
    }
    resp = client.post(f"/api/projects/{PROJECT_ID}/bus", json=body,
                       headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert _master_name() == "Acme"           # 既存 master は無傷
