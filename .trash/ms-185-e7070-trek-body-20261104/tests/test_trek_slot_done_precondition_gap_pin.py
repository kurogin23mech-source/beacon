"""Gap-pin tests for ``check_slot_done_precondition`` (ms-99 / e-2839).

Complements ``TestSlotDonePreconditionHelper`` in ``tests/test_trek_api.py``
by covering the 4 branches that the existing helper class does not exercise:

  1. **MS ref, all children done → SLOT_DONE_ALLOWED** — the MS happy path
     was previously only reached transitively; this file pins it directly.
  2. **Task ref, no scope project has record → SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED**
     (task 経路の末尾 reject: どの scope project にも該当 task が無い)
  3. **MS ref, no scope project has the MS → SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED**
     (MS 経路の末尾 reject: どの scope project にも該当 MS が無い)
  4. **Empty ``task_id`` precondition → SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED**
     (関数入口の precondition: ``task_id`` が空文字列 / None)

なぜ本 file を分けるか: ``TestSlotDonePreconditionHelper`` は
task ref happy / task ref reject / MS ref empty / MS ref partial / cross-project
exception fallback の 5 branch を pin している。 上記 4 branch は
現行 code の到達可能経路として存在するのに pin されていなかったため、
regression が silent に混入するリスクがあった (memo doc
``JipCZ4lk3s6eBFxbTb4x`` §gap#1 参照)。

Layer A の unit / branch pin として ms-99 の Phase 1 と並列で land する。
"""

from __future__ import annotations

import os
import sys

# lib/ を import path に載せる (test_trek_api.py と同じ pattern)。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import trek as trek_mod  # noqa: E402


def _make_trek_doc(*, project: str = "p-x", milestone: str = "ms-1") -> dict:
    """Minimum trek_doc for slot-done tests (mirrors the pattern
    ``TestSlotDonePreconditionHelper._make_trek_doc`` uses)."""
    return {
        "trek_id": "tk-gap-pin",
        "scope": [{"project": project, "milestone": milestone}],
        "task_states": {},
    }


# ---------------------------------------------------------------------------
# Branch 1: MS ref, all children done → SLOT_DONE_ALLOWED
# ---------------------------------------------------------------------------

def test_helper_allows_ms_when_all_children_done():
    """MS slot done の happy path: 配下 task が全て done なら allow。

    現状 test_trek_api.py の helper class は MS ref の reject 3 branch
    (empty / partial done / not found) は pin しているが、 all-done の
    happy path 直接 pin が抜けていた。 本 test で pinning。
    """
    doc = _make_trek_doc(milestone="ms-all-done")

    def _gp(pid: str) -> dict:
        return {
            "milestones": [{
                "id": "ms-all-done",
                "entries": [
                    {"id": "e-1", "type": "task", "status": "done"},
                    {"id": "e-2", "type": "task", "status": "done"},
                    {"id": "e-3", "type": "task", "status": "done"},
                ],
            }],
            "operations": [],
        }

    allowed, code, msg = trek_mod.check_slot_done_precondition(
        doc, task_id="ms-all-done", get_project=_gp,
    )
    assert allowed is True
    assert code == trek_mod.SLOT_DONE_ALLOWED
    assert msg == ""


def test_helper_allows_ms_with_only_task_children_done_ignoring_non_task_type():
    """MS ref all-done judgement は type=task の child だけを見る。

    (line 2359-2363: ``task_children = [c for c in children if ...
    (c.get("type") or "task") == "task"]``)

    非-task 子 (= sub-milestone / operation / commit 等) が done でなくても、
    全 task-typed child が done なら allow。 現行 code の branch を pin。
    """
    doc = _make_trek_doc(milestone="ms-mixed-types")

    def _gp(pid: str) -> dict:
        return {
            "milestones": [{
                "id": "ms-mixed-types",
                "entries": [
                    {"id": "e-1", "type": "task", "status": "done"},
                    # 非-task child は状態が todo でも判定に含まれない
                    {"id": "sub-1", "type": "milestone", "status": "todo"},
                    {"id": "op-1", "type": "operation", "status": "in_progress"},
                ],
            }],
            "operations": [],
        }

    allowed, code, _msg = trek_mod.check_slot_done_precondition(
        doc, task_id="ms-mixed-types", get_project=_gp,
    )
    assert allowed is True
    assert code == trek_mod.SLOT_DONE_ALLOWED


# ---------------------------------------------------------------------------
# Branch 2: Task ref, no scope project has record → LOOKUP_FAILED
# ---------------------------------------------------------------------------

def test_helper_rejects_task_when_no_scope_project_has_record():
    """Task ref path で全 scope project 舐め切っても該当 entry が無い場合、
    LOOKUP_FAILED で reject (line 2415-2420)。

    phantom done 変種 (= task が存在しないのに slot だけ done) の構造防御。
    """
    doc = _make_trek_doc(project="p-empty", milestone="ms-x")

    def _gp(pid: str) -> dict:
        # 空の project pool を返す
        return {"milestones": [], "operations": []}

    allowed, code, msg = trek_mod.check_slot_done_precondition(
        doc, task_id="e-missing", get_project=_gp,
    )
    assert allowed is False
    assert code == trek_mod.SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED
    assert "e-missing" in msg
    # human message には phantom done への言及があること (SPEC の recovery guidance 要件)
    assert "phantom done" in msg


