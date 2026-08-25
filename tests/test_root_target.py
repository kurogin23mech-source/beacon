"""Unit tests for root_target — reading a project AS the root target (ms-153
e-5546 / SPEC 方針1, 受入条件1-2).

Pure-function tests (no I/O, no CLI): hand ``project_as_root_target`` a plain
project dict and assert the projection shape, the phase-less / evidence-less arm
mapping, and — critically — that the view NEVER mutates the input (in-place
strangler = read-only view over live data, SPEC 受入条件1).
"""

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import root_target  # noqa: E402
import work_model    # noqa: E402
import occupation    # noqa: E402


def _dev_project(milestones=None):
    return {
        "name": "Beacon",
        "objective": "AI 開発の進捗を透明化する",
        "summary": "直近: root-target 化に着手",
        "profession": "dev",
        "milestones": milestones if milestones is not None else [],
    }


def _ms(id_, status="todo"):
    return {"id": id_, "status": status, "label": id_, "entries": []}


# --- arm mapping declaration (受入条件2) -------------------------------------

def test_arm_mapping_is_phase_less_and_evidence_less():
    arms = root_target.root_target_arms()
    assert arms["kind"] == "root"
    # phase-less
    assert arms["phase_ball"] is None
    assert arms["state_model"] is None
    # evidence-less
    assert arms["evidence_arms"] == []
    # work-item = children via the enumeration seam, NOT a literal collection
    assert arms["work_item_arm"]["item_type"] == "target"
    assert arms["work_item_arm"]["via"] == "occupation.iter_target_records"
    # decision = children's completion approval (declared, ms-154 adjudicates)
    assert arms["decision"]["kind"] == "completion_approval"
    # deliverable = achievement (declared, ms-155 generalises)
    assert arms["deliverable"]["kind"] == "achievement"


def test_arm_mapping_read_is_a_fresh_copy():
    """Callers must not be able to corrupt the module constant."""
    a = root_target.root_target_arms()
    a["evidence_arms"].append("boom")
    a["work_item_arm"]["via"] = "tampered"
    b = root_target.root_target_arms()
    assert b["evidence_arms"] == []
    assert b["work_item_arm"]["via"] == "occupation.iter_target_records"


# --- projection shape (受入条件1) -------------------------------------------

def test_projection_shape_matches_shared_frame():
    data = _dev_project([_ms("ms-1", "in_progress")])
    root = root_target.project_as_root_target(data)
    for key in ("id", "label", "status", "kind", "work_items_total",
                "work_items_done", "profession", "arms",
                "projection", "narrative"):
        assert key in root
    assert root["id"] == "root"
    assert root["kind"] == "root"
    assert root["label"] == "Beacon"
    assert root["profession"] == "dev"


# --- project-level 2-split (e-5547 / 方針2, 受入条件3) ------------------------

def test_narrative_is_root_owned_not_derived():
    """大目的 / 経緯 are read off the root, not rolled up from children."""
    data = _dev_project([_ms("ms-1", "in_progress")])
    root = root_target.project_as_root_target(data)
    narrative = root["narrative"]
    assert narrative["objective"] == "AI 開発の進捗を透明化する"
    assert narrative["summary"] == "直近: root-target 化に着手"
    # narrative carries ONLY authored story — no synthesized counts leak in
    assert "counts" not in narrative
    assert "targets" not in narrative


def test_narrative_survives_when_all_children_removed():
    """The narrative is the irreducible core: it remains with zero children."""
    data = _dev_project([])
    root = root_target.project_as_root_target(data)
    assert root["narrative"]["objective"] == "AI 開発の進捗を透明化する"
    assert root["narrative"]["summary"] == "直近: root-target 化に着手"
    # while the synthesized half correctly collapses to empty
    assert root["projection"]["counts"] == {"total": 0, "done": 0, "open": 0}
    assert root["projection"]["targets"] == []


def test_projection_is_synthesized_rollup_over_children():
    data = _dev_project([
        _ms("ms-1", "done"),
        _ms("ms-2", "in_progress"),
        _ms("ms-3", work_model.DONE_STATUS),
    ])
    proj = root_target.synthesized_projection(data)
    assert proj["counts"] == {"total": 3, "done": 2, "open": 1}
    assert len(proj["targets"]) == 3
    # deliverable seam present but empty (ms-155 fills it)
    assert proj["deliverables"] == []


def test_top_level_counts_agree_with_projection():
    """The shared-frame work_items_* must not diverge from projection.counts."""
    data = _dev_project([_ms("ms-1", "done"), _ms("ms-2", "in_progress")])
    root = root_target.project_as_root_target(data)
    assert root["work_items_total"] == root["projection"]["counts"]["total"]
    assert root["work_items_done"] == root["projection"]["counts"]["done"]


def test_children_counted_as_root_work_items():
    data = _dev_project([
        _ms("ms-1", "done"),
        _ms("ms-2", "in_progress"),
        _ms("ms-3", work_model.DONE_STATUS),
    ])
    root = root_target.project_as_root_target(data)
    assert root["work_items_total"] == 3
    assert root["work_items_done"] == 2


