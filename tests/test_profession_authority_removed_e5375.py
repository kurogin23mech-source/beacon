"""ms-147 e-5375 — profession authority removal (SPEC 方針1 / 受入条件1).

A target-class descriptor's ``profession`` field is now PROVENANCE (where the
class was authored), never a wiring input. These tests pin the two halves of
受入条件1:

  * "外しても回帰しない": a descriptor with NO ``profession`` field runs the full
    generic lifecycle (project / add work-item / record evidence / advance phase)
    and is enumerated / owned exactly like a stamped one.
  * "配線判断には使わない": a class whose stamp DISAGREES with the project's
    profession still surfaces — enumeration reads the effective set, not the stamp
    (``occupation._descriptors_owned_by``), so no stamp filters a project's own
    declared/adopted classes.

dev / sales built-in projection is asserted unchanged as the regression floor.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402
import target_engine as te  # noqa: E402
import target_descriptor as td  # noqa: E402


# A profession-NEUTRAL material: everything a well-formed class needs, no stamp.
NEUTRAL = {
    "kind": "undertaking",
    "label": "やること",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [],
    "phases": [
        {"key": "started", "label": "着手"},
        {"key": "enough", "label": "十分やった", "terminal": True},
    ],
}


# ---------------------------------------------------------------------------
# 受入条件1a — a stampless descriptor is well-formed and runs the full lifecycle.
# ---------------------------------------------------------------------------

def test_stampless_descriptor_validates():
    assert "profession" not in NEUTRAL
    assert td.validate_descriptor(NEUTRAL) == []


def test_stampless_class_projects_add_work_item_evidence_advance():
    data = {"name": "t", "profession": "dev", "milestones": []}
    rec = te.create_target(data, NEUTRAL, label="掃除", actor="claude")
    assert rec["phase"] == "started"

    # add work-item
    te.add_work_item(data, NEUTRAL, rec["id"], "床を拭く", actor="claude")
    assert [w.get("description") for w in te.list_work_items(rec)] == ["床を拭く"]

    # record evidence
    te.add_evidence(data, NEUTRAL, rec["id"], summary="ピカピカ", actor="claude")
    assert len(te.list_evidence(rec)) == 1

    # advance phase to terminal
    te.advance_target(data, NEUTRAL, rec["id"], actor="claude")
    assert rec["phase"] == "enough"
    assert te.is_terminal_phase(NEUTRAL, rec["phase"])

    # and it surfaces in the shared Target frame
    proj = te.project_target(NEUTRAL, rec)
    assert proj["kind"] == "undertaking" and proj["id"] == rec["id"]


def test_stampless_class_is_owned_and_enumerated():
    data = {"name": "t", "profession": "dev", "milestones": [],
            "target_classes": [dict(NEUTRAL)]}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(data)]
    assert "undertaking" in kinds
    assert "undertaking" in occupation.owned_target_classes(data, "dev")


# ---------------------------------------------------------------------------
# 受入条件1b — the stamp is not a filter: a disagreeing stamp still surfaces.
# ---------------------------------------------------------------------------

def test_disagreeing_stamp_does_not_hide_a_declared_class():
    # A class stamped "dev" declared in a sales project (legacy: no adopted key)
    # is still owned — declaration decided membership, not the stamp.
    stamped_dev = dict(NEUTRAL, profession="dev")
    data = {"name": "t", "profession": "sales", "opportunities": [],
            "target_classes": [stamped_dev]}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(data)]
    assert "undertaking" in kinds


def test_build_descriptor_omits_empty_profession_but_keeps_a_given_one():
    neutral = td.build_descriptor(kind="k", label="L", dtype="single-shot",
                                  id_prefix="k-", collection="ks",
                                  phases=[{"key": "a"}])
    assert "profession" not in neutral, "no empty stamp key on a neutral class"
    assert td.validate_descriptor(neutral) == []

    stamped = td.build_descriptor(kind="k", label="L", dtype="single-shot",
                                  id_prefix="k-", collection="ks",
                                  profession="sales", phases=[{"key": "a"}])
    assert stamped["profession"] == "sales"  # provenance recorded verbatim


# ---------------------------------------------------------------------------
# Regression floor — dev / sales built-in projection unchanged.
# ---------------------------------------------------------------------------

def test_dev_builtin_projection_unchanged():
    data = {"name": "d", "profession": "dev", "milestones": [
        {"id": "ms-1", "title": "A", "status": "in_progress", "entries": []}]}
    assert [t["id"] for t in occupation.project_targets(data)] == ["ms-1"]


def test_sales_builtin_projection_unchanged():
    data = {"name": "s", "profession": "sales", "opportunities": [
        {"id": "opp-1", "label": "B", "status": "in_progress"}]}
    assert [t["id"] for t in occupation.project_targets(data)] == ["opp-1"]
