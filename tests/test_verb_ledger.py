"""Tests for lib/verb_ledger.py + verb_ledger_data.py — the live Q/R/B/C verb
ledger (ms-114 e-3740). The load-bearing test is ``test_ledger_covers_live_surface``:
it pins the ledger against the real CLI dispatch surface so a newly-added verb
fails CI until it is classified (that is what makes the memo a *live* ledger, not
stale prose). ``test_surface_matches_check_map_drift`` pins the ledger's notion of
"the live surface" to ``check-map-drift.enumerate_cli`` so the two stay 整合."""

import importlib.util
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import verb_ledger as vl  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")
VALID_CLASSES = set(vl.CLASSES)  # {"Q","R","B","C"}


# --- coverage / drift gate -------------------------------------------------

def test_ledger_covers_live_surface():
    """Every live CLI verb is classified and no ledger entry is stale (e-3740
    AC1: 全 CLI verb が Q/R/B/C に分類され live 台帳)."""
    rec = vl.reconcile()
    assert rec["unclassified"] == [], (
        f"未分類の verb があります (ledger に追記してください): {rec['unclassified']}")
    assert rec["stale"] == [], (
        f"存在しない verb が ledger に残っています (削除/alias してください): {rec['stale']}")


def test_surface_matches_check_map_drift():
    """verb_ledger.enumerate_live_verbs() must equal check-map-drift.enumerate_cli()
    so the ledger tracks exactly the surface the map-drift lint does (e-3740 AC3:
    check-map-drift の surface 追跡と整合)."""
    path = os.path.join(REPO, "scripts", "check-map-drift.py")
    spec = importlib.util.spec_from_file_location("check_map_drift", path)
    cmd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cmd)
    assert vl.enumerate_live_verbs() == cmd.enumerate_cli()


# --- data integrity --------------------------------------------------------

def test_every_entry_wellformed():
    for verb, entry in vl.VERB_LEDGER.items():
        assert entry.get("cls") in VALID_CLASSES, f"{verb}: bad cls {entry.get('cls')}"
        secondary = entry.get("secondary")
        assert isinstance(secondary, list), f"{verb}: secondary must be a list"
        for s in secondary:
            assert s in VALID_CLASSES, f"{verb}: bad secondary class {s}"
        # a verb should not list its own primary class as a secondary system
        assert entry["cls"] not in secondary, f"{verb}: primary class duplicated in secondary"
        assert isinstance(entry.get("note", ""), str)


def test_classify_hit_and_miss():
    assert vl.classify("task_done")["cls"] == "R"
    assert vl.classify("no_such_verb") is None


def test_classify_returns_defensive_copy():
    """Mutating a classify() result must not pollute the VERB_LEDGER constant."""
    entry = vl.classify("task_done")
    entry["cls"] = "TAMPERED"
    entry["secondary"].append("X")
    fresh = vl.classify("task_done")
    assert fresh["cls"] == "R" and "X" not in fresh["secondary"]


def test_log_is_the_report_prototype():
    """`beacon log` is the SPEC's R系統 雛形: report(R) with server author(B) as secondary."""
    entry = vl.classify("log")
    assert entry["cls"] == "R" and "B" in entry["secondary"]


# --- seams / class membership / summary ------------------------------------

def test_fusion_seams_are_only_seam_verbs():
    seams = vl.fusion_seams()
    assert seams, "there should be seam verbs (the decomposition work queue)"
    for s in seams:
        assert s["secondary"], f"{s['verb']} listed as a seam but has no secondary classes"
    # the canonical seam examples from the memo table are present
    verbs = {s["verb"] for s in seams}
    for expected in ("log", "task_done", "milestone_start", "pr_merge"):
        assert expected in verbs


def test_verbs_in_class_is_primary_only_and_validated():
    r_primary = vl.verbs_in_class("R")
    # pr_merge is primary C (secondary R) — NOT returned by verbs_in_class("R")
    assert "pr_merge" not in r_primary
    assert "task_add" in r_primary
    with pytest.raises(ValueError):
        vl.verbs_in_class("r")  # lowercase / invalid class must raise, not return []


def test_verbs_involving_includes_secondary():
    """verbs_involving(cls) catches fused verbs that touch cls in the secondary
    slot, so folding 'all R verbs' into the report primitive misses nothing."""
    r_involved = vl.verbs_involving("R")
    assert "pr_merge" in r_involved          # primary C, secondary R
    assert "task_add" in r_involved          # primary R
    assert set(vl.verbs_in_class("R")).issubset(set(r_involved))
    with pytest.raises(ValueError):
        vl.verbs_involving("bogus")


def test_verbs_in_class_partition():
    """The per-class lists partition the ledger exactly (each verb in one class)."""
    total = sum(len(vl.verbs_in_class(c)) for c in vl.CLASSES)
    assert total == len(vl.VERB_LEDGER)


def test_summary_consistent():
    s = vl.summary()
    assert s["total"] == len(vl.VERB_LEDGER)
    assert sum(s["per_class"].values()) == s["total"]
    assert s["fused"] == len(vl.fusion_seams())
