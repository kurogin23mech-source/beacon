"""ms-111 e-3621 chunk2b: whole-project write ingest での master linking (AC1・AC2).

server の put_project ingest は全 Account が通る唯一の choke point。そこで呼ぶ純関数
``master_projection.link_project_accounts`` の振る舞いを pin する:

  - 未 link の Account/Contact を Beacon-default master に link し external_ref を張る (AC1)。
  - link 済は冪等に skip (二重起票しない)。
  - same-org 違反 (cross-org poisoning の疑い) はその account だけ skip して他を続ける。
  - link 後は read 側 resolver が master 真値を返す (identity 一本化)。
  - 束縛軸 org_id / system は master_binding が単一真実源 (AC2 = §5・§8 org スコープ)。

server 側の flag ゲート (``BEACON_MASTER_LINKING_ENABLED`` default OFF) は ingest wrapper
の 2 行 guard で、本 test は「ON にした時に link が正しく張られるか」の中核を pure に固める。
CLI は backend adapter を持てない (store_router は server 専用) ため linking は必ずこの
ingest に集約する = 部分 swap (stale と master の混在) を構造的に防ぐ。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import master_binding as mb  # noqa: E402
import master_projection as mp  # noqa: E402
import master_store  # noqa: E402


def _adapter():
    """InMemory backed Beacon-default adapter (backend 非依存の pure テスト用)。"""
    return master_store.BeaconDefaultAdapter(master_store.InMemoryMasterStore())


def _project(org_id: str = "org-p-u1"):
    """owner 付き project (org_id は owner の personal org を導出) + Account 1 + Contact 1。"""
    return {
        "owner": "u1",
        "accounts": [
            {
                "id": "acc-1",
                "name": "Acme Inc",
                "owner_org_id": org_id,
                "phase": "lead",           # work データ (master には写らない)
                "contacts": [
                    {"id": "c-1", "name": "Taro", "email": "taro@acme.example",
                     "role": "CTO"},
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# AC1: 未 link の Account/Contact を master に link し external_ref を張る
# ---------------------------------------------------------------------------
def test_link_project_accounts_links_account_and_contact():
    adapter = _adapter()
    proj = _project()
    org_id = mb.master_binding_org_id(proj)   # AC2: 束縛軸は master_binding が真実源
    assert org_id == "org-p-u1"

    result = mp.link_project_accounts(proj, adapter, org_id=org_id, now="2026-07-28T00:00:00Z")

    assert result["linked_accounts"] == 1
    assert result["linked_contacts"] == 1
    assert result["skipped"] == []

    acc = proj["accounts"][0]
    contact = acc["contacts"][0]
    # 投影に external_ref (= master 参照) が張られた
    assert mp.is_account_linked(acc)
    assert mp.contact_master_ref(contact)
    # master store 側にも identity record が起きた
    master_acc_id = mp.linked_master_account_id(acc)
    assert adapter.get_account(master_acc_id) is not None
    # read 側 resolver が master 真値を返す (identity 一本化)
    assert mp.resolve_account_identity(acc, adapter) == "Acme Inc"


# ---------------------------------------------------------------------------
# 冪等: 2 回目の ingest は二重起票しない
# ---------------------------------------------------------------------------
def test_link_project_accounts_is_idempotent():
    adapter = _adapter()
    proj = _project()
    org_id = mb.master_binding_org_id(proj)

    first = mp.link_project_accounts(proj, adapter, org_id=org_id, now="2026-07-28T00:00:00Z")
    master_acc_id = mp.linked_master_account_id(proj["accounts"][0])

    second = mp.link_project_accounts(proj, adapter, org_id=org_id, now="2026-07-28T01:00:00Z")

    assert first["linked_accounts"] == 1 and second["linked_accounts"] == 0
    assert second["linked_contacts"] == 0
    # master id は変わらない (= 二重 master を作らない)
    assert mp.linked_master_account_id(proj["accounts"][0]) == master_acc_id


# ---------------------------------------------------------------------------
# same-org 違反: 1 件を skip して他は続ける (cross-org poisoning fail-closed)
# ---------------------------------------------------------------------------
def test_link_project_accounts_skips_cross_org_account():
    adapter = _adapter()
    proj = _project(org_id="org-p-u1")
    # account の owner_org が link org_id と食い違う = cross-org。もう 1 件は健全にする。
    proj["accounts"].append({
        "id": "acc-2", "name": "Beta LLC", "owner_org_id": "org-p-u1", "contacts": [],
    })
    proj["accounts"][0]["owner_org_id"] = "org-OTHER"

    result = mp.link_project_accounts(proj, adapter, org_id="org-p-u1",
                                      now="2026-07-28T00:00:00Z")

    # 健全な acc-2 は link、cross-org の acc-1 は skip
    assert result["linked_accounts"] == 1
    assert any(entry[0] == "acc-1" for entry in result["skipped"])
    assert not mp.is_account_linked(proj["accounts"][0])   # acc-1 未 link のまま
    assert mp.is_account_linked(proj["accounts"][1])       # acc-2 は link 済


# ---------------------------------------------------------------------------
# AC2: 明示 master_binding 宣言 (外部 CRM 想定) は Beacon-default と system が異なる
#   → ingest wrapper が scope 外として skip する前提を pin (binding が真実源であること)
# ---------------------------------------------------------------------------
def test_external_crm_binding_is_out_of_scope():
    proj = _project()
    mb.set_master_binding(proj, system="salesforce")
    binding = mb.resolve_master_binding(proj)
    assert binding["system"] == "salesforce"
    assert not mb.is_beacon_default(proj)   # ingest guard がこれを見て link を skip する


# ---------------------------------------------------------------------------
# 空の accounts / org 未定でも壊れない (local mode / migration 期)
# ---------------------------------------------------------------------------
def test_link_project_accounts_empty_is_noop():
    adapter = _adapter()
    result = mp.link_project_accounts({"accounts": []}, adapter,
                                      org_id="org-p-u1", now="2026-07-28T00:00:00Z")
    assert result == {"linked_accounts": 0, "linked_contacts": 0, "skipped": []}