def test_cancelled_children_excluded():
    data = _dev_project([
        _ms("ms-1", "done"),
        _ms("ms-2", work_model.CANCELLED_STATUS),
    ])
    root = root_target.project_as_root_target(data)
    # cancelled child dropped from the count, matching the default status view
    assert root["work_items_total"] == 1
    assert root["work_items_done"] == 1


def test_status_todo_when_no_children():
    root = root_target.project_as_root_target(_dev_project([]))
    assert root["status"] == work_model.TODO_STATUS


def test_status_active_when_children_present():
    root = root_target.project_as_root_target(
        _dev_project([_ms("ms-1", "in_progress")]))
    assert root["status"] == "active"


def test_status_active_even_when_all_children_done():
    """A root does NOT auto-derive 'done' — completion approval is ms-154."""
    root = root_target.project_as_root_target(
        _dev_project([_ms("ms-1", "done")]))
    assert root["status"] == "active"
    assert root["status"] != work_model.DONE_STATUS


# --- in-place strangler: read-only view (受入条件1) --------------------------

def test_view_does_not_mutate_input():
    data = _dev_project([_ms("ms-1", "in_progress"), _ms("ms-2", "done")])
    before = copy.deepcopy(data)
    root_target.project_as_root_target(data)
    assert data == before, "project_as_root_target must not mutate the project"


def test_view_adds_no_new_records():
    """No new top-level collection / key is written onto the project."""
    data = _dev_project([_ms("ms-1")])
    keys_before = set(data.keys())
    root_target.project_as_root_target(data)
    assert set(data.keys()) == keys_before


# --- occupation-agnostic: a sales project reads as a root too ----------------

def test_sales_project_reads_as_root_via_iter_target_records():
    data = {
        "name": "Sales Co",
        "objective": "新規顧客 10 社",
        "profession": "sales",
        "opportunities": [
            {"id": "opp-1", "status": "in_progress", "label": "A 社"},
            {"id": "opp-2", "status": "done", "label": "B 社"},
        ],
    }
    root = root_target.project_as_root_target(data)
    assert root["kind"] == "root"
    assert root["profession"] == "sales"
    # children resolve to opportunities through the per-class projection, no branch
    assert root["work_items_total"] == 2
    assert root["work_items_done"] == 1
    assert root["status"] == "active"


# --- creation = root-target instance化 (e-5548 / 方針4, 受入条件4) ------------

def test_build_new_project_reads_back_as_root_dev():
    """A freshly created dev project round-trips through the root view."""
    data = occupation.build_new_project("Proj", "大目的テキスト", "dev")
    root = root_target.project_as_root_target(data)
    assert root["kind"] == "root"
    assert root["label"] == "Proj"
    assert root["profession"] == "dev"
    # 大目的 wired into the root-owned narrative at birth
    assert root["narrative"]["objective"] == "大目的テキスト"
    # fresh project → no children yet
    assert root["status"] == work_model.TODO_STATUS
    assert root["projection"]["counts"] == {"total": 0, "done": 0, "open": 0}


def test_build_new_project_reads_back_as_root_sales():
    """Occupation-agnostic: a sales project also instantiates as a root."""
    data = occupation.build_new_project("Sales Co", "受注 10 社", "sales")
    root = root_target.project_as_root_target(data)
    assert root["kind"] == "root"
    assert root["profession"] == "sales"
    assert root["narrative"]["objective"] == "受注 10 社"
    assert root["projection"]["counts"]["total"] == 0


def test_narrative_home_exists_at_birth():
    """The root-owned narrative fields (objective / summary) have a home from
    creation, so the write side matches the read-side 2-split (方針2)."""
    for prof in ("dev", "sales", "backoffice", "legal"):
        data = occupation.build_new_project("p", "obj", prof)
        assert "objective" in data
        assert "summary" in data          # home exists even before first write
        assert data["objective"] == "obj"
        # adopted-class wiring preserved (back-compat with seam probe)
        assert "adopted_target_classes" in data


# --- root-target field write seams (e-5551 / 方針5, 受入条件7) ----------------

def test_set_root_label():
    data = {"name": "old", "milestones": []}
    root_target.set_root_label(data, "new")
    assert data["name"] == "new"


def test_set_root_archived_coerces_to_bool():
    data = {"name": "p", "milestones": []}
    root_target.set_root_archived(data, True)
    assert data["archived"] is True
    root_target.set_root_archived(data, False)
    assert data["archived"] is False
    # non-bool truthy input is coerced to a clean flag
    root_target.set_root_archived(data, 1)
    assert data["archived"] is True


def test_set_root_seams_touch_only_their_field():
    data = {"name": "p", "archived": False, "milestones": [{"id": "ms-1"}]}
    root_target.set_root_label(data, "q")
    assert data["milestones"] == [{"id": "ms-1"}]  # untouched
    assert data["archived"] is False               # untouched


# --- scale contract (CORE doc scale-contract-principle) ----------------------

def test_scale_many_children():
    """A root over many child Targets counts correctly and stays read-only."""
    ms = [_ms(f"ms-{i}", "done" if i % 2 == 0 else "in_progress")
          for i in range(500)]
    data = _dev_project(ms)
    before = copy.deepcopy(data)
    root = root_target.project_as_root_target(data)
    assert root["work_items_total"] == 500
    assert root["work_items_done"] == 250  # i=0,2,4,... → 250 done
    assert data == before
