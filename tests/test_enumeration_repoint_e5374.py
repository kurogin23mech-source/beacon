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
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj, "legal")]
    assert "release" in kinds, \
        "adoption decided membership; the dev stamp must not undo it"


def test_owned_target_classes_includes_adopted_release_for_non_dev():
    assert "release" in occupation.owned_target_classes(
        _adopting_release("legal"), "legal")


def test_adopted_release_surfaces_as_a_projected_target_row():
    proj = _adopting_release("legal")
    proj["release_targets"] = [{"id": "rel-1", "label": "v1", "phase": "draft"}]
    rows = occupation.project_targets(proj)
    assert any(r.get("id") == "rel-1" for r in rows), \
        "an adopted class's instances must appear in the project's Target frame"


# --- dev on the adopted model keeps its built-ins AND release ----------------

def test_dev_adopted_still_owns_builtins_and_release():
    owned = occupation.owned_target_classes(_adopting_release("dev"), "dev")
    for kind in ("milestone", "operation", "release"):
        assert kind in owned


# --- legacy projects (no adopted key) keep the profession filter -------------

def test_legacy_sales_declaring_dev_stamped_release_still_filters_it_out():
    # A legacy project has no adopted key, so the profession filter still runs:
    # a stray dev-stamped release declared in a sales project is NOT enumerated
    # (pre-e5374 behaviour preserved — no silent M:N leak for legacy projects).
    proj = {"name": "p", "profession": "sales", "milestones": [],
            "target_classes": [dict(td.RELEASE_DESCRIPTOR)]}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj, "sales")]
    assert "release" not in kinds


def test_legacy_dev_still_surfaces_release_default():
    # Legacy dev project (no adopted key) still gets its built-in release via the
    # live derivation + profession filter — ms-142 e-5161 behaviour unchanged.
    proj = {"name": "p", "profession": "dev", "milestones": []}
    kinds = [d["kind"] for d in occupation._descriptors_owned_by(proj, "dev")]
    assert kinds == ["release"]
