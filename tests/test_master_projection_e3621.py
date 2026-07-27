"""Unit tests for projection→master reference layer (ms-111 / e-3621 part2 前半).

投影 (project の Account/Contact) からマスター (顧客 identity 真値源) への参照を
張る additive 層を pin する。ここは非破壊: 既存 identity フィールドを保ったまま
master_ref を足し、投影 identity → マスター record の mapper を確立する。

ここで pin する不変条件:
  - link は既存フィールドを壊さず master_ref を足す。read は未 link を None で返す。
  - projection → master mapper は identity だけを写し work データを落とす
    (= 薄いマスター schema の allowlist を通る)。org_id 束縛 (SPEC §8) が刻まれる。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import master_identity as mi  # noqa: E402
import master_projection as mp  # noqa: E402
import master_store as ms  # noqa: E402

ORG = "org-p-u1"
NOW = "2026-07-27T00:00:00+00:00"


@pytest.fixture
def adapter():
    return ms.BeaconDefaultAdapter(ms.InMemoryMasterStore())


# ---------------------------------------------------------------------------
# link / read (非破壊)
# ---------------------------------------------------------------------------
def test_link_account_is_additive():
    acc = {"id": "acc-1", "name": "Acme", "phase": "リード", "contacts": []}
    mp.link_account_to_master(acc, "macc-1")
    # 既存フィールドは保たれる
    assert acc["name"] == "Acme" and acc["phase"] == "リード" and acc["contacts"] == []
    # 参照が足された
    assert mp.is_account_linked(acc)
    assert mp.linked_master_account_id(acc) == "macc-1"
    assert acc["master_ref"] == {"system": "beacon-default", "master_account_id": "macc-1"}


def test_unlinked_account_reads_none():
    assert mp.account_master_ref({"id": "acc-1", "name": "Acme"}) is None
    assert not mp.is_account_linked({"name": "Acme"})
    assert mp.linked_master_account_id({"name": "Acme"}) == ""


def test_link_account_requires_master_id():
    with pytest.raises(ValueError):
        mp.link_account_to_master({"name": "Acme"}, "")


def test_link_contact_carries_parent_and_system():
    c = {"name": "Taro", "email": "t@acme.example"}
    mp.link_contact_to_master(c, "mcon-1", "macc-1", system="salesforce")
    ref = mp.contact_master_ref(c)
    assert ref["master_contact_id"] == "mcon-1"
    assert ref["master_account_id"] == "macc-1"
    assert ref["system"] == "salesforce"
    assert c["email"] == "t@acme.example"   # 非破壊


# ---------------------------------------------------------------------------
# projection → master mapper: identity のみ写す
# ---------------------------------------------------------------------------
def test_project_account_drops_work_data():
    acc = {"id": "acc-1", "name": "Acme", "phase": "成約顧客", "health": "good",
           "contacts": [{"name": "x"}], "assignee": "u9"}
    master = mp.project_account_to_master(acc, org_id=ORG, now=NOW,
                                          master_account_id="macc-1")
    # 薄いマスター schema の allowlist を通る = work データは落ちている
    assert mi.is_identity_only(master, kind="account")
    assert master["name"] == "Acme"
    assert master["org_id"] == ORG
    assert "phase" not in master and "contacts" not in master


def test_project_account_uses_label_fallback():
    acc = {"id": "acc-1", "label": "Acme Corp"}   # name 無し → label
    master = mp.project_account_to_master(acc, org_id=ORG, now=NOW, master_account_id="macc-1")
    assert master["name"] == "Acme Corp"


def test_project_contact_maps_identity_attrs():
    c = {"name": "Taro", "email": "t@acme.example", "phone": "03", "role": "CTO"}
    master = mp.project_contact_to_master(c, org_id=ORG, master_account_id="macc-1",
                                          now=NOW, master_contact_id="mcon-1")
    assert mi.is_identity_only(master, kind="contact")
    assert master["email"] == "t@acme.example" and master["role"] == "CTO"
    assert master["master_account_id"] == "macc-1" and master["org_id"] == ORG


def test_project_account_to_master_is_pure():
    # mapper は元の投影 dict を書き換えない (link とは別責務)
    acc = {"id": "acc-1", "name": "Acme", "phase": "リード"}
    mp.project_account_to_master(acc, org_id=ORG, now=NOW, master_account_id="macc-1")
    assert "master_ref" not in acc and acc["phase"] == "リード"


# ---------------------------------------------------------------------------
# 読み出しのマスター一本化: link 済は master、未 link/adapter 無しは投影 fallback
# ---------------------------------------------------------------------------
def test_resolve_account_identity_reads_master_when_linked(adapter):
    # ms-111 e-3622: master を採用する条件に org 一致が加わったので、投影 Account は
    # 自身の所有 org (owner_org_id) を ORG で宣言する (= same-org、fail-closed の対)。
    acc = {"id": "acc-1", "name": "旧名 (投影)", "label": "旧名 (投影)", "phase": "リード",
           "owner_org_id": ORG}
    mp.link_new_account_to_master(acc, adapter, org_id=ORG, now=NOW)
    # master 側の name を後から変える (= master が真値源)
    mid = mp.linked_master_account_id(acc)
    adapter.put_account({**adapter.get_account(mid), "name": "新名 (master)"}, now=NOW)
    # 投影の name は "旧名" のままだが、resolve は master 経由で "新名" を返す
    assert acc["name"] == "旧名 (投影)"
    assert mp.resolve_account_identity(acc, adapter) == "新名 (master)"


def test_resolve_account_identity_falls_back_when_unlinked(adapter):
    acc = {"id": "acc-1", "name": "Acme", "label": "Acme"}
    # 未 link → 投影の tolerant read
    assert mp.resolve_account_identity(acc, adapter) == "Acme"
    # adapter 無し (local mode) でも投影へ fallback
    assert mp.resolve_account_identity(acc, None) == "Acme"
    # label 優先の tolerant 挙動 (work_model.target_label 準拠)
    assert mp.resolve_account_identity({"name": "N", "label": "L"}, None) == "L"


def test_link_new_account_is_idempotent(adapter):
    acc = {"id": "acc-1", "name": "Acme"}
    a = mp.link_new_account_to_master(acc, adapter, org_id=ORG, now=NOW)
    b = mp.link_new_account_to_master(acc, adapter, org_id=ORG, now=NOW)
    assert a["master_account_id"] == b["master_account_id"]   # 二重起票しない
    assert len(adapter.list_accounts(ORG)) == 1


def test_link_new_contact_and_resolve(adapter):
    acc = {"id": "acc-1", "name": "Acme"}
    master_acc = mp.link_new_account_to_master(acc, adapter, org_id=ORG, now=NOW)
    c = {"name": "Taro", "email": "t@acme.example", "role": "CTO", "phone": "03"}
    mp.link_new_contact_to_master(c, adapter, org_id=ORG,
                                  master_account_id=master_acc["master_account_id"], now=NOW)
    ident = mp.resolve_contact_identity(c, adapter)
    assert ident == {"name": "Taro", "email": "t@acme.example", "phone": "03", "role": "CTO"}
    # 未 link contact は投影 fallback
    assert mp.resolve_contact_identity({"name": "X"}, adapter)["name"] == "X"
