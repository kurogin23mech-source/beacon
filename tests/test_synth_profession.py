"""e-5011: the synthetic-profession fixture flows through the SAME abstract paths
as dev and sales, purely from its manifest declaration (ms-142 の芯).

The fixture (``synth_profession.build_synthetic_project``) declares a profession
whose arms are named ``duties`` / ``attestations`` — nothing like dev's
``entries`` or sales' ``activities`` — with EXPLICIT arm roles. These tests pin
that the abstraction reads those declared roles (not hard-coded arm names), so:

  * the descriptor helper resolves arm roles from the explicit declaration;
  * ``profession_manifest`` surfaces the obligation class with duties=work-item
    arm, attestations=evidence arm;
  * ``iter_work_items`` yields the duty (not the attestation) with no branch;
  * ``iter_deadline_candidates`` + ``beacon deadline due`` light the duty's
    deadline up — i.e. the deadline capability (e-5010) works for a profession it
    was never coded for.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

LIB = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).parent))   # for synth_profession

import occupation as occ  # noqa: E402
import target_descriptor as td  # noqa: E402
import commands  # noqa: E402
from synth_profession import (  # noqa: E402
    build_synthetic_project, SYNTHETIC_DESCRIPTOR, SYNTHETIC_PROFESSION)


def _obligation_class(manifest):
    for tc in manifest["target_classes"]:
        if tc["collection"] == "obligations":
            return tc
    raise AssertionError("obligations class missing from manifest")


# ---------------------------------------------------------------------------
# Descriptor helper: explicit arm roles win over the name convention.
# ---------------------------------------------------------------------------

def test_descriptor_arm_roles_read_explicit_declaration():
    roles = td.arm_roles(SYNTHETIC_DESCRIPTOR)
    assert roles["work_item_arm"] == {"arm": "duties", "item_type": None,
                                      "kind": "duty"}
    assert roles["evidence_arms"] == [{"arm": "attestations", "item_type": None}]


def test_descriptor_arm_roles_convention_fallback():
    # A descriptor with NO explicit roles falls back to the work_items/evidence
    # name convention (back-compat for build_descriptor / backoffice_seed).
    legacy = {"kind": "matter", "collection": "matters",
              "decomposition": {"id_field": "id",
                                "arms": ["work_items", "evidence"]}}
    roles = td.arm_roles(legacy)
    assert roles["work_item_arm"] == {"arm": "work_items", "item_type": None,
                                      "kind": "work_item"}
    assert roles["evidence_arms"] == [{"arm": "evidence", "item_type": None}]


# ---------------------------------------------------------------------------
# Manifest: the synthetic profession lights up with NO occupation.py edit.
# ---------------------------------------------------------------------------

def test_manifest_lights_up_synthetic_profession():
    m = occ.profession_manifest(build_synthetic_project())
    assert m["profession"] == SYNTHETIC_PROFESSION
    obl = _obligation_class(m)
    assert obl["kind"] == "obligation"
    assert obl["id_prefix"] == "obl-"
    # arm names are the profession's OWN, resolved from declared roles — the
    # manifest did not need to know 'duties'/'attestations' in advance.
    assert obl["work_item_arm"] == {"arm": "duties", "item_type": None,
                                    "kind": "duty"}
    assert obl["evidence_arms"] == [{"arm": "attestations", "item_type": None}]
    # same key set as any built-in class (occupation-agnostic contract).
    assert set(obl) == {"kind", "collection", "id_field", "id_prefix",
                        "narrowing", "arms", "work_item_arm", "evidence_arms",
                        "phase_ball"}


# ---------------------------------------------------------------------------
# Work-item iterator: yields the duty, not the attestation, no profession branch.
# ---------------------------------------------------------------------------

def test_iter_work_items_yields_duty_only():
    items = list(occ.iter_work_items(build_synthetic_project()))
    assert [(wi["id"], arm) for wi, _t, arm in items] == [("duty-1", "duties")]
    _wi, target, _arm = items[0]
    assert target["id"] == "obl-1"           # parent Target attached


# ---------------------------------------------------------------------------
# Deadline (e-5010) works for a profession it was never coded for.
# ---------------------------------------------------------------------------

def test_deadline_candidates_include_synthetic_duty():
    cands = list(occ.iter_deadline_candidates(build_synthetic_project()))
    duty = [c for c in cands if c["kind"] == "duty"]
    assert len(duty) == 1
    assert duty[0]["label"] == "点検レポート提出"
    assert duty[0]["recipient"] == "sv-comp"
    assert duty[0]["target_status"] == "raised"


def test_deadline_due_verb_surfaces_synthetic_duty(tmp_path, monkeypatch, capsys):
    project = build_synthetic_project()
    cwd = tmp_path / "proj"
    (cwd / ".beacon").mkdir(parents=True)
    (cwd / ".beacon" / "project.json").write_text(
        json.dumps(project, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("BEACON_PROJECT_FILE", str(cwd / ".beacon" / "project.json"))
    monkeypatch.setenv("BEACON_JSON", "1")
    monkeypatch.setattr(commands, "_today_iso", lambda: "2026-08-09")
    commands.cmd_deadline_due()
    items = json.loads(capsys.readouterr().out)["items"]
    labels = {i["label"] for i in items}
    assert "点検レポート提出" in labels   # the compliance duty is overdue and surfaces
