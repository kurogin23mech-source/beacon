"""Unit tests for the ms-143 profession-generic create primitive
(``occupation.create_target`` / ``next_target_id`` / ``target_class``).

これが『1 本の抽象 create が両職種を服す』(leader 握り 設計判断 b 系統2) の証明:
dev の milestone と sales の opportunity が、profession を分岐する if 無しに、
同じ ``create_target`` 呼び出しで各自の collection に正しく生まれる。id 空間は
target-class ごとに独立 (ms- と opp- が互いに影響しない)・決定的であること
(設計判断 ii) も pin する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation  # noqa: E402
import work_base   # noqa: E402
import work_model  # noqa: E402

FIXED_TS = "2026-08-09T00:00:00Z"
FIXED_ACTOR = "test-actor"


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(work_base, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(work_base, "current_actor", lambda: FIXED_ACTOR)


def _dev():
    return {"id": "p", "name": "d", "profession": "dev", "milestones": []}


def _sales():
    return {"id": "p", "name": "s", "profession": "sales", "opportunities": []}


def test_create_milestone_via_generic_primitive():
    data = _dev()
    rec = occupation.create_target(
        data, "milestone", label="抽象MS", target_date="2026-09-01", commits=[])

    assert rec["id"] == "ms-1"
    assert data["milestones"] == [rec]
    # base skeleton from work_model.new_target
    assert rec["label"] == "抽象MS"
    assert rec["status"] == work_model.TODO_STATUS
    assert rec["created_at"] == FIXED_TS
    assert rec["created_by"] == FIXED_ACTOR
    # profession-specific extras ride via **extra
    assert rec["target_date"] == "2026-09-01"
    assert rec["commits"] == []


def test_create_opportunity_via_same_primitive():
    data = _sales()
    rec = occupation.create_target(
        data, "opportunity", label="A社商談", phase="lead", account_id=None)

    assert rec["id"] == "opp-1"
    assert data["opportunities"] == [rec]
    assert rec["label"] == "A社商談"
    assert rec["phase"] == "lead"


def test_id_spaces_are_independent_and_deterministic():
    """ms- と opp- が別空間: 片方を何個作っても他方の採番に影響しない。同じ状態から
    同じ id が出る (決定的)。"""
    data = {"id": "p", "profession": "dev",
            "milestones": [], "opportunities": []}
    m1 = occupation.create_target(data, "milestone", label="m1")
    o1 = occupation.create_target(data, "opportunity", label="o1")
    m2 = occupation.create_target(data, "milestone", label="m2")
    o2 = occupation.create_target(data, "opportunity", label="o2")

    assert [m1["id"], m2["id"]] == ["ms-1", "ms-2"]
    assert [o1["id"], o2["id"]] == ["opp-1", "opp-2"]


def test_next_target_id_is_max_plus_one_not_count():
    """physically-removed id を再発行しない (max+1 不変条件)。かつて Issue#14 で
    core.next_milestone_id が入れたこの規則は、e-6022 で generic allocator
    occupation.next_target_id に一本化された (旧 dev/sales 専用採番器は dead code
    として除去済)。"""
    data = {"id": "p", "profession": "dev",
            "milestones": [{"id": "ms-1"}, {"id": "ms-5"}]}
    assert occupation.next_target_id(data, "milestone") == "ms-6"


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        occupation.next_target_id(_dev(), "nonesuch")
    with pytest.raises(ValueError):
        occupation.create_target(_dev(), "nonesuch", label="x")


def test_unknown_kind_error_names_valid_kinds():
    """review finding #3: the error lists what IS available."""
    try:
        occupation.create_target(_dev(), "nonesuch", label="x")
        assert False, "should have raised"
    except ValueError as e:
        assert "milestone" in str(e)  # a valid dev kind is named


def test_create_target_rejects_id_collision(monkeypatch):
    """review finding #1: create_target is the single writer, so a corrupted id
    space (allocator returns an already-present id) must raise, not silently
    append a duplicate. Force the collision by stubbing the allocator to return
    an id that already exists."""
    data = {"id": "p", "profession": "dev", "milestones": [{"id": "ms-1"}]}
    monkeypatch.setattr(occupation, "next_target_id", lambda d, k: "ms-1")
    with pytest.raises(ValueError, match="collision"):
        occupation.create_target(data, "milestone", label="dup")
    # nothing appended — the pre-append guard fired.
    assert [m["id"] for m in data["milestones"]] == ["ms-1"]
