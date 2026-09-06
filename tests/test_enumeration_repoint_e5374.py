"""ms-147 e-5374 — enumeration repoint: a project's target enumeration reads its
ADOPTED set, not a profession-filtered subset.

The observable win (SPEC 受入条件3): a NON-dev project that adopts the dev-stamped
``release`` class actually enumerates it — the M:N adoption the whole inversion
exists for. The compat guarantee: a LEGACY project (no adopted key) keeps the old
profession-filtered behaviour byte-for-byte, so dev/sales/back-office are unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402
import target_descriptor as td  # noqa: E402


def _adopting_release(profession):
    return {"name": "p", "profession": profession, "milestones": [],
            "adopted_target_classes": ["release"]}


# --- M:N: a non-dev profession that ADOPTED release enumerates it (受入条件3) --

def test_non_dev_profession_adopting_release_owns_it():
    proj = _adopting_release("legal")
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj)]
    assert "release" in kinds, \
        "adoption decided membership; the dev stamp must not undo it"


def test_owned_target_classes_includes_adopted_release_for_non_dev():
    assert "release" in occupation.owned_target_classes(
        _adopting_release("legal"))


def test_adopted_release_surfaces_as_a_projected_target_row():
    proj = _adopting_release("legal")
    proj["release_targets"] = [{"id": "rel-1", "label": "v1", "phase": "draft"}]
    rows = occupation.project_targets(proj)
    assert any(r.get("id") == "rel-1" for r in rows), \
        "an adopted class's instances must appear in the project's Target frame"


# --- dev on the adopted model keeps its built-ins AND release ----------------

def test_dev_complete_adopted_set_owns_all_builtins_and_release():
    # ms-150 completed the inversion e-5374 staged: milestone / operation are now
    # catalog material carried in the adopted set, not a separate profession-keyed
    # authority. A dev project seeded with the full default adopted set owns all of
    # them via the SAME single read (owned_target_classes = effective kinds).
    proj = {"name": "p", "profession": "dev", "milestones": [],
            "adopted_target_classes": td.profession_adopted_kinds("dev")}
    owned = occupation.owned_target_classes(proj)
    for kind in ("milestone", "operation", "release"):
        assert kind in owned


def test_partial_adopted_set_is_authoritative_ms150():
    # ms-150 条件B — the adopted set is the COMPLETE ownership truth (一本読み). A
    # dev project whose copied set lists only ["release"] (e.g. the pre-ms-150 seed,
    # which stored only shareable materials) owns EXACTLY release until it is
    # backfilled — the profession is NEVER re-consulted to silently re-add milestone.
    # This is the negative anchor: the ownership read must not re-impose the
    # built-ins (the backfill migration, not a hidden fallback, upgrades such a
    # project — ms-150 実装順序4).
    proj = {"name": "p", "profession": "dev", "milestones": [],
            "adopted_target_classes": ["release"]}
    assert set(occupation.owned_target_classes(proj)) == {"release"}


# --- legacy projects (no adopted key): the stamp is provenance, not a filter ---
# ms-147 e-5375 completed the inversion e-5374 staged: even a LEGACY project no
# longer re-filters its effective set by each descriptor's profession stamp. A
# class DECLARED in the project is the project's, regardless of where it was
# authored. (Built-in scoping still happens upstream in effective_descriptors, so
# a legacy sales project with NO declarations still gets no dev built-in.)

def test_legacy_project_enumerates_a_declared_class_regardless_of_stamp():
    # e-5375: a dev-stamped release DECLARED in a legacy sales project IS now
    # enumerated — the stamp is provenance, declaration decided membership. This
    # is the behaviour e-5374's staged compat test (filters-it-out) deliberately
    # held until profession authority was removed.
    proj = {"name": "p", "profession": "sales", "milestones": [],
            "target_classes": [dict(td.RELEASE_DESCRIPTOR)]}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj)]
    assert "release" in kinds


def test_legacy_sales_gets_its_own_builtins_not_dev_ones():
    # ms-150 (5-built-in extension): a legacy sales project (no adopted key) derives
    # its OWN profession defaults (opportunity / account / acquisition) via
    # effective_descriptors — and still NO dev built-in (milestone / operation /
    # release). The stamp filter is gone; profession-level seeding (WHICH built-ins a
    # profession carries by default) remains and is now symmetric across dev / sales.
    proj = {"name": "p", "profession": "sales", "milestones": []}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj)]
    assert kinds == ["opportunity", "account", "acquisition"]
    for dev_builtin in ("milestone", "operation", "release"):
        assert dev_builtin not in kinds


def test_legacy_dev_surfaces_its_builtin_defaults():
    # ms-150 (5-built-in extension of ms-142 e-5161): a legacy dev project (no
    # adopted key) derives its FULL built-in default set (milestone / operation /
    # release) via the live manifest-seed derivation.
    proj = {"name": "p", "profession": "dev", "milestones": []}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj)]
    assert kinds == ["milestone", "operation", "release"]
