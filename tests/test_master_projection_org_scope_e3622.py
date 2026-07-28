"""org-scoped fail-closed な identity 解決 + same-org linking ガード
(ms-111 / e-3622 chunk1、leader caveat = cross-org poisoning 防止).

投影 (org-A) が別 org (org-B) の master を指してしまった時 (= poisoning) に、read が
その別 org の identity を漏らさないことを固定する。防御は 2 段:
  - read 側 (resolve_*_identity / account_read_view): fetch した master の org が投影の
    所有 org と一致しない限り master を採らず投影 fallback (fail-closed)。
  - write 側 (link_new_account_to_master): 所有 org と link org の不一致を構造で拒否。

hermetic (InMemoryMasterStore + BeaconDefaultAdapter、live backend 非依存)。
"""
from __future__ import annotations

import os
import sys

import pytest

THIS = os.path.dirname(__file__)
sys.path.insert(0, os.path.normpath(os.path.join(THIS, "..", "lib")))

import disclosure  # noqa: E402
import master_identity as mi  # noqa: E402
import master_projection as mp  # noqa: E402
import master_store as ms  # noqa: E402
import sales_entities  # noqa: E402

ORG_A = "org-p-a"
ORG_B = "org-p-b"
T1 = "2026-07-27T01:00:00+00:00"


@pytest.fixture
def adapter():
    return ms.BeaconDefaultAdapter(ms.InMemoryMasterStore())


def _linked_account(adapter, *, org):
    """org 所有の投影 Account を作り、同 org の master に link して返す。"""
    data = {"accounts": []}
    sales_entities.account_add(data, "Acme", owner_org_id=org, created_at=T1,
                               master_adapter=adapter)
    return data["accounts"][-1]


# ---------------------------------------------------------------------------
# read 側: resolve_account_identity の org 照合
# ---------------------------------------------------------------------------
def test_same_org_link_returns_master_name(adapter):
    acc = _linked_account(adapter, org=ORG_A)
    rec = adapter.get_account(mp.linked_master_account_id(acc))
    rec["name"] = "Acme Inc."
    adapter.put_account(rec, now=T1)
    acc["name"] = "Acme"                       # 投影は古い
    assert mp.resolve_account_identity(acc, adapter) == "Acme Inc."  # 同 org → master


def test_cross_org_master_is_not_leaked_falls_back_to_projection(adapter):
    """投影(org-A)が org-B の master を指していたら master を採らず投影へ落とす。"""
    # org-B の master を直接立てる。
    m_b = mi.new_master_account("SECRET-B", org_id=ORG_B, now=T1, master_account_id="macc-b")
    adapter.put_account(m_b, now=T1)
    # org-A の投影を作り、org-B の master へ (異常に) link させる。
    acc = {"id": "acc-1", "name": "Acme-A", "label": "Acme-A",
           disclosure.OWNER_ORG_FIELD: ORG_A, "contacts": []}
    mp.link_account_to_master(acc, "macc-b")
    # fail-closed: org 不一致なので master(SECRET-B) を漏らさず投影名を返す。
    assert mp.resolve_account_identity(acc, adapter) == "Acme-A"


def test_projection_without_owner_org_falls_back_even_if_linked(adapter):
    """所有 org 未宣言の投影が link 済でも照合材料が無いので fail-closed (投影)。"""
    m = mi.new_master_account("MasterName", org_id=ORG_A, now=T1, master_account_id="macc-x")
    adapter.put_account(m, now=T1)
    acc = {"id": "acc-2", "name": "ProjName", "label": "ProjName", "contacts": []}  # owner_org 無し
    mp.link_account_to_master(acc, "macc-x")
    assert mp.resolve_account_identity(acc, adapter) == "ProjName"


# ---------------------------------------------------------------------------
# read 側: resolve_contact_identity の expected_org 照合
# ---------------------------------------------------------------------------
def test_contact_same_org_returns_master(adapter):
    # org-A の投影 account (data 内) に contact を足して同 org master に link。
    data = {"accounts": []}
    sales_entities.account_add(data, "Acme", owner_org_id=ORG_A, created_at=T1,
                               master_adapter=adapter)
    acc = data["accounts"][-1]
    c = sales_entities.contact_add(data, acc["id"], "Bob", role="CTO",
                                   master_adapter=adapter, now=T1)
    ident = mp.resolve_contact_identity(c, adapter, expected_org=ORG_A)
    assert ident["name"] == "Bob" and ident["role"] == "CTO"


def test_contact_cross_org_falls_back(adapter):
    """master contact が org-B でも expected_org=org-A なら投影値へ fail-closed。"""
    m_c = mi.new_master_contact("SECRET-C", org_id=ORG_B, master_account_id="macc-b",
                                now=T1, master_contact_id="mct-b")
    adapter.put_contact(m_c, now=T1)
    contact = {"name": "Proj-Bob", "role": "eng", "email": "", "phone": ""}
    mp.link_contact_to_master(contact, "mct-b", "macc-b")
    ident = mp.resolve_contact_identity(contact, adapter, expected_org=ORG_A)
    assert ident["name"] == "Proj-Bob"          # master(SECRET-C) を採らない


def test_contact_empty_expected_org_falls_back(adapter):
    m_c = mi.new_master_contact("MasterBob", org_id=ORG_A, master_account_id="macc-a",
                                now=T1, master_contact_id="mct-a")
    adapter.put_contact(m_c, now=T1)
    contact = {"name": "ProjBob", "role": "", "email": "", "phone": ""}
    mp.link_contact_to_master(contact, "mct-a", "macc-a")
    assert mp.resolve_contact_identity(contact, adapter, expected_org="")["name"] == "ProjBob"


# ---------------------------------------------------------------------------
# account_read_view が contact に親 org を渡す
# ---------------------------------------------------------------------------
def test_account_read_view_threads_org_to_contacts(adapter):
    """親(org-A)配下の contact が org-B master を指しても view は master を漏らさない。"""
    m_c = mi.new_master_contact("SECRET-C", org_id=ORG_B, master_account_id="macc-b",
                                now=T1, master_contact_id="mct-b")
    adapter.put_contact(m_c, now=T1)
    contact = {"name": "Proj-Bob", "role": "", "email": "", "phone": ""}
    mp.link_contact_to_master(contact, "mct-b", "macc-b")
    acc = {"id": "acc-1", "name": "Acme", "label": "Acme",
           disclosure.OWNER_ORG_FIELD: ORG_A, "contacts": [contact]}
    view = mp.account_read_view(acc, adapter)
    assert view["contacts"][0]["name"] == "Proj-Bob"   # cross-org master を漏らさない


# ---------------------------------------------------------------------------
# write 側: same-org linking ガード
# ---------------------------------------------------------------------------
def test_link_new_account_rejects_cross_org(adapter):
    acc = {"id": "acc-1", "name": "Acme", "label": "Acme",
           disclosure.OWNER_ORG_FIELD: ORG_A, "contacts": []}
    with pytest.raises(ValueError, match="same-org linking violation"):
        mp.link_new_account_to_master(acc, adapter, org_id=ORG_B, now=T1)


def test_link_new_account_allows_when_owner_org_unset(adapter):
    """所有 org 未宣言なら照合材料が無いので拒否せず link する (read 側 fail-closed で守る)。"""
    acc = {"id": "acc-2", "name": "Acme", "label": "Acme", "contacts": []}
    saved = mp.link_new_account_to_master(acc, adapter, org_id=ORG_A, now=T1)
    assert saved is not None and mp.is_account_linked(acc)
