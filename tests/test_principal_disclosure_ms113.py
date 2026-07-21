"""Unit tests for the principal + disclosure engines (ms-113).

これらは ms-113 の中核 = 「project 参加を唯一の開示境界にする」を pin する。1 つの
disclosure プリミティブと principal 合成契約が、以下のタスク AC を横断して満たす:

  - e-3732 principal 合成 (client = 参加 ∩ / backend = min)
  - e-3733 project 参加でのみ開示 / query 時評価 / 剥奪即時
  - e-3734 org 所有 identity + project リンク開示 (Account を含む一般性)
  - e-3735 外部ゲスト (org 非所属で特定 project にだけ参加)
  - e-3736 backend = min(service, work-unit, originating) で権限昇格不可
  - e-3737 リンク張替え / re-home はイベント (identity 不変) / 剥奪即時
  - e-3738 エージェント間問い合わせを要求元 principal でフィルタ + ms-63 合成
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import principal  # noqa: E402
import disclosure  # noqa: E402
import org  # noqa: E402


# ===========================================================================
# e-3732 / e-3736: principal effective-scope composition
# ===========================================================================

def test_client_effective_is_capped_by_participation():
    # 宣言や focus で参加範囲を超えられない (= 覗けない)。
    p = principal.make_principal("u1", "org-1",
                                 declared_scope=["p1", "p2", "p3"])
    eff = principal.client_effective_scope(p, participation={"p1", "p2"})
    assert eff == {"p1", "p2"}  # p3 は宣言しても参加していないので不可


def test_client_declared_scope_narrows():
    p = principal.make_principal("u1", declared_scope=["p1"])
    eff = principal.client_effective_scope(p, participation={"p1", "p2"})
    assert eff == {"p1"}  # 宣言は絞る方向にのみ効く


def test_client_focus_narrows_to_single_project():
    p = principal.make_principal("u1", focus="p2")
    eff = principal.client_effective_scope(p, participation={"p1", "p2", "p3"})
    assert eff == {"p2"}


def test_client_focus_on_non_participated_project_yields_empty():
    p = principal.make_principal("u1", focus="pX")
    eff = principal.client_effective_scope(p, participation={"p1"})
    assert eff == set()  # 参加していない project に focus しても覗けない


def test_declared_none_means_unconstrained():
    # None (宣言なし) は参加そのまま。空リストとは違う。
    p = principal.make_principal("u1", declared_scope=None)
    assert principal.client_effective_scope(p, {"p1", "p2"}) == {"p1", "p2"}
    p_empty = principal.make_principal("u1", declared_scope=[])
    assert principal.client_effective_scope(p_empty, {"p1", "p2"}) == set()


def test_backend_effective_is_min_of_three():
    # backend は 3 者の min = どれよりも広くならない。
    eff = principal.backend_effective_scope(
        service_grant={"p1", "p2", "p3"},
        work_unit_scope={"p1", "p2"},
        originating_scope={"p2", "p3"},
    )
    assert eff == {"p2"}


def test_backend_cannot_exceed_originating_principal():
    # 混乱した代理人: originating が p1 のみなら、service/work-unit が広くても p1 止まり。
    eff = principal.backend_effective_scope(
        service_grant={"p1", "p2", "p3", "p9"},
        work_unit_scope={"p1", "p2", "p3"},
        originating_scope={"p1"},
    )
    assert eff == {"p1"}


def test_backend_missing_component_is_fail_closed():
    # いずれか欠落 (空) は min ゆえ空集合 = 何も触れない (安全側)。
    assert principal.backend_effective_scope({"p1"}, set(), {"p1"}) == set()


def test_effective_scope_facade_routes_by_agent_kind():
    client = principal.make_principal("u1", agent_kind=principal.AGENT_CLIENT)
    assert principal.effective_scope(client, participation={"p1"}) == {"p1"}
    backend = principal.make_principal("svc", agent_kind=principal.AGENT_BACKEND)
    assert principal.effective_scope(
        backend, service_grant={"p1", "p2"},
        work_unit_scope={"p1"}, originating_scope={"p1", "p2"}) == {"p1"}


def test_make_principal_rejects_bad_agent_kind():
    with pytest.raises(ValueError):
        principal.make_principal("u1", agent_kind="daemon")


def test_can_access_project():
    assert principal.can_access_project({"p1", "p2"}, "p1") is True
    assert principal.can_access_project({"p1"}, "p2") is False
    assert principal.can_access_project(set(), "p1") is False


# ===========================================================================
# e-3733 / e-3734: disclosure primitive (participation-gated, fail-closed)
# ===========================================================================

def _account(links):
    # e-3734: Account は org 所有 identity 1 個 + project リンク集合。
    return {"id": "acc-1", disclosure.OWNER_ORG_FIELD: "org-1",
            disclosure.PROJECT_LINKS_FIELD: list(links), "name": "取引先A"}


def test_disclose_requires_participation_in_a_linked_project():
    acc = _account(["p1", "p2"])
    # p1 に参加している user には見える。
    assert disclosure.can_disclose(acc, {"p1"}) is True
    # p3 にしか参加していない user には見えない (org に居ても、だ)。
    assert disclosure.can_disclose(acc, {"p3"}) is False


def test_unlinked_resource_is_disclosed_to_nobody():
    # fail-closed: どの project にも紐付かない resource は誰にも開示しない。
    assert disclosure.can_disclose(_account([]), {"p1", "p2", "p3"}) is False


def test_filter_disclosable_only_returns_visible():
    resources = [_account(["p1"]), _account(["p2"]), _account([])]
    visible = disclosure.filter_disclosable(resources, {"p1"})
    assert len(visible) == 1
    assert visible[0][disclosure.PROJECT_LINKS_FIELD] == ["p1"]


def test_revocation_is_immediate_at_query_time():
    # e-3733: 判定は query 時の現在 membership で評価 = 剥奪即時。
    acc = _account(["p1"])
    eff_before = {"p1"}          # 参加中は見える
    assert disclosure.can_disclose(acc, eff_before) is True
    eff_after = set()            # 剥奪後 (参加集合が空になった) は即座に見えない
    assert disclosure.can_disclose(acc, eff_after) is False


# ===========================================================================
# e-3737: relink / re-home keeps identity, expressed as add/remove
# ===========================================================================

def test_relink_keeps_identity_only_changes_links():
    acc = _account(["p1"])
    original_id = acc["id"]
    assert disclosure.link_resource_to_project(acc, "p2") is True
    assert disclosure.unlink_resource_from_project(acc, "p1") is True
    # identity (id / owner_org) は不変、リンク集合だけが変わった (= re-home)。
    assert acc["id"] == original_id
    assert acc[disclosure.OWNER_ORG_FIELD] == "org-1"
    assert set(acc[disclosure.PROJECT_LINKS_FIELD]) == {"p2"}


def test_link_and_unlink_are_idempotent_noops():
    acc = _account(["p1"])
    assert disclosure.link_resource_to_project(acc, "p1") is False   # 既存
    assert disclosure.unlink_resource_from_project(acc, "pX") is False  # 無い


# ===========================================================================
# e-3735: external guest — project participation without org membership
# ===========================================================================

def test_external_guest_sees_only_invited_project():
    o = org.build_personal_org("owner", now="2026-07-21T00:00:00+00:00")
    guest = "guest-user"
    # guest は org member ではないが project p-shared に参加している。
    assert org.is_external_guest(o, guest, participates_in_project=True) is True
    assert org.is_org_member(o, guest) is False
    # guest の参加は招待された project だけ ⇒ そこにリンクされた resource のみ見える。
    shared = _account(["p-shared"])
    other = _account(["p-internal"])
    guest_participation = {"p-shared"}
    assert disclosure.can_disclose(shared, guest_participation) is True
    assert disclosure.can_disclose(other, guest_participation) is False


def test_org_member_is_not_a_guest():
    o = org.build_personal_org("owner", now="2026-07-21T00:00:00+00:00")
    assert org.is_external_guest(o, "owner", participates_in_project=True) is False


# ===========================================================================
# e-3738: inter-agent query filtered by REQUESTER principal + ms-63 gate
# ===========================================================================

def test_inter_agent_filters_by_requester_participation():
    # 営業エージェント (全 Account を持つ) に、開発のみ user のエージェントが問い合わせ。
    all_accounts = [_account(["p-sales"]), _account(["p-sales2"])]
    dev_only_participation = {"p-dev"}   # 要求元は営業 project に非参加
    out = disclosure.compose_inter_agent_disclosure(all_accounts, dev_only_participation)
    assert out == []   # 踏み台にしても 1 件も引き出せない


def test_inter_agent_composes_with_sensitivity_gate():
    accounts = [_account(["p1"]), _account(["p1"])]
    accounts[0]["sensitive"] = True
    # ms-63 相当の機微フィルタ: sensitive を落とす。
    gate = lambda r: not r.get("sensitive")
    out = disclosure.compose_inter_agent_disclosure(
        accounts, {"p1"}, sensitivity_gate=gate)
    # 参加フィルタは両方通すが、機微フィルタで 1 件に絞られる (= 合成)。
    assert len(out) == 1
    assert out[0].get("sensitive") is not True
