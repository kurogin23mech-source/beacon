"""Unit tests for the unified deliverable-projection read (ms-155 e-5598).

e-5597 added the per-class ``deliverable`` DECLARATION field to descriptors; this
task declares the milestone code class's deliverable (機能→application-map) in the
built-in master (``target_state.BUILTIN_TARGET_CLASSES``) and exposes ONE accessor
(``occupation.deliverable_projection_for``) that reads a class's deliverable
whether the class is CODE (milestone/opportunity/…) or DATA (a descriptor). These
tests pin:

  * milestone's deliverable is declared and reads as the application-map doc spec.
  * the accessor resolves both code classes and descriptor classes on one path.
  * a class with no deliverable (opportunity today, release) returns None.
  * milestone→application-map surfaces by class ADOPTION, not a profession branch.
  * the built-in master validator enforces the same {kind, projector} rule via the
    shared ``target_descriptor.validate_deliverable`` (no code-class drift).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import occupation  # noqa: E402
import target_state as tstate  # noqa: E402
import target_descriptor as td  # noqa: E402


# ---------------------------------------------------------------------------
# The milestone code class declares its deliverable in the single-source master.
# ---------------------------------------------------------------------------

def test_milestone_declares_feature_map_deliverable():
    dl = tstate.BUILTIN_TARGET_CLASSES["milestone"]["deliverable"]
    assert dl == {"kind": "feature-map", "label": "機能",
                  "projector": "doc", "ref": "application-map"}


def test_deliverable_is_stripped_from_the_state_model():
    # ``deliverable`` is registry-only data (like arm_roles): it must NOT leak into
    # the derived state model, else BUILTIN_STATE_MODELS carries a non-state field.
    assert "deliverable" in tstate.REGISTRY_ONLY_KEYS
    assert "deliverable" not in tstate.BUILTIN_STATE_MODELS["milestone"]


# ---------------------------------------------------------------------------
# The unified accessor — one read over code AND descriptor classes.
# ---------------------------------------------------------------------------

def test_accessor_reads_milestone_code_class():
    proj = occupation.deliverable_projection_for({"profession": "dev"}, "milestone")
    assert proj == {"kind": "feature-map", "label": "機能",
                    "projector": "doc", "ref": "application-map"}


def test_accessor_reads_descriptor_class():
    # A data-defined class declaring a deliverable resolves through the SAME accessor.
    deal = td.build_descriptor(
        kind="deal", label="商談", dtype="single-shot",
        id_prefix="deal-", collection="deals",
        deliverable={"kind": "pipeline", "projector": "rollup"})
    data = {"name": "t", "profession": "sales"}
    assert td.append_descriptor(data, deal) == []
    proj = occupation.deliverable_projection_for(data, "deal")
    assert proj == {"kind": "pipeline", "label": "",
                    "projector": "rollup", "ref": ""}


def test_accessor_none_for_class_without_deliverable():
    # opportunity (code class) declares no deliverable today → None (道筋は e-5601).
    assert occupation.deliverable_projection_for({"profession": "sales"},
                                                 "opportunity") is None
    # release (built-in-as-data descriptor) declares none → None.
    assert occupation.deliverable_projection_for({"profession": "dev"},
                                                 "release") is None


def test_accessor_none_for_unknown_kind_or_empty():
    assert occupation.deliverable_projection_for({}, "ghost") is None
    assert occupation.deliverable_projection_for({}, "") is None
    assert occupation.deliverable_projection_for(None, "milestone") is not None


def test_map_gate_is_class_adoption_not_a_profession_branch():
    # The milestone deliverable resolves for the milestone KIND regardless of the
    # data's profession field — the gate is that only a dev project ENUMERATES the
    # milestone class (so only there does the map surface), not an if profession==dev.
    for prof in ("dev", "sales", "backoffice", ""):
        proj = occupation.deliverable_projection_for({"profession": prof},
                                                     "milestone")
        assert proj["ref"] == "application-map"


# ---------------------------------------------------------------------------
# Drift guard: the code-class deliverable obeys the SAME rule as descriptors.
# ---------------------------------------------------------------------------

def test_every_builtin_deliverable_passes_shared_validation():
    for kind, cls in tstate.BUILTIN_TARGET_CLASSES.items():
        assert td.validate_deliverable(cls.get("deliverable"), kind) == []


def test_shared_validation_rejects_bad_projector():
    # A code-class author who typo'd the projector would be caught by the same
    # helper the import-time master validator runs.
    problems = td.validate_deliverable({"kind": "x", "projector": "bogus"}, "milestone")
    assert any("projector" in p for p in problems)
    assert td.validate_deliverable(None, "any") == []   # absent is valid


# ---------------------------------------------------------------------------
# The root deliverable UNION (ms-155 e-5599) — occupation.project_deliverables
# collects the deliverable of every ADOPTED class, tagged with the producing
# class. Fills the seam root_target.synthesized_projection left empty in ms-153.
# ---------------------------------------------------------------------------

def test_union_surfaces_milestone_map_for_dev():
    dev = {"name": "D", "profession": "dev", "milestones": []}
    assert occupation.project_deliverables(dev) == [
        {"target_class": "milestone", "kind": "feature-map", "label": "機能",
         "projector": "doc", "ref": "application-map"},
    ]


def test_union_empty_for_sales_until_a_class_declares_one():
    sales = {"name": "S", "profession": "sales", "opportunities": []}
    assert occupation.project_deliverables(sales) == []


def test_union_includes_adopted_descriptor_class():
    # A descriptor class the project declares contributes to the union too — the
    # "declare, don't wire" contract: adopting a class adds its deliverable.
    deal = td.build_descriptor(
        kind="deal", label="商談案件", dtype="single-shot",
        id_prefix="deal-", collection="deals",
        deliverable={"kind": "pipeline", "label": "パイプライン",
                     "projector": "rollup"})
    data = {"name": "D", "profession": "dev", "milestones": []}
    assert td.append_descriptor(data, deal) == []
    union = occupation.project_deliverables(data)
    classes = {d["target_class"] for d in union}
    assert classes == {"milestone", "deal"}   # code class + descriptor class
    deal_row = next(d for d in union if d["target_class"] == "deal")
    assert deal_row["projector"] == "rollup" and deal_row["label"] == "パイプライン"


def test_union_skips_adopted_classes_without_a_deliverable():
    # operation is adopted by dev but declares no deliverable → not in the union.
    dev = {"name": "D", "profession": "dev", "milestones": [], "operations": []}
    classes = {d["target_class"] for d in occupation.project_deliverables(dev)}
    assert "operation" not in classes
    assert "milestone" in classes


# ---------------------------------------------------------------------------
# deliverable_bearing_classes (ms-155 e-5600) — the single source a consumer
# (cmd_retro) asks instead of hardcoding "milestone".
# ---------------------------------------------------------------------------

def test_bearing_classes_is_milestone_for_dev():
    dev = {"name": "D", "profession": "dev", "milestones": []}
    assert occupation.deliverable_bearing_classes(dev) == ["milestone"]


def test_bearing_classes_empty_for_sales_today():
    sales = {"name": "S", "profession": "sales", "opportunities": []}
    assert occupation.deliverable_bearing_classes(sales) == []


def test_bearing_classes_includes_descriptor_class():
    deal = td.build_descriptor(
        kind="deal", label="商談", dtype="single-shot",
        id_prefix="deal-", collection="deals",
        deliverable={"kind": "pipeline", "projector": "rollup"})
    data = {"name": "D", "profession": "dev", "milestones": []}
    assert td.append_descriptor(data, deal) == []
    assert occupation.deliverable_bearing_classes(data) == ["milestone", "deal"]