def test_helper_rejects_task_when_all_scope_projects_return_none():
    """Multi-project scope で全部 get_project が None を返した場合も
    LOOKUP_FAILED (line 2352-2353 continue → 末尾 reject)。
    """
    doc = {
        "trek_id": "tk-gap-pin",
        "scope": [
            {"project": "p-a", "milestone": "ms-1"},
            {"project": "p-b", "milestone": "ms-1"},
        ],
        "task_states": {},
    }

    def _gp(pid: str) -> None:
        return None

    allowed, code, _msg = trek_mod.check_slot_done_precondition(
        doc, task_id="e-nowhere", get_project=_gp,
    )
    assert allowed is False
    assert code == trek_mod.SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED


# ---------------------------------------------------------------------------
# Branch 3: MS ref, no scope project has the MS → LOOKUP_FAILED
# ---------------------------------------------------------------------------

def test_helper_rejects_ms_when_no_scope_project_has_ms():
    """MS ref path で全 scope project 舐め切っても該当 MS が無い場合、
    LOOKUP_FAILED (line 2409-2414)。

    Task ref 経路の LOOKUP_FAILED とは分岐が別 (is_ms_ref 判定で先に落ちる)、
    別 human message を返す。
    """
    doc = _make_trek_doc(project="p-other", milestone="ms-99")

    def _gp(pid: str) -> dict:
        # ms-99 は存在しない project pool
        return {
            "milestones": [{"id": "ms-other", "entries": [
                {"id": "e-1", "type": "task", "status": "done"}
            ]}],
            "operations": [],
        }

    allowed, code, msg = trek_mod.check_slot_done_precondition(
        doc, task_id="ms-99", get_project=_gp,
    )
    assert allowed is False
    assert code == trek_mod.SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED
    assert "ms-99" in msg
    # MS 経路特有の message ("scope の整合性が崩れて" 等)
    assert "scope" in msg


# ---------------------------------------------------------------------------
# Branch 4: Empty task_id precondition → LOOKUP_FAILED
# ---------------------------------------------------------------------------

def test_helper_rejects_empty_task_id():
    """関数入口の precondition (line 2329-2332): task_id 空文字列は
    LOOKUP_FAILED で即 reject。
    """
    doc = _make_trek_doc()

    def _gp(pid: str) -> None:
        # get_project は呼ばれないはず (precondition で先に落ちる)
        raise AssertionError("get_project should not be called for empty task_id")

    allowed, code, msg = trek_mod.check_slot_done_precondition(
        doc, task_id="", get_project=_gp,
    )
    assert allowed is False
    assert code == trek_mod.SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED
    assert "task_id" in msg


def test_helper_rejects_none_task_id():
    """task_id=None も empty string と同様に precondition で reject。

    ``if not task_id:`` は "" と None の両方を捕捉する branch。
    """
    doc = _make_trek_doc()

    def _gp(pid: str) -> None:
        raise AssertionError("get_project should not be called for None task_id")

    allowed, code, _msg = trek_mod.check_slot_done_precondition(
        doc, task_id=None, get_project=_gp,  # type: ignore[arg-type]
    )
    assert allowed is False
    assert code == trek_mod.SLOT_DONE_REJECT_TASK_POOL_LOOKUP_FAILED


# ---------------------------------------------------------------------------
# Bonus branch: dedup / order — scope に同 project が複数回入っていても
# ---------------------------------------------------------------------------

def test_helper_deduplicates_scope_project_ids():
    """scope に同 project が複数回登録されていても、 get_project は
    project あたり 1 回しか呼ばれない (line 2334-2341: seen set で dedup)。

    現状 code は dedup しているがその挙動が pin されていなかった。
    dedup 破綻すると N-project scope で N 倍の Firestore 読みが走るため
    性能 regression の指標としても意味のある branch。
    """
    doc = {
        "trek_id": "tk-gap-pin",
        "scope": [
            {"project": "p-dup", "milestone": "ms-1"},
            {"project": "p-dup", "milestone": "ms-1"},  # 重複
            {"project": "p-dup", "milestone": "ms-2"},  # 同 project 別 MS
        ],
        "task_states": {},
    }
    call_count = {"n": 0}

    def _gp(pid: str) -> dict:
        call_count["n"] += 1
        return {
            "milestones": [{"id": "ms-1", "entries": [
                {"id": "e-1", "type": "task", "status": "done"},
            ]}],
            "operations": [],
        }

    allowed, code, _msg = trek_mod.check_slot_done_precondition(
        doc, task_id="e-1", get_project=_gp,
    )
    assert allowed is True
    assert code == trek_mod.SLOT_DONE_ALLOWED
    assert call_count["n"] == 1, (
        f"get_project should be called exactly once (dedup), "
        f"but was called {call_count['n']} times"
    )
