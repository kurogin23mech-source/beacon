"""ms-147 e-5377 — M:N adoption + additive declaration, end-to-end (受入条件3/5).

This is the integration proof that the axis inversion (職種→project) actually
holds across all three consumer sides the inversion touched:

  * enumeration   (occupation.project_targets / owned_target_classes)
  * ownership gate (occupation.assert_target_class_owned — the operate guard)
  * engine operate (target_engine.create_target / advance_target)

Two things must be true:

  受入条件3 (M:N): the SAME catalog class ``release`` (dev's built-in material)
  can be ADOPTED by dev AND by a non-dev profession, and BOTH projects enumerate
  AND operate it. Adoption — not the descriptor's provenance stamp — decides
  membership, so a legal project that adopts release owns it exactly as dev does.

  受入条件5 (additive): a project can carry a target-class BEYOND its profession's
  built-in defaults. A data-defined class (the e-5334 「やること」 undertaking)
  declared in a legal project (whose built-in default set is empty) is enumerated
  and operable via the same generic path.

The negative controls pin that adoption is what does the work: a project that
does NOT adopt release neither enumerates nor is allowed to create one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402
import target_engine as te  # noqa: E402


# The e-5334 「やること」 material — a data-defined class, profession-neutral.
UNDERTAKING = {
    "kind": "undertaking",
    "label": "やること",
    "type": "single-shot",
    "id_prefix": "ut-",
    "collection": "undertakings",
    "decomposition": {"id_field": "id", "arms": ["work_items", "evidence"]},
    "fields": [],
    "phases": [{"key": "started", "label": "着手"},
               {"key": "enough", "label": "十分やった", "terminal": True}],
}


def _adopting_release(profession):
    # A project on the adopted-set model that adopted the dev catalog class.
    base = {"name": "p", "profession": profession,
            "adopted_target_classes": ["release"]}
    # give each profession its built-in target collection so project_targets runs
    base.setdefault("milestones", [])
    return base


def _operate_release(data):
    """Resolve → gate → create → advance a release target the way the CLI does,
    and return the enumerated Target ids after."""
    desc = occupation.effective_get_descriptor(data, "release")
    assert desc is not None, "adopted catalog class must resolve to a descriptor"
    occupation.assert_target_class_owned(data, "release")  # operate guard
    rec = te.create_target(data, desc, label="v1", actor="claude")
    assert rec["phase"] == "draft"
    te.advance_target(data, desc, rec["id"], actor="claude")
    assert rec["phase"] == "published"
    return [t["id"] for t in occupation.project_targets(data)]


# ---------------------------------------------------------------------------
# 受入条件3 — the same catalog class works in TWO professions (M:N).
# ---------------------------------------------------------------------------

def test_dev_adopting_release_can_operate_and_enumerate_it():
    dev = _adopting_release("dev")
    assert "release" in occupation.owned_target_classes(dev, "dev")
    assert "rel-1" in _operate_release(dev)


def test_non_dev_adopting_release_can_operate_and_enumerate_it():
    legal = _adopting_release("legal")
    assert "release" in occupation.owned_target_classes(legal, "legal")
    # the whole point: a legal project drives the dev-provenance class end-to-end
    assert "rel-1" in _operate_release(legal)


def test_same_catalog_class_two_professions_are_independent():
    # M:N is real, not shared state: each project keeps its own release records.
    dev, legal = _adopting_release("dev"), _adopting_release("legal")
    _operate_release(dev)
    _operate_release(legal)
    assert dev["release_targets"][0]["id"] == "rel-1"
    assert legal["release_targets"][0]["id"] == "rel-1"
    assert dev is not legal and dev["release_targets"] is not legal["release_targets"]


# ---------------------------------------------------------------------------
# Negative control — adoption (not the stamp, not the profession) decides.
# ---------------------------------------------------------------------------

def test_a_legal_project_that_did_not_adopt_release_does_not_own_it():
    # adopted set present but empty → no release. The stamp/profession never
    # sneaks it back in.
    legal = {"name": "p", "profession": "legal", "milestones": [],
             "adopted_target_classes": []}
    assert "release" not in occupation.owned_target_classes(legal, "legal")


def test_operating_release_without_adopting_it_is_refused():
    legal = {"name": "p", "profession": "legal", "milestones": [],
             "adopted_target_classes": []}
    with pytest.raises(occupation.TargetClassProfessionError):
        occupation.assert_target_class_owned(legal, "release")


# ---------------------------------------------------------------------------
# 受入条件5 — additive: a class beyond the profession default (the e-5334 やること).
# ---------------------------------------------------------------------------

def test_legal_project_declaring_undertaking_beyond_its_empty_default():
    # legal's built-in default set is empty; declaring 「やること」 adds it additively.
    legal = {"name": "p", "profession": "legal", "milestones": [],
             "adopted_target_classes": [], "target_classes": [dict(UNDERTAKING)]}
    assert "undertaking" in occupation.owned_target_classes(legal, "legal")

    # and it operates end-to-end via the generic engine
    occupation.assert_target_class_owned(legal, "undertaking")
    rec = te.create_target(legal, UNDERTAKING, label="掃除", actor="claude")
    te.advance_target(legal, UNDERTAKING, rec["id"], actor="claude")
    assert rec["phase"] == "enough"
    assert rec["id"] in [t["id"] for t in occupation.project_targets(legal)]


def test_a_project_can_carry_both_an_adopted_and_a_declared_class():
    # additive + M:N together: legal adopts release (catalog) AND declares
    # undertaking (data-defined). Both enumerate.
    legal = {"name": "p", "profession": "legal", "milestones": [],
             "adopted_target_classes": ["release"],
             "target_classes": [dict(UNDERTAKING)]}
    owned = occupation.owned_target_classes(legal, "legal")
    assert "release" in owned and "undertaking" in owned
