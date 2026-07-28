"""投影 rename の master への write-through (master 権威、ms-111 / e-3622 chunk2a).

leader 合意 guard: user の rename は投影を直接 authoritative にせず、必ず master へ
透過する (投影 name = cache、master = 唯一の真値)。write-through の lib core が:
  - link 済 + same-org → master の name を更新 (master-wins)、
  - 未 link / adapter 無し → None (master 実体なし)、
  - cross-org → fail-closed で拒否、
  - 空 name → 拒否、
を満たすことを hermetic に固定する。bus event 発行 / server consumer 配線は後続。
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
T2 = "2026-07-27T02:00:00+00:00"


@pytest.fixture
def adapter():
    return ms.BeaconDefaultAdapter(ms.InMemoryMasterStore())


def _linked_account(adapter, *, org=ORG_A, name="Acme"):
    data = {"accounts": []}
    sales_entities.account_add(data, name, owner_org_id=org, created_at=T1,
                               master_adapter=adapter)
    return data["accounts"][-1]


def test_write_through_updates_master_name_same_org(adapter):
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    updated = mp.write_through_account_name(acc, adapter, "Acme Inc.", now=T2)
    assert updated is not None and updated["name"] == "Acme Inc."
    # master に永続化されている。
    rec = adapter.get_account(mp.linked_master_account_id(acc))
    assert rec["name"] == "Acme Inc."


def test_write_through_is_master_wins_visible_via_resolve(adapter):
    """write-through 後、resolve が master の新名を返す (master-wins、投影は cache)。"""
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    mp.write_through_account_name(acc, adapter, "Renamed Co.", now=T2)
    # 投影の name は古いまま (cache) でも resolve は master 真値を返す。
    acc["name"] = "Acme"
    assert mp.resolve_account_identity(acc, adapter) == "Renamed Co."


def test_write_through_unlinked_returns_none(adapter):
    acc = {"id": "acc-x", "name": "Acme", "label": "Acme",
           disclosure.OWNER_ORG_FIELD: ORG_A, "contacts": []}  # 未 link
    assert mp.write_through_account_name(acc, adapter, "New", now=T2) is None


def test_write_through_no_adapter_returns_none():
    acc = {"id": "acc-x", "name": "Acme"}
    assert mp.write_through_account_name(acc, None, "New", now=T2) is None


def test_write_through_cross_org_is_refused(adapter):
    """org-A の投影が org-B の master を指す状態での write-through は fail-closed。"""
    m_b = mi.new_master_account("B-name", org_id=ORG_B, now=T1, master_account_id="macc-b")
    adapter.put_account(m_b, now=T1)
    acc = {"id": "acc-1", "name": "Acme-A", "label": "Acme-A",
           disclosure.OWNER_ORG_FIELD: ORG_A, "contacts": []}
    mp.link_account_to_master(acc, "macc-b")
    with pytest.raises(ValueError, match="cross-org write-through refused"):
        mp.write_through_account_name(acc, adapter, "Hijack", now=T2)
    # master(org-B) は書き換わっていない。
    assert adapter.get_account("macc-b")["name"] == "B-name"


def test_write_through_empty_name_is_refused(adapter):
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    with pytest.raises(ValueError, match="cannot be empty"):
        mp.write_through_account_name(acc, adapter, "   ", now=T2)


def test_write_through_preserves_created_at(adapter):
    """master-wins write は created_at を保存し updated_at だけ進める (不変性)。"""
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    before = adapter.get_account(mp.linked_master_account_id(acc))
    updated = mp.write_through_account_name(acc, adapter, "Acme Inc.", now=T2)
    assert updated["created_at"] == before["created_at"]   # 作成時刻は不変
    assert updated["updated_at"] == T2                      # 書き込みで進む


# ---------------------------------------------------------------------------
# part2: server consumer core (apply_master_name_sync) + CLI producer (payload)
# ---------------------------------------------------------------------------
def test_apply_master_name_sync_same_org_updates(adapter):
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    mid = mp.linked_master_account_id(acc)
    out = mp.apply_master_name_sync(adapter, master_account_id=mid,
                                    new_name="Acme Inc.", expected_org=ORG_A, now=T2)
    assert out is not None and out["name"] == "Acme Inc."
    assert adapter.get_account(mid)["name"] == "Acme Inc."


def test_apply_master_name_sync_cross_org_drops_silently(adapter):
    """event 経路: cross-org は raise せず None で drop、master は不変。"""
    m_a = mi.new_master_account("A-name", org_id=ORG_A, now=T1, master_account_id="macc-a")
    adapter.put_account(m_a, now=T1)
    out = mp.apply_master_name_sync(adapter, master_account_id="macc-a",
                                    new_name="Hijack", expected_org=ORG_B, now=T2)
    assert out is None
    assert adapter.get_account("macc-a")["name"] == "A-name"   # 書き換わっていない


def test_apply_master_name_sync_unknown_id_or_empty_drops(adapter):
    assert mp.apply_master_name_sync(adapter, master_account_id="nope",
                                     new_name="X", expected_org=ORG_A, now=T2) is None
    assert mp.apply_master_name_sync(adapter, master_account_id="",
                                     new_name="X", expected_org=ORG_A, now=T2) is None
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    mid = mp.linked_master_account_id(acc)
    assert mp.apply_master_name_sync(adapter, master_account_id=mid,
                                     new_name="  ", expected_org=ORG_A, now=T2) is None


def test_master_sync_payload_from_linked_account(adapter):
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    payload = mp.master_sync_payload(acc, "Acme Inc.")
    assert payload["kind"] == "master_sync" and payload["entity"] == "account"
    assert payload["master_account_id"] == mp.linked_master_account_id(acc)
    assert payload["new_name"] == "Acme Inc." and payload["org_id"] == ORG_A


def test_master_sync_payload_unlinked_or_empty_is_none(adapter):
    assert mp.master_sync_payload({"id": "a", "name": "X"}, "New") is None   # 未 link
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    assert mp.master_sync_payload(acc, "   ") is None                        # 空 name


def test_payload_roundtrips_through_apply(adapter):
    """producer→consumer 一巡: payload を apply に通すと master が更新される。"""
    acc = _linked_account(adapter, org=ORG_A, name="Acme")
    payload = mp.master_sync_payload(acc, "Roundtrip Co.")
    out = mp.apply_master_name_sync(adapter, master_account_id=payload["master_account_id"],
                                    new_name=payload["new_name"],
                                    expected_org=payload["org_id"], now=T2)
    assert out["name"] == "Roundtrip Co."
