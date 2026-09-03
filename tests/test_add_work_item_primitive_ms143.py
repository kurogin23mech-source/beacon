"""Unit tests for the ms-143 profession-generic work-item primitive
(``occupation.add_work_item``, 設計判断 b 系統1 + 設計判断 a global-by-prefix 採番).

dev の task (milestone の entries、type="task") と sales の activity (opportunity の
activities、type なし) が、profession 分岐なしに同じ ``add_work_item`` で各自の arm に
生まれる。id は prefix ごとの GLOBAL 空間で採番され、既存の hand-rolled allocator
(core.next_entry_id が milestones+operations を走査、sales_entities.next_activity_id が
opportunities を走査) と【同じ結果】を出すことを parity で pin する (leader 握り)。

乖離が出たら silent に変えず surface する規律 (ms-142 terminal 差と同じ) — この harness が
その乖離検知器。乖離無し = 旧も (a) と同じ範囲を走査済、の証拠。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import occupation      # noqa: E402
import core            # noqa: E402
import sales_entities  # noqa: E402
import work_base       # noqa: E402
import work_model      # noqa: E402

FIXED_TS = "2026-08-09T00:00:00Z"


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(work_base, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(work_base, "current_actor", lambda: "test-actor")


def _dev_rich():
    """milestones with nested subtasks + an operation carrying e- entries — the
    cross-collection e- space that a naive milestone-only scan would miss."""
    return {"id": "p", "profession": "dev",
            "milestones": [
                {"id": "ms-1", "label": "M", "status": "in_progress", "entries": [
                    {"id": "e-1", "type": "task", "description": "a", "status": "todo"},
                    {"id": "e-2", "type": "task", "description": "b", "status": "todo",
                     "entries": [
                         {"id": "e-3", "type": "task", "description": "c", "status": "todo"},
                     ]},
                ]},
            ],
            "operations": [
                {"id": "op-1", "entries": [
                    {"id": "e-4", "type": "save", "description": "run"},
                    {"id": "e-5", "type": "save", "description": "run2"},
                ]},
            ]}


def _sales_rich():
    return {"id": "p", "profession": "sales",
            "opportunities": [
                {"id": "opp-1", "label": "O", "phase": "lead", "activities": [
                    {"id": "act-1", "description": "x", "status": "todo"},
                    {"id": "act-2", "description": "y", "status": "done"},
                ]},
            ]}


def test_add_task_lands_under_milestone_with_type():
    data = _dev_rich()
    item = occupation.add_work_item(data, "ms-1", description="new task")
    assert item["type"] == "task"
    assert item["status"] == work_model.TODO_STATUS
    assert item["description"] == "new task"
    assert data["milestones"][0]["entries"][-1] is item


def test_add_activity_lands_under_opportunity_no_type():
    data = _sales_rich()
    item = occupation.add_work_item(data, "opp-1", description="visit")
    assert "type" not in item  # opportunity work_item_arm item_type is None
    assert item["status"] == work_model.TODO_STATUS
    assert data["opportunities"][0]["activities"][-1] is item


def test_extra_fields_ride_through():
    data = _sales_rich()
    item = occupation.add_work_item(
        data, "opp-1", description="call", deadline="2026-09-01",
        who_has_the_ball="them")
    assert item["deadline"] == "2026-09-01"
    assert item["who_has_the_ball"] == "them"


# ms-167 e-6042 (Stage 1) — the skeleton guarantees created_at at the lowest layer,
# so a DIRECT call (the generic /work-items endpoint) can't produce a created_at-less
# item, while a frontend that already passes created_at keeps its exact value.

def test_created_at_stamped_by_skeleton_when_absent():
    # A direct skeleton call passing no created_at must still get one (FIXED_TS via
    # the frozen now_iso) — otherwise the deadline engine / ordering silently break.
    item = occupation.add_work_item(_dev_rich(), "ms-1", description="new task")
    assert item["created_at"] == FIXED_TS


def test_created_at_preserves_caller_value_byte_for_byte():
    # dev core.task_add / sales activity_add always pass created_at; setdefault is a
    # no-op for them, so the item shape stays byte-for-byte unchanged.
    item = occupation.add_work_item(
        _dev_rich(), "ms-1", description="t", created_at="2020-01-01T00:00:00Z")
    assert item["created_at"] == "2020-01-01T00:00:00Z"


def test_sales_explicit_empty_created_at_not_overridden():
    # sales activity_add passes created_at explicitly (possibly ""). The skeleton must
    # NOT force-fill it — an explicit "" is kept, so the sales shape is unchanged.
    item = occupation.add_work_item(_sales_rich(), "opp-1", description="v",
                                    created_at="")
    assert item["created_at"] == ""


# ms-167 review (maintainability F5): the "both frontends already pass created_at so
# the skeleton setdefault is a no-op" assumption lived only in a comment. Machine-check
# it end-to-end through the real frontends.

def test_dev_frontend_passes_real_created_at():
    # A dev task added through core.task_add carries a real created_at (the frozen
    # now) — so the skeleton setdefault is a no-op for the dev path (byte-for-byte).
    data = _dev_rich()
    eid = core.task_add(data, "ms-1", "new task", priority="high")
    entry = next(e for m in data["milestones"]
                 for e in m.get("entries", []) if e["id"] == eid)
    assert entry["created_at"] == FIXED_TS


def test_sales_frontend_passes_created_at_key_but_value_is_empty():
    # sales activity_add passes created_at explicitly, so the KEY is present and the
    # skeleton setdefault stays a no-op (byte-for-byte). BUT it defaults the value to
    # "" — a sales activity added without an explicit timestamp gets an EMPTY
    # created_at, not a real one. That empty value is a KNOWN gap surfaced by the AX
    # review (the deadline engine / ordering read created_at); it is tracked as a
    # follow-up and deliberately NOT changed here (out of the Stage-1 byte-for-byte
    # scope). This test pins the current shape so the follow-up is a conscious change.
    data = _sales_rich()
    aid = sales_entities.activity_add(data, "opp-1", "visit")
    act = next(a for o in data["opportunities"]
               for a in o.get("activities", []) if a["id"] == aid)
    assert "created_at" in act
    assert act["created_at"] == ""


# ms-167 Stage2 review (maintainability): the shared arm resolver + the read-back
# locator have their found / not-found paths pinned directly.

def test_find_work_item_found_and_missing_paths():
    data = _dev_rich()
    item = occupation.add_work_item(data, "ms-1", description="t")
    # found: returns the SAME dict object that lives in the arm
    assert occupation._find_work_item(data, "ms-1", item["id"]) is item
    # id miss / target miss → None (the frontend adapters' fallback trigger)
    assert occupation._find_work_item(data, "ms-1", "e-nope") is None
    assert occupation._find_work_item(data, "ms-404", item["id"]) is None


def test_work_item_frontends_registry_keys_are_reachable_kinds():
    # a frontend registered under a kind no target id ever resolves to would be dead
    # (a typo like "milstone"). Assert every registry key is a reachable target kind.
    valid = {work_model.target_kind(pfx + "1")
             for pfx in work_model.known_target_prefixes()}
    for kind in occupation._WORK_ITEM_FRONTENDS:
        assert kind in valid, f"{kind!r} is not a reachable target kind"


def test_missing_target_raises():
    with pytest.raises(ValueError, match="not found"):
        occupation.add_work_item(_dev_rich(), "ms-99", description="x")


# --- 採番 parity: (a) global-by-prefix == 旧 hand-rolled allocator -------------

def test_task_id_matches_next_entry_id_across_operations():
    """dev の e- 採番が core.next_entry_id (milestones+operations 走査) と一致。
    max は operation の e-5 なので naive な milestone-only 走査だと e-4 を返し
    衝突するが、global-by-prefix は e-6 を返す = next_entry_id と同結果。"""
    data = _dev_rich()
    expected = core.next_entry_id(data)          # 旧経路
    item = occupation.add_work_item(data, "ms-1", description="z")
    assert item["id"] == expected == "e-6"


def test_activity_id_matches_next_activity_id():
    data = _sales_rich()
    expected = sales_entities.next_activity_id(data)   # 旧経路
    item = occupation.add_work_item(data, "opp-1", description="z")
    assert item["id"] == expected == "act-3"


def test_no_prefix_crosstalk():
    """act- 採番は e- id を拾わない (prefix グローバル一意の前提)。"""
    data = {"id": "p", "profession": "sales",
            "opportunities": [{"id": "opp-1", "label": "O", "phase": "lead",
                               "activities": [{"id": "act-1", "description": "x"}]}],
            # a stray milestones collection with e- ids must NOT affect act- alloc
            "milestones": [{"id": "ms-1", "entries": [{"id": "e-9", "type": "task"}]}]}
    item = occupation.add_work_item(data, "opp-1", description="z")
    assert item["id"] == "act-2"
