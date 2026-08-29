"""Unit tests for the unified deliverable-projection read (ms-155 e-5598).

e-5597 added the per-class ``deliverable`` DECLARATION field to descriptors; this
task declares the milestone code class's deliverable (機能→application-map) in the
built-in master (``target_state.BUILTIN_TARGET_CLASSES``) and exposes ONE accessor
(``occupation.resolve_deliverable``) that reads a class's deliverable
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
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import occupation  # noqa: E402
import target_state as tstate  # noqa: E402
import target_descriptor as td  # noqa: E402


# ---------------------------------------------------------------------------
# The milestone code class declares its deliverable in the single-source master.
# ---------------------------------------------------------------------------

def test_milestone_declares_feature_map_deliverable():
    # ms-161 e-5825: repointed off the doc→application-map proxy onto the
    # ``changelog`` projector (produced value = root deliverable-changelog summary).
    # No ``ref`` — the value is the log, not a named doc.
    dl = tstate.BUILTIN_TARGET_CLASSES["milestone"]["deliverable"]
    assert dl == {"kind": "feature-map", "label": "機能",
                  "projector": "changelog"}


def test_deliverable_is_stripped_from_the_state_model():
    # ``deliverable`` is registry-only data (like arm_roles): it must NOT leak into
    # the derived state model, else BUILTIN_STATE_MODELS carries a non-state field.
    assert "deliverable" in tstate.REGISTRY_ONLY_KEYS
    assert "deliverable" not in tstate.BUILTIN_STATE_MODELS["milestone"]


# ---------------------------------------------------------------------------
# The unified accessor — one read over code AND descriptor classes.
# ---------------------------------------------------------------------------

def test_accessor_reads_milestone_code_class():
    proj = occupation.resolve_deliverable({"profession": "dev"}, "milestone")
    assert proj == {"kind": "feature-map", "label": "機能",
                    "projector": "changelog", "ref": ""}


def test_accessor_reads_descriptor_class():
    # A data-defined class declaring a deliverable resolves through the SAME accessor.
    deal = td.build_descriptor(
        kind="deal", label="商談", dtype="single-shot",
        id_prefix="deal-", collection="deals",
        deliverable={"kind": "pipeline", "projector": "rollup"})
    data = {"name": "t", "profession": "sales"}
    assert td.append_descriptor(data, deal) == []
    proj = occupation.resolve_deliverable(data, "deal")
    assert proj == {"kind": "pipeline", "label": "",
                    "projector": "rollup", "ref": ""}


def test_accessor_none_for_class_without_deliverable():
    # opportunity (code class) declares no deliverable today → None (道筋は e-5601).
    assert occupation.resolve_deliverable({"profession": "sales"},
                                                 "opportunity") is None
    # release (built-in-as-data descriptor) declares none → None.
    assert occupation.resolve_deliverable({"profession": "dev"},
                                                 "release") is None


def test_accessor_none_for_unknown_kind_or_empty():
    assert occupation.resolve_deliverable({}, "ghost") is None
    assert occupation.resolve_deliverable({}, "") is None
    assert occupation.resolve_deliverable(None, "milestone") is not None


def test_map_gate_is_class_adoption_not_a_profession_branch():
    # The milestone deliverable resolves for the milestone KIND regardless of the
    # data's profession field — the gate is that only a dev project ENUMERATES the
    # milestone class (so only there does the map surface), not an if profession==dev.
    for prof in ("dev", "sales", "backoffice", ""):
        proj = occupation.resolve_deliverable({"profession": prof},
                                                     "milestone")
        # ms-161 e-5825: identified by the changelog projector now (no doc ref proxy).
        assert proj["projector"] == "changelog"


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
         "projector": "changelog", "ref": ""},
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


# ---------------------------------------------------------------------------
# opportunity→pipeline 同型化の道筋 (ms-155 e-5601) — proves EXECUTABLY that an
# opportunity would surface a deliverable through the IDENTICAL slot + accessor +
# union as milestone, with zero new wiring. The live slot is deliberately not
# shipped (no rollup resolver yet, see target_state comment); this pins that
# enabling it later is a one-line uncomment, not a code path that must be built.
# ---------------------------------------------------------------------------

def test_opportunity_deliverable_is_isomorphic():
    opp = tstate.BUILTIN_TARGET_CLASSES["opportunity"]
    assert "deliverable" not in opp   # not shipped live (hollow-spec avoidance)
    sales = {"name": "S", "profession": "sales", "opportunities": []}
    assert occupation.project_deliverables(sales) == []   # empty until declared

    # Declare the SAME-shaped slot opportunity would carry via patch.dict — an
    # atomic, auto-restoring swap of the whole entry (ms-155 e-5598 maintainability
    # review: direct in-place mutation + finally-del is a parallel-test footgun; a
    # context manager cannot leave the module dict dirty for a sibling test).
    patched = {**opp, "deliverable": {"kind": "pipeline", "label": "パイプライン",
                                      "projector": "rollup"}}
    with mock.patch.dict(tstate.BUILTIN_TARGET_CLASSES,
                         {"opportunity": patched}):
        # the very same accessor + union surface it — no new wiring, isomorphic.
        assert occupation.resolve_deliverable(sales, "opportunity") == {
            "kind": "pipeline", "label": "パイプライン",
            "projector": "rollup", "ref": ""}
        assert occupation.project_deliverables(sales) == [
            {"target_class": "opportunity", "kind": "pipeline",
             "label": "パイプライン", "projector": "rollup", "ref": ""}]
        # and it validates under the same shared rule milestone's does
        assert td.validate_deliverable(patched["deliverable"], "opportunity") == []
    assert "deliverable" not in tstate.BUILTIN_TARGET_CLASSES["opportunity"]


# ---------------------------------------------------------------------------
# Independent-review follow-ups (ms-155 e-5597/e-5599 AX + maintainability).
# ---------------------------------------------------------------------------

def test_doc_projector_requires_ref():
    # AX high: a "doc" deliverable IS the document named by ref, so an empty ref
    # has nothing to resolve. Validate flags it AND normalize drops it to None,
    # so a hollow doc spec never silently enters the union.
    bad = {"kind": "feature-map", "projector": "doc"}          # no ref
    assert any("ref" in p for p in td.validate_deliverable(bad, "x"))
    assert td.normalize_deliverable(bad) is None
    bad_empty = {"kind": "feature-map", "projector": "doc", "ref": "  "}
    assert td.normalize_deliverable(bad_empty) is None
    # a doc projector WITH a ref is fine
    ok = {"kind": "feature-map", "projector": "doc", "ref": "application-map"}
    assert td.validate_deliverable(ok, "x") == []
    assert td.normalize_deliverable(ok)["ref"] == "application-map"
    # rollup needs no ref (its value is a roll-up, not a document)
    assert td.validate_deliverable({"kind": "p", "projector": "rollup"}, "x") == []


def test_project_deliverables_tolerates_none_like_sibling():
    # AX medium: resolve_deliverable(None, ...) is tolerated, so project_deliverables
    # (and deliverable_bearing_classes) must not crash on None either.
    assert occupation.project_deliverables(None) == []
    assert occupation.deliverable_bearing_classes(None) == []
