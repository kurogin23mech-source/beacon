"""Unit tests for the backend service-identity layer (ms-113 / e-3736).

test_principal_disclosure_ms113 が raw な `backend_effective_scope` (= 3 者の min) を
pin する一方、本ファイルは e-3736 の「サービス identity 実体化 + ms-114 接続 seam」を pin する:

  - make_service_identity          : authz 主体 (= 天井 grant を持つ非人間アクター) の値
  - backend_principal              : service identity から 1 作業単位用の backend principal
  - backend_work_unit_effective_scope : ms-114 サーバサイド authoring が呼ぶ接続 seam
                                        (= 天井を service identity から解決し、宣言 scope と
                                        呼び出し元実効の min を取る)

これらは純関数。service identity の registry / 保存は server 側 (= 本層のスコープ外)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import principal  # noqa: E402


# ---------------------------------------------------------------------------
# make_service_identity
# ---------------------------------------------------------------------------

def test_make_service_identity_builds_value():
    svc = principal.make_service_identity(
        "op-runner", {"p2", "p1"}, org_id="org-team-9"
    )
    assert svc["service_id"] == "op-runner"
    assert svc["service_grant"] == ["p1", "p2"]  # sorted, de-duped
    assert svc["org_id"] == "org-team-9"


def test_make_service_identity_rejects_empty_id():
    with pytest.raises(ValueError):
        principal.make_service_identity("", {"p1"})


def test_make_service_identity_empty_grant_is_no_access():
    svc = principal.make_service_identity("op-runner", set())
    assert svc["service_grant"] == []


# ---------------------------------------------------------------------------
# backend_principal
# ---------------------------------------------------------------------------

def test_backend_principal_carries_kind_org_scope_and_service_id():
    svc = principal.make_service_identity("op-runner", {"p1", "p2"}, org_id="org-9")
    p = principal.backend_principal(
        svc, work_unit_scope={"p1"}, delegating_user_id="u1"
    )
    assert p["agent_kind"] == principal.AGENT_BACKEND
    assert p["org_id"] == "org-9"          # org は service identity から継ぐ
    assert p["user_id"] == "u1"            # 委任元の人間 (監査用)
    assert p["declared_scope"] == ["p1"]   # work-unit 宣言が declared_scope に乗る
    assert p["service_id"] == "op-runner"  # どのサービスが動かすか刻む


def test_backend_principal_rejects_non_service_identity():
    with pytest.raises(ValueError):
        principal.backend_principal({"not": "a service"}, work_unit_scope={"p1"})


def test_backend_principal_focus_is_preserved():
    svc = principal.make_service_identity("op-runner", {"p1", "p2"})
    p = principal.backend_principal(svc, work_unit_scope={"p1", "p2"}, focus="p2")
    assert p["focus"] == "p2"


# ---------------------------------------------------------------------------
# backend_work_unit_effective_scope — the ms-114 connection seam
# ---------------------------------------------------------------------------

def test_seam_is_min_of_service_workunit_originating():
    svc = principal.make_service_identity("op-runner", {"p1", "p2", "p3"})
    eff = principal.backend_work_unit_effective_scope(
        svc, work_unit_scope={"p1", "p2"}, originating_effective={"p2", "p3"}
    )
    assert eff == {"p2"}  # 天井∩宣言∩呼び出し元 = {p1,p2,p3}∩{p1,p2}∩{p2,p3}


def test_seam_work_unit_cannot_exceed_service_ceiling():
    # 宣言 scope が天井を超えても、天井までしか触れない。
    svc = principal.make_service_identity("op-runner", {"p1"})
    eff = principal.backend_work_unit_effective_scope(
        svc, work_unit_scope={"p1", "p2", "p9"}, originating_effective={"p1", "p2", "p9"}
    )
    assert eff == {"p1"}


def test_seam_cannot_exceed_originating_confused_deputy():
    # 混乱した代理人: 呼び出し元が p1 のみなら、天井・宣言が広くても p1 止まり。
    svc = principal.make_service_identity("op-runner", {"p1", "p2", "p3"})
    eff = principal.backend_work_unit_effective_scope(
        svc, work_unit_scope={"p1", "p2", "p3"}, originating_effective={"p1"}
    )
    assert eff == {"p1"}


def test_seam_empty_grant_is_fail_closed():
    svc = principal.make_service_identity("op-runner", set())
    eff = principal.backend_work_unit_effective_scope(
        svc, work_unit_scope={"p1"}, originating_effective={"p1"}
    )
    assert eff == set()


def test_seam_matches_facade_effective_scope():
    # seam の結果は、principal を組み立てて facade effective_scope を呼んだ結果と一致する
    # (= server が backend principal を伝播させて実効を取る経路と同値であることを保証)。
    svc = principal.make_service_identity("op-runner", {"p1", "p2", "p3"})
    p = principal.backend_principal(svc, work_unit_scope={"p1", "p2"})
    via_facade = principal.effective_scope(
        p,
        service_grant=svc["service_grant"],
        work_unit_scope=p["declared_scope"],
        originating_scope={"p2", "p3"},
    )
    via_seam = principal.backend_work_unit_effective_scope(
        svc, work_unit_scope=p["declared_scope"], originating_effective={"p2", "p3"}
    )
    assert via_facade == via_seam == {"p2"}
