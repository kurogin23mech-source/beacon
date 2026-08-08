"""ms-111 e-3621 chunk2b: server ingest wrapper のフラグゲート + 束縛 scope 判定 (AC1・AC2).

pure な linking 中核 (``master_projection.link_project_accounts``) は
test_master_ingest_linking_e3621 で固めた。本 test は server 側 wrapper
(``app._link_body_accounts_to_master`` / ``app._master_linking_enabled``) の 2 つのゲートを pin:

  - env flag ``BEACON_MASTER_LINKING_ENABLED`` が OFF (default) の間は ingest は何もしない
    (= 従来どおり投影が唯一の source、regression なし)。本番投下は user の一手。
  - ON かつ Beacon-default binding の時だけ、backend 配線済み adapter で link する (AC1)。
  - 外部 CRM binding (system != beacon-default、未実装) と org 未定は scope 外で skip。
  - master_binding.resolve_master_binding が束縛軸の真実源として ingest から読まれる (AC2)。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("BEACON_OPERATIONS_BACKEND", "mock")

import app as app_module  # noqa: E402
# ms-127 e-4871 (PR1): _link_body_accounts_to_master / _master_linking_enabled
# moved to routers_projects.py (module-level helpers). master_adapter is the same
# shared module object in both, so monkeypatching app_module.master_adapter still
# reaches the moved helper.
import routers_projects as rp  # noqa: E402
import master_projection as mp  # noqa: E402
import master_store  # noqa: E402


def _inmem_adapter():
    return master_store.BeaconDefaultAdapter(master_store.InMemoryMasterStore())


def _body(org_declared: bool = False):
    proj = {
        "owner": "u1",
        "accounts": [
            {"id": "acc-1", "name": "Acme Inc", "owner_org_id": "org-p-u1",
             "contacts": [{"id": "c-1", "name": "Taro"}]},
        ],
    }
    if org_declared:
        proj["org_id"] = "org-p-u1"
    return proj


def test_flag_off_is_noop(monkeypatch):
    monkeypatch.delenv("BEACON_MASTER_LINKING_ENABLED", raising=False)
    monkeypatch.setattr(app_module.master_adapter, "get_master_adapter", _inmem_adapter)
    body = _body()
    rp._link_body_accounts_to_master(body)
    # flag OFF → 投影は素通り、external_ref は張られない
    assert not mp.is_account_linked(body["accounts"][0])


def test_flag_on_links_accounts_via_ingest(monkeypatch):
    monkeypatch.setenv("BEACON_MASTER_LINKING_ENABLED", "1")
    adapter = _inmem_adapter()
    monkeypatch.setattr(app_module.master_adapter, "get_master_adapter", lambda: adapter)
    body = _body()
    rp._link_body_accounts_to_master(body)
    acc = body["accounts"][0]
    # flag ON + Beacon-default binding → link 済 (AC1 = external_ref 化)
    assert mp.is_account_linked(acc)
    assert mp.contact_master_ref(acc["contacts"][0])
    assert adapter.get_account(mp.linked_master_account_id(acc)) is not None


def test_external_crm_binding_is_skipped(monkeypatch):
    monkeypatch.setenv("BEACON_MASTER_LINKING_ENABLED", "1")
    monkeypatch.setattr(app_module.master_adapter, "get_master_adapter", _inmem_adapter)
    body = _body()
    body["master_binding"] = {"system": "salesforce", "org_id": "org-p-u1"}
    rp._link_body_accounts_to_master(body)
    # 外部 CRM (未実装) は scope 外 → link しない
    assert not mp.is_account_linked(body["accounts"][0])


def test_owner_less_project_has_no_org_and_is_skipped(monkeypatch):
    monkeypatch.setenv("BEACON_MASTER_LINKING_ENABLED", "1")
    monkeypatch.setattr(app_module.master_adapter, "get_master_adapter", _inmem_adapter)
    body = {"accounts": [{"id": "acc-1", "name": "Acme"}]}  # owner なし → org_id ""
    rp._link_body_accounts_to_master(body)
    assert not mp.is_account_linked(body["accounts"][0])


def test_master_linking_enabled_flag_parsing(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("BEACON_MASTER_LINKING_ENABLED", val)
        assert rp._master_linking_enabled() is True
    for val in ("0", "false", "", "off", "no"):
        monkeypatch.setenv("BEACON_MASTER_LINKING_ENABLED", val)
        assert rp._master_linking_enabled() is False
