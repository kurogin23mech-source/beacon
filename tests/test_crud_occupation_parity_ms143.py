"""Parity harness for the ms-143 CRUD 抽象経路化 (leader 握り: 設計判断 b).

ms-143 の目的: profession-shared な CRUD verb (create / add-work-item / set-state)
が特定職種の concrete collection (``data['milestones']`` / ``data['opportunities']``)
を直叩きしている状態を、occupation 側の抽象 primitive + profession_manifest dispatch
に寄せる。その付け替えが【挙動不変】であることを、ms-142 e-5010 と同じ規律で先に pin
する: 「既存テストが緑」は必要条件だが十分条件ではない (既存テストが CRUD 契約の全 field
を覆う保証がない)。so this pins the CONTRACT DIRECTLY at the verb/function boundary.

現況 (この harness が凍結する対象):
  - dev  : ``core.milestone_add`` (next_milestone_id + data['milestones'].append) /
           ``core.task_add`` (find_target_milestone + next_entry_id) /
           ``core.task_done`` (find_entry walk of data['milestones']).
  - sales: ``sales_entities.opportunity_add`` (next_opportunity_id + hand-built
           skeleton + data['opportunities'].append) / ``activity_add`` /
           ``activity_set_status`` (find_activity walk of data['opportunities']).

付け替え後 (create_target / add_work_item / find_target_entry+set_entry_state を
occupation 側に立てて milestone_add/task_done/opportunity_add/activity_* を wire) も、
下の GOLDEN (正規化済レコード) と INVARIANT (基底骨格 / done スタンプ / 探索到達性の
集合) が一致し続けることが parity の証明になる。CLAUDE.md のデバッグ原則の parity 形:
「旧経路と新経路が同じ集合/レコードを出す」を証拠化する (テストが緑、で済ませない)。

cross-profession 骨格 parity (BASE_SKELETON_KEYS が milestone と opportunity の両方に
含まれる) は、『1 本の抽象 create/set primitive が両職種を服す』ための必要条件でもある
— dev で pattern を立てて sales へミラーする時、この集合が崩れていないことを守る。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Human create path: an explicit priority is REQUIRED and mutually exclusive with
# the machine ``allow_untriaged`` sentinel path (core._resolve_priority_for_write),
# so the fixtures below always pass a real severity and never allow_untriaged.

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))

import core          # noqa: E402
import sales_entities  # noqa: E402
import work_base     # noqa: E402
import work_model    # noqa: E402

FIXED_TS = "2026-08-09T00:00:00Z"
FIXED_ACTOR = "test-actor"

# The base skeleton keys BOTH professions carry TODAY. A milestone (built by
# ``work_model.new_target``) and an opportunity (hand-built) currently share
# exactly {id, label, status, created_at}. ms-143 create_target must keep this
# shared set intact so ONE primitive can mint both.
BASE_SKELETON_KEYS = {"id", "label", "status", "created_at"}

# FINDING (parity harness surfaced this): ``created_by`` is a dev-ONLY field today
# — ``work_model.new_target`` stamps it on milestones, but the sales hand-built
# opportunity skeleton omits it. When create_target unifies both through
# ``new_target``, opportunities would GAIN ``created_by`` — a behavior CHANGE, not
# parity. That must be a conscious, leader-held decision, so this asymmetry is
# pinned explicitly below rather than silently absorbed.
DEV_ONLY_SKELETON_KEYS = {"created_by"}

# The done-stamp contract ``work_model.mark_done`` writes; set-state primitive
# must preserve it for BOTH a dev task and a sales activity.
DONE_STAMP_KEYS = {"status", "done_at"}


@pytest.fixture(autouse=True)
def _freeze_time_and_actor(monkeypatch):
    """Normalise the only volatile fields (timestamp / actor) so record equality
    is deterministic. Both the dev path (core._now_iso / core._get_actor) and the
    sales path (work_base.now_iso / work_base.current_actor) are pinned."""
    monkeypatch.setattr(core, "_now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(core, "_get_actor", lambda: FIXED_ACTOR)
    monkeypatch.setattr(work_base, "now_iso", lambda: FIXED_TS)
    monkeypatch.setattr(work_base, "current_actor", lambda: FIXED_ACTOR)
    # work_model may hold its own bound references; pin defensively if present.
    if hasattr(work_model, "now_iso"):
        monkeypatch.setattr(work_model, "now_iso", lambda: FIXED_TS, raising=False)


def _dev_project() -> dict:
    return {"id": "p-dev", "name": "dev", "profession": "dev", "milestones": []}


def _sales_project() -> dict:
    return {"id": "p-sales", "name": "sales", "profession": "sales",
            "opportunities": [], "accounts": []}


# --- CREATE parity: milestone_add / opportunity_add ---------------------------

def test_create_milestone_record_golden():
    """dev の create pattern (leader が最初に立てろと言った1本) をレコード全体で pin."""
    data = _dev_project()
    ms_id = core.milestone_add(
        data, "抽象経路化MS", priority="high")

    assert ms_id == "ms-1"
    assert len(data["milestones"]) == 1
    rec = data["milestones"][0]

    # 正規化済みレコードの GOLDEN。create_target 付け替え後もこの dict と一致すること。
    assert rec == {
        "id": "ms-1",
        "label": "抽象経路化MS",
        "status": "todo",
        "created_at": FIXED_TS,
        "created_by": FIXED_ACTOR,
        "title": "抽象経路化MS",     # dev legacy dual-write (ride-along extra)
        "target_date": "",
        "commits": [],
        "priority": "high",
    }


def test_create_opportunity_carries_base_skeleton():
    data = _sales_project()
    opp_id = sales_entities.opportunity_add(
        data, "A社商談", phase="lead", created_at=FIXED_TS)

    assert opp_id == "opp-1"
    assert len(data["opportunities"]) == 1
    rec = data["opportunities"][0]
    # sales は今 hand-built skeleton だが、create_target 統合後も基底骨格キーは
    # 保たれること (= 職種を跨いで同じ create 抽象が服せる必要条件)。
    assert BASE_SKELETON_KEYS <= set(rec), \
        f"opportunity missing base skeleton: {BASE_SKELETON_KEYS - set(rec)}"
    assert rec["id"] == "opp-1"
    assert rec["label"] == "A社商談"


def test_create_cross_profession_base_parity():
    """milestone と opportunity が同じ基底骨格キー集合を持つ = 1 本の create
    primitive で両職種を mint できる、の構造的必要条件。"""
    dev = _dev_project()
    core.milestone_add(dev, "M", priority="high")
    sales = _sales_project()
    sales_entities.opportunity_add(sales, "O", phase="lead", created_at=FIXED_TS)

    ms_keys = set(dev["milestones"][0])
    opp_keys = set(sales["opportunities"][0])
    assert BASE_SKELETON_KEYS <= ms_keys
    assert BASE_SKELETON_KEYS <= opp_keys


def test_created_by_is_dev_only_today():
    """FINDING (harness が surface): 現状 milestone は created_by を持つが opportunity
    は持たない。create_target 統合で opportunity に created_by が付くのは【挙動変更】
    なので、leader 握りの意図的判断として顕在化しておく (silent に吸収しない)。"""
    dev = _dev_project()
    core.milestone_add(dev, "M", priority="high")
    sales = _sales_project()
    sales_entities.opportunity_add(sales, "O", phase="lead", created_at=FIXED_TS)

    assert DEV_ONLY_SKELETON_KEYS <= set(dev["milestones"][0])
    assert not (DEV_ONLY_SKELETON_KEYS & set(sales["opportunities"][0])), \
        "opportunity gained created_by — this is a behavior change; confirm with " \
        "leader whether create_target should stamp it on sales targets."


# --- ADD-WORK-ITEM parity: task_add / activity_add ----------------------------

def test_add_work_item_task_lands_under_milestone():
    data = _dev_project()
    core.milestone_add(data, "M", priority="high")
    eid = core.task_add(data, "ms-1", "抽象化タスク",
                        priority="high")

    entries = data["milestones"][0]["entries"]
    assert [e["id"] for e in entries] == [eid]
    task = entries[0]
    assert task["type"] == "task"
    assert task["description"] == "抽象化タスク"
    assert task["status"] == "todo"


def test_add_work_item_activity_lands_under_opportunity():
    data = _sales_project()
    sales_entities.opportunity_add(data, "O", phase="lead", created_at=FIXED_TS)
    aid = sales_entities.activity_add(data, "opp-1", "初回訪問",
                                      created_at=FIXED_TS)

    acts = data["opportunities"][0]["activities"]
    assert [a["id"] for a in acts if a["id"] == aid] == [aid]
    act = next(a for a in acts if a["id"] == aid)
    assert act["description"] == "初回訪問"
    assert act["status"] == "todo"


# --- SET-STATE parity: task_done / activity_set_status ------------------------

def test_set_state_task_done_stamp():
    data = _dev_project()
    core.milestone_add(data, "M", priority="high")
    eid = core.task_add(data, "ms-1", "完了対象",
                        priority="high")
    _ms, entry = core.task_done(data, eid)

    assert entry["status"] == work_model.DONE_STATUS
    assert entry.get("done_at")
    assert DONE_STAMP_KEYS <= set(entry)


def test_set_state_activity_done_stamp():
    data = _sales_project()
    sales_entities.opportunity_add(data, "O", phase="lead", created_at=FIXED_TS)
    aid = sales_entities.activity_add(data, "opp-1", "完了対象活動",
                                      created_at=FIXED_TS)
    act = sales_entities.activity_set_status(data, aid, "done", at=FIXED_TS)

    assert act["status"] == work_model.DONE_STATUS
    assert act.get("done_at")
    assert DONE_STAMP_KEYS <= set(act)


def test_set_state_done_stamp_parity_across_professions():
    """task done と activity done が同じ done-stamp キー集合を書く = set-state
    primitive が両職種を服せる (work_model.mark_done 共有の現契約を凍結)。"""
    dev = _dev_project()
    core.milestone_add(dev, "M", priority="high")
    e = core.task_add(dev, "ms-1", "t", priority="high")
    _ms, task = core.task_done(dev, e)

    sales = _sales_project()
    sales_entities.opportunity_add(sales, "O", phase="lead", created_at=FIXED_TS)
    a = sales_entities.activity_add(sales, "opp-1", "a", created_at=FIXED_TS)
    act = sales_entities.activity_set_status(sales, a, "done", at=FIXED_TS)

    assert DONE_STAMP_KEYS <= set(task)
    assert DONE_STAMP_KEYS <= set(act)


# --- FIND parity: find_entry (dev) / find_activity (sales) --------------------
# find_target_entry (ms-143) must reach BOTH by walking manifest arms instead of
# hardcoding data['milestones'].

def test_find_reaches_dev_task():
    data = _dev_project()
    core.milestone_add(data, "M", priority="high")
    eid = core.task_add(data, "ms-1", "探索対象",
                        priority="high")
    found = core.find_entry(data, eid)
    assert found is not None
    _container, _entries, entry, _idx = found
    assert entry["id"] == eid


def test_find_reaches_sales_activity():
    data = _sales_project()
    sales_entities.opportunity_add(data, "O", phase="lead", created_at=FIXED_TS)
    aid = sales_entities.activity_add(data, "opp-1", "探索対象活動",
                                      created_at=FIXED_TS)
    opp, act = sales_entities.find_activity(data, aid)
    assert act is not None
    assert act["id"] == aid
    assert opp["id"] == "opp-1"
