"""End-to-end proof that back-office works as the first DATA-defined occupation
(ms-122 e-3958): the shipped descriptor seed validates, its target-classes run
through the generic engine (create / advance incl. the 法務レビュー per-phase
fields / close), and the occupation registry projects them into the shared
frame — all with no code container (superseding the ms-121 milestone-流用 stub)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import backoffice_seed as seed  # noqa: E402
import target_descriptor as td  # noqa: E402
import target_engine as te  # noqa: E402
import occupation as occ  # noqa: E402
import work_model as wm  # noqa: E402


def test_seed_project_shape_and_validity():
    data = seed.build_backoffice_project("経理チーム", "業務を回す")
    assert data["profession"] == "backoffice"
    assert data["milestones"] == []          # shared validator passes
    kinds = [c["kind"] for c in data["target_classes"]]
    assert kinds == ["contract", "evaluation", "monthly_close", "attendance_watch"]
    # Every shipped descriptor is well-formed.
    assert td.validate_target_classes(data) == {}


def test_seed_is_deep_copied():
    a = seed.build_backoffice_project("A")
    b = seed.build_backoffice_project("B")
    a["target_classes"][0]["label"] = "MUTATED"
    assert b["target_classes"][0]["label"] == "契約"
    assert seed.BACKOFFICE_TARGET_CLASSES[0]["label"] == "契約"


def test_contract_runs_through_its_phases_with_per_phase_fields():
    data = seed.build_backoffice_project("経理")
    desc = td.get_descriptor(data, "contract")
    rec = te.create_target(data, desc, label="A社 業務委託",
                           fields={"counterparty": "A社", "amount": "500000"})
    assert rec["id"] == "ctr-1"
    assert rec["phase"] == "drafting"

    # 法務レビュー phase surfaces fields that only appear there (SPEC §4).
    fkeys = [f["key"] for f in td.fields_at_phase(desc, "legal_review")]
    assert "counterparty" in fkeys and "reviewer" in fkeys and "risk" in fkeys
    assert "reviewer" not in [f["key"] for f in td.fields_at_phase(desc, "drafting")]

    te.advance_target(data, desc, "ctr-1", reason="起草完了")     # → legal_review
    te.advance_target(data, desc, "ctr-1")                        # → signed (terminal)
    assert te.find_target(data, desc, "ctr-1")["phase"] == "signed"
    assert te.is_terminal_phase(desc, "signed")
    te.close_target(data, desc, "ctr-1", reason="締結")
    assert wm.is_done(te.find_target(data, desc, "ctr-1"))


def test_phaseless_attendance_watch():
    data = seed.build_backoffice_project("経理")
    desc = td.get_descriptor(data, "attendance_watch")
    rec = te.create_target(data, desc, label="営業部 勤怠",
                           fields={"department": "営業部"})
    assert rec["id"] == "aw-1"
    assert td.phase_keys(desc) == []


def test_occupation_projects_backoffice_targets_in_shared_frame():
    data = seed.build_backoffice_project("経理")
    te.create_target(data, td.get_descriptor(data, "contract"),
                     label="A社 契約", fields={"counterparty": "A社"})
    te.create_target(data, td.get_descriptor(data, "monthly_close"),
                     label="2026-07", fields={"period": "2026-07"})
    rows = occ.project_targets(data)
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["contract", "monthly_close"]
    # And the registry recognises back-office ownership without editing occupation.py
    assert set(occ.owned_target_classes(data)) == {
        "contract", "evaluation", "monthly_close", "attendance_watch"}


def test_backoffice_rejects_dev_and_sales_classes():
    data = seed.build_backoffice_project("経理")
    for wrong in ("milestone", "opportunity"):
        try:
            occ.assert_target_class_owned(data, wrong)
            assert False, f"expected rejection of {wrong}"
        except occ.TargetClassProfessionError:
            pass
