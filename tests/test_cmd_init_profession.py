"""Tests for ``beacon init`` 職種テンプレート選択 (ms-106 ① AC1).

BEACON_PROFESSION picks the job-template. Default "dev" keeps the historical
schema (plus an explicit profession marker); "sales" emits the sales entity
schema. Both must pass the shared validator.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

import commands  # noqa: E402
import core  # noqa: E402
import occupation  # noqa: E402


@pytest.fixture
def project_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    (cwd / ".beacon").mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_NAME", "test-proj")
    monkeypatch.setenv("BEACON_OBJECTIVE", "test obj")
    monkeypatch.setenv("BEACON_RETRO_DAY", "monday")
    monkeypatch.setattr(commands, "_maybe_prompt_initial_profile", lambda: None)
    monkeypatch.delenv("BEACON_SENSITIVITY", raising=False)
    monkeypatch.delenv("BEACON_PROFESSION", raising=False)
    return cwd


def _read(cwd: Path) -> dict:
    return json.loads((cwd / ".beacon" / "project.json").read_text())


def test_default_profession_is_dev(project_cwd):
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "dev"
    assert data["milestones"] == []
    assert "opportunities" not in data
    core.validate_project(data)


def test_sales_profession_emits_sales_schema(project_cwd, monkeypatch, capsys):
    monkeypatch.setenv("BEACON_PROFESSION", "sales")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "sales"
    assert data["opportunities"] == []
    assert data["accounts"] == []
    assert data["milestones"] == []  # validator-compat
    core.validate_project(data)
    out = capsys.readouterr().out
    assert "profession = sales" in out
    assert "beacon account add" in out


def test_sales_profession_case_insensitive(project_cwd, monkeypatch):
    monkeypatch.setenv("BEACON_PROFESSION", "SALES")
    commands.cmd_init()
    assert _read(project_cwd)["profession"] == "sales"


def test_data_defined_profession_creates_descriptor_skeleton(project_cwd,
                                                             monkeypatch,
                                                             capsys):
    # ms-124 e-4091: a profession is no longer a hardcoded enum. An unrecognised
    # name creates a data-defined occupation skeleton (empty target_classes),
    # which the owner fills with `beacon target-class add` — no code change.
    monkeypatch.setenv("BEACON_PROFESSION", "legal")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "legal"
    assert data["milestones"] == []       # validator-compat
    assert data["target_classes"] == []   # targets come from descriptors
    core.validate_project(data)
    out = capsys.readouterr().out
    assert "profession = legal" in out
    assert "beacon target-class add" in out


# ---------------------------------------------------------------------------
# ms-150 seam probe (characterization) — the composition CONTRACT `beacon init`
# writes, locked BEFORE the profession cascade is extracted behind one seam
# (occupation.build_new_project). Every assertion here must hold byte-for-byte
# after the extraction; that is what proves the Transform is behaviour-
# preserving. The `adopted_target_classes` copy (ms-147 e-5397, stamped once for
# every profession) is the axis-inversion seam future per-class migrations plug
# into, so it gets its own coverage per profession.
# ---------------------------------------------------------------------------

def test_dev_stamps_adopted_release(project_cwd):
    # dev's only built-in-as-data class today is `release`; the manifest copies
    # it into the project's adopted set at init.
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["adopted_target_classes"] == ["release"]


def test_sales_stamps_empty_adopted_set(project_cwd, monkeypatch):
    # sales' target-classes (opportunity / account) are still code-wired, not in
    # the built-in descriptor catalog, so nothing is copied yet.
    monkeypatch.setenv("BEACON_PROFESSION", "sales")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["adopted_target_classes"] == []


def test_data_defined_stamps_empty_adopted_set(project_cwd, monkeypatch):
    monkeypatch.setenv("BEACON_PROFESSION", "legal")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["adopted_target_classes"] == []


def test_backoffice_seeds_descriptors_and_empty_adopted_set(project_cwd,
                                                            monkeypatch,
                                                            capsys):
    # backoffice is the half-migrated case: its target-classes ARE data
    # (descriptors under `target_classes`), but they live in the project's own
    # declared list, not the profession manifest catalog — so the copied adopted
    # set is empty while `target_classes` is seeded.
    monkeypatch.setenv("BEACON_PROFESSION", "backoffice")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "backoffice"
    assert data["milestones"] == []            # validator-compat
    assert len(data["target_classes"]) > 0     # descriptor seed
    assert data["adopted_target_classes"] == []
    core.validate_project(data)
    out = capsys.readouterr().out
    assert "profession = backoffice" in out


def test_backoffice_hyphenated_alias(project_cwd, monkeypatch):
    # The composition branch accepts both "backoffice" and "back-office"; the
    # hyphenated alias is a live path, so lock it (PR #669 maintainability #4).
    monkeypatch.setenv("BEACON_PROFESSION", "back-office")
    commands.cmd_init()
    data = _read(project_cwd)
    assert data["profession"] == "backoffice"   # alias resolves to canonical
    assert len(data["target_classes"]) > 0
    assert data["adopted_target_classes"] == []
    core.validate_project(data)


# ---------------------------------------------------------------------------
# ms-150 seam probe — pin the composition seam (occupation.build_new_project)
# as the UNIT under test, independent of cmd_init's file-write path (PR #669
# maintainability #3: testing only through cmd_init would let a re-inlining of
# the cascade pass silently). Also verifies the seam OWNS normalisation so a raw
# (un-normalised) profession does not fall through to the data-defined branch
# (PR #669 AX #1 / maintainability #1 consensus).
# ---------------------------------------------------------------------------

def test_seam_composes_dev_directly():
    data = occupation.build_new_project("p", "obj", "dev")
    assert data["profession"] == "dev"
    assert data["milestones"] == []
    assert data["adopted_target_classes"] == ["release"]
    assert "opportunities" not in data


def test_seam_composes_sales_directly():
    data = occupation.build_new_project("p", "obj", "sales")
    assert data["profession"] == "sales"
    assert data["opportunities"] == []
    assert data["accounts"] == []
    assert data["adopted_target_classes"] == []


def test_seam_normalises_raw_profession():
    # A caller passing an un-normalised value must still hit the right branch —
    # the seam owns strip/lower, so "Sales" resolves to the sales builder rather
    # than silently falling through to the data-defined skeleton.
    data = occupation.build_new_project("p", "obj", "  SALES  ")
    assert data["profession"] == "sales"
    assert data["opportunities"] == []


def test_seam_empty_profession_is_dev():
    data = occupation.build_new_project("p", "obj", "")
    assert data["profession"] == "dev"
    assert data["adopted_target_classes"] == ["release"]


def test_init_display_single_source_mapping():
    # ms-150 e-5465: occupation.init_display is the ONE home for the
    # profession → user-feedback strings (schema label + "Next:" hint) that
    # cmd_init used to branch on with profession literals. Pin each branch so a
    # future edit cannot drift the CLI feedback from the composition seam.
    dev = occupation.init_display("dev")
    assert dev["schema_label"] == ""            # dev prints no schema-label line
    assert dev["next_hint"] == "Next: beacon milestone add"

    sales = occupation.init_display("sales")
    assert "profession = sales" in sales["schema_label"]
    assert sales["next_hint"] == "Next: beacon account add / beacon opportunity add"

    back = occupation.init_display("back-office")   # alias resolves to canonical
    assert "profession = backoffice" in back["schema_label"]
    assert "beacon target create" in back["next_hint"]

    legal = occupation.init_display("legal")        # data-defined occupation
    assert "profession = legal" in legal["schema_label"]
    assert "beacon target-class add" in legal["next_hint"]


def test_init_display_normalises_like_the_seam():
    # Raw / empty values must select the same branch build_new_project does, so
    # the display and the composition never disagree on which profession a value
    # means.
    assert occupation.init_display("  SALES  ")["next_hint"] \
        == occupation.init_display("sales")["next_hint"]
    assert occupation.init_display("")["next_hint"] \
        == occupation.init_display("dev")["next_hint"]
