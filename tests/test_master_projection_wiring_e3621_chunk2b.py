"""投影→master の read resolver 配線 + account_add/contact_add の adapter seam
(ms-111 / e-3621 part2 後半 chunk2b).

leader 判断 A: e-3621 chunk2b は additive slice = (1) account_add / contact_add に
optional master_adapter seam を足す (渡されたら link、既定 None は inert)、(2) 投影
Account/Contact の read を master 経由 resolver に一本化する (未 link / adapter 無しは
投影 fallback = 従来値保持)。実 linking の write-through は e-3622 (bus 同期) が駆動。

ここでは lib だけで hermetic に検証する (InMemoryMasterStore + BeaconDefaultAdapter、
live backend 非依存)。server endpoint 配線 (disclosed-accounts) は account_read_view の
shape 不変性で担保し、app.py 本体は import しない (heavy)。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import master_projection as mp  # noqa: E402
import master_store as ms  # noqa: E402
import sales_entities  # noqa: E402

ORG = "org-p-u1"
T1 = "2026-07-27T01:00:00+00:00"


@pytest.fixture
def adapter():
    """Beacon-default master adapter を in-memory persistence で作る (hermetic)。"""
    return ms.BeaconDefaultAdapter(ms.InMemoryMasterStore())


def _project(org_id=ORG):
    # account_add は owner_org を org.project_org_id(data) から導出するが、
    # owner_org_id 明示指定が優先されるのでそれで org を固定する。
    return {"accounts": []}


# ---------------------------------------------------------------------------
# 1. account_add の adapter seam (additive)
# ---------------------------------------------------------------------------
def test_account_add_without_adapter_is_inert(adapter):
    """adapter 未指定 (既定 None) は従来動作 = master に link しない。"""
    data = _project()
    acc_id = sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1)
    acc = data["accounts"][-1]
    assert acc["id"] == acc_id
    assert mp.MASTER_REF_FIELD not in acc          # link されていない
    assert not mp.is_account_linked(acc)


def test_account_add_with_adapter_links_to_master(adapter):
    """adapter を渡すと投影 Account が master に link され、master に record が起きる。"""
    data = _project()
    sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1,
                               master_adapter=adapter)
    acc = data["accounts"][-1]
    assert mp.is_account_linked(acc)               # 投影に master_ref が張られた
    macc_id = mp.linked_master_account_id(acc)
    rec = adapter.get_account(macc_id)
    assert rec is not None and rec["name"] == "Acme"
    # org 束縛軸で master に載っている (SPEC §8)。
    assert [r["master_account_id"] for r in adapter.list_accounts(ORG)] == [macc_id]


def test_account_add_with_adapter_is_idempotent_on_link(adapter):
    """既 link の account を再 link しても master を二重起票しない (link_new の冪等性)。"""
    data = _project()
    sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1,
                               master_adapter=adapter)
    acc = data["accounts"][-1]
    first = mp.linked_master_account_id(acc)
    mp.link_new_account_to_master(acc, adapter, org_id=ORG, now=T1)  # 再呼び出し
    assert mp.linked_master_account_id(acc) == first
    assert len(adapter.list_accounts(ORG)) == 1


# ---------------------------------------------------------------------------
# 2. contact_add の adapter seam
# ---------------------------------------------------------------------------
def test_contact_add_without_adapter_is_inert(adapter):
    data = _project()
    sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1)
    acc_id = data["accounts"][-1]["id"]
    c = sales_entities.contact_add(data, acc_id, "Bob", role="CTO")
    assert mp.MASTER_REF_FIELD not in c


def test_contact_add_with_adapter_links_under_parent(adapter):
    """adapter を渡すと Contact が親会社 (master_account_id) 配下に link される。"""
    data = _project()
    sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1,
                               master_adapter=adapter)
    acc = data["accounts"][-1]
    parent_master = mp.linked_master_account_id(acc)
    c = sales_entities.contact_add(data, acc["id"], "Bob", role="CTO",
                                   email="bob@acme.example",
                                   master_adapter=adapter, now=T1)
    ref = mp.contact_master_ref(c)
    assert ref is not None
    rec = adapter.get_contact(ref["master_contact_id"])
    assert rec["name"] == "Bob" and rec["role"] == "CTO"
    # 親会社に紐付く。
    assert [r["master_contact_id"] for r in adapter.list_contacts(parent_master)] == \
        [ref["master_contact_id"]]


# ---------------------------------------------------------------------------
# 3. read 側: resolve_*_identity の master 権威 / 投影 fallback
# ---------------------------------------------------------------------------
def test_resolve_account_identity_fallback_when_no_adapter():
    """adapter=None は投影 fallback = target_label 相当 (= 従来値)。"""
    acc = {"name": "Acme", "label": "Acme"}
    assert mp.resolve_account_identity(acc, None) == "Acme"


def test_resolve_account_identity_master_is_authoritative_when_linked(adapter):
    """link 済 + adapter は master が真値 (投影の name が古くても master を返す)。"""
    data = _project()
    sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1,
                               master_adapter=adapter)
    acc = data["accounts"][-1]
    macc_id = mp.linked_master_account_id(acc)
    # master 側の canonical name を変える (rename が master に届いた想定)。
    rec = adapter.get_account(macc_id)
    rec["name"] = "Acme Inc."
    adapter.put_account(rec, now=T1)
    # 投影の name は古いまま。
    acc["name"] = "Acme"
    assert mp.resolve_account_identity(acc, adapter) == "Acme Inc."   # master 真値
    assert mp.resolve_account_identity(acc, None) == "Acme"           # fallback は投影


# ---------------------------------------------------------------------------
# 4. account_read_view: shape 不変性 (server endpoint 配線の核)
# ---------------------------------------------------------------------------
def test_account_read_view_unlinked_preserves_shape_and_values():
    """未 link Account は shape も値も従来通り (regression なし)、extra は上乗せ。"""
    acc = {"id": "acc-1", "name": "Acme", "label": "Acme", "phase": "リード",
           "contacts": [{"name": "Bob", "role": "CTO", "email": "b@x", "phone": ""}],
           "project_links": []}
    view = mp.account_read_view(acc, None, {"home_project_id": "P2",
                                            "home_project_name": "営業"})
    # 既存キーは保たれる。
    assert view["id"] == "acc-1" and view["phase"] == "リード"
    assert view["name"] == "Acme" and view["label"] == "Acme"
    # contact も shape 保持 (4 field + 元キー)。
    assert view["contacts"][0]["name"] == "Bob"
    assert view["contacts"][0]["role"] == "CTO"
    # extra が上乗せされる。
    assert view["home_project_id"] == "P2" and view["home_project_name"] == "営業"


def test_account_read_view_linked_routes_identity_through_master(adapter):
    """link 済なら name/label/contact identity が master 経由に差し替わる (shape 不変)。"""
    data = _project()
    sales_entities.account_add(data, "Acme", owner_org_id=ORG, created_at=T1,
                               master_adapter=adapter)
    acc = data["accounts"][-1]
    sales_entities.contact_add(data, acc["id"], "Bob", role="CTO",
                               master_adapter=adapter, now=T1)
    # master 側 canonical を更新。
    rec = adapter.get_account(mp.linked_master_account_id(acc))
    rec["name"] = "Acme Inc."
    adapter.put_account(rec, now=T1)
    acc["name"] = "Acme"  # 投影は古い
    view = mp.account_read_view(acc, adapter)
    assert view["name"] == "Acme Inc." and view["label"] == "Acme Inc."
    # 元の投影キーは残る (shape 不変)。
    assert view["id"] == acc["id"]
    assert view["contacts"][0]["name"] == "Bob"
