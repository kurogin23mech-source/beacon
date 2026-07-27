"""Server-side master adapter factory wiring (ms-111 / e-3621 part2 後半 chunk2a).

server/master_adapter.get_master_adapter() が、lib の BeaconDefaultAdapter を
store_router の master_get / master_put / master_scan プリミティブに配線して返す
ことを pin する。live backend は使わず、store_router の master_* を in-memory fake に
差し替えて adapter が backend seam 越しに round-trip することを確認する
(= endpoint 配線の次 chunk が get_master_adapter() 1 つで master を読み書きできる前提)。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "server")))
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import master_identity as mi  # noqa: E402
import store_router  # noqa: E402
import master_adapter  # noqa: E402

ORG = "org-p-u1"
T1 = "2026-07-27T01:00:00+00:00"


@pytest.fixture
def wired(monkeypatch):
    """store_router.master_* を in-memory dict に差し替え、adapter を新規化する。"""
    store: dict = {}
    monkeypatch.setattr(store_router, "master_get",
                        lambda entity, pk: store.get((entity, pk)))
    monkeypatch.setattr(store_router, "master_put",
                        lambda entity, pk, data: store.__setitem__((entity, pk), dict(data)))
    monkeypatch.setattr(store_router, "master_scan",
                        lambda entity: [dict(v) for k, v in store.items() if k[0] == entity])
    master_adapter.reset_master_adapter()
    yield master_adapter.get_master_adapter()
    master_adapter.reset_master_adapter()


def test_adapter_roundtrips_through_store_router(wired):
    adapter = wired
    acc = mi.new_master_account("Acme", org_id=ORG, now=T1, master_account_id="macc-1",
                                external_ref={"system": "salesforce", "ref_id": "SF-1"})
    adapter.put_account(acc, now=T1)
    # get / list(org) / external_ref 逆引きが store_router プリミティブ越しに通る
    assert adapter.get_account("macc-1")["name"] == "Acme"
    assert [r["master_account_id"] for r in adapter.list_accounts(ORG)] == ["macc-1"]
    assert adapter.resolve_by_external_ref(ORG, "salesforce", "SF-1")["master_account_id"] == "macc-1"


def test_get_master_adapter_is_cached_until_reset():
    a1 = master_adapter.get_master_adapter()
    a2 = master_adapter.get_master_adapter()
    assert a1 is a2                       # プロセス内で使い回す
    master_adapter.reset_master_adapter()
    assert master_adapter.get_master_adapter() is not a1   # reset で作り直す


def test_persistence_satisfies_contract(wired):
    # BeaconDefaultAdapter が要求する MasterPersistence 契約 (get/put/scan) を満たす
    p = master_adapter._StoreRouterMasterPersistence()
    for m in ("get", "put", "scan"):
        assert callable(getattr(p, m))
