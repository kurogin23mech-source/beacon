"""Unit tests for master identity adapter + Beacon-default impl (ms-111 / e-3620).

e-3620 は「マスター store 本体 + adapter 契約 + Beacon-default 実装」。ここでは
lib 層の adapter 契約 (SPEC §5) と Beacon-default 実装の書き込み意味論を、注入した
インメモリ保存 (= hermetic、live backend を触らない) で pin する。server の
entity 登録 + /master API + 本番 DDL は e-3620 の次スライスなので本テストの対象外。

ここで pin する不変条件:
  - adapter 契約: put/get/list/external_ref 逆引きが Beacon-default 実装で通る。
  - org_id 束縛 (SPEC §8): list_accounts は org_id で絞る。他 org は混ざらない。
  - master 権威の書き込み (SPEC §7): put のたび updated_at を進め、既存 id への
    更新では created_at を保存する (作成時刻は不変)。
  - 薄さの二重防御: work data を混ぜた record は保存経路 (put) でも弾かれる。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import master_identity as mi  # noqa: E402
import master_store as ms  # noqa: E402

ORG_A = "org-p-u1"
ORG_B = "org-t-team9"
T1 = "2026-07-27T01:00:00+00:00"
T2 = "2026-07-27T02:00:00+00:00"


@pytest.fixture
def adapter():
    return ms.BeaconDefaultAdapter(ms.InMemoryMasterStore())


# ---------------------------------------------------------------------------
# Account: put / get round-trip + master 権威 stamp
# ---------------------------------------------------------------------------
def test_put_get_account_roundtrip(adapter):
    acc = mi.new_master_account("Acme", org_id=ORG_A, now=T1, master_account_id="macc-1")
    saved = adapter.put_account(acc, now=T1)
    assert saved["created_at"] == T1 and saved["updated_at"] == T1
    got = adapter.get_account("macc-1")
    assert got["name"] == "Acme" and got["org_id"] == ORG_A


def test_put_account_update_preserves_created_at_bumps_updated_at(adapter):
    acc = mi.new_master_account("Acme", org_id=ORG_A, now=T1, master_account_id="macc-1")
    adapter.put_account(acc, now=T1)
    # 同 id を後で更新 (名前変更) → created_at 不変、updated_at 前進 (master 権威)
    acc2 = mi.new_master_account("Acme Corp", org_id=ORG_A, now=T2, master_account_id="macc-1")
    saved = adapter.put_account(acc2, now=T2)
    assert saved["created_at"] == T1     # 作成時刻は変わらない
    assert saved["updated_at"] == T2     # 書くたびに更新
    assert adapter.get_account("macc-1")["name"] == "Acme Corp"


def test_get_missing_account_returns_none(adapter):
    assert adapter.get_account("macc-nope") is None
    assert adapter.get_account("") is None


# ---------------------------------------------------------------------------
# list_accounts: org_id 束縛 (SPEC §8) — 他 org は混ざらない
# ---------------------------------------------------------------------------
def test_list_accounts_scoped_by_org(adapter):
    adapter.put_account(mi.new_master_account("A1", org_id=ORG_A, now=T1, master_account_id="macc-a1"), now=T1)
    adapter.put_account(mi.new_master_account("A2", org_id=ORG_A, now=T2, master_account_id="macc-a2"), now=T2)
    adapter.put_account(mi.new_master_account("B1", org_id=ORG_B, now=T1, master_account_id="macc-b1"), now=T1)
    a_ids = {r["master_account_id"] for r in adapter.list_accounts(ORG_A)}
    assert a_ids == {"macc-a1", "macc-a2"}          # ORG_A のみ
    assert {r["master_account_id"] for r in adapter.list_accounts(ORG_B)} == {"macc-b1"}
    assert adapter.list_accounts("") == []
    # 新しい created_at が先頭 (並び規約)
    ordered = adapter.list_accounts(ORG_A)
    assert ordered[0]["master_account_id"] == "macc-a2"


# ---------------------------------------------------------------------------
# external_ref 逆引き (adapter 突合の鍵)
# ---------------------------------------------------------------------------
def test_resolve_by_external_ref(adapter):
    acc = mi.new_master_account(
        "Acme", org_id=ORG_A, now=T1, master_account_id="macc-1",
        external_ref={"system": "salesforce", "ref_id": "SF-42"},
    )
    adapter.put_account(acc, now=T1)
    found = adapter.resolve_by_external_ref("salesforce", "SF-42")
    assert found is not None and found["master_account_id"] == "macc-1"
    # 別 system / 別 ref は当たらない
    assert adapter.resolve_by_external_ref("hubspot", "SF-42") is None
    assert adapter.resolve_by_external_ref("salesforce", "SF-99") is None
    assert adapter.resolve_by_external_ref("", "") is None


# ---------------------------------------------------------------------------
# Contact: 親会社で list、put/get
# ---------------------------------------------------------------------------
def test_contact_put_list_by_parent(adapter):
    c1 = mi.new_master_contact("Taro", org_id=ORG_A, master_account_id="macc-1", now=T1,
                               master_contact_id="mcon-1", email="taro@acme.example")
    c2 = mi.new_master_contact("Hanako", org_id=ORG_A, master_account_id="macc-1", now=T2,
                               master_contact_id="mcon-2")
    c3 = mi.new_master_contact("Other", org_id=ORG_A, master_account_id="macc-9", now=T1,
                               master_contact_id="mcon-3")
    for c in (c1, c2, c3):
        adapter.put_contact(c, now=c["created_at"])
    ids = {r["master_contact_id"] for r in adapter.list_contacts("macc-1")}
    assert ids == {"mcon-1", "mcon-2"}              # 親 macc-1 のみ
    assert adapter.get_contact("mcon-1")["email"] == "taro@acme.example"
    assert adapter.list_contacts("") == []


# ---------------------------------------------------------------------------
# 薄さの二重防御: work data 混入は保存経路 (put) でも弾く
# ---------------------------------------------------------------------------
def test_put_rejects_work_data(adapter):
    acc = mi.new_master_account("Acme", org_id=ORG_A, now=T1, master_account_id="macc-1")
    acc["phase"] = "リード"          # 投影側の work field を混入させる
    with pytest.raises(ValueError):
        adapter.put_account(acc, now=T1)
    # 弾かれたので保存もされていない
    assert adapter.get_account("macc-1") is None


# ---------------------------------------------------------------------------
# 契約の網羅: 抽象メソッドが Beacon-default で実装されている
# ---------------------------------------------------------------------------
def test_beacon_default_satisfies_contract():
    assert issubclass(ms.BeaconDefaultAdapter, ms.MasterIdentityAdapter)
    a = ms.BeaconDefaultAdapter(ms.InMemoryMasterStore())
    for name in ("put_account", "get_account", "list_accounts",
                 "put_contact", "get_contact", "list_contacts",
                 "resolve_by_external_ref"):
        assert callable(getattr(a, name))
