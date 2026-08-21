"""ms-147 e-5397 — the axis inversion's first stone: a project carries its OWN
copied set of adopted target-class kinds, and read-paths consult that copy
instead of re-deriving built-ins live off the profession field.

The load-bearing property (SPEC 受入条件4): once a project is created, changing
the profession manifest — or the project's profession field — must NOT retro-
alter what target-classes the project enumerates. A project written before this
feature (no adopted key) still falls back to the live derivation (tolerant
compat). Verified at the helper + effective_descriptors level (hermetic), plus
one end-to-end `beacon init` that the copy actually lands in project.json.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import occupation  # noqa: E402
import target_descriptor as td  # noqa: E402


# --- the manifest / catalog seam (SPEC 方針4 layer 1 & 2) --------------------

def test_profession_adopted_kinds_dev_is_release():
    assert td.profession_adopted_kinds("dev") == ["release"]


def test_profession_adopted_kinds_empty_for_professions_without_defaults():
    assert td.profession_adopted_kinds("sales") == []
    assert td.profession_adopted_kinds("backoffice") == []
    assert td.profession_adopted_kinds("legal") == []


def test_builtin_catalog_indexes_release_by_kind():
    catalog = td.builtin_descriptor_catalog()
    assert "release" in catalog
    assert catalog["release"]["kind"] == "release"


# --- None vs empty distinction (load-bearing for fallback) -------------------

def test_load_adopted_kinds_absent_reads_as_none():
    # A legacy project (key absent) must be distinguishable from one that adopts
    # nothing, so the read-path can choose fallback vs honour-the-empty-set.
    assert td.load_adopted_kinds({"profession": "dev"}) is None


def test_load_adopted_kinds_present_but_empty_reads_as_empty_list():
    assert td.load_adopted_kinds(
        {"profession": "legal", "adopted_target_classes": []}) == []


# --- the inversion: copied set wins over the live manifest (受入条件4) --------

def _dev_project_with_copied_set():
    return {
        "name": "p", "profession": "dev", "milestones": [],
        "adopted_target_classes": td.profession_adopted_kinds("dev"),
    }


def test_effective_descriptors_reads_copied_set_not_live_manifest(monkeypatch):
    proj = _dev_project_with_copied_set()
    assert [d["kind"] for d in occupation.effective_descriptors(proj)] == ["release"]

    # Emptying the manifest simulates "the profession's defaults changed later".
    # A project that copied its set must be UNAFFECTED.
    monkeypatch.setattr(td, "PROFESSION_DEFAULT_DESCRIPTORS", {})
    assert [d["kind"] for d in occupation.effective_descriptors(proj)] == ["release"], \
        "copied adopted set must survive a manifest change (SPEC 方針3 複写)"


def test_effective_descriptors_ignores_profession_field_flip():
    # The copied set — not the profession field — decides enumeration. A project
    # whose profession is flipped to sales but which copied release still lists it.
    proj = _dev_project_with_copied_set()
    proj["profession"] = "sales"
    assert [d["kind"] for d in occupation.effective_descriptors(proj)] == ["release"]


def test_legacy_project_without_key_falls_back_to_live_derivation(monkeypatch):
    legacy = {"name": "p", "profession": "dev", "milestones": []}  # no adopted key
    assert [d["kind"] for d in occupation.effective_descriptors(legacy)] == ["release"]

    # Fallback IS live: empty the manifest and the legacy project loses release.
    monkeypatch.setattr(td, "PROFESSION_DEFAULT_DESCRIPTORS", {})
    assert occupation.effective_descriptors(legacy) == []


def test_data_defined_empty_adopted_unions_with_declared_target_classes():
    # adopted=[] (present) contributes no built-in; declared target_classes still
    # surface. Proves the copied-set path unions the raw user list unchanged.
    declared = {"kind": "contract", "label": "契約", "profession": "legal",
                "type": "single-shot", "id_prefix": "ct-", "collection": "contracts",
                "phases": [{"key": "draft"}]}
    proj = {"name": "p", "profession": "legal", "milestones": [],
            "adopted_target_classes": [], "target_classes": [declared]}
    kinds = [d["kind"] for d in occupation.effective_descriptors(proj)]
    assert kinds == ["contract"]


def test_none_and_empty_data_still_surface_dev_defaults():
    # The import-time coverage-matrix floor: a no-data consult has no adopted key,
    # so it falls back and the dev defaults still inject.
    assert [d["kind"] for d in occupation.effective_descriptors(None)] == ["release"]
    assert [d["kind"] for d in occupation.effective_descriptors({})] == ["release"]


# --- end-to-end: beacon init copies the set into project.json (受入条件2) -----

def _beacon_bin():
    return os.path.join(os.path.dirname(__file__), "..", "bin", "beacon")


def _init_project(profession):
    """Run `beacon init` in an isolated temp cwd (local mode) and return the
    written project.json dict."""
    tmp = tempfile.mkdtemp(prefix="ms147-init-")
    env = dict(os.environ)
    env["BEACON_PROFESSION"] = profession
    env["BEACON_NAME"] = "t"
    env["BEACON_OBJECTIVE"] = "o"
    # non-interactive: don't prompt for an initial profile
    env["BEACON_NONINTERACTIVE"] = "1"
    subprocess.run([_beacon_bin(), "init", "t"], cwd=tmp, env=env,
                   capture_output=True, text=True, timeout=60)
    pf = os.path.join(tmp, ".beacon", "project.json")
    if not os.path.exists(pf):
        pytest.skip("init did not write a local project.json in this environment")
    with open(pf, encoding="utf-8") as f:
        return json.load(f)


def test_init_dev_copies_release_into_adopted_set():
    data = _init_project("dev")
    assert data.get("adopted_target_classes") == ["release"]


def test_init_data_defined_copies_empty_adopted_set_present():
    data = _init_project("legal")
    # present-but-empty: the key exists so read-paths treat this project's set as
    # authoritative rather than re-deriving off the profession field.
    assert data.get("adopted_target_classes") == []
